"""Runtime switches that outlive a restart but are not configuration.

Currently one: whether branch-line Roblox roles are enabled. Executives flip it
with /aria branch-roles, so it cannot live in roles_config.json, which is
deployed with the code. It is kept in data/roles_state.json, the same JSON-in-
data/ convention the sync queue uses.

`roblox.branch_roles_enabled` in roles_config.json is the starting value while
no state file exists. Once a switch has been made, the file wins. If the host
wipes data/ on redeploy, the configured value applies again — set it there as
well to make a decision permanent.
"""

import json
import os
from datetime import datetime

from roles import config as roles_config

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE_FILE = os.path.join(DATA_DIR, "roles_state.json")

_state = None


def _load():
    global _state
    if _state is not None:
        return _state

    _state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                _state = loaded
        except (IOError, json.JSONDecodeError) as exc:
            print(f"[Roles:State] Could not read {STATE_FILE}: {exc}. Using configured defaults.")
    return _state


def _save():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump(_state, handle, indent=2, default=str)
    except IOError as exc:
        # The switch still holds in memory for this process; it is only the
        # restart that would lose it.
        print(f"[Roles:State] Could not persist {STATE_FILE}: {exc}")


def branch_roles_enabled(cfg=None):
    """Whether members may take their Roblox role from a branch line."""
    state = _load()
    if "branch_roles_enabled" in state:
        return bool(state["branch_roles_enabled"])
    cfg = cfg or roles_config.current()
    return cfg.roblox.branch_roles_enabled


def set_branch_roles_enabled(enabled, actor_discord_id=None):
    state = _load()
    state["branch_roles_enabled"] = bool(enabled)
    state["branch_roles_changed_by"] = actor_discord_id
    state["branch_roles_changed_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _save()


def describe():
    """One line for the status report."""
    state = _load()
    enabled = branch_roles_enabled()
    where = "switched" if "branch_roles_enabled" in state else "configured default"
    who = state.get("branch_roles_changed_by")
    when = state.get("branch_roles_changed_at")
    detail = f" by <@{who}> at {when} UTC" if who and when else ""
    return f"{'enabled' if enabled else 'DISABLED'} ({where}{detail})"
