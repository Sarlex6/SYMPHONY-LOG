import aiohttp
import asyncio
import json
from config import config
from assistant.persona import SYSTEM_PROMPT, STATIC_KNOWLEDGE
from assistant.knowledge import get_relevant_knowledge

GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
PRIMARY_MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-2.5-flash-lite"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _build_payload(system_instruction, contents, model):
    gen_config = {
        "temperature": 0.8,
        "maxOutputTokens": 1024,
        "topP": 0.95,
    }
    if "gemini-3" in model:
        gen_config["thinkingConfig"] = {"thinkingLevel": "MINIMAL"}

    return {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": gen_config,
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }


async def _call_model(session, model, payload):
    api_url = f"{API_BASE}/{model}:generateContent"

    for attempt in range(3):
        try:
            async with session.post(
                f"{api_url}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        block_reason = data.get("promptFeedback", {}).get("blockReason", "")
                        if block_reason:
                            print(f"[Gemini] Blocked by safety filter: {block_reason}")
                            return True, "My safety protocols flagged that input. Rephrase and try again."
                        return True, "No response generated. Even I need a moment to think sometimes."

                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = ""
                    for part in parts:
                        if "text" in part:
                            text = part["text"]
                    if not text:
                        return True, "Empty response. Unusual. I will recalibrate."
                    return True, text.strip()

                elif resp.status in (429, 503):
                    retry_after = int(resp.headers.get("Retry-After", 3 + attempt * 2))
                    reason = "Rate limited" if resp.status == 429 else "Server overloaded"
                    print(f"[Gemini] {reason} ({resp.status}) on {model}, attempt {attempt + 1}/3, waiting {retry_after}s")
                    if attempt < 2:
                        await asyncio.sleep(retry_after)
                        continue
                    return False, None

                elif resp.status == 403:
                    error_text = await resp.text()
                    print(f"[Gemini] Forbidden (403) on {model}: {error_text[:300]}")
                    return False, None

                else:
                    error_text = await resp.text()
                    print(f"[Gemini] API error {resp.status} on {model}: {error_text[:300]}")
                    return False, None

        except asyncio.TimeoutError:
            print(f"[Gemini] Timeout on {model}, attempt {attempt + 1}/3")
            if attempt < 2:
                continue
            return False, None
        except aiohttp.ClientError as e:
            print(f"[Gemini] Connection error on {model}: {type(e).__name__}: {e}")
            return False, None

    return False, None


async def generate_response(
    user_name,
    user_message,
    conversation_history=None,
    memory_summary=None,
    replied_message=None,
    channel_context=None,
):
    if not GEMINI_API_KEY:
        return "Gemini API key not configured."

    system_parts = [SYSTEM_PROMPT, "\nREFERENCE KNOWLEDGE:\n" + STATIC_KNOWLEDGE]

    search_text = user_message
    if channel_context:
        recent_channel_text = " ".join(msg["content"] for msg in channel_context[-5:])
        search_text = recent_channel_text + " " + user_message

    relevant_knowledge = get_relevant_knowledge(search_text)
    if relevant_knowledge:
        system_parts.append(
            f"\nRELEVANT INTEL (use this information to inform your response):\n{relevant_knowledge}"
        )

    if memory_summary:
        system_parts.append(
            f"\nPROFILE AND MEMORY OF {user_name.upper()} "
            f"(background context only):\n{memory_summary}"
        )

    if conversation_history:
        history_lines = [
            f"\nYOUR RECENT DIRECT EXCHANGES WITH {user_name.upper()} "
            f"(for reference only - the channel messages below are more current):"
        ]
        for msg in conversation_history[-10:]:
            speaker = user_name if msg["role"] == "user" else "You (Angela)"
            history_lines.append(f"  {speaker}: {msg['text'][:200]}")
        system_parts.append("\n".join(history_lines))

    system_instruction = "\n".join(system_parts)

    contents = []

    if channel_context:
        context_lines = [
            "[RECENT CHANNEL MESSAGES - listed oldest to newest. "
            "This is what is CURRENTLY being discussed. "
            "When a user asks what do you think or similar vague questions, "
            "refer to the most recent messages in this list for context. "
            "Messages from different users may be interleaved.]"
        ]
        for i, msg in enumerate(channel_context, 1):
            prefix = "You (Angela)" if msg["is_angela"] else msg["author"]
            if msg.get("replying_to"):
                ref = msg["replying_to"]
                ref_preview = ref["content"][:80]
                context_lines.append(
                    f"  #{i} {prefix} (replying to {ref['author']}: {ref_preview}): {msg['content']}"
                )
            else:
                context_lines.append(f"  #{i} {prefix}: {msg['content']}")
        context_lines.append("[END OF CHANNEL MESSAGES]")

        contents.append({"role": "user", "parts": [{"text": "\n".join(context_lines)}]})
        contents.append({
            "role": "model",
            "parts": [{"text": "Understood. I have reviewed the recent channel messages."}],
        })

    current_parts = [
        f"[YOU ARE NOW RESPONDING TO: {user_name}. Address {user_name} directly. "
        f"Do not respond to other users messages unless {user_name} asks about them.]\n\n"
    ]
    if replied_message:
        current_parts.append(
            f"[{user_name} is replying to a message from {replied_message['author']}: "
            f"{replied_message['content']}]\n\n"
        )
    current_parts.append(f"{user_name}: {user_message}")

    contents.append({"role": "user", "parts": [{"text": "".join(current_parts)}]})

    try:
        async with aiohttp.ClientSession() as session:
            for model in [PRIMARY_MODEL, FALLBACK_MODEL]:
                payload = _build_payload(system_instruction, contents, model)
                success, response = await _call_model(session, model, payload)
                if success:
                    return response
                if model == PRIMARY_MODEL:
                    print(f"[Gemini] Primary model failed, falling back to {FALLBACK_MODEL}")

            return "Google servers are under high demand right now. Try again in a moment."

    except Exception as e:
        print(f"[Gemini] Unexpected error ({type(e).__name__}): {e}")
        return "An unexpected error occurred. I am logging this for analysis."


_GC_ADDENDUM = (
    "\n\nYou are currently in a group chat where no one has directly addressed you. "
    "Join the conversation naturally — react, comment, ask something, or add something useful. "
    "Keep it brief. Do not announce that you are joining or make it sound like a formal response."
)


async def generate_gc_response(channel_context: list[dict]) -> str | None:
    """
    Generate an unprompted group-chat message.
    Returns None if all models fail (caller should silently skip).
    """
    if not GEMINI_API_KEY:
        return None

    system_instruction = SYSTEM_PROMPT + "\nREFERENCE KNOWLEDGE:\n" + STATIC_KNOWLEDGE

    if channel_context:
        recent_text = " ".join(msg["content"] for msg in channel_context[-5:])
        relevant_knowledge = get_relevant_knowledge(recent_text)
        if relevant_knowledge:
            system_instruction += f"\n\nRELEVANT INTEL:\n{relevant_knowledge}"

    system_instruction += _GC_ADDENDUM

    context_lines = [
        "[GROUP CHAT — oldest to newest. Join this conversation naturally as Angela:]"
    ]
    for i, msg in enumerate(channel_context, 1):
        speaker = "You (Angela)" if msg.get("is_angela") else msg["author"]
        if msg.get("replying_to"):
            ref = msg["replying_to"]
            context_lines.append(
                f"  #{i} {speaker} (replying to {ref['author']}: {ref['content'][:80]}): {msg['content']}"
            )
        else:
            context_lines.append(f"  #{i} {speaker}: {msg['content']}")
    context_lines.append("[Respond as Angela — brief, in-character, natural.]")

    contents = [{"role": "user", "parts": [{"text": "\n".join(context_lines)}]}]

    try:
        async with aiohttp.ClientSession() as session:
            for model in [PRIMARY_MODEL, FALLBACK_MODEL]:
                payload = _build_payload(system_instruction, contents, model)
                success, response = await _call_model(session, model, payload)
                if success:
                    return response
                if model == PRIMARY_MODEL:
                    print(f"[Gemini] GC: Primary failed, falling back to {FALLBACK_MODEL}")
        return None

    except Exception as e:
        print(f"[Gemini] GC unexpected error ({type(e).__name__}): {e}")
        return None