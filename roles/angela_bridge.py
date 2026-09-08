"""Angela's interface to the role management system.

    User -> Angela -> proposed action -> Role Manager authorization -> execution

ANGELA IS AN INTERFACE, NOT AN AUTHORITY.

Concretely, that is enforced structurally rather than by instruction:

  * `execute()` takes `actor_discord_id` from the Discord message author, which
    the caller reads off `message.author.id`. The model never supplies it, and
    there is no parameter through which it could.
  * The proposed action carries an action name and arguments — nothing else. It
    has no field for a rank, a category, a permission or an override, so a model
    that decides someone is a high rank has nowhere to put that opinion.
  * Execution calls the same RoleManagerService methods the slash commands call,
    which run the same permissions.check(). An Angela-originated request and a
    typed slash command are indistinguishable to the authorization layer.

Angela's remaining job is conversational: work out what the user meant, ask when
it is unclear, and explain the outcome in her own voice.
"""

import json
import re

from roles import config as roles_config
from roles.models import ActionContext, ActionOrigin, ActionResult, ActionStatus
from roles.service import service

# ── Action catalogue ─────────────────────────────────────────────────────────
# The complete set of things Angela may propose. Anything not listed here cannot
# be reached through her, no matter what the model emits.

ACTION_SPECS = {
    "register": {
        "description": "Register the requesting user in PERSONNEL. Only ever acts on the requester.",
        "args": {},
        "self_only": True,
    },
    "set_timezone": {
        "description": "Set a timezone. Omit 'user' to mean the requester.",
        "args": {"timezone": "IANA identifier, e.g. Europe/Prague", "user": "optional Discord user ID"},
        "self_only": False,
    },
    "set_rank": {
        "description": "Set another user's rank.",
        "args": {"rank": "rank name or key", "user": "Discord user ID (required)"},
        "self_only": False,
    },
    "set_branch": {
        "description": "Set another user's branch.",
        "args": {"branch": "branch name or key", "user": "Discord user ID (required)"},
        "self_only": False,
    },
    "set_status": {
        "description": "Set an activity status: ACTIVE, SEMI-ACTIVE or IN-ACTIVE.",
        "args": {"status": "ACTIVE|SEMI-ACTIVE|IN-ACTIVE", "user": "optional Discord user ID"},
        "self_only": False,
    },
    "set_loa": {
        "description": "Record a leave of absence in the form #/#/#, or NONE to clear.",
        "args": {"value": "#/#/# or NONE", "user": "optional Discord user ID"},
        "self_only": False,
    },
    "whois": {
        "description": "Show a PERSONNEL record.",
        "args": {"user": "optional Discord user ID"},
        "self_only": False,
    },
    "force_sync": {
        "description": "Re-push a record to Discord and Roblox.",
        "args": {"user": "optional Discord user ID"},
        "self_only": False,
    },
    "roles_status": {
        "description": "Integrity, synchronization and configuration report.",
        "args": {},
        "self_only": False,
    },
}


class ProposedAction:
    """A management action Angela believes the user asked for.

    Note what is absent: no actor, no rank, no permission, no authority of any
    kind. Those cannot be proposed, only resolved by the service.
    """

    def __init__(self, action, args=None, confidence="high", clarification=""):
        self.action = action
        self.args = args or {}
        self.confidence = confidence
        #: Set when Angela needs the user to disambiguate before anything runs.
        self.clarification = clarification

    def __repr__(self):
        return f"ProposedAction({self.action}, {self.args}, {self.confidence})"

    @property
    def needs_clarification(self):
        return bool(self.clarification) or not self.action

    @property
    def is_known(self):
        return self.action in ACTION_SPECS


# ── Capability briefing for Angela's conversational model ───────────────────

def capability_briefing():
    """What Angela is told about her own personnel-management abilities.

    Combines the static rules from persona.py with the live configuration, so
    the rank and branch names she quotes are the ones that actually exist.

    Returns "" when the system is unconfigured — she is not told she can do this
    until she genuinely can, otherwise she would promise members actions that
    cannot be carried out.
    """
    cfg = roles_config.current()
    if not cfg.is_configured():
        return ""

    try:
        from assistant.persona import MANAGEMENT_CAPABILITIES
    except ImportError:
        return ""

    parts = [MANAGEMENT_CAPABILITIES]

    ranks = [r.display for r in cfg.normal_ranks()]
    specials = [r.display for r in cfg.special_ranks()]
    branches = [b.display for b in cfg.branches.values()]

    if ranks:
        parts.append("\nCONFIGURED RANKS (most senior first):\n" + ", ".join(ranks))
    if specials:
        parts.append("CONFIGURED SPECIAL RANKS:\n" + ", ".join(specials))
    if branches:
        parts.append("CONFIGURED BRANCHES:\n" + ", ".join(branches))

    return "\n".join(parts)


# ── Intent detection ─────────────────────────────────────────────────────────

#: Cheap pre-filter. Avoids an LLM call on every message Angela receives —
#: only text that plausibly concerns role management gets parsed as a command.
#:
#: Erring wide is the right trade here. A false positive costs one cheap
#: flash-lite call that returns "not a management request"; a false negative
#: sends the request to Angela's conversational path, where she now knows she
#: has these abilities and might describe an action that never ran. Persona rule
#: 3 ("never claim an action was performed unless the system reported back") is
#: the backstop, but the gate should not be leaning on it.
_INTENT_HINTS = re.compile(
    r"\b("
    r"register|registration|verify|verified|"
    r"rank|ranks|promote|promotion|promoted|demote|demotion|demoted|"
    r"branch|branches|transfer|transferred|reassign|assign|appoint|"
    r"timezone|time ?zone|tz|"
    r"loa|leave of absence|on leave|going away|"
    r"status|active|inactive|semi.?active|"
    r"personnel|roster|record|records|"
    r"sync|synchronise|synchronize|resync|"
    r"whois|who ?is|my (rank|record|branch|status|timezone)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_management_request(text):
    """Whether a message is worth parsing as a management command."""
    if not text or len(text.strip()) < 3:
        return False
    return bool(_INTENT_HINTS.search(text))


def _build_parser_prompt():
    catalogue = []
    for name, spec in ACTION_SPECS.items():
        args = ", ".join(f"{k} ({v})" for k, v in spec["args"].items()) or "no arguments"
        catalogue.append(f"- {name}: {spec['description']} Arguments: {args}")

    cfg = roles_config.current()
    ranks = ", ".join(sorted(r.display for r in cfg.ranks.values())) or "(none configured yet)"
    branches = ", ".join(sorted(b.display for b in cfg.branches.values())) or "(none configured yet)"

    return (
        "You translate a L.O.T.U.S. member's message into ONE role-management action.\n\n"
        "AVAILABLE ACTIONS:\n" + "\n".join(catalogue) + "\n\n"
        f"CONFIGURED RANKS: {ranks}\n"
        f"CONFIGURED BRANCHES: {branches}\n\n"
        "RULES:\n"
        "- Output JSON only, no prose, no markdown fences.\n"
        "- Never decide whether the user is permitted to do this. You do not "
        "  evaluate authority; the backend does. Just identify the request.\n"
        "- 'user' must be a numeric Discord ID taken from a <@123> mention in the "
        "  message. If a user is named but not mentioned, do not guess an ID — ask "
        "  for a mention via 'clarification'.\n"
        "- If the request is ambiguous, incomplete, or names a rank/branch that is "
        "  not in the configured lists, set 'clarification' to a short question and "
        "  leave 'action' empty.\n"
        "- If the message is not a management request at all, return "
        '{"action": "", "args": {}, "clarification": ""}.\n\n'
        "OUTPUT SHAPE:\n"
        '{"action": "<name or empty>", "args": {...}, "confidence": "high|low", '
        '"clarification": "<question or empty>"}'
    )


async def parse_intent(text, mentioned_ids=None):
    """Turn a natural-language message into a ProposedAction.

    Returns None when the message is not a management request. Falls back to a
    clarification rather than a guess whenever the model output is unusable.
    """
    if not looks_like_management_request(text):
        return None

    try:
        from assistant.gemini import generate_json
    except ImportError:
        print("[Roles:Angela] assistant.gemini unavailable; intent parsing disabled.")
        return None

    hint = ""
    if mentioned_ids:
        hint = f"\n\nDiscord IDs mentioned in this message: {list(mentioned_ids)}"

    raw = await generate_json(_build_parser_prompt(), text + hint)
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print(f"[Roles:Angela] Unparseable intent JSON: {raw[:200]!r}")
        return None

    action = (data.get("action") or "").strip()
    if not action and not data.get("clarification"):
        return None

    proposed = ProposedAction(
        action=action,
        args=data.get("args") or {},
        confidence=(data.get("confidence") or "high").strip().lower(),
        clarification=(data.get("clarification") or "").strip(),
    )

    if action and not proposed.is_known:
        print(f"[Roles:Angela] Model proposed unknown action {action!r}; rejecting.")
        return ProposedAction(
            "", clarification=f"I do not have an action called `{action}`.",
        )

    return proposed


# ── Execution ────────────────────────────────────────────────────────────────

def _target_id(args, actor_discord_id, required=False):
    """Resolve the target user ID from proposed arguments.

    Accepts a raw ID or a <@123> mention. Returns None to mean "the requester",
    which the service resolves itself.
    """
    raw = args.get("user") or args.get("target") or args.get("member")
    if raw in (None, "", "me", "self"):
        return None if not required else actor_discord_id

    text = str(raw).strip()
    match = re.search(r"\d{15,25}", text)
    if not match:
        return None
    return int(match.group())


async def execute(proposed, actor_discord_id, guild_id=None, channel_id=None,
                  actor_username=""):
    """Run a proposed action as the given Discord user.

    `actor_discord_id` must be the real message author's ID, read from Discord.
    It is the only identity the authorization layer consults, and the model
    cannot influence it.

    `actor_username` is the author's account username, read from Discord in the
    same way. It is display data for the roster only and carries no authority.
    """
    if proposed is None or not proposed.is_known:
        return ActionResult.invalid("No recognizable management action.")

    context = ActionContext(
        actor_discord_id=actor_discord_id,
        origin=ActionOrigin.ANGELA,
        guild_id=guild_id,
        channel_id=channel_id,
    )

    args = proposed.args or {}
    action = proposed.action
    spec = ACTION_SPECS[action]

    # A self-only action can never be pointed at somebody else, whatever the
    # model emitted.
    target_id = None if spec["self_only"] else _target_id(args, actor_discord_id)

    print(
        f"{context.log_prefix()} ANGELA action={action} actor={actor_discord_id} "
        f"target={target_id or 'self'} args={ {k: v for k, v in args.items() if k != 'user'} }"
    )

    try:
        if action == "register":
            return await service.register(
                context, discord_username=actor_username or str(actor_discord_id)
            )

        if action == "set_timezone":
            value = args.get("timezone") or args.get("value")
            if not value:
                return ActionResult.invalid("No timezone given.")
            return await service.set_timezone(context, str(value), target_id)

        if action == "set_rank":
            value = args.get("rank") or args.get("value")
            if not value:
                return ActionResult.invalid("No rank given.")
            if target_id is None:
                return ActionResult.invalid("Mention the user whose rank should change.")
            return await service.set_rank(context, str(value), target_id)

        if action == "set_branch":
            value = args.get("branch") or args.get("value")
            if not value:
                return ActionResult.invalid("No branch given.")
            if target_id is None:
                return ActionResult.invalid("Mention the user whose branch should change.")
            return await service.set_branch(context, str(value), target_id)

        if action == "set_status":
            value = args.get("status") or args.get("value")
            if not value:
                return ActionResult.invalid("No status given.")
            return await service.set_status(context, str(value), target_id)

        if action == "set_loa":
            value = args.get("value") or args.get("loa")
            if not value:
                return ActionResult.invalid("No LOA value given.")
            return await service.set_loa(context, str(value), target_id)

        if action == "whois":
            return await service.get_record(context, target_id)

        if action == "force_sync":
            return await service.force_sync(context, target_id)

        if action == "roles_status":
            return await service.inspect(context)

    except Exception as exc:
        print(f"{context.log_prefix()} ANGELA action failed: {type(exc).__name__}: {exc}")
        return ActionResult.error(f"The operation failed: {type(exc).__name__}: {exc}")

    return ActionResult.invalid(f"Action `{action}` is not wired up.")


# ── Response generation ──────────────────────────────────────────────────────

_RESPONSE_PROMPT = (
    "You are Angela S.T.R.T.S. reporting the outcome of a role-management "
    "operation a member asked you to perform.\n\n"
    "Rules:\n"
    "- Stay in character: dry, precise, mildly exasperated when warranted.\n"
    "- Report the outcome faithfully. Do not soften a refusal into a maybe, and "
    "  do not claim anything succeeded that did not.\n"
    "- If the request was refused, state that plainly and give the stated reason. "
    "  Never suggest a way around it — you do not decide authorization.\n"
    "- If the request was unclear or inefficient, say what you need instead.\n"
    "- Two or three sentences. No markdown headers, no bullet lists.\n"
)


async def narrate(result, user_request="", user_name=""):
    """Have Angela phrase an ActionResult in her own voice.

    Falls back to the service's own message if the model is unavailable — the
    user always gets the truthful outcome, styled or not.
    """
    try:
        from assistant.gemini import generate_plain
    except ImportError:
        return result.message

    outcome = {
        ActionStatus.OK: "SUCCEEDED",
        ActionStatus.DENIED: "REFUSED — the member is not authorized",
        ActionStatus.INVALID: "REJECTED — the request was invalid or incomplete",
        ActionStatus.NOT_FOUND: "FAILED — no such record",
        ActionStatus.ERROR: "FAILED — an internal error occurred",
    }[result.status]

    prompt = (
        f"Member: {user_name or 'a member'}\n"
        f"They asked: {user_request[:400] or '(not recorded)'}\n"
        f"Outcome: {outcome}\n"
        f"System message (this is the factual result — do not contradict it):\n"
        f"{result.message[:800]}\n\n"
        f"Report this to them as Angela."
    )

    try:
        response = await generate_plain(_RESPONSE_PROMPT, prompt)
    except Exception as exc:
        print(f"[Roles:Angela] Narration failed: {type(exc).__name__}: {exc}")
        return result.message

    return response or result.message


# ── Entry point used by assistant/bot.py ─────────────────────────────────────

async def handle_message(message, cleaned_text):
    """Try to handle a message as a role-management request.

    Returns the reply text, or None if this was not a management request and
    Angela's normal conversational path should handle it.
    """
    if not roles_config.current().is_configured():
        return None

    mentioned_ids = [u.id for u in message.mentions if not u.bot]

    proposed = await parse_intent(cleaned_text, mentioned_ids)
    if proposed is None:
        return None

    if proposed.needs_clarification:
        return await narrate(
            ActionResult.invalid(
                proposed.clarification or "I could not tell what you were asking for."
            ),
            cleaned_text,
            message.author.display_name,
        )

    result = await execute(
        proposed,
        actor_discord_id=message.author.id,  # the real author. Not model-supplied.
        # Account username, not display_name: display_name is the per-server
        # nickname, and the roster records the account.
        actor_username=message.author.name,
        guild_id=message.guild.id if message.guild else None,
        channel_id=message.channel.id,
    )

    # A verification link binds whoever opens it. Angela answers in-channel, so
    # it must never appear in her reply — it goes to the author by DM instead.
    auth_url = result.detail.get("auth_url")
    if result.ok and auth_url:
        delivered = await _dm_auth_url(message.author, auth_url)
        if delivered:
            return await narrate(
                ActionResult.success(
                    "I have sent you a private verification link. Open it to link "
                    "your Roblox account."
                ),
                cleaned_text, message.author.display_name,
            )
        return await narrate(
            ActionResult.invalid(
                "I could not send you a direct message, and this link must not be "
                "posted in a channel. Enable DMs from server members, or use the "
                "/register command instead."
            ),
            cleaned_text, message.author.display_name,
        )

    return await narrate(result, cleaned_text, message.author.display_name)


async def _dm_auth_url(user, auth_url):
    """Send a verification link privately. Returns False if DMs are closed."""
    try:
        await user.send(
            "🔗 **Link your Roblox account**\n"
            f"{auth_url}\n\n"
            "This link is yours alone and expires shortly. Do not share it — "
            "whoever opens it links *their* Roblox account to *your* Discord account."
        )
        return True
    except Exception as exc:
        print(f"[Roles:Angela] Could not DM verification link: {type(exc).__name__}: {exc}")
        return False
