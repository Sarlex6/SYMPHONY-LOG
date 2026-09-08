import json
import os

# Secrets that must be present for the process to start at all.
REQUIRED = ("DISCORD_TOKEN", "SPREADSHEET_ID", "LOG_CHANNEL_ID", "APPROVAL_CHANNEL_ID")

# Everything else is optional — the dependent feature stays dormant without it.
OPTIONAL = (
    "ASSISTANT_TOKEN",            # Angela assistant bot
    "GEMINI_API_KEY",             # Angela's model access
    "ROLES_TOKEN",                # role management bot
    "ROBLOX_API_KEY",             # Roblox Open Cloud (group:read + group:write)
    "ROBLOX_OAUTH_CLIENT_ID",     # Roblox OAuth — default /register verification
    "ROBLOX_OAUTH_CLIENT_SECRET",
    "ROVER_API_KEY",              # only if registration.verification_method is ROVER
    "ROLES_CONFIG_PATH",          # optional override for roles_config.json location
)

INT_KEYS = ("LOG_CHANNEL_ID", "APPROVAL_CHANNEL_ID")


def load_config():
    """Load from environment variables (Koyeb/Railway) or fall back to config.json (local).

    The environment is used whenever DISCORD_TOKEN is set; config.json is the
    local-development fallback and is deliberately not committed, since it holds
    secrets. A deployment therefore needs the environment variables set — there
    is no config.json on the server to fall back to.
    """
    if os.environ.get("DISCORD_TOKEN"):
        missing = [k for k in REQUIRED if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): " + ", ".join(missing)
                + ".\nDISCORD_TOKEN is set, so this process is reading its configuration "
                "from the environment. Set the missing variable(s) on the service and redeploy."
            )

        data = {k: os.environ[k] for k in REQUIRED}
        data.update({k: os.environ.get(k, "") for k in OPTIONAL})

        for key in INT_KEYS:
            try:
                data[key] = int(data[key])
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"Environment variable {key} must be a number, got {data[key]!r}."
                )
        return data

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        raise RuntimeError(
            "No configuration found.\n"
            "  - No DISCORD_TOKEN in the environment, so the environment path was skipped.\n"
            f"  - No config.json at {config_path} to fall back to.\n"
            "\n"
            "On a deployed host this means the service's environment variables are not set. "
            "Set at least: " + ", ".join(REQUIRED) + ".\n"
            "Optional: " + ", ".join(OPTIONAL) + ".\n"
            "\n"
            "Locally, create config.json with those keys instead."
        )

    with open(config_path, "r") as f:
        data = json.load(f)

    # Ensure optional keys exist even if not in config.json yet
    for key in OPTIONAL:
        data.setdefault(key, "")

    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise RuntimeError(
            f"config.json is missing required key(s): {', '.join(missing)}."
        )
    return data


config = load_config()
