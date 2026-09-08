"""Terms of Service and Privacy Policy pages.

Served from this application's own web server, so they sit on the same HTTPS
host as the OAuth callback and need no separate hosting. Roblox requires both
URLs (HTTPS, max 256 characters) before an OAuth app can be submitted for
review and leave private mode.

    GET /terms     Terms of Service
    GET /privacy   Privacy Policy

EDIT THE THREE CONSTANTS BELOW before publishing. Everything else describes
what this codebase actually does — if you change what data is collected, update
the Privacy Policy to match, or it stops being accurate.

These are plain-language drafts, not legal advice. Have someone review them if
anything about your situation is unusual.
"""

from aiohttp import web

# ── Fill these in ────────────────────────────────────────────────────────────

#: The name people know this organization by.
ORGANIZATION = "L.O.T.U.S."

#: How someone reaches a human. A Discord handle is fine; an email is better if
#: you have one, because a data request may come from someone who has already
#: left the server.
CONTACT = "the #support channel in our Discord server, or a Discord staff member"

#: Update whenever the substance of these documents changes.
LAST_UPDATED = "8 September 2026"

# ── Shared layout ────────────────────────────────────────────────────────────

_LAYOUT = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {org}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 3rem 1.25rem 5rem;
    background: #0f1115; color: #d7dbe4;
    font: 16px/1.7 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  main {{ max-width: 46rem; margin: 0 auto; }}
  .eyebrow {{
    text-transform: uppercase; letter-spacing: .12em; font-size: .75rem;
    color: #7c869b; margin-bottom: .5rem;
  }}
  h1 {{ font-size: 2rem; line-height: 1.2; margin: 0 0 .35rem; color: #f2f4f8; }}
  .updated {{ color: #7c869b; font-size: .9rem; margin-bottom: 2.5rem; }}
  h2 {{
    font-size: 1.1rem; margin: 2.5rem 0 .75rem; color: #f2f4f8;
    padding-top: 1.25rem; border-top: 1px solid #232935;
  }}
  h2:first-of-type {{ border-top: 0; padding-top: 0; }}
  p, li {{ color: #c2c9d6; }}
  ul {{ padding-left: 1.25rem; }}
  li {{ margin: .4rem 0; }}
  strong {{ color: #eef1f6; }}
  code {{
    background: #171a21; border: 1px solid #232935; border-radius: 5px;
    padding: .1rem .35rem; font-size: .9em; color: #cdd4e3;
  }}
  .note {{
    background: #141821; border: 1px solid #232935; border-left: 3px solid #4f7fd4;
    border-radius: 8px; padding: 1rem 1.15rem; margin: 1.5rem 0;
  }}
  .note p {{ margin: 0; }}
  footer {{
    margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid #232935;
    color: #7c869b; font-size: .875rem;
  }}
  a {{ color: #7aa2f7; }}
</style>
<main>
  <div class="eyebrow">{org}</div>
  <h1>{title}</h1>
  <div class="updated">Last updated: {updated}</div>
  {body}
  <footer>
    Questions about this document? Contact {contact}.
    &nbsp;·&nbsp; <a href="{other_href}">{other_label}</a>
  </footer>
</main>
"""


def _page(title, body, other_href, other_label):
    return _LAYOUT.format(
        title=title, body=body, org=ORGANIZATION,
        updated=LAST_UPDATED, contact=CONTACT,
        other_href=other_href, other_label=other_label,
    )


# ── Terms of Service ─────────────────────────────────────────────────────────

_TERMS_BODY = """
<p>
  These terms cover the {org} personnel management system — the Discord bots and
  the account-linking service that keep member records, Discord roles and Roblox
  group roles in step. Using them means accepting what follows.
</p>

<h2>1. What this service does</h2>
<p>
  The service maintains a roster of {org} members and applies the roles that go
  with a member's rank and branch across our Discord servers and our Roblox
  group. It also provides an assistant that answers questions and can carry out
  management commands on behalf of authorized members.
</p>

<h2>2. Who may use it</h2>
<ul>
  <li>You must be a member of our Discord server and, where applicable, our Roblox group.</li>
  <li>You must comply with the
      <a href="https://en.help.roblox.com/hc/en-us/articles/115004647846">Roblox Terms of Use</a>
      and the <a href="https://discord.com/terms">Discord Terms of Service</a>.
      Nothing here overrides either.</li>
  <li>You must meet the minimum age required by Discord and Roblox in your country.</li>
</ul>

<h2>3. Linking your account</h2>
<p>
  Registration asks you to authorize us through Roblox so we can confirm which
  Roblox account belongs to you. You are authorizing this yourself and can
  decline. If you decline, you simply will not be registered.
</p>
<div class="note">
  <p>
    <strong>Your verification link is personal.</strong> Anyone who opens it links
    <em>their</em> Roblox account to <em>your</em> Discord account. Do not forward
    or post it. Links expire shortly and can only be used once.
  </p>
</div>

<h2>4. Acceptable use</h2>
<p>Do not:</p>
<ul>
  <li>Attempt to gain a rank, role or permission you have not been granted.</li>
  <li>Impersonate another member, or register on someone else's behalf.</li>
  <li>Interfere with, overload or attempt to circumvent the service or its
      authorization checks.</li>
  <li>Use the service to harass others or to break Discord's or Roblox's rules.</li>
</ul>

<h2>5. Ranks, roles and staff decisions</h2>
<p>
  Ranks, branches and membership are decided by {org} staff. The system applies
  those decisions; it does not make them. Roles granted through this service are
  a reflection of your standing in the organization and can be changed or removed
  at any time at staff discretion.
</p>
<p>
  If your record is removed, the roles the system granted you are withdrawn along
  with it, and rejoining requires registering again.
</p>

<h2>6. Availability</h2>
<p>
  The service is provided as-is, without warranty, and free of charge. It depends
  on Discord, Roblox and Google, any of which may be unavailable or change
  without notice. We do not guarantee uptime, and we are not liable for losses
  arising from downtime, delays, or errors in synchronization.
</p>

<h2>7. Ending your use</h2>
<p>
  You may ask for your record to be removed at any time — see the Privacy Policy.
  We may suspend or remove access for anyone who breaks these terms, leaves the
  organization, or is removed from it.
</p>

<h2>8. Changes</h2>
<p>
  These terms may change as the service changes. The date at the top reflects the
  most recent revision. Continuing to use the service after a change means
  accepting the revised terms.
</p>

<h2>9. Not affiliated</h2>
<p>
  This service is operated by {org} and is not endorsed by, affiliated with, or
  sponsored by Roblox Corporation or Discord Inc.
</p>
"""


# ── Privacy Policy ───────────────────────────────────────────────────────────

_PRIVACY_BODY = """
<p>
  This policy explains what the {org} personnel management system stores about
  you, why, and how to have it removed. We collect what the roster needs and
  nothing more.
</p>

<h2>1. What we store</h2>
<ul>
  <li><strong>Discord account ID and username</strong> — the ID identifies your
      record; the username is shown so the roster is readable.</li>
  <li><strong>Roblox account ID and username</strong> — obtained once, when you
      authorize us, and used to apply your Roblox group roles.</li>
  <li><strong>Membership details</strong> — your rank, branch, activity status,
      leave-of-absence entries, and the date you joined.</li>
  <li><strong>Timezone</strong>, if you choose to set one.</li>
  <li><strong>Staff notes</strong> about your membership, written by staff.</li>
  <li><strong>Technical records</strong> — when your roles were last synchronized
      and whether it succeeded, so failures can be diagnosed and retried.</li>
</ul>
<p>
  Our assistant also retains recent conversation context and a short summary for
  members who talk to it, so it can follow a conversation. This is kept for a
  limited period and then deleted automatically.
</p>

<h2>2. What we do not store</h2>
<ul>
  <li>No passwords. We never see your Discord or Roblox credentials.</li>
  <li>No email address. We do not request the email permission from Roblox.</li>
  <li>No payment information. The service does not take payments.</li>
  <li><strong>No standing access to your Roblox account.</strong> The access token
      from linking is used once to read your user ID and username, then
      immediately revoked. We cannot act on your Roblox account afterwards.</li>
</ul>

<h2>3. Where it comes from</h2>
<ul>
  <li><strong>From Discord</strong> — your account ID and display name when you
      use a command.</li>
  <li><strong>From Roblox</strong> — your user ID and username, only after you
      authorize it, and only the <code>openid</code> and <code>profile</code>
      permissions.</li>
  <li><strong>From staff</strong> — rank, branch, status and notes.</li>
  <li><strong>From you</strong> — your timezone and leave-of-absence entries.</li>
</ul>

<h2>4. Why we store it</h2>
<p>
  To maintain the member roster, to apply the correct Discord and Roblox roles
  for your rank and branch, to check whether someone is authorized to make a
  change, and to keep an audit trail of changes so mistakes can be traced. We do
  not use it for advertising, and we do not sell it or trade it.
</p>

<h2>5. Who else can see it</h2>
<ul>
  <li><strong>{org} staff</strong> with access to the roster.</li>
  <li><strong>Google</strong> — the roster is held in Google Sheets.</li>
  <li><strong>Discord</strong> and <strong>Roblox</strong> — role changes are sent
      to their platforms.</li>
  <li><strong>Our hosting provider</strong>, which runs the service.</li>
  <li><strong>Google (Gemini)</strong> — messages sent to the assistant are
      processed to generate a reply.</li>
</ul>
<p>
  Each handles data under its own privacy policy. We do not share your data with
  anyone else, and we do not sell it.
</p>

<h2>6. Members' visibility</h2>
<p>
  The roster is visible to staff, and your rank and branch are visible to other
  members through the roles you hold in our servers. Treat your rank, branch and
  activity status as visible within the organization rather than private.
</p>

<h2>7. How long we keep it</h2>
<p>
  Your record is kept while you are a member. When your record is removed, the
  roles the system granted are withdrawn. Assistant conversation data expires
  automatically after a short period. Backups or logs may retain some information
  briefly after deletion.
</p>

<h2>8. Your choices</h2>
<ul>
  <li><strong>Don't link.</strong> Registration is something you authorize. You can
      decline.</li>
  <li><strong>Unlink at any time.</strong> You can revoke this application's access
      in your Roblox account settings.</li>
  <li><strong>Ask what we hold.</strong> Staff can show you your record.</li>
  <li><strong>Ask for removal.</strong> Contact {contact} and we will remove your
      record. Note that this also removes the roles it granted, and rejoining
      means registering again.</li>
  <li><strong>Correct it.</strong> If something is wrong, ask staff to fix it.</li>
</ul>

<h2>9. Younger members</h2>
<p>
  Our community includes people of a range of ages. We deliberately keep what we
  store to a minimum for this reason: no email addresses, no real names, no
  contact details, no location beyond a timezone you choose to give. If you are a
  parent or guardian and want a record removed, contact {contact} and we will
  remove it.
</p>

<h2>10. Security</h2>
<p>
  Credentials are held as environment secrets and are not stored in our source
  code. Roblox access tokens are revoked immediately after linking. Access to the
  roster is limited to staff. No system is perfectly secure, so we cannot
  guarantee against every possible breach.
</p>

<h2>11. Changes</h2>
<p>
  If we change what we collect or how we use it, we will update this page and the
  date at the top.
</p>

<h2>12. Contact</h2>
<p>
  For any question about your data, or to ask for it to be removed, contact
  {contact}.
</p>
"""


# ── Handlers ─────────────────────────────────────────────────────────────────

async def handle_terms(request):
    return web.Response(
        text=_page(
            "Terms of Service",
            _TERMS_BODY.format(org=ORGANIZATION, contact=CONTACT),
            "/privacy", "Privacy Policy",
        ),
        content_type="text/html",
    )


async def handle_privacy(request):
    return web.Response(
        text=_page(
            "Privacy Policy",
            _PRIVACY_BODY.format(org=ORGANIZATION, contact=CONTACT),
            "/terms", "Terms of Service",
        ),
        content_type="text/html",
    )


def add_routes(app):
    """Attach the legal pages to an existing aiohttp application."""
    app.router.add_get("/terms", handle_terms)
    app.router.add_get("/privacy", handle_privacy)
    print("[Web] Legal pages mounted at GET /terms and GET /privacy")
    return app
