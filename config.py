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
        }
    else:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            data = json.load(f)
        # Ensure assistant keys exist even if not in config.json yet
        data.setdefault("ASSISTANT_TOKEN", "")
        data.setdefault("GEMINI_API_KEY", "")
        return data

config = load_config()