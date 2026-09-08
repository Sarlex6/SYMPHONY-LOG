"""Rover API client — used ONLY during /register.

Rover translates a Discord account to a Roblox account once, at registration.
After that the Discord ID and Roblox ID stored in the technical columns are
canonical, and no further Rover call is ever made. That keeps registration the
single point of dependency on a third-party verification service.

Follows the aiohttp + retry style of assistant/gemini.py.
"""

import asyncio

import aiohttp

from roles import config as roles_config


class RoverResult:
    """Outcome of a Discord -> Roblox lookup."""

    def __init__(self, roblox_id=None, roblox_username="", error="", retryable=False):
        self.roblox_id = roblox_id
        self.roblox_username = roblox_username
        self.error = error
        self.retryable = retryable

    @property
    def ok(self):
        return bool(self.roblox_id)


async def lookup_roblox(discord_id, guild_id=None, api_key=None):
    """Resolve a Discord user to their verified Roblox account.

    Returns a RoverResult; never raises. A registration that cannot verify must
    fail cleanly with an explanation, not with a traceback in a slash command.
    """
    cfg = roles_config.current()
    api_key = api_key or roles_config.secret("ROVER_API_KEY")
    guild_id = guild_id or cfg.discord.main_guild_id

    if not api_key:
        return RoverResult(error="ROVER_API_KEY is not configured.")
    if not guild_id:
        return RoverResult(error="The main Discord server ID is not configured.")

    url = f"{cfg.rover.api_base}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=cfg.rover.request_timeout)

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        roblox_id = data.get("robloxId") or data.get("roblox_id")
                        if not roblox_id:
                            return RoverResult(
                                error="Rover returned no Roblox ID for this account."
                            )
                        return RoverResult(
                            roblox_id=int(roblox_id),
                            roblox_username=(
                                data.get("cachedUsername")
                                or data.get("robloxUsername")
                                or ""
                            ),
                        )

                    if resp.status == 404:
                        return RoverResult(
                            error="This Discord account is not verified with Rover. "
                                  "Verify at verify.rover.link and try again."
                        )

                    if resp.status in (401, 403):
                        body = await resp.text()
                        print(f"[Roles:Rover] Auth failure {resp.status}: {body[:200]}")
                        return RoverResult(
                            error="Rover rejected the API key. An administrator needs "
                                  "to check ROVER_API_KEY."
                        )

                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 3 + attempt * 2))
                        print(f"[Roles:Rover] Rate limited, waiting {retry_after}s "
                              f"(attempt {attempt + 1}/3)")
                        if attempt < 2:
                            await asyncio.sleep(retry_after)
                            continue
                        return RoverResult(
                            error="Rover is rate limiting requests. Try again shortly.",
                            retryable=True,
                        )

                    body = await resp.text()
                    print(f"[Roles:Rover] API error {resp.status}: {body[:200]}")
                    if attempt < 2 and resp.status >= 500:
                        await asyncio.sleep(2 + attempt * 2)
                        continue
                    return RoverResult(
                        error=f"Rover returned an unexpected error ({resp.status}).",
                        retryable=resp.status >= 500,
                    )

        except asyncio.TimeoutError:
            print(f"[Roles:Rover] Timeout, attempt {attempt + 1}/3")
            if attempt < 2:
                continue
            return RoverResult(error="Rover did not respond in time.", retryable=True)

        except aiohttp.ClientError as exc:
            print(f"[Roles:Rover] Connection error: {type(exc).__name__}: {exc}")
            return RoverResult(
                error="Could not reach Rover. Try again shortly.", retryable=True
            )

    return RoverResult(error="Rover verification failed after 3 attempts.", retryable=True)
