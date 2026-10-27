"""Configuration structures for ranks, branches, servers and permissions.

NOTHING IN THIS FILE CONTAINS REAL DATA. Every rank, branch, server ID, role ID
and Roblox mapping is a placeholder to be filled in later via
``roles_config.json`` (see ``roles_config.example.json`` for the template).

Secrets (bot token, Rover key, Roblox Open Cloud key) do NOT live here — they go
through the project's existing ``config.py`` env-var mechanism.

Load order:
    1. dataclass defaults below (empty registries)
    2. roles_config.json, if present, overlaid on top
    3. ROLES_CONFIG_PATH env var can point somewhere else

Call ``reload()`` to pick up edits without restarting the process.
"""

import json
import os
import re
from dataclasses import dataclass, field

from config import config as app_config

from roles.models import BRANCH_SEPARATOR, Category

# ── Paths ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = (
    os.environ.get("ROLES_CONFIG_PATH")
    or app_config.get("ROLES_CONFIG_PATH")
    or os.path.join(_PROJECT_ROOT, "roles_config.json")
)

#: Sentinel used throughout the example config. Any value equal to this (or any
#: unset ID) marks the entry as "not supplied yet" and disables the dependent
#: synchronization path instead of guessing.
PLACEHOLDER = None


# ── Rank configuration ───────────────────────────────────────────────────────

@dataclass
class RobloxRoleRef:
    """One Roblox group role a rank grants.

    `group_id` of None means "resolve at use time" — from the branch's own
    Roblox group if it has one, otherwise the main group.
    """

    role_id: int
    group_id: "int | None" = None
    #: Which branch key this came from ("N/A" or a branch), for messages.
    source: str = ""


@dataclass
class RobloxAssignment:
    """A fully resolved (group, role) pair the member should hold."""

    group_id: int
    role_id: int
    source: str = ""

    def __repr__(self):
        return f"RobloxAssignment(group={self.group_id}, role={self.role_id}, from={self.source})"


@dataclass
class RankDef:
    """One rank.

    There will eventually be 14 normal ranks (Training Associate .. Branch Head)
    plus a 15th Owner/CEO rank, plus an open-ended set of special ranks. None of
    those names or ordering values are invented here — supply them in
    roles_config.json.
    """

    #: Stable internal key. Used in code, logs and technical columns. Never
    #: rendered to users; `display` is what goes into column F.
    key: str
    #: Exact string as it appears in the column F dropdown.
    display: str = ""
    #: Which section of the sheet this rank sorts into.
    category: "Category | None" = None
    #: Ordering weight. Higher = more senior. Used for sorting AND as the basis
    #: for rank comparison in the permission system.
    order: int = 0
    #: False for the 14 normal ranks + Owner/CEO; True for special ranks.
    special: bool = False
    #: Groups special ranks together in the sort (all Overseers, then all
    #: Ambassadors, ...). Ignored for normal ranks. Lower value sorts first.
    special_group_order: int = 0
    #: Discord role ID in the MAIN server. None = not supplied / not applicable.
    main_discord_role_id: "int | None" = None
    #: Additional MAIN-SERVER-ONLY roles granted by this rank, on top of the
    #: primary one above. Auxiliary roles are never applied in branch servers.
    main_aux_discord_role_ids: list = field(default_factory=list)
    #: True for ranks whose authority is not tied to one branch (Executive Team,
    #: CEO). A universal rank receives its branch-server roles in EVERY branch
    #: server that maps the rank, whatever the member's own branches are. A
    #: normal rank only receives them in the servers of its own branches.
    #: Discord only — the Roblox primary line still has to be one of the
    #: member's own branches.
    universal: bool = False
    #: Roblox group roles for this rank, keyed by line, as RobloxRoleRef.
    #: {"N/A": <ref>, "BRANCH_KEY": <ref>}
    #:
    #: These are ALTERNATIVES: a member holds exactly one of them, their
    #: primary role. "N/A" is the mainline and the default; a branch entry is
    #: the branch line, which a member of that branch may switch to with
    #: /set primary. See RolesConfig.roblox_assignments().
    #:
    #: An empty mapping means this rank has no Roblox equivalent — Roblox
    #: synchronization is skipped entirely rather than guessed at.
    roblox_roles_by_branch: dict = field(default_factory=dict)
    #: Explicit opt-in for Roblox sync. Special ranks default to False so a
    #: special rank can never silently move someone's Roblox group role.
    syncs_roblox: bool = True
    #: Free-form extension point for per-rank data added later.
    extra: dict = field(default_factory=dict)

    def roblox_role_ref(self, branch_key):
        """The raw RobloxRoleRef mapped for one branch key, if any."""
        if not self.syncs_roblox:
            return None
        return self.roblox_roles_by_branch.get((branch_key or NA_BRANCH_KEY).upper())

    def roblox_role_for(self, branch_key):
        """Role ID mapped for one line. Says nothing about which line a member
        is on — use RolesConfig.roblox_assignments() for what they should hold."""
        ref = self.roblox_role_ref(branch_key)
        return ref.role_id if ref else None


@dataclass
class BranchDef:
    """One branch, including the explicit N/A branch for unaffiliated users."""

    key: str
    display: str = ""
    #: Discord server ID for this branch's own server.
    discord_guild_id: "int | None" = None
    #: Branch-specific Discord role IDs, keyed by rank key:
    #: {"RANK_KEY": [<role_id>, ...]}. Applied inside `discord_guild_id`.
    discord_roles_by_rank: dict = field(default_factory=dict)
    #: MAIN-SERVER-ONLY roles granted by belonging to this branch — the branch
    #: half of the auxiliary role mapping. These live in the main server, not in
    #: the branch server, so a member's branch is visible where everyone is.
    main_aux_discord_role_ids: list = field(default_factory=list)
    #: This branch's own Roblox group, if it has a separate one. When set, the
    #: branch's role mappings resolve against it instead of the main group.
    roblox_group_id: "int | None" = None
    #: True only for the sentinel "no branch" entry.
    is_na: bool = False
    extra: dict = field(default_factory=dict)

    def discord_roles_for(self, rank_key):
        """Role IDs this branch's server grants to one rank. Empty if unmapped."""
        return list(self.discord_roles_by_rank.get((rank_key or "").upper(), ()))


#: Key used for the explicit "no branch" configuration entry.
NA_BRANCH_KEY = "N/A"

#: `GrantableRoleDef.guilds` entry meaning "the main server" rather than a branch.
MAIN_GUILD_SCOPE = "MAIN"


# ── Grantable roles ──────────────────────────────────────────────────────────

@dataclass
class GrantableRoleDef:
    """A role modifier a member can hold ON TOP of their rank.

    Not a rank: holding one never changes column F, the member's category, their
    place on the sheet or their standing in the permission system. It only
    adjusts which Discord roles they carry in ONE server.

    Granted and revoked with /grant by anyone who may use SET_RANK on the
    member, and stored on the member's record, so it survives rank and branch
    changes until it is revoked.
    """

    #: Stable internal key. Stored in the record's technical GRANTS column.
    key: str
    display: str = ""
    description: str = ""
    #: Which servers this applies in: each entry is MAIN_GUILD_SCOPE or a branch
    #: key. A branch scope means that branch's own server, and does not require
    #: the member to belong to the branch.
    guilds: list = field(default_factory=lambda: [MAIN_GUILD_SCOPE])
    #: Roles added on top of what the rank and branch already give.
    #:
    #: With more than one server in `guilds`, both role lists may mix IDs from
    #: all of them. Discord role IDs are globally unique, so each server simply
    #: applies the ones that exist there — see DiscordSynchronizer._sync_guild.
    add_discord_role_ids: list = field(default_factory=list)
    #: Roles the rank or branch would otherwise give that this modifier cancels.
    #: Only ever subtracts from the foundational roles — a role the member holds
    #: for an unrelated, unmanaged reason is not touched.
    remove_discord_role_ids: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

# ── Discord server configuration ─────────────────────────────────────────────

@dataclass
class DiscordConfig:
    #: The main designated server, where /register is used.
    main_guild_id: "int | None" = None
    #: Channel for synchronization failure reports. Optional.
    log_channel_id: "int | None" = None
    #: Roles the system is allowed to add/remove. Populated automatically from
    #: rank + branch configuration; anything outside it is never touched, so
    #: unmanaged roles on a member survive synchronization untouched.
    extra_managed_role_ids: list = field(default_factory=list)
    #: Seconds to wait between per-member role edits, to stay under rate limits.
    member_edit_delay: float = 0.35


@dataclass
class RobloxConfig:
    group_id: "int | None" = None
    #: Open Cloud base URL. Overridable in case the endpoint moves.
    api_base: str = "https://apis.roblox.com/cloud/v2"
    request_timeout: float = 15.0
    #: Public group page, shown to registrants who are not members yet.
    #: Derived from group_id when left blank.
    group_url: str = ""
    #: Pause between role mutations. assignRole/unassignRole are limited to
    #: 300/min per API-key owner and reconciling a member costs one call per
    #: role, so a small gap keeps bulk syncs under the ceiling.
    request_delay: float = 0.25
    #: Whether members may take their Roblox role from a branch line. Only the
    #: starting value: /aria branch-roles switches it at runtime, and that
    #: choice is kept in data/roles_state.json, which wins while it exists.
    #: See roles/state.py.
    branch_roles_enabled: bool = True


@dataclass
class RobloxOAuthConfig:
    """Roblox OAuth 2.0 / OIDC — the default account verification method.

    Preferred over Rover because it asks the user directly: Rover requires each
    user to separately opt in before a third party may read their Discord/Roblox
    link, which is an extra step outside this system's control.

    client_id and client_secret are secrets and come from the environment, not
    from here.
    """

    #: Public callback URL registered with the Roblox app. Must be plain HTTPS
    #: (or http://localhost for local debugging), max 256 characters.
    #: e.g. https://your-app.koyeb.app/roblox/callback
    redirect_uri: str = ""
    #: Identity only. Anything more would mean a scarier consent screen for the
    #: user and no functional gain here.
    scopes: list = field(default_factory=lambda: ["openid", "profile"])
    #: How long an unfinished verification link stays valid.
    state_ttl_seconds: int = 600
    request_timeout: float = 15.0


@dataclass
class RoverConfig:
    """Rover — the legacy verification method, kept as a fallback.

    Used ONLY during /register. After that the stored Discord ID and Roblox ID
    in the technical columns are canonical.
    """

    api_base: str = "https://registry.rover.link/api"
    request_timeout: float = 15.0


@dataclass
class RegistrationConfig:
    #: How /register verifies a Roblox account.
    #:   "OAUTH" — Roblox OAuth 2.0 (default; no third-party opt-in needed)
    #:   "ROVER" — the legacy Rover lookup
    verification_method: str = "OAUTH"

    #: Rank granted when the registrant is ALREADY in the Roblox group.
    #: Someone outside the group gets `default_registration_rank` instead and is
    #: told to join. Set to None to skip the group check entirely and always use
    #: the default rank.
    in_group_rank: "str | None" = None

    #: How often members still holding `default_registration_rank` are re-checked
    #: against the Roblox group, in minutes. One who has been accepted since
    #: registering is moved up to `in_group_rank` automatically, exactly as if
    #: they had been in the group when they registered. Needs `in_group_rank`;
    #: 0 turns the check off.
    applicant_check_minutes: int = 10


# ── Permission configuration ─────────────────────────────────────────────────

class Relation(str):
    """Relationship between the acting user and the target user."""

    SELF = "SELF"
    LOWER = "LOWER"      # target ranks below the actor
    EQUAL = "EQUAL"      # target ranks the same as the actor
    HIGHER = "HIGHER"    # target ranks above the actor
    UNRANKED = "UNRANKED"  # target has no resolvable rank


@dataclass
class PermissionGrant:
    """One way to satisfy a permission. Conditions inside a grant are ANDed.

    So a grant of {relations: [LOWER], categories: [HIGH-RANK]} means
    "a HIGH-RANK actor, acting on a lower-ranked target".
    """

    #: Which relations to the target are allowed (see Relation).
    allowed_relations: list = field(default_factory=list)
    #: Optional floor: actor's rank order must be >= this to use the action.
    min_rank_order: "int | None" = None
    #: Optional restriction: actor's category must be one of these.
    allowed_categories: list = field(default_factory=list)
    #: Human-readable label, used in denial messages.
    note: str = ""

    def is_configured(self):
        return bool(self.allowed_relations)

    def describe(self):
        """'a lower-ranked user, if you are HIGH-RANK' — for denial messages."""
        who = {
            Relation.SELF: "yourself",
            Relation.LOWER: "a lower-ranked user",
            Relation.EQUAL: "a user of equal rank",
            Relation.HIGHER: "a higher-ranked user",
            Relation.UNRANKED: "a user with no resolvable rank",
        }
        targets = " or ".join(who.get(r, r.lower()) for r in self.allowed_relations)

        conditions = []
        if self.allowed_categories:
            conditions.append(
                "you are " + " or ".join(c.value for c in self.allowed_categories)
            )
        if self.min_rank_order is not None:
            conditions.append(f"your rank order is at least {self.min_rank_order}")

        if conditions:
            return f"{targets}, if {' and '.join(conditions)}"
        return targets


@dataclass
class PermissionRule:
    """Authorization for one action: a list of alternative grants, ORed.

    The action is permitted if ANY grant matches. That is what lets one
    permission say "anyone, on themselves — OR a high rank, on someone below
    them", which a single flat set of conditions cannot express.

    Deliberately empty by default: no permission is granted until it is
    explicitly configured. The exact hierarchy has not been specified yet, and
    guessing at it would be the wrong kind of wrong.
    """

    grants: list = field(default_factory=list)
    #: Human-readable note, surfaced in the denial message when unconfigured.
    note: str = ""

    def is_configured(self):
        return any(g.is_configured() for g in self.grants)

    def describe(self):
        """'yourself; or a lower-ranked user, if you are HIGH-RANK'"""
        parts = [g.describe() for g in self.grants if g.is_configured()]
        return "; or ".join(parts) if parts else "nobody"


class Permission(str):
    """Every gated action in the system. Commands reference these constants —
    they must never implement their own rank checks."""

    REGISTER_SELF = "REGISTER_SELF"
    VIEW_RECORD = "VIEW_RECORD"
    SET_TIMEZONE = "SET_TIMEZONE"
    SET_RANK = "SET_RANK"
    SET_BRANCH = "SET_BRANCH"
    SET_STATUS = "SET_STATUS"
    SET_LOA = "SET_LOA"
    #: Choose which line the member's Roblox role comes from (mainline or one
    #: of their branches).
    SET_PRIMARY_ROLE = "SET_PRIMARY_ROLE"
    #: Switch branch-line Roblox roles on or off for everyone.
    TOGGLE_BRANCH_ROLES = "TOGGLE_BRANCH_ROLES"
    FORCE_SYNC = "FORCE_SYNC"
    RESTRUCTURE_SHEET = "RESTRUCTURE_SHEET"
    ADMIN_INSPECT = "ADMIN_INSPECT"

    ALL = (
        REGISTER_SELF, VIEW_RECORD, SET_TIMEZONE, SET_RANK, SET_BRANCH,
        SET_STATUS, SET_LOA, SET_PRIMARY_ROLE, TOGGLE_BRANCH_ROLES,
        FORCE_SYNC, RESTRUCTURE_SHEET, ADMIN_INSPECT,
    )


@dataclass
class PermissionConfig:
    #: Permission -> PermissionRule. Missing entries deny.
    rules: dict = field(default_factory=dict)
    #: Bootstrap escape hatch: Discord IDs that bypass rank checks entirely.
    #: Needed because nobody has a rank before the sheet is populated. Empty by
    #: default — populate deliberately, and prune once ranks exist.
    bootstrap_discord_ids: list = field(default_factory=list)
    #: What to do when a permission has no rule configured. "DENY" is the only
    #: safe default; "ALLOW" exists purely for local development.
    default_decision: str = "DENY"

    def rule_for(self, permission):
        return self.rules.get(permission, PermissionRule())


# ── Synchronization / polling ────────────────────────────────────────────────

@dataclass
class SyncConfig:
    #: Background poll interval. 30-60s is the intended operating range.
    poll_interval_seconds: int = 45
    #: Retry backoff for failed sync jobs, in seconds, per attempt.
    retry_backoff_seconds: list = field(default_factory=lambda: [10, 30, 120, 600, 1800])
    #: Attempts before a job is moved to the dead-letter list for manual review.
    max_attempts: int = 5
    #: Apply Discord/Roblox changes at all. False = dry run (log only), useful
    #: while the real IDs are still placeholders.
    enabled: bool = True
    #: Re-layout the sheet automatically when the poller sees misplaced records.
    auto_restructure: bool = True

    # ── Removal policy ──
    # A record disappearing from the sheet means the person is no longer
    # personnel. Their access is revoked; re-entry requires /register again.

    #: Strip every managed Discord role, in the main server AND all branch
    #: servers, when a record is removed from the sheet.
    on_removal_strip_discord: bool = True

    #: What removal does to Roblox group roles. unassignRole makes a real
    #: revocation possible, so this mirrors the Discord strip by default.
    #:   "UNASSIGN" — unassign every managed role, in every managed group (default)
    #:   "SET_ROLE" — unassign the managed roles, then leave `removal_roblox_role_id`
    #:                behind in the main group as a "removed" marker
    #:   "IGNORE"   — leave Roblox roles alone
    #: Built-in roles (Owner/Member/Guest) are never touched, so a stripped
    #: member falls back to plain Member rather than being removed from the group.
    on_removal_roblox: str = "UNASSIGN"

    #: Role left behind when on_removal_roblox is "SET_ROLE".
    removal_roblox_role_id: "int | None" = None
    #: Install conditional-format rules for column J. Off by default so an
    #: automated run never clobbers hand-made formatting rules.
    manage_status_formatting: bool = False
    #: How often the Discord half of column D ("Roblox (discord)") is re-checked
    #: against each member's current Discord username, in minutes. Once a day by
    #: default. The Roblox half is NOT refreshed — it is set at registration and
    #: looked up once more only for rows that predate the combined format. The
    #: first pass runs right after startup. 0 turns the pass off entirely.
    username_refresh_minutes: int = 1440


# ── Root configuration object ────────────────────────────────────────────────

@dataclass
class RolesConfig:
    ranks: dict = field(default_factory=dict)        # key -> RankDef
    branches: dict = field(default_factory=dict)     # key -> BranchDef
    grantable_roles: dict = field(default_factory=dict)  # key -> GrantableRoleDef
    #: Discord roles every registered member holds whatever their rank or
    #: branch, by scope: {"MAIN": [role_id, ...], "BRANCH_KEY": [...]}. A
    #: branch scope means that branch's own server, for every registered member
    #: who is in it. A grantable role's remove list can cancel them.
    general_roles: dict = field(default_factory=dict)
    #: Rank key assigned by /register. None = leave the new record's rank blank
    #: rather than guess. A blank rank sorts to the bottom of its section and
    #: synchronizes nothing, which is the safe state until this is supplied.
    default_registration_rank: "str | None" = None
    #: Branch key assigned by /register. None, or a key that is not a configured
    #: branch, falls back to N/A. See registration_branch_key().
    default_registration_branch: "str | None" = None
    #: Sheet section order, top to bottom.
    category_order: list = field(default_factory=lambda: [
        Category.HIGH_RANK, Category.MID_RANK, Category.LOW_RANK,
    ])
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    roblox: RobloxConfig = field(default_factory=RobloxConfig)
    roblox_oauth: RobloxOAuthConfig = field(default_factory=RobloxOAuthConfig)
    rover: RoverConfig = field(default_factory=RoverConfig)
    registration: RegistrationConfig = field(default_factory=RegistrationConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    loaded_from: str = ""

    # ── Lookups ──

    def rank(self, key):
        if not key:
            return None
        return self.ranks.get(str(key).strip().upper())

    def rank_by_display(self, display):
        """Resolve a column F dropdown value back to a RankDef."""
        if not display:
            return None
        needle = str(display).strip().casefold()
        for rank in self.ranks.values():
            if rank.display.strip().casefold() == needle:
                return rank
            if rank.key.casefold() == needle:
                return rank
        return None

    def branch(self, key):
        if not key:
            return self.branches.get(NA_BRANCH_KEY)
        return self.branches.get(str(key).strip().upper())

    def branch_by_display(self, display):
        if not display:
            return self.branches.get(NA_BRANCH_KEY)
        needle = str(display).strip().casefold()
        for branch in self.branches.values():
            if branch.display.strip().casefold() == needle:
                return branch
            if branch.key.casefold() == needle:
                return branch
        return None

    # ── Branch membership ──

    def normalize_branch_keys(self, keys):
        """Canonical form of a set of branch keys.

        De-duplicated, N/A dropped (it means "no branch", so it cannot sit next
        to a real one), configured branches in configuration order, then any
        unrecognised keys as given. Unknown keys are kept rather than dropped,
        the same way an unknown rank is, so a typo on the sheet is reported
        instead of silently erased.
        """
        wanted = []
        for key in keys or ():
            key = str(key).strip().upper()
            if key and key != NA_BRANCH_KEY and key not in wanted:
                wanted.append(key)
        known = [key for key in self.branches if key in wanted]
        unknown = [key for key in wanted if key not in self.branches]
        return tuple(known + unknown)

    def branches_of(self, branch_keys):
        """BranchDefs a record belongs to, in configuration order.

        A record in no configured branch belongs to N/A, so N/A's main-server
        auxiliary roles still apply to it.
        """
        found = [
            self.branches[key] for key in self.normalize_branch_keys(branch_keys)
            if key in self.branches
        ]
        if found:
            return found
        na = self.branches.get(NA_BRANCH_KEY)
        return [na] if na else []

    def branches_display(self, branch_keys):
        """Column G text: 'ECHELON, MEDICAL', or N/A for no branch."""
        if not branch_keys:
            na = self.branches.get(NA_BRANCH_KEY)
            return na.display if na else ""
        return BRANCH_SEPARATOR.join(
            self.branches[key].display if key in self.branches else key
            for key in branch_keys
        )

    # ── Grantable roles ──

    def grantable(self, key):
        if not key:
            return None
        return self.grantable_roles.get(str(key).strip().upper())

    def grantable_by_display(self, display):
        if not display:
            return None
        needle = str(display).strip().casefold()
        for grantable in self.grantable_roles.values():
            if grantable.display.strip().casefold() == needle:
                return grantable
            if grantable.key.casefold() == needle:
                return grantable
        return None

    def _scope_guild_id(self, scope):
        if scope == MAIN_GUILD_SCOPE:
            return self.discord.main_guild_id
        branch = self.branches.get(scope)
        return branch.discord_guild_id if branch else None

    def grantable_guild_ids(self, grantable):
        """The servers a grantable role applies in. Scopes that are not
        resolvable yet (unknown branch, server ID not supplied) are left out,
        so an empty list means the role cannot take effect anywhere."""
        guild_ids = []
        for scope in grantable.guilds:
            guild_id = self._scope_guild_id(scope)
            if guild_id and guild_id not in guild_ids:
                guild_ids.append(guild_id)
        return guild_ids

    def grantable_guild_label(self, grantable):
        """'the main server' / 'the ECHELON and R&D servers', for messages."""
        names = []
        for scope in grantable.guilds:
            if scope == MAIN_GUILD_SCOPE:
                names.append("main")
            else:
                branch = self.branches.get(scope)
                names.append(branch.display if branch else scope)
        if len(names) == 1:
            return f"the {names[0]} server"
        return f"the {', '.join(names[:-1])} and {names[-1]} servers"

    def grant_adjustments(self, grant_keys, guild_id):
        """What a member's held grantable roles change in one server.

        Returns (added_ids, suppressed_ids). Keys that are not configured are
        ignored rather than guessed at, the same way an unknown rank is.

        For a grantable role spanning several servers the IDs returned are the
        whole mixed list; the ones belonging to other servers are filtered out
        against the live server by the synchronizer.
        """
        added, suppressed = set(), set()
        for key in grant_keys or ():
            grantable = self.grantable(key)
            if grantable is None or not guild_id:
                continue
            if guild_id not in self.grantable_guild_ids(grantable):
                continue
            added.update(grantable.add_discord_role_ids)
            suppressed.update(grantable.remove_discord_role_ids)
        return added, suppressed

    def shared_grant_role_ids(self, guild_id):
        """Role IDs reaching this server through a grantable role that spans
        several servers — the only IDs that may legitimately not exist here."""
        shared = set()
        for grantable in self.grantable_roles.values():
            guild_ids = self.grantable_guild_ids(grantable)
            if len(grantable.guilds) > 1 and guild_id in guild_ids:
                shared.update(grantable.add_discord_role_ids)
                shared.update(grantable.remove_discord_role_ids)
        return shared

    # ── General roles ──

    def general_role_ids(self, guild_id):
        """Roles every registered member holds in one server."""
        role_ids = set()
        if not guild_id:
            return role_ids
        for scope, ids in self.general_roles.items():
            if self._scope_guild_id(scope) == guild_id:
                role_ids.update(ids)
        return role_ids

    # ── Discord server resolution ──

    def branch_guild_ids(self):
        """Every distinct branch server, excluding the main one. Config order."""
        guild_ids = []
        for branch in self.branches.values():
            guild_id = branch.discord_guild_id
            if guild_id and guild_id != self.discord.main_guild_id and guild_id not in guild_ids:
                guild_ids.append(guild_id)
        return guild_ids

    # ── Registration ranks ──

    def applicant_promotion(self):
        """(applicant_rank, in_group_rank) when joining the Roblox group should
        move a member from the first to the second, else None.

        None covers every way the pair can be unusable — either rank missing
        from the configuration, or both naming the same rank — so callers have
        one thing to check.
        """
        applicant = self.rank(self.default_registration_rank)
        joined = self.rank(self.registration.in_group_rank)
        if applicant is None or joined is None or applicant.key == joined.key:
            return None
        return applicant, joined

    def registration_branch_key(self):
        """Branch key a new record starts in: `default_registration_branch` when
        it names a configured branch, else N/A."""
        key = str(self.default_registration_branch or "").strip().upper()
        return key if key in self.branches else NA_BRANCH_KEY

    # ── Roblox role resolution ──

    def primary_line_options(self, rank, branch_keys, branch_lines_enabled=True):
        """Branch lines a member could take their Roblox role from.

        The branches they belong to that map a Roblox role at their rank, in
        configuration order. The mainline is always the alternative and is not
        listed. Empty while branch lines are switched off.
        """
        if rank is None or not rank.syncs_roblox or not branch_lines_enabled:
            return []
        return [
            branch.key for branch in self.branches_of(branch_keys)
            if not branch.is_na and rank.roblox_role_ref(branch.key) is not None
        ]

    def effective_primary_line(self, rank, branch_keys, primary_line,
                               branch_lines_enabled=True):
        """The line actually used: the chosen branch line while it is still
        available, otherwise the mainline (returned as "").

        Falling back is what keeps a stale choice harmless — after leaving the
        branch, after a rank change to one that branch does not map, or while
        branch lines are switched off.
        """
        line = str(primary_line or "").strip().upper()
        if line and line in self.primary_line_options(rank, branch_keys, branch_lines_enabled):
            return line
        return ""

    def roblox_assignments(self, rank, branch_keys=(), primary_line="",
                           branch_lines_enabled=True):
        """The (group, role) a member of this rank should hold: at most one.

        The rank's mappings are alternatives. A member holds the role of their
        effective primary line — the mainline ("N/A" entry) by default, or a
        branch entry they switched to with /set primary.

        The role resolves to a group: the ref's explicit group_id if given,
        else the branch's own `roblox_group_id`, else the main group.

        Returns [] when nothing is mapped for that line — never a guess.
        """
        if rank is None or not rank.syncs_roblox:
            return []

        line = self.effective_primary_line(
            rank, branch_keys, primary_line, branch_lines_enabled
        )
        if line:
            ref = rank.roblox_role_ref(line)
            resolved = self._resolve_roblox_ref(ref, self.branches.get(line), line)
        else:
            ref = rank.roblox_role_ref(NA_BRANCH_KEY)
            resolved = (
                self._resolve_roblox_ref(ref, None, NA_BRANCH_KEY) if ref else None
            )

        return [resolved] if resolved else []

    def _resolve_roblox_ref(self, ref, branch, source):
        group_id = ref.group_id
        if group_id is None and branch is not None:
            group_id = branch.roblox_group_id
        if group_id is None:
            group_id = self.roblox.group_id
        if group_id is None:
            return None
        return RobloxAssignment(group_id=group_id, role_id=ref.role_id, source=source)

    def _all_roblox_refs(self):
        """Every configured (rank, branch_key, ref) triple."""
        for rank in self.ranks.values():
            if not rank.syncs_roblox:
                continue
            for branch_key, ref in rank.roblox_roles_by_branch.items():
                yield rank, branch_key, ref

    def managed_roblox_role_ids(self, group_id):
        """Every Roblox role ID this system owns in one group.

        Roles outside this set are never unassigned, so a role granted for an
        unrelated reason survives synchronization — the same rule the Discord
        side follows.
        """
        managed = set()
        for _rank, branch_key, ref in self._all_roblox_refs():
            branch = self.branch(branch_key) if branch_key != NA_BRANCH_KEY else None
            resolved = self._resolve_roblox_ref(ref, branch, branch_key)
            if resolved and resolved.group_id == group_id:
                managed.add(resolved.role_id)
        return managed

    def managed_roblox_groups(self):
        """Every Roblox group the configuration touches."""
        groups = set()
        for _rank, branch_key, ref in self._all_roblox_refs():
            branch = self.branch(branch_key) if branch_key != NA_BRANCH_KEY else None
            resolved = self._resolve_roblox_ref(ref, branch, branch_key)
            if resolved:
                groups.add(resolved.group_id)
        return groups

    def normal_ranks(self):
        return sorted(
            (r for r in self.ranks.values() if not r.special),
            key=lambda r: r.order,
            reverse=True,
        )

    def special_ranks(self):
        return sorted(
            (r for r in self.ranks.values() if r.special),
            key=lambda r: (r.special_group_order, -r.order),
        )

    def managed_role_ids(self, guild_id):
        """Every Discord role ID the system owns in one guild.

        Roles outside this set are never added or removed, so manually assigned
        cosmetic roles survive synchronization. Auxiliary roles are included so
        that obsolete ones are cleaned up when a rank or branch changes, and so
        are the roles a grantable role ADDS, so they come off when it is revoked,
        and the general roles, so they come off when a record is removed.

        A grantable role's `remove_discord_role_ids` are deliberately NOT
        included: they only cancel roles that are already managed. Owning them
        here would strip that role from every member who merely holds it.
        """
        managed = set(self.discord.extra_managed_role_ids)

        if guild_id and guild_id == self.discord.main_guild_id:
            for rank in self.ranks.values():
                if rank.main_discord_role_id:
                    managed.add(rank.main_discord_role_id)
                managed.update(rank.main_aux_discord_role_ids)
            # Branch auxiliary roles live in the MAIN server, not the branch one.
            for branch in self.branches.values():
                managed.update(branch.main_aux_discord_role_ids)

        for branch in self.branches.values():
            if branch.discord_guild_id and branch.discord_guild_id == guild_id:
                for role_ids in branch.discord_roles_by_rank.values():
                    managed.update(role_ids)

        if guild_id:
            for grantable in self.grantable_roles.values():
                if guild_id in self.grantable_guild_ids(grantable):
                    managed.update(grantable.add_discord_role_ids)

        managed.update(self.general_role_ids(guild_id))

        return managed

    # ── Readiness ──

    def is_configured(self):
        """True once enough real data exists for the system to do useful work."""
        return bool(self.ranks) and bool(self.branches)

    def missing_configuration(self):
        """Human-readable list of what still has to be supplied."""
        missing = []
        if not self.ranks:
            missing.append("rank definitions (14 normal + Owner/CEO + special ranks)")
        if not self.branches:
            missing.append("branch definitions (including the N/A branch)")
        if not self.discord.main_guild_id:
            missing.append("main Discord server ID")
        if not any(r.main_discord_role_id for r in self.ranks.values()):
            missing.append("main-server Discord role IDs per rank")
        if not self.roblox.group_id:
            missing.append("Roblox group ID")
        if not any(r.roblox_roles_by_branch for r in self.ranks.values()):
            missing.append("Roblox rank+branch -> group role mappings")
        if self.registration.verification_method == "ROVER":
            if not secret("ROVER_API_KEY"):
                missing.append("ROVER_API_KEY (registration.verification_method is ROVER)")
        else:
            if not secret("ROBLOX_OAUTH_CLIENT_ID"):
                missing.append("ROBLOX_OAUTH_CLIENT_ID")
            if not secret("ROBLOX_OAUTH_CLIENT_SECRET"):
                missing.append("ROBLOX_OAUTH_CLIENT_SECRET")
            if not self.roblox_oauth.redirect_uri:
                missing.append("roblox_oauth.redirect_uri (public HTTPS callback URL)")
        if not app_config.get("ROBLOX_API_KEY"):
            missing.append("ROBLOX_API_KEY (Open Cloud)")
        if not app_config.get("ROLES_TOKEN"):
            missing.append("ROLES_TOKEN (Discord bot token)")
        unconfigured = [p for p in Permission.ALL if not self.permissions.rule_for(p).is_configured()]
        if unconfigured:
            missing.append(f"permission rules: {', '.join(unconfigured)}")
        return missing


# ── Loading ──────────────────────────────────────────────────────────────────

def _as_int(value):
    if value in (None, "", PLACEHOLDER):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_list(raw):
    """[123, null, 456] -> [123, 456]. Placeholders are dropped, not zeroed."""
    return [i for i in (_as_int(x) for x in (raw or [])) if i is not None]


def _int_list_map(raw):
    """{"KEY": [1, 2]} or {"KEY": 1} -> {"KEY": [1, 2]}.

    A single ID is accepted as a one-element list. "_comment" keys and entries
    with no usable ID are dropped.
    """
    result = {}
    for key, value in (raw or {}).items():
        key = str(key).strip().upper()
        if key.startswith("_"):
            continue
        ids = _int_list(value if isinstance(value, (list, tuple)) else [value])
        if ids:
            result[key] = ids
    return result


def _roblox_role_map(raw):
    """Parse roblox_roles_by_branch. Accepts either form per entry:

        "N/A": 108666463                              plain role id
        "ECHELON": {"role_id": 1, "group_id": 555}    explicit group

    null entries are dropped rather than becoming role 0.
    """
    result = {}
    for key, value in (raw or {}).items():
        key = str(key).strip().upper()
        if key.startswith("_") or value is None:
            continue

        if isinstance(value, dict):
            role_id = _as_int(value.get("role_id") or value.get("role"))
            if role_id is None:
                continue
            result[key] = RobloxRoleRef(
                role_id=role_id,
                group_id=_as_int(value.get("group_id") or value.get("group")),
                source=key,
            )
            continue

        role_id = _as_int(value)
        if role_id is not None:
            result[key] = RobloxRoleRef(role_id=role_id, source=key)

    return result


def _build_rank(key, raw):
    key = str(key).strip().upper()
    special = bool(raw.get("special", False))
    return RankDef(
        key=key,
        display=raw.get("display", key),
        category=Category.parse(raw.get("category")),
        order=int(raw.get("order", 0)),
        special=special,
        special_group_order=int(raw.get("special_group_order", 0)),
        main_discord_role_id=_as_int(raw.get("main_discord_role_id")),
        main_aux_discord_role_ids=_int_list(raw.get("main_aux_discord_role_ids")),
        universal=bool(raw.get("universal", False)),
        roblox_roles_by_branch=_roblox_role_map(raw.get("roblox_roles_by_branch")),
        # Special ranks are not necessarily tied to a Roblox group role, so they
        # must opt in explicitly before Roblox is ever touched for them.
        syncs_roblox=bool(raw.get("syncs_roblox", not special)),
        extra=raw.get("extra", {}) or {},
    )


def _build_branch(key, raw):
    key = str(key).strip().upper()
    display = str(raw.get("display", key)).strip()
    if "," in key or "," in display:
        # Column G holds several branches separated by commas, so a comma
        # inside one name would split it in two on the way back in.
        print(f"[Roles] Branch {key!r}: commas are not allowed in a branch key "
              f"or display name; they are removed.")
        key, display = key.replace(",", ""), display.replace(",", "")
    return BranchDef(
        key=key,
        display=display,
        discord_guild_id=_as_int(raw.get("discord_guild_id")),
        discord_roles_by_rank=_int_list_map(raw.get("discord_roles_by_rank")),
        main_aux_discord_role_ids=_int_list(raw.get("main_aux_discord_role_ids")),
        roblox_group_id=_as_int(raw.get("roblox_group_id")),
        is_na=bool(raw.get("is_na", key == NA_BRANCH_KEY)),
        extra=raw.get("extra", {}) or {},
    )


#: Grantable role keys are stored comma-separated in one sheet cell, so a key
#: must not contain a comma, and must start with a letter so the cell can never
#: be read back by Sheets as a number or a formula.
_GRANTABLE_KEY = re.compile(r"^[A-Z][A-Z0-9 _&./\-]*$")


def _build_grantable(key, raw, branches):
    """Build one GrantableRoleDef, or None if the entry is unusable."""
    key = str(key).strip().upper()
    if not _GRANTABLE_KEY.match(key):
        print(f"[Roles] Grantable role {key!r} skipped: a key must start with a "
              f"letter and may only contain letters, digits, spaces and _ & . / -")
        return None

    # "guild" takes one scope or a list of them; "guilds" is accepted as well.
    scopes = raw.get("guild") or raw.get("guilds") or MAIN_GUILD_SCOPE
    if not isinstance(scopes, (list, tuple)):
        scopes = [scopes]
    guilds = []
    for scope in scopes:
        scope = str(scope).strip().upper()
        if scope and scope not in guilds:
            guilds.append(scope)
    guilds = guilds or [MAIN_GUILD_SCOPE]

    for scope in guilds:
        if scope == MAIN_GUILD_SCOPE:
            continue
        branch = branches.get(scope)
        if branch is None or branch.is_na:
            print(f"[Roles] Grantable role {key}: guild {scope!r} is neither "
                  f"{MAIN_GUILD_SCOPE} nor a configured branch. It has no effect "
                  f"there until that is fixed.")

    added = _int_list(raw.get("add_discord_role_ids"))
    removed = _int_list(raw.get("remove_discord_role_ids"))
    overlap = sorted(set(added) & set(removed))
    if overlap:
        print(f"[Roles] Grantable role {key}: {overlap} listed under both add and "
              f"remove. Add wins; the role will be granted.")

    return GrantableRoleDef(
        key=key,
        display=raw.get("display", key),
        description=raw.get("description", ""),
        guilds=guilds,
        add_discord_role_ids=added,
        remove_discord_role_ids=removed,
        extra=raw.get("extra", {}) or {},
    )


def _build_grant(raw):
    relations = [str(r).strip().upper() for r in raw.get("allowed_relations", [])]
    min_rank_order = _as_int(raw.get("min_rank_order"))

    # A floor that was written but cannot be read must not quietly become "no
    # floor" — that widens the grant to every rank. Disable the grant instead.
    if raw.get("min_rank_order") is not None and min_rank_order is None:
        print(f"[Roles] Permission grant ignored: min_rank_order "
              f"{raw.get('min_rank_order')!r} is not a number.")
        relations = []

    return PermissionGrant(
        allowed_relations=relations,
        min_rank_order=min_rank_order,
        allowed_categories=[
            c for c in (Category.parse(x) for x in raw.get("allowed_categories", [])) if c
        ],
        note=raw.get("note", ""),
    )


def _build_rule(raw):
    """Accept either shape.

    Flat (one grant):
        {"allowed_relations": ["SELF"], "allowed_categories": ["HIGH-RANK"]}

    Alternatives (ORed):
        {"grants": [{"allowed_relations": ["SELF"]},
                    {"allowed_relations": ["LOWER"], "allowed_categories": ["HIGH-RANK"]}]}
    """
    note = raw.get("note", "")

    if isinstance(raw.get("grants"), list):
        return PermissionRule(
            grants=[_build_grant(g) for g in raw["grants"] if isinstance(g, dict)],
            note=note,
        )

    return PermissionRule(grants=[_build_grant(raw)], note=note)


def _build_permissions(raw):
    cfg = PermissionConfig()
    if not raw:
        return cfg
    for permission, rule_raw in (raw.get("rules") or {}).items():
        if str(permission).startswith("_") or not isinstance(rule_raw, dict):
            continue
        cfg.rules[str(permission).strip().upper()] = _build_rule(rule_raw)
    cfg.bootstrap_discord_ids = [
        i for i in (_as_int(x) for x in raw.get("bootstrap_discord_ids", [])) if i
    ]
    cfg.default_decision = str(raw.get("default_decision", "DENY")).strip().upper()
    return cfg


def _apply_dataclass(instance, raw):
    """Overlay known keys from a dict onto a dataclass instance, ignoring extras."""
    for key, value in (raw or {}).items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    return instance


def load(path=None):
    """Build a RolesConfig from disk. Missing file -> empty (unconfigured) config."""
    path = path or CONFIG_PATH
    cfg = RolesConfig()

    if not os.path.exists(path):
        print(f"[Roles] No roles_config.json at {path} — running unconfigured.")
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (IOError, json.JSONDecodeError) as exc:
        print(f"[Roles] Failed to read {path}: {exc}. Running unconfigured.")
        return cfg

    cfg.loaded_from = path

    for key, rank_raw in (raw.get("ranks") or {}).items():
        if str(key).startswith("_"):  # allow "_comment" keys in the JSON
            continue
        cfg.ranks[str(key).strip().upper()] = _build_rank(key, rank_raw)

    for key, branch_raw in (raw.get("branches") or {}).items():
        if str(key).startswith("_"):
            continue
        branch = _build_branch(key, branch_raw)
        cfg.branches[branch.key] = branch

    # The N/A branch is required by the data model; synthesize it if the config
    # forgot it, so "no branch" is always a valid, explicit state.
    if cfg.branches and NA_BRANCH_KEY not in cfg.branches:
        cfg.branches[NA_BRANCH_KEY] = BranchDef(key=NA_BRANCH_KEY, display="N/A", is_na=True)

    for key, grantable_raw in (raw.get("grantable_roles") or {}).items():
        if str(key).startswith("_") or not isinstance(grantable_raw, dict):
            continue
        grantable = _build_grantable(key, grantable_raw, cfg.branches)
        if grantable is not None:
            cfg.grantable_roles[grantable.key] = grantable

    cfg.general_roles = _int_list_map(raw.get("general_roles"))
    for scope in list(cfg.general_roles):
        if scope == MAIN_GUILD_SCOPE:
            continue
        branch = cfg.branches.get(scope)
        if branch is None or branch.is_na:
            print(f"[Roles] General roles: scope {scope!r} is neither "
                  f"{MAIN_GUILD_SCOPE} nor a configured branch; those roles are ignored.")
            del cfg.general_roles[scope]

    default_rank = raw.get("default_registration_rank")
    if default_rank and not str(default_rank).startswith("_"):
        cfg.default_registration_rank = str(default_rank).strip().upper()

    default_branch = raw.get("default_registration_branch")
    if default_branch and not str(default_branch).startswith("_"):
        cfg.default_registration_branch = str(default_branch).strip().upper()
        if cfg.default_registration_branch not in cfg.branches:
            print(f"[Roles] default_registration_branch "
                  f"'{cfg.default_registration_branch}' is not a configured branch; "
                  f"new records will start in {NA_BRANCH_KEY}.")

    order = [Category.parse(c) for c in (raw.get("category_order") or [])]
    order = [c for c in order if c]
    if order:
        cfg.category_order = order

    _apply_dataclass(cfg.discord, raw.get("discord"))
    cfg.discord.main_guild_id = _as_int(cfg.discord.main_guild_id)
    cfg.discord.log_channel_id = _as_int(cfg.discord.log_channel_id)
    cfg.discord.extra_managed_role_ids = [
        i for i in (_as_int(x) for x in cfg.discord.extra_managed_role_ids) if i
    ]

    _apply_dataclass(cfg.roblox, raw.get("roblox"))
    cfg.roblox.group_id = _as_int(cfg.roblox.group_id)

    _apply_dataclass(cfg.roblox_oauth, raw.get("roblox_oauth"))
    _apply_dataclass(cfg.rover, raw.get("rover"))
    _apply_dataclass(cfg.registration, raw.get("registration"))
    cfg.registration.verification_method = str(
        cfg.registration.verification_method
    ).strip().upper()
    if cfg.registration.in_group_rank:
        cfg.registration.in_group_rank = str(
            cfg.registration.in_group_rank
        ).strip().upper()
    _apply_dataclass(cfg.sync, raw.get("sync"))
    cfg.sync.on_removal_roblox = str(cfg.sync.on_removal_roblox).strip().upper()
    cfg.sync.removal_roblox_role_id = _as_int(cfg.sync.removal_roblox_role_id)
    cfg.permissions = _build_permissions(raw.get("permissions"))

    cfg.roblox.branch_roles_enabled = bool(cfg.roblox.branch_roles_enabled)

    print(
        f"[Roles] Config loaded from {path}: "
        f"{len(cfg.ranks)} rank(s), {len(cfg.branches)} branch(es), "
        f"{len(cfg.grantable_roles)} grantable role(s), "
        f"{sum(len(ids) for ids in cfg.general_roles.values())} general role(s)."
    )
    return cfg


#: The live configuration. Import the module and use `roles_config.current()`
#: rather than binding this at import time, so reload() is visible everywhere.
_current = load()


def current():
    return _current


def reload(path=None):
    """Re-read the config file. Returns the new RolesConfig."""
    global _current
    _current = load(path)
    return _current


# ── Secrets, via the project's existing config mechanism ────────────────────

def secret(name, default=""):
    """Read a secret from env or config.json. Never store these in roles_config.json."""
    return os.environ.get(name) or app_config.get(name, default) or default
