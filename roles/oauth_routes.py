"""HTTP callback for Roblox OAuth verification.

Mounted onto the aiohttp server that already runs in assistant/web.py, so there
is one web server for the process rather than a second one.

The route is public by necessity — Roblox redirects the user's browser here.
It is safe to expose because it carries no authority of its own: the only way to
reach a Discord account is to present a `state` value this process generated and
still holds, and each one is single-use, expiring and bound to one Discord ID.
An attacker with no valid state can do nothing but receive an error page.
"""

from aiohttp import web

CALLBACK_PATH = "/roblox/callback"

_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0f1115; color:#e6e8ee; }}
  .card {{ max-width:34rem; padding:2.5rem; background:#171a21;
          border:1px solid #262b36; border-radius:14px; text-align:center; }}
  .mark {{ font-size:2.5rem; line-height:1; }}
  h1 {{ font-size:1.35rem; margin:.75rem 0 .5rem; }}
  p {{ margin:.5rem 0; color:#a8b0c0; }}
  .ok {{ color:#5ad18b; }} .bad {{ color:#ff6b6b; }}
  code {{ background:#0f1115; padding:.15rem .4rem; border-radius:5px; color:#cdd4e3; }}
</style>
<div class="card">
  <div class="mark {cls}">{mark}</div>
  <h1>{title}</h1>
  {body}
  <p style="margin-top:1.5rem;font-size:.9rem">You can close this tab.</p>
</div>
"""


def _render(title, body, ok=True, status=200):
    return web.Response(
        text=_PAGE.format(
            title=title,
            body=body,
            mark="✓" if ok else "✕",
            cls="ok" if ok else "bad",
        ),
        content_type="text/html",
        status=status,
    )


async def handle_callback(request):
    """Roblox redirects the user's browser here after they authorize (or don't)."""
    # Imported lazily and defensively: this route is public, so a broken or
    # partially installed role-management stack must render an error page rather
    # than an unhandled 500.
    try:
        from roles.models import ActionStatus
        from roles.service import service
    except Exception as exc:
        print(f"[Roles:OAuth] Role management unavailable: {type(exc).__name__}: {exc}")
        return _render(
            "Service unavailable",
            "<p>Account linking is not available right now. Please try again "
            "later, or contact a staff member.</p>",
            ok=False, status=503,
        )

    # The user declined, or Roblox refused.
    error = request.query.get("error")
    if error:
        description = request.query.get("error_description", "")
        print(f"[Roles:OAuth] Callback returned error={error}: {description[:200]}")
        return _render(
            "Verification cancelled",
            "<p>Roblox did not authorize the request. Nothing was changed.</p>"
            "<p>Run <code>/register</code> again if you want to retry.</p>",
            ok=False, status=400,
        )

    state = request.query.get("state", "")
    code = request.query.get("code", "")

    if not state or not code:
        return _render(
            "Invalid request",
            "<p>This link is missing information. Run <code>/register</code> "
            "again to get a fresh one.</p>",
            ok=False, status=400,
        )

    try:
        result = await service.complete_registration(state, code)
    except Exception as exc:
        print(f"[Roles:OAuth] Callback failed: {type(exc).__name__}: {exc}")
        return _render(
            "Something went wrong",
            "<p>The verification could not be completed. Please try "
            "<code>/register</code> again.</p>",
            ok=False, status=500,
        )

    if result.status is ActionStatus.OK:
        record = result.record
        roblox = result.detail.get("roblox_id", "")
        await _notify_discord(record, result)
        return _render(
            "Registered",
            f"<p>Your Roblox account <strong>{roblox}</strong> is now linked.</p>"
            f"<p>Your roles are being applied — this can take a moment.</p>",
        )

    return _render(
        "Could not complete registration",
        f"<p>{_escape(result.message)}</p>",
        ok=False, status=400,
    )


async def _notify_discord(record, result):
    """Best-effort DM so the user learns the outcome in Discord too."""
    if record is None:
        return
    try:
        from roles.discord_sync import synchronizer
        client = synchronizer.client
        if client is None or not client.is_ready():
            return
        user = client.get_user(record.discord_id) or await client.fetch_user(record.discord_id)
        if user:
            await user.send(f"✅ {result.message}")
    except Exception as exc:
        print(f"[Roles:OAuth] Could not DM registration result: {type(exc).__name__}: {exc}")


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("**", "")
    )


def add_routes(app):
    """Attach the callback route to an existing aiohttp application."""
    app.router.add_get(CALLBACK_PATH, handle_callback)
    print(f"[Roles] OAuth callback mounted at GET {CALLBACK_PATH}")
    return app
