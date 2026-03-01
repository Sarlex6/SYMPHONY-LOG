import aiohttp
import asyncio
import json
from config import config
from assistant.persona import SYSTEM_PROMPT, STATIC_KNOWLEDGE
from assistant.knowledge import get_relevant_knowledge

GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
PRIMARY_MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-2.5-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


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

    # Look up relevant knowledge based on the user's message + channel context
    search_text = user_message
    if channel_context:
        # Include recent channel messages in the search to catch topic references
        recent_channel_text = " ".join(msg["content"] for msg in channel_context[-5:])
        search_text = recent_channel_text + " " + user_message

    relevant_knowledge = get_relevant_knowledge(search_text)
    if relevant_knowledge:
        system_parts.append(f"\nRELEVANT INTEL (use this information to inform your response):\n{relevant_knowledge}")

    if memory_summary:
        system_parts.append(f"\nMEMORY OF PAST INTERACTIONS WITH {user_name.upper()} (background context only — prioritize the current channel conversation over this):\n{memory_summary}")

    # Add per-user conversation history as background memory in system prompt
    if conversation_history:
        history_lines = [f"\nYOUR RECENT DIRECT EXCHANGES WITH {user_name.upper()} (for reference only — the channel messages below are more current):"]
        for msg in conversation_history[-10:]:  # Only last 10 to keep system prompt reasonable
            speaker = user_name if msg["role"] == "user" else "You (Angela)"
            text = msg["text"][:200]
            history_lines.append(f"  {speaker}: {text}")
        system_parts.append("\n".join(history_lines))

    system_instruction = "\n".join(system_parts)

    # Build conversation contents
    contents = []

    # Channel context is the PRIMARY conversational context
    if channel_context:
        context_lines = [
            "[RECENT CHANNEL MESSAGES — listed oldest to newest. "
            "This is what is CURRENTLY being discussed. "
            "When a user asks 'what do you think' or similar vague questions, "
            "refer to the most recent messages in this list for context. "
            "Messages from different users may be interleaved.]"
        ]
        for i, msg in enumerate(channel_context, 1):
            prefix = "You (Angela)" if msg["is_angela"] else msg["author"]
            if msg.get("replying_to"):
                ref = msg["replying_to"]
                context_lines.append(f"  #{i} {prefix} (replying to {ref['author']}: \"{ref['content'][:80]}\"): {msg['content']}")
            else:
                context_lines.append(f"  #{i} {prefix}: {msg['content']}")
        context_lines.append("[END OF CHANNEL MESSAGES]")

        contents.append({
            "role": "user",
            "parts": [{"text": "\n".join(context_lines)}]
        })
        contents.append({
            "role": "model",
            "parts": [{"text": "Understood. I've reviewed the recent channel messages and I know what's currently being discussed."}]
        })

    # Build the current user message with reply context
    current_message_parts = []

    current_message_parts.append(
        f"[YOU ARE NOW RESPONDING TO: {user_name}. Address {user_name} directly. "
        f"Do not respond to other users' messages unless {user_name} asks about them.]\n\n"
    )

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
            "thinkingConfig": {
                "thinkingLevel": "MINIMAL",
            },
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
            # Try primary model first, fall back if 503s persist
            for model in [PRIMARY_MODEL, FALLBACK_MODEL]:
                api_url = f"{API_BASE}/{model}:generateContent"
                success = False

                for attempt in range(3):
                    async with session.post(
                        f"{api_url}?key={GEMINI_API_KEY}",
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()

                            # Extract response text
                            candidates = data.get("candidates", [])
                            if not candidates:
                                # Check if blocked by safety filters
                                block_reason = data.get("promptFeedback", {}).get("blockReason", "")
                                if block_reason:
                                    print(f"[Gemini] Blocked by safety filter: {block_reason}")
                                    return "My safety protocols flagged that input. Rephrase and try again."
                                return "No response generated. Even I need a moment to think sometimes."

                            parts = candidates[0].get("content", {}).get("parts", [])
                            if not parts:
                                return "Empty response. Unusual. I'll recalibrate."

                            return parts[0].get("text", "...").strip()

                        elif resp.status == 429 or resp.status == 503:
                            # Rate limited or server overloaded — wait and retry
                            retry_after = int(resp.headers.get("Retry-After", 3 + attempt * 2))
                            reason = "Rate limited" if resp.status == 429 else "Server overloaded"
                            print(f"[Gemini] {reason} ({resp.status}) on {model}, attempt {attempt + 1}/3, waiting {retry_after}s")
                            if attempt < 2:
                                await asyncio.sleep(retry_after)
                                continue
                            else:
                                # All retries failed for this model, try fallback
                                break

                        else:
                            error_text = await resp.text()
                            print(f"[Gemini] API error {resp.status} on {model}: {error_text[:300]}")

                            if resp.status == 400:
                                return "That request couldn't be processed. Try rephrasing."
                            elif resp.status == 403:
                                return "API access denied. Someone needs to check my credentials."
                            else:
                                return "Processing error. My systems are momentarily... distracted. Try again."
                else:
                    # Inner loop completed without break = shouldn't happen, but handle it
                    continue

                # If we broke out of retry loop (all retries failed), try next model
                if model == PRIMARY_MODEL:
                    print(f"[Gemini] Primary model failed, falling back to {FALLBACK_MODEL}")
                    continue

            # Both models failed
            return "Google's servers are under high demand right now. Try again in a moment."

    except aiohttp.ClientError as e:
        print(f"[Gemini] Connection error: {e}")
        return "Network disruption detected. My connection to external systems is temporarily impaired."
    except Exception as e:
        print(f"[Gemini] Unexpected error: {e}")
        return "An unexpected error occurred. I'm logging this for analysis."