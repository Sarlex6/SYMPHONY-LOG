"""Roblox OAuth 2.0 / OIDC account verification.

Replaces Rover for binding a Discord account to a Roblox account. Rover requires
each user to separately opt in before a third party may read their link; this
flow asks the user directly and needs no intermediary.

Flow
----
    /register  ──> begin()  ──> authorization URL (delivered PRIVATELY)
                                     │
                       user authorizes on roblox.com
                                     │
                                     v
    GET /roblox/callback?code=..&state=..  ──> complete()
                                     │
                       token exchange + userinfo
                                     │
                                     v
                        RoleManagerService.complete_registration()

Endpoints (from the OIDC discovery document at
https://apis.roblox.com/oauth/.well-known/openid-configuration):

    authorize  GET  https://apis.roblox.com/oauth/v1/authorize
    token      POST https://apis.roblox.com/oauth/v1/token
    userinfo   GET  https://apis.roblox.com/oauth/v1/userinfo

`sub` is the Roblox user ID; `preferred_username` is the Roblox username. Only
the `openid` and `profile` scopes are requested — nothing about email, age or
credentials is asked for, because none of it is needed here.

Security properties
-------------------
  * `state` is 256 bits of CSPRNG entropy, single-use, and expires. It is the
    ONLY thing binding a callback to a Discord account — the callback never
    reads a Discord ID from the query string, so a third party cannot bind their
    Roblox account to someone else's Discord account by crafting a URL.

  * PKCE (S256) is always used, so an intercepted authorization code is useless
    without the verifier held server-side.

  * The authorization URL is returned in `detail`, never in a user-facing
    message, so an interface has to deliberately deliver it privately. Anyone
    who obtains a live URL could bind THEIR Roblox account to the requester's
    Discord ID, which is exactly the confusion this flow exists to prevent.
"""

import base64
import hashlib
import secrets
import time
import urllib.parse

import aiohttp

from roles import config as roles_config

# ── Endpoints ────────────────────────────────────────────────────────────────

AUTHORIZE_URL = "https://apis.roblox.com/oauth/v1/authorize"
TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
USERINFO_URL = "https://apis.roblox.com/oauth/v1/userinfo"
REVOKE_URL = "https://apis.roblox.com/oauth/v1/token/revoke"

#: Identity only. Deliberately minimal — more scopes would mean a scarier
#: consent screen for no functional gain.
DEFAULT_SCOPES = ("openid", "profile")


class VerificationResult:
    """Outcome of an OAuth verification."""

    def __init__(self, roblox_id=None, roblox_username="", discord_id=0,
                 guild_id=None, discord_username="", error="", retryable=False):
        self.roblox_id = roblox_id
        self.roblox_username = roblox_username
        self.discord_id = discord_id
        self.guild_id = guild_id
        self.discord_username = discord_username
        self.error = error
        self.retryable = retryable

    @property
    def ok(self):
        return bool(self.roblox_id and self.discord_id)


class _Pending:
    """One in-flight verification, keyed by `state`."""

    __slots__ = ("discord_id", "guild_id", "discord_username", "code_verifier",
                 "created_at", "nonce")

    def __init__(self, discord_id, guild_id, discord_username, code_verifier, nonce):
        self.discord_id = discord_id
        self.guild_id = guild_id
        self.discord_username = discord_username
        self.code_verifier = code_verifier
        self.nonce = nonce
        self.created_at = time.time()


class RobloxOAuthVerifier:
    """Begins and completes Roblox account verifications.

    Pending verifications live in memory only. A restart drops them, which costs
    the user one re-run of /register — not worth a persistence layer for a
    ten-minute window.
    """

    def __init__(self):
        self._pending = {}

    # ── Configuration ──

    def is_configured(self):
        cfg = roles_config.current()
        return bool(
            roles_config.secret("ROBLOX_OAUTH_CLIENT_ID")
            and roles_config.secret("ROBLOX_OAUTH_CLIENT_SECRET")
            and cfg.roblox_oauth.redirect_uri
        )

    def missing_configuration(self):
        missing = []
        if not roles_config.secret("ROBLOX_OAUTH_CLIENT_ID"):
            missing.append("ROBLOX_OAUTH_CLIENT_ID")
        if not roles_config.secret("ROBLOX_OAUTH_CLIENT_SECRET"):
            missing.append("ROBLOX_OAUTH_CLIENT_SECRET")
        if not roles_config.current().roblox_oauth.redirect_uri:
            missing.append("roblox_oauth.redirect_uri")
        return missing

    # ── Begin ──

    def begin(self, discord_id, guild_id, discord_username=""):
        """Create a pending verification. Returns (auth_url, state) or (None, error).

        The returned URL must be delivered ONLY to `discord_id` — ephemerally or
        by DM. Whoever opens it binds their Roblox account to this Discord ID.
        """
        if not self.is_configured():
            return None, (
                "Roblox OAuth is not configured: "
                + ", ".join(self.missing_configuration())
            )

        cfg = roles_config.current().roblox_oauth
        self.sweep_expired()

        # One in-flight verification per user; a re-run invalidates the old link.
        self.cancel_for_user(discord_id)

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        code_verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")

        self._pending[state] = _Pending(
            discord_id, guild_id, discord_username, code_verifier, nonce
        )

        params = {
            "client_id": roles_config.secret("ROBLOX_OAUTH_CLIENT_ID"),
            "redirect_uri": cfg.redirect_uri,
            "scope": " ".join(cfg.scopes or DEFAULT_SCOPES),
            "response_type": "code",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state

    # ── Complete ──

    async def complete(self, state, code):
        """Redeem an authorization code. Returns a VerificationResult.

        Consumes `state` unconditionally: a code is single-use with a one-minute
        lifetime, so a retry has to start over anyway, and leaving the state
        alive would keep a binding window open after a failure.
        """
        self.sweep_expired()

        pending = self._pending.pop(state, None)
        if pending is None:
            return VerificationResult(
                error="This verification link is invalid, already used, or expired. "
                      "Run /register again to get a fresh one."
            )

        if not code:
            return VerificationResult(
                discord_id=pending.discord_id,
                error="Roblox did not return an authorization code.",
            )

        cfg = roles_config.current().roblox_oauth
        token = await self._exchange_code(code, pending.code_verifier, cfg)
        if token.get("error"):
            return VerificationResult(
                discord_id=pending.discord_id,
                error=token["error"],
                retryable=token.get("retryable", False),
            )

        info = await self._fetch_userinfo(token["access_token"], cfg)
        if info.get("error"):
            return VerificationResult(
                discord_id=pending.discord_id,
                error=info["error"],
                retryable=info.get("retryable", False),
            )

        # The access token has served its purpose; nothing here needs standing
        # access to the user's Roblox account.
        await self._revoke(token.get("access_token"), cfg)

        try:
            roblox_id = int(info["sub"])
        except (KeyError, TypeError, ValueError):
            return VerificationResult(
                discord_id=pending.discord_id,
                error="Roblox returned no usable user ID.",
            )

        return VerificationResult(
            roblox_id=roblox_id,
            roblox_username=info.get("preferred_username") or info.get("name") or "",
            discord_id=pending.discord_id,
            guild_id=pending.guild_id,
            discord_username=pending.discord_username,
        )

    # ── HTTP ──

    async def _exchange_code(self, code, code_verifier, cfg):
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": roles_config.secret("ROBLOX_OAUTH_CLIENT_ID"),
            "client_secret": roles_config.secret("ROBLOX_OAUTH_CLIENT_SECRET"),
            "redirect_uri": cfg.redirect_uri,
        }
        timeout = aiohttp.ClientTimeout(total=cfg.request_timeout)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    TOKEN_URL, data=payload, timeout=timeout,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as resp:
                    body = await resp.text()

                    if resp.status != 200:
                        print(f"[Roles:OAuth] Token exchange failed {resp.status}: {body[:300]}")
                        if resp.status == 400:
                            return {"error": (
                                "Roblox rejected the authorization code. Codes expire "
                                "after one minute — run /register and finish promptly."
                            )}
                        if resp.status in (401, 403):
                            return {"error": (
                                "Roblox rejected the app credentials. An administrator "
                                "needs to check ROBLOX_OAUTH_CLIENT_ID / SECRET and the "
                                "registered redirect URI."
                            )}
                        return {
                            "error": f"Roblox token endpoint returned {resp.status}.",
                            "retryable": resp.status >= 500,
                        }

                    data = await resp.json()
                    if not data.get("access_token"):
                        return {"error": "Roblox returned no access token."}
                    return data

        except aiohttp.ClientError as exc:
            print(f"[Roles:OAuth] Token exchange error: {type(exc).__name__}: {exc}")
            return {"error": "Could not reach Roblox to complete verification.",
                    "retryable": True}
        except Exception as exc:  # timeouts included
            print(f"[Roles:OAuth] Token exchange error: {type(exc).__name__}: {exc}")
            return {"error": "Verification timed out talking to Roblox.",
                    "retryable": True}

    async def _fetch_userinfo(self, access_token, cfg):
        timeout = aiohttp.ClientTimeout(total=cfg.request_timeout)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        print(f"[Roles:OAuth] userinfo failed {resp.status}: {body[:200]}")
                        return {
                            "error": f"Roblox userinfo returned {resp.status}.",
                            "retryable": resp.status >= 500,
                        }
                    return await resp.json()

        except Exception as exc:
            print(f"[Roles:OAuth] userinfo error: {type(exc).__name__}: {exc}")
            return {"error": "Could not read your Roblox profile.", "retryable": True}

    async def _revoke(self, token, cfg):
        """Best-effort token revocation. Never fails the verification."""
        if not token:
            return
        payload = {
            "token": token,
            "client_id": roles_config.secret("ROBLOX_OAUTH_CLIENT_ID"),
            "client_secret": roles_config.secret("ROBLOX_OAUTH_CLIENT_SECRET"),
        }
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    REVOKE_URL, data=payload,
                    timeout=aiohttp.ClientTimeout(total=cfg.request_timeout),
                )
        except Exception as exc:
            print(f"[Roles:OAuth] Token revocation failed (non-fatal): {exc}")

    # ── Pending state maintenance ──

    def sweep_expired(self):
        cfg = roles_config.current().roblox_oauth
        cutoff = time.time() - cfg.state_ttl_seconds
        expired = [s for s, p in self._pending.items() if p.created_at < cutoff]
        for state in expired:
            self._pending.pop(state, None)
        return len(expired)

    def cancel_for_user(self, discord_id):
        """Invalidate any outstanding link for this user."""
        stale = [s for s, p in self._pending.items() if p.discord_id == discord_id]
        for state in stale:
            self._pending.pop(state, None)
        return len(stale)

    def pending_count(self):
        return len(self._pending)


#: Shared instance.
verifier = RobloxOAuthVerifier()
