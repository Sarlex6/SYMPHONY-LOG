"""Centralized authorization.

The ONLY place in the system that decides whether an action is allowed. Commands
and Angela both route through `check()`; neither implements a rank check of its
own.

Two rules make this trustworthy:

  1. Authority comes from the authoritative Google Sheet record for the acting
     Discord ID — never from a Discord role. Discord roles are a synchronized
     *output* of the sheet, so treating one as proof of rank would let a stale or
     manually granted role become a privilege escalation.

  2. It fails closed. A permission with no configured rule is denied, and the
     denial says which rule is missing. The real hierarchy has not been specified
     yet; guessing at it would silently hand out authority.
"""

from roles import config as roles_config
from roles.config import Permission, Relation
from roles.models import ActionOrigin


class PermissionDecision:
    """Why an action was allowed or refused."""

    def __init__(self, allowed, reason="", permission="", relation="", unconfigured=False):
        self.allowed = allowed
        self.reason = reason
        self.permission = permission
        self.relation = relation
        #: True when the refusal is "nobody has defined this yet" rather than
        #: "you specifically may not". Worth phrasing differently to the user.
        self.unconfigured = unconfigured

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        verdict = "ALLOW" if self.allowed else "DENY"
        return f"PermissionDecision({verdict}, {self.permission}, {self.reason!r})"

    @classmethod
    def allow(cls, permission, relation="", reason=""):
        return cls(True, reason or "Authorized.", permission, relation)

    @classmethod
    def deny(cls, permission, reason, relation="", unconfigured=False):
        return cls(False, reason, permission, relation, unconfigured)


# ── Rank comparison ──────────────────────────────────────────────────────────

def rank_order(record, cfg):
    """Numeric seniority of a record's rank, or None when unresolvable."""
    if record is None:
        return None
    rank = cfg.rank(record.rank_key)
    return rank.order if rank else None


def relation_of(actor, target, cfg):
    """How `target` relates to `actor`, by configured rank order."""
    if actor is None:
        return Relation.UNRANKED
    if target is None:
        return Relation.UNRANKED
    if actor.record_uid and actor.record_uid == target.record_uid:
        return Relation.SELF
    if actor.discord_id and actor.discord_id == target.discord_id:
        return Relation.SELF

    actor_order = rank_order(actor, cfg)
    target_order = rank_order(target, cfg)

    if actor_order is None or target_order is None:
        return Relation.UNRANKED
    if target_order < actor_order:
        return Relation.LOWER
    if target_order > actor_order:
        return Relation.HIGHER
    return Relation.EQUAL


# ── The check ────────────────────────────────────────────────────────────────

def check(permission, actor_record, target_record=None, cfg=None, actor_discord_id=None,
          origin=ActionOrigin.DISCORD_COMMAND):
    """Decide whether `actor_record` may perform `permission` on `target_record`.

    `origin` is accepted for logging only. A request arriving through Angela is
    evaluated identically to one typed as a slash command — same actor record,
    same rules, same outcome.
    """
    cfg = cfg or roles_config.current()
    permissions = cfg.permissions

    # Bootstrap allowlist. Deliberately empty by default; it exists because
    # nobody has a rank before the sheet is configured.
    effective_id = actor_discord_id or (actor_record.discord_id if actor_record else None)
    if effective_id and effective_id in permissions.bootstrap_discord_ids:
        return PermissionDecision.allow(
            permission, reason="Bootstrap administrator."
        )

    if actor_record is None:
        # Registering yourself is the one thing an unregistered user can do.
        if permission == Permission.REGISTER_SELF:
            rule = permissions.rule_for(permission)
            for grant in rule.grants:
                # An unregistered actor has no rank, so only an unconditional
                # SELF grant can apply here.
                if (Relation.SELF in grant.allowed_relations
                        and grant.min_rank_order is None
                        and not grant.allowed_categories):
                    return PermissionDecision.allow(permission, Relation.SELF)
        return PermissionDecision.deny(
            permission,
            "You have no PERSONNEL record. Use /register in the main server first.",
            Relation.UNRANKED,
        )

    rule = permissions.rule_for(permission)

    if not rule.is_configured():
        if permissions.default_decision == "ALLOW":
            return PermissionDecision.allow(
                permission, reason="No rule configured; default_decision is ALLOW."
            )
        note = f" ({rule.note})" if rule.note else ""
        return PermissionDecision.deny(
            permission,
            f"`{permission}` has no authorization rule configured yet{note}. "
            f"Denied until the rank hierarchy for it is defined.",
            unconfigured=True,
        )

    relation = relation_of(actor_record, target_record, cfg)

    # Grants are alternatives: the first one that matches authorizes the action.
    # This is what lets one permission mean "anyone, on themselves — OR a high
    # rank, on someone below them".
    for grant in rule.grants:
        if not grant.is_configured():
            continue
        if _grant_matches(grant, actor_record, relation, cfg):
            return PermissionDecision.allow(permission, relation)

    return PermissionDecision.deny(
        permission, _explain_denial(permission, relation, rule), relation,
    )


def _grant_matches(grant, actor_record, relation, cfg):
    """Whether one grant's conditions all hold. Conditions within a grant are ANDed."""
    if relation not in grant.allowed_relations:
        return False

    actor_order = rank_order(actor_record, cfg)

    if grant.min_rank_order is not None:
        if actor_order is None or actor_order < grant.min_rank_order:
            return False

    if grant.allowed_categories:
        if _category_of(actor_record, cfg) not in grant.allowed_categories:
            return False

    return True


def _category_of(record, cfg):
    rank = cfg.rank(record.rank_key)
    if rank and rank.category:
        return rank.category
    return record.category


def _explain_denial(permission, relation, rule):
    """Say what was attempted and what the permission actually allows."""
    attempted = {
        Relation.SELF: "yourself",
        Relation.LOWER: "a lower-ranked user",
        Relation.EQUAL: "a user of equal rank",
        Relation.HIGHER: "a higher-ranked user",
        Relation.UNRANKED: "a user whose rank cannot be resolved",
    }.get(relation, "that user")

    return (
        f"You are not authorized to use `{permission}` on {attempted}. "
        f"This action allows: {rule.describe()}."
    )


# ── Introspection ────────────────────────────────────────────────────────────

def describe_permissions(actor_record, cfg=None):
    """What the acting user may currently do, for a /permissions style command."""
    cfg = cfg or roles_config.current()
    summary = {}
    for permission in Permission.ALL:
        decision = check(permission, actor_record, actor_record, cfg)
        summary[permission] = {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "unconfigured": decision.unconfigured,
        }
    return summary


def unconfigured_permissions(cfg=None):
    """Permissions still awaiting a rule. Surfaced by /roles config status."""
    cfg = cfg or roles_config.current()
    return [p for p in Permission.ALL if not cfg.permissions.rule_for(p).is_configured()]


def audit_line(context, permission, decision, target_record=None):
    """Uniform authorization log line. Every decision is logged, allow or deny."""
    target = target_record.label() if target_record else "-"
    verdict = "ALLOW" if decision.allowed else "DENY"
    return (
        f"{context.log_prefix()} AUTHZ {verdict} {permission} "
        f"actor={context.actor_discord_id} origin={context.origin.value} "
        f"target={target} relation={decision.relation or '-'} :: {decision.reason}"
    )
