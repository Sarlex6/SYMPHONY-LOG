import json
import os

def load_config():
    """Load from environment variables (Koyeb/Railway) or fall back to config.json (local)."""
    if os.environ.get("DISCORD_TOKEN"):
        return {
            "DISCORD_TOKEN": os.environ["DISCORD_TOKEN"],
            "SPREADSHEET_ID": os.environ["SPREADSHEET_ID"],
            "LOG_CHANNEL_ID": int(os.environ["LOG_CHANNEL_ID"]),
            "APPROVAL_CHANNEL_ID": int(os.environ["APPROVAL_CHANNEL_ID"]),
            "ASSISTANT_TOKEN": os.environ.get("ASSISTANT_TOKEN", ""),
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
            # ── Role management (all optional; the system stays dormant without them) ──
            "ROLES_TOKEN": os.environ.get("ROLES_TOKEN", ""),
            "ROBLOX_API_KEY": os.environ.get("ROBLOX_API_KEY", ""),
            # Roblox OAuth — the default /register verification method
            "ROBLOX_OAUTH_CLIENT_ID": os.environ.get("ROBLOX_OAUTH_CLIENT_ID", ""),
            "ROBLOX_OAUTH_CLIENT_SECRET": os.environ.get("ROBLOX_OAUTH_CLIENT_SECRET", ""),
            # Only needed if registration.verification_method is switched to ROVER
            "ROVER_API_KEY": os.environ.get("ROVER_API_KEY", ""),
            "ROLES_CONFIG_PATH": os.environ.get("ROLES_CONFIG_PATH", ""),
        }
    else:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            data = json.load(f)
        # Ensure optional keys exist even if not in config.json yet
        for key in ("ASSISTANT_TOKEN", "GEMINI_API_KEY", "ROLES_TOKEN",
                    "ROBLOX_API_KEY", "ROBLOX_OAUTH_CLIENT_ID",
                    "ROBLOX_OAUTH_CLIENT_SECRET", "ROVER_API_KEY",
                    "ROLES_CONFIG_PATH"):
            data.setdefault(key, "")
        return data

config = load_config()