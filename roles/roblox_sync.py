"""Roblox group role synchronization via Open Cloud v2 (multi-role model).

Mapping is `rank + branch -> Roblox group roles`, additively: the rank's "N/A"
entry is the baseline and is always applied, and the branch entry is applied on
top of it. A member can therefore hold several group roles at once.

API model
---------
This uses the current multi-role endpoints, NOT the deprecated
``PATCH /memberships/{id}`` (UpdateGroupMembership), which modelled membership as
exactly one role:

    POST {base}/groups/{gid}/memberships/{mid}:assignRole     body {"role": ...}
    POST {base}/groups/{gid}/memberships/{mid}:unassignRole    body {"role": ...}

Both are idempotent no-ops when the member already does / does not hold the role,
and neither disturbs the member's other roles. There is no batch variant, so N
roles means N sequential calls. The GroupMembership resource carries a ``roles``
array (plus a legacy singular ``role``); both are read here.

Guarantees
----------
  * A rank with no Roblox mapping, or with ``syncs_roblox: false`` (the default
    for special ranks), results in NO Roblox API call at all.

  * Only roles this system manages are ever unassigned. A role granted for some
    unrelated reason survives synchronization, exactly like an unmanaged Discord
    role does.

  * Nothing read from Roblox is written back to the sheet. A failure here is
    reported and retried; the authoritative record is untouched.

  * Every mutation is verified by re-reading the membership. A key missing the
    ``group:write`` scope can return 200 without applying the change, so a
    success response is not taken as proof on its own.

Uses the stored Roblox ID from the technical columns — Rover is a registration-
time dependency only.
"""

import asyncio

import aiohttp

from roles import config as roles_config
from roles.models import SyncTarget

from roles.discord_sync import SyncOutcome

#: Built-in roles Open Cloud refuses to assign or unassign. Never sent, even if
#: someone puts one in the configuration by mistake.
PROTECTED_ROLE_NAMES = ("Owner", "Member", "Guest")


class RobloxSynchronizer:
    """Applies a record's rank+branch mapping to Roblox group roles."""

    # ── Desired state ──

    def desired_assignments(self, record, cfg=None):
        """Every (group, role) this record should hold.

        Additive: the rank's N/A role is always included, plus its branch role
        when the member belongs to a branch. Empty means "do not touch Roblox",
        never "remove them from the group".
        """
        cfg = cfg or roles_config.current()
        rank = cfg.rank(record.rank_key)
        return cfg.roblox_assignments(rank, record.branch_key)

    def desired_by_group(self, record, cfg=None):
        """{group_id: {role_id, ...}} the record should hold."""
        grouped = {}
        for assignment in self.desired_assignments(record, cfg):
            grouped.setdefault(assignment.group_id, set()).add(assignment.role_id)
        return grouped

    def should_sync(self, record, cfg=None):
        cfg = cfg or roles_config.current()
        if not record.roblox_id:
            return False
        return bool(self.desired_assignments(record, cfg))

    # ── Application ──

    async def sync_record(self, record, cfg=None, dry_run=False, strip=False):
        """Reconcile the member's roles in every group they have mappings for.

        `strip=True` revokes instead: every managed role is unassigned, in each
        group the record has mappings in. Now that unassignRole exists this is a
        real revocation rather than a demotion.
        """
        cfg = cfg or roles_config.current()
        target = SyncTarget.ROBLOX

        api_key = roles_config.secret("ROBLOX_API_KEY")
        if not api_key:
            return [SyncOutcome(target, True, "ROBLOX_API_KEY not configured; skipped.")]

        if not record.roblox_id:
            return [SyncOutcome(target, True, "Record has no Roblox ID; skipped.")]

        rank = cfg.rank(record.rank_key)
        if rank is None:
            return [SyncOutcome(
                target, True,
                f"Rank '{record.rank_key}' is not in the configuration; Roblox untouched.",
            )]

        if not rank.syncs_roblox:
            return [SyncOutcome(
                target, True,
                f"Rank {rank.key} has no Roblox equivalent; Roblox untouched.",
            )]

        if strip:
            policy = (cfg.sync.on_removal_roblox or "UNASSIGN").upper()

            if policy == "IGNORE":
                return [SyncOutcome(
                    target, True,
                    "Removal policy is IGNORE; Roblox roles left unchanged.",
                )]

            # Visit every group the configuration touches, so a role left over
            # from a previous rank or branch is not missed.
            groups = cfg.managed_roblox_groups()
            desired_by_group = {}

            if policy == "SET_ROLE":
                # Strip the managed roles, then leave one role behind in the
                # main group as the "removed" marker.
                role_id = cfg.sync.removal_roblox_role_id
                if not role_id:
                    return [SyncOutcome(
                        target, False,
                        "Removal policy is SET_ROLE but removal_roblox_role_id is "
                        "not configured; Roblox roles left unchanged.",
                    )]
                if cfg.roblox.group_id:
                    desired_by_group = {cfg.roblox.group_id: {role_id}}
                    groups.add(cfg.roblox.group_id)
            elif policy != "UNASSIGN":
                return [SyncOutcome(
                    target, False,
                    f"Removal policy '{policy}' is not recognized. "
                    f"Use UNASSIGN, SET_ROLE or IGNORE.",
                )]
        else:
            desired_by_group = self.desired_by_group(record, cfg)
            groups = set(desired_by_group)

        if not groups:
            return [SyncOutcome(
                target, True,
                f"No Roblox roles mapped for {rank.key} + branch "
                f"{record.branch_key or 'N/A'}; Roblox untouched.",
            )]

        outcomes = []
        for group_id in sorted(groups):
            outcomes.append(await self._sync_group(
                cfg, api_key, record, group_id,
                desired_by_group.get(group_id, set()), target, dry_run,
            ))

        return outcomes

    async def _sync_group(self, cfg, api_key, record, group_id, desired, target, dry_run):
        """Reconcile one group: assign what is missing, unassign what is stale."""
        managed = cfg.managed_roblox_role_ids(group_id)

        if not managed and not desired:
            return SyncOutcome(
                target, True, f"Group {group_id}: nothing managed; skipped.",
            )

        membership = await self._find_membership(cfg, api_key, group_id, record.roblox_id)
        if membership.error:
            return SyncOutcome(
                target, False, f"Group {group_id}: {membership.error}",
                retryable=membership.retryable,
            )

        if membership.path is None:
            # Not in that group. Not a failure — nothing to synchronize.
            return SyncOutcome(
                target, True,
                f"Roblox user {record.roblox_id} is not in group {group_id}; skipped.",
            )

        current = set(membership.role_ids)

        to_assign = sorted(desired - current)
        # Only ever remove roles this system owns. Anything granted for another
        # reason stays, the same way an unmanaged Discord role does.
        to_unassign = sorted((managed & current) - desired)

        if not to_assign and not to_unassign:
            return SyncOutcome(target, True, f"Group {group_id}: already correct.")

        if dry_run:
            return SyncOutcome(
                target, True,
                f"Dry run — group {group_id}: would assign {to_assign}, "
                f"unassign {to_unassign}.",
                added=to_assign, removed=to_unassign,
            )

        assigned, unassigned, errors, retryable = [], [], [], False

        for role_id in to_assign:
            ok, error, can_retry = await self._mutate_role(
                cfg, api_key, membership.path, group_id, role_id, "assignRole"
            )
            if ok:
                assigned.append(role_id)
            else:
                errors.append(f"assign {role_id}: {error}")
                retryable = retryable or can_retry

        for role_id in to_unassign:
            ok, error, can_retry = await self._mutate_role(
                cfg, api_key, membership.path, group_id, role_id, "unassignRole"
            )
            if ok:
                unassigned.append(role_id)
            else:
                errors.append(f"unassign {role_id}: {error}")
                retryable = retryable or can_retry

        # A key without group:write can return 200 without applying anything, so
        # confirm against the membership rather than trusting the response.
        verify = await self._find_membership(cfg, api_key, group_id, record.roblox_id)
        if not verify.error and verify.path is not None:
            confirmed = set(verify.role_ids)
            missing = sorted(set(assigned) - confirmed)
            lingering = sorted(set(unassigned) & confirmed)
            if missing or lingering:
                detail = []
                if missing:
                    detail.append(f"still missing {missing}")
                if lingering:
                    detail.append(f"still present {lingering}")
                return SyncOutcome(
                    target, False,
                    f"Group {group_id}: the API accepted the changes but the "
                    f"membership does not reflect them ({'; '.join(detail)}). "
                    f"The API key most likely lacks the group:write scope for this "
                    f"group, or the bot outranks nothing there.",
                    added=assigned, removed=unassigned,
                )

        if errors:
            return SyncOutcome(
                target, False,
                f"Group {group_id}: {'; '.join(errors)}",
                added=assigned, removed=unassigned, retryable=retryable,
            )

        return SyncOutcome(
            target, True,
            f"Group {group_id}: assigned {assigned}, unassigned {unassigned}.",
            added=assigned, removed=unassigned,
        )

    # ── Open Cloud calls ──

    class _Membership:
        def __init__(self, path=None, role_ids=None, error="", retryable=False):
            self.path = path
            #: Every role the member currently holds in the group.
            self.role_ids = role_ids or []
            self.error = error
            self.retryable = retryable

    @staticmethod
    def _role_id_from_path(path):
        """'groups/1/roles/2' -> 2."""
        if not path:
            return None
        try:
            return int(str(path).rsplit("/", 1)[-1])
        except (ValueError, TypeError):
            return None

    async def _find_membership(self, cfg, api_key, group_id, roblox_user_id):
        """Locate the user's membership and read every role they hold."""
        url = f"{cfg.roblox.api_base}/groups/{group_id}/memberships"
        params = {
            "maxPageSize": "1",
            "filter": f"user == 'users/{roblox_user_id}'",
        }

        ok, data, error, retryable = await self._request(
            "GET", url, api_key, cfg, params=params
        )
        if not ok:
            return self._Membership(error=error, retryable=retryable)

        memberships = (data or {}).get("groupMemberships") or []
        if not memberships:
            return self._Membership()

        entry = memberships[0]

        # Prefer the multi-role array; fall back to the legacy singular field.
        role_ids = [
            rid for rid in (self._role_id_from_path(p) for p in (entry.get("roles") or []))
            if rid is not None
        ]
        if not role_ids:
            legacy = self._role_id_from_path(entry.get("role"))
            if legacy is not None:
                role_ids = [legacy]

        return self._Membership(path=entry.get("path"), role_ids=role_ids)

    async def _mutate_role(self, cfg, api_key, membership_path, group_id, role_id, action):
        """assignRole / unassignRole for one role. Returns (ok, error, retryable)."""
        url = f"{cfg.roblox.api_base}/{membership_path}:{action}"
        payload = {"role": f"groups/{group_id}/roles/{role_id}"}

        ok, _data, error, retryable = await self._request(
            "POST", url, api_key, cfg, json_body=payload
        )

        # Stay clear of the 300/min per-key limit when reconciling several roles.
        if cfg.roblox.request_delay:
            await asyncio.sleep(cfg.roblox.request_delay)

        return ok, error, retryable

    async def _request(self, method, url, api_key, cfg, params=None, json_body=None):
        """One Open Cloud call with retry on rate limits and 5xx.

        Returns (ok, data, error_message, retryable).
        """
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=cfg.roblox.request_timeout)

        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, url, headers=headers, params=params,
                        json=json_body, timeout=timeout,
                    ) as resp:
                        if resp.status in (200, 201):
                            try:
                                return True, await resp.json(), "", False
                            except (aiohttp.ContentTypeError, ValueError):
                                return True, {}, "", False

                        body = await resp.text()

                        if resp.status == 429:
                            wait = int(resp.headers.get("Retry-After", 5 + attempt * 5))
                            print(f"[Roles:Roblox] Rate limited, waiting {wait}s "
                                  f"(attempt {attempt + 1}/3)")
                            if attempt < 2:
                                await asyncio.sleep(wait)
                                continue
                            return False, None, "Roblox is rate limiting requests.", True

                        if resp.status in (401, 403):
                            print(f"[Roles:Roblox] Auth failure {resp.status}: {body[:200]}")
                            return False, None, (
                                "Roblox rejected the Open Cloud key, or it lacks the "
                                "group:write scope for this group."
                            ), False

                        if resp.status == 404:
                            return False, None, (
                                "Roblox resource not found — check the group ID and the "
                                "configured role IDs."
                            ), False

                        if resp.status == 400:
                            print(f"[Roles:Roblox] Bad request: {body[:300]}")
                            return False, None, (
                                f"Roblox rejected the request as invalid: {body[:150]}"
                            ), False

                        if resp.status >= 500:
                            print(f"[Roles:Roblox] Server error {resp.status}: {body[:200]}")
                            if attempt < 2:
                                await asyncio.sleep(3 + attempt * 3)
                                continue
                            return False, None, f"Roblox server error ({resp.status}).", True

                        print(f"[Roles:Roblox] Unexpected {resp.status}: {body[:200]}")
                        return False, None, f"Unexpected Roblox response ({resp.status}).", False

            except asyncio.TimeoutError:
                print(f"[Roles:Roblox] Timeout, attempt {attempt + 1}/3")
                if attempt < 2:
                    continue
                return False, None, "Roblox did not respond in time.", True

            except aiohttp.ClientError as exc:
                print(f"[Roles:Roblox] Connection error: {type(exc).__name__}: {exc}")
                return False, None, "Could not reach Roblox Open Cloud.", True

        return False, None, "Roblox request failed after 3 attempts.", True

    # ── Diagnostics ──

    async def list_group_roles(self, group_id, cfg=None):
        """GET the group's roles, for mapping display names to IDs.

        Handy when filling in roblox_roles_by_branch: returns
        [{"id": ..., "displayName": ..., "rank": ...}] or an error string.
        """
        cfg = cfg or roles_config.current()
        api_key = roles_config.secret("ROBLOX_API_KEY")
        if not api_key:
            return [], "ROBLOX_API_KEY is not configured."

        url = f"{cfg.roblox.api_base}/groups/{group_id}/roles"
        ok, data, error, _retryable = await self._request(
            "GET", url, api_key, cfg, params={"maxPageSize": "100"}
        )
        if not ok:
            return [], error

        roles = []
        for entry in (data or {}).get("groupRoles") or []:
            roles.append({
                "id": self._role_id_from_path(entry.get("path")),
                "displayName": entry.get("displayName", ""),
                "rank": entry.get("rank"),
            })
        return roles, ""

    def validate_configuration(self, cfg=None):
        """Report Roblox mapping gaps.

        Expected to list a lot until the branch mappings are supplied.
        """
        cfg = cfg or roles_config.current()
        problems = []

        if not cfg.roblox.group_id:
            problems.append("Roblox group ID is not configured.")
        if not roles_config.secret("ROBLOX_API_KEY"):
            problems.append(
                "ROBLOX_API_KEY is not configured (needs group:read + group:write)."
            )

        for rank in cfg.ranks.values():
            if not rank.syncs_roblox:
                continue

            if not rank.roblox_roles_by_branch:
                problems.append(f"Rank {rank.key}: no Roblox role mappings at all.")
                continue

            if rank.roblox_role_ref(roles_config.NA_BRANCH_KEY) is None:
                problems.append(
                    f"Rank {rank.key}: no \"N/A\" baseline role. The N/A entry is "
                    f"always applied, so without it branch members receive only "
                    f"their branch role."
                )

            for branch in cfg.branches.values():
                if branch.is_na:
                    continue
                if rank.roblox_role_ref(branch.key) is None:
                    problems.append(
                        f"Rank {rank.key} + branch {branch.key}: no branch Roblox "
                        f"role mapped (the N/A baseline still applies)."
                    )

        return problems


#: Shared instance.
synchronizer = RobloxSynchronizer()
