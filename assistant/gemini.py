import aiohttp
import json
from config import config
from assistant.persona import SYSTEM_PROMPT, STATIC_KNOWLEDGE

GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash-lite"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


async def generate_response(user_name, user_message, conversation_history=None, memory_summary=None, replied_message=None, channel_context=None):
    """
    Call Gemini Flash-Lite with the full context:
    - System prompt (persona + static knowledge)
    - Memory summary of past conversations with this user
    - Recent channel activity (last 25 messages from the channel)
    - Recent conversation history (per-user memory)
    - The message being replied to (if any)
    - The user's current message
    """
    if not GEMINI_API_KEY:
        return "⚠️ Gemini API key not configured."

    # Build system instruction
    system_parts = [SYSTEM_PROMPT, "\nREFERENCE KNOWLEDGE:\n" + STATIC_KNOWLEDGE]

    if memory_summary:
        system_parts.append(f"\nMEMORY OF PAST INTERACTIONS WITH {user_name.upper()}:\n{memory_summary}")

    system_instruction = "\n".join(system_parts)

    # Build conversation contents
    contents = []

    # Add channel context as the first user message so the AI knows what's been discussed
    if channel_context:
        context_lines = ["[RECENT CHANNEL ACTIVITY — use this to understand the ongoing conversation]"]
        for msg in channel_context:
            prefix = "Angela" if msg["is_angela"] else msg["author"]
            line = f"{prefix}: {msg['content']}"
            if msg.get("replying_to"):
                ref = msg["replying_to"]
                line = f"{prefix} (replying to {ref['author']}: \"{ref['content'][:80]}\"): {msg['content']}"
            context_lines.append(line)
        context_lines.append("[END OF CHANNEL ACTIVITY]")

        contents.append({
            "role": "user",
            "parts": [{"text": "\n".join(context_lines)}]
        })
        contents.append({
            "role": "model",
            "parts": [{"text": "Understood. I've reviewed the recent channel activity."}]
        })

    # Add recent per-user conversation history
    if conversation_history:
        for msg in conversation_history:
            contents.append({
                "role": msg["role"],
                "parts": [{"text": msg["text"]}]
            })

    # Build the current user message with reply context
    current_message_parts = []

    if replied_message:
        current_message_parts.append(
            f"[{user_name} is replying to a message from {replied_message['author']}: \"{replied_message['content']}\"]\n\n"
        )

    current_message_parts.append(f"{user_name}: {user_message}")

    contents.append({
        "role": "user",
        "parts": [{"text": "".join(current_message_parts)}]
    })

    # Build request payload
    payload = {
        "system_instruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": 1024,
            "topP": 0.95,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_URL}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"[Gemini] API error {resp.status}: {error_text[:200]}")
                    return "Processing error. My systems are momentarily... distracted. Try again."

                data = await resp.json()

                # Extract response text
                candidates = data.get("candidates", [])
                if not candidates:
                    return "No response generated. Even I need a moment to think sometimes."

                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    return "Empty response. Unusual. I'll recalibrate."

                return parts[0].get("text", "...").strip()

    except aiohttp.ClientError as e:
        print(f"[Gemini] Connection error: {e}")
        return "Network disruption detected. My connection to external systems is temporarily impaired."
    except Exception as e:
        print(f"[Gemini] Unexpected error: {e}")
        return "An unexpected error occurred. I'm logging this for analysis."