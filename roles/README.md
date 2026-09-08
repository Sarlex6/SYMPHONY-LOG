# Role Management / Cross-Platform Synchronization

Groundwork stage. The architecture is complete and exercised; the **data** —
ranks, branches, server IDs, role IDs, Roblox mappings, credentials — is not
supplied yet and is represented by placeholders throughout.

Nothing in this package invents an ID, a rank name, a role mapping or a
permission rule. Where a value is missing, the dependent behavior is **disabled**
rather than guessed.

---

## Source of truth

The **PERSONNEL** sheet (5th tab of the existing inventory spreadsheet) is
authoritative. Discord and Roblox conform to it, never the reverse.

```
Discord command ─┐
                 ├─> RoleManagerService ─> Google Sheets ─> SyncQueue ─> Discord
Angela ──────────┘         (authoritative write)              │           Roblox
                                                              └─> retries, reports
Manual sheet edit ─> SheetPoller ─> (same SyncQueue)
```

A change detected in the sheet is authoritative regardless of where it came
from. A Discord or Roblox failure is retried and reported; it can never write
back to the sheet.

---

## Modules

| Module | Responsibility |
|---|---|
| `columns.py` | Physical sheet layout. The only place a column letter or row number is hardcoded. |
| `models.py` | `UserRecord`, enums, `ActionContext` / `ActionResult`. |
| `config.py` | Ranks, branches, servers, permissions, sync tuning. Loads `roles_config.json`. |
| `sheets_gateway.py` | Raw worksheet I/O. Wraps blocking gspread in `asyncio.to_thread` + a write lock. |
| `repository.py` | Row ↔ record translation, the authoritative snapshot, change detection. |
| `layout.py` | Dynamic row/category placement and sorting. Pure functions. |
| `permissions.py` | **Centralized authorization.** The only place an action is allowed or refused. |
| `service.py` | `RoleManagerService` — the single mutation path. |
| `sync_queue.py` | Retryable outbound sync with backoff and a persisted dead-letter list. |
| `discord_sync.py` | Rank → main role + auxiliary roles, rank+branch → branch role. |
| `roblox_sync.py` | rank+branch → group roles, via Open Cloud v2 multi-role. |
| `roblox_oauth.py` | Discord → Roblox account verification, **registration only**. |
| `oauth_routes.py` | The OAuth callback, mounted on the existing aiohttp server. |
| `rover.py` | Legacy verification fallback. |
| `poller.py` | Background change detection (30–60s, configurable). |
| `commands.py` | Slash commands. Thin — no permission logic, no sheet access. |
| `bot.py` | Discord client host (`ROLES_TOKEN`), or `attach_to()` an existing client. |
| `angela_bridge.py` | Angela's interface. Proposes actions; holds no authority. |
| `timezones.py` | Canonical timezone format. |
| `loa.py` | LOA storage — **format deliberately undefined**. |

---

## Sheet contract

Managed area starts at **row 10**. Nothing at or above row 9 is ever written.

| Col | Content | System behavior |
|---|---|---|
| A, B, I, M | design | never written — not in `WRITABLE_COLUMNS` |
| C | CATEGORY | written; derived from the rank's configured category |
| D | IDENTIFICATION | written; Discord **username**, display only |
| E | TIMEZONE | written; canonical IANA identifier |
| F | RANK | written as the configured *display* string; dropdown managed |
| G | BRANCH | written as the configured *display* string; dropdown managed |
| H | NOTES | **never authored** — carried with the record when rows move |
| J | STATUS | written; ACTIVE / SEMI-ACTIVE / IN-ACTIVE / EMPTY |
| K | DATE OF ENTRY | `MM.DD.YYYY`, written once at registration, never again |
| L | VERIFICATION | `AUTOMATIC EXECUTIVE SYSTEM` on system changes |
| N, O | gap | never written |
| P–X | technical | Discord ID, Roblox ID, record UID, sync hash, sync status, last sync, revision, LOA, source — hidden, 40px |

The visual footer row (initially 113) stays a design boundary. Inserting managed
rows above it pushes it down, so its position is read live rather than hardcoded.

**Row moves preserve formatting.** A re-layout writes managed *values* into
managed *rows* in the target order. It never sorts or moves the physical rows, so
every design cell, data validation rule and conditional format stays attached to
its row.

---

## Assumptions made (all configurable)

These were not specified. Each is a deliberate choice with a stated reason, and
each is a one-line change if you want it different.

1. **Column H travels with the record** (`columns.MOVE_NOTES_WITH_RECORD`).
   The system never authors note text, but when a person relocates their note
   moves with them — otherwise notes silently reattach to whoever lands on that
   row. Set `False` to pin notes to physical rows instead.

2. **Column L is provenance, not authority.**
   The system stamps `AUTOMATIC EXECUTIVE SYSTEM` when *it* changes a record.
   When the poller detects a **manual** edit, it does **not** overwrite L — that
   would erase the evidence a human made the change. The manual value is kept and
   technical column X records `MANUAL`. Manual edits remain fully authoritative;
   authority is never read from L.

3. **Timezone canonical format = IANA identifier** (`Europe/Prague`).
   Chosen over free text and over UTC offsets: unambiguous (`CST` is three
   different zones), survives daylight saving, and validates against stdlib
   `zoneinfo`. `normalize()` accepts abbreviations and offsets and resolves them;
   genuinely ambiguous input returns candidates for the user to pick rather than
   a guess.

4. **Permissions fail closed.** Any permission without a configured rule is
   denied, and the denial says which rule is missing.

5. **Rank is authoritative over category.** Column C is a rendering of the rank's
   configured category, so a rank change relocates the person automatically.

6. **`SLACK_ROWS = 0`** — exact fit, so no EMPTY rows accumulate. Raise it to
   trade a few EMPTY rows for fewer insert/delete API calls.

---

## Removal = revocation

A record does not vanish from the sheet on its own. If it is deleted, the person
is no longer personnel, so their access goes with the record and re-entry
requires `/register` again.

**Discord** (`sync.on_removal_strip_discord`, default `true`): every managed role
is removed, in the main server **and every configured branch server** — not just
their current branch, since the record's last branch is not a reliable guide to
which servers still carry roles from an earlier one. Unmanaged roles are still
left alone; this revokes personnel access, it does not wipe the member.

**Roblox** works the same way, via `unassignRole`:

| `sync.on_removal_roblox` | Behavior |
|---|---|
| `UNASSIGN` (default) | Unassign every managed role, in every managed group |
| `SET_ROLE` | Unassign the managed roles, then leave `removal_roblox_role_id` in the main group as a "removed" marker |
| `IGNORE` | Leave Roblox roles alone |

Built-in roles (Owner / Member / Guest) are never touched, so a stripped member
falls back to plain Member rather than being removed from the group. Unmanaged
roles survive, exactly as on Discord.

**Durability.** The row is already gone when the revocation is detected, so a
restart mid-strip would lose it — the next poll has nothing left to diff against.
Removal jobs therefore carry their own record snapshot and are persisted to
`data/roles_pending_removals.json` the moment they are created, replayed on
startup, and only cleared once the strip succeeds. A revocation that fails
permanently stays in that file *and* the dead-letter list, and `/roles status`
reports it as an incomplete revocation — an unfinished access revocation should
be loud, not filed away. If the record reappears on the sheet before the job
runs, the revocation is cancelled rather than applied to a current member.

---

## Account verification (registration)

`registration.verification_method` selects how `/register` proves that a Discord
user owns a Roblox account:

- **`OAUTH`** (default) — Roblox OAuth 2.0 / OIDC. Asks the user directly.
- **`ROVER`** — the legacy registry lookup, which additionally requires each user
  to opt in to Rover sharing their link with third parties.

Either way, verification happens **once**. The Discord ID and Roblox ID land in
the technical columns and are canonical from then on.

### The OAuth flow

```
/register ──> begin() ──> authorization URL  (delivered PRIVATELY)
                               │
                 user authorizes on roblox.com
                               │
                               v
        GET /roblox/callback?code=..&state=..  ──> complete()
                               │
                 token exchange (PKCE) + userinfo
                               │
                               v
              RoleManagerService.complete_registration()
```

Endpoints, per the OIDC discovery document:

| | |
|---|---|
| authorize | `GET https://apis.roblox.com/oauth/v1/authorize` |
| token | `POST https://apis.roblox.com/oauth/v1/token` |
| userinfo | `GET https://apis.roblox.com/oauth/v1/userinfo` |

Only `openid` and `profile` are requested — `sub` is the Roblox user ID and
`preferred_username` the username, which is everything needed. Asking for more
would mean a scarier consent screen for no gain. The access token is revoked
immediately after userinfo; nothing here needs standing account access.

### Why the link must stay private

**Whoever opens the authorization link binds *their* Roblox account to the
requesting Discord ID.** That is the entire point of the `state` binding, and
also its sharp edge. So the URL is returned in `ActionResult.detail`, never in
`message` — an interface has to deliberately reach for it:

- `/register` replies ephemerally.
- Angela **DMs** it, and refuses (telling the user to use `/register`) if their
  DMs are closed. She never posts it in a channel.

### Security properties

- `state` is 256 bits of CSPRNG entropy, single-use, expiring
  (`state_ttl_seconds`, default 600) and bound to one Discord ID. The callback
  never reads a Discord ID from the query string, so a crafted URL cannot target
  someone else's account.
- PKCE S256 always. The verifier stays server-side, so an intercepted code is
  useless.
- Re-running `/register` invalidates the previous link — one live link per user.
- A failed exchange consumes the state, closing the binding window rather than
  leaving it open for retry.
- **Every safeguard is re-run at completion**, not just at `/register`: the
  permission check, the main-guild check, the already-registered check and the
  duplicate-Roblox-account check. Time passes while the browser tab is open, and
  the tests cover both races (registered by another route in between; the Roblox
  account claimed in between).

Pending verifications live in memory. A restart drops them, costing the user one
re-run — not worth a persistence layer for a ten-minute window.

### Setup

1. Register an OAuth app on the Creator Dashboard; add
   `https://YOUR-HOST/roblox/callback` as a redirect URL (plain HTTPS, or
   `http://localhost` for local debugging; max 256 chars, up to 10 URLs).
2. Set `ROBLOX_OAUTH_CLIENT_ID` and `ROBLOX_OAUTH_CLIENT_SECRET` in the
   environment, and `roblox_oauth.redirect_uri` in the config to the same URL.
3. **Submit the app for review.** A new app runs in *private mode, limited to 10
   unique users*, and approval is one-way — it cannot be reverted to private.

The callback is mounted on the aiohttp server that already runs in
`assistant/web.py`, so there is one web server for the process. It is public by
necessity but carries no authority: without a valid `state` it can only render an
error page.

---

## Roblox role model

Roles are **additive**. The rank's `N/A` entry is the baseline and is always
applied; the branch entry is applied on top. A `TRAINING_ASSOCIATE` in `ECHELON`
holds both the N/A role and the ECHELON role.

Applied with the current Open Cloud multi-role endpoints — **not** the deprecated
`PATCH /memberships/{id}` (UpdateGroupMembership), which modelled membership as a
single rank:

```
POST {base}/groups/{gid}/memberships/{mid}:assignRole     {"role": "groups/{gid}/roles/{rid}"}
POST {base}/groups/{gid}/memberships/{mid}:unassignRole   {"role": "groups/{gid}/roles/{rid}"}
```

Both are idempotent and leave the member's other roles alone. There is no batch
variant, so N roles is N sequential calls — `roblox.request_delay` (default
0.25s) spaces them under the 300/min per-key limit.

Reconciliation per group is the same shape as the Discord side:

```
assign   = desired − current
unassign = (managed ∩ current) − desired
```

`managed` is every role ID the configuration owns in that group, so a role
granted for an unrelated reason is never removed.

**Writes are verified.** A key lacking `group:write` can return 200 without
applying anything, so after mutating, the membership is re-read and compared. A
change that did not stick is reported as a failure naming the likely scope
problem — not as a success. Reads accept both the `roles` array and the legacy
singular `role` field.

Roles live in the main group by default. If a branch has its own Roblox group,
set `branches.<KEY>.roblox_group_id`, or give a single entry an explicit group:
`"ECHELON": {"role_id": 1, "group_id": 12345}`.

`roblox_sync.list_group_roles(group_id)` fetches a group's roles with their IDs
and display names, for filling in the mappings.

---

## Concurrency and race conditions

- One `asyncio.Lock` (`sheets_gateway.write_lock`) covers every read-modify-write
  cycle. Commands and the poller cannot interleave.
- Every record carries a monotonic `revision`, bumped on each system write.
- A sync job records the revision it was built from. Before running, it re-reads
  the record; if the snapshot has a **higher** revision, the job is dropped —
  a newer job already covers it. This is what stops a stale poll result from
  overwriting newer state.
- The poller skips a cycle entirely while a mutation holds the lock, so it never
  reads a half-written layout.
- `service._commit` re-reads under the lock and re-applies only the fields the
  command intended to change, so a concurrent edit to an unrelated field survives.

---

## Angela is an interface, not an authority

Enforced structurally, not by prompt instruction:

- `execute()` takes `actor_discord_id` from `message.author.id`. The model never
  supplies it and there is no parameter through which it could.
- `ProposedAction` carries an action name and arguments — and nothing else. There
  is no field for a rank, a category, a permission or an override, so a model
  that concludes "this user is a high rank" has nowhere to put that conclusion.
- Execution calls the same `RoleManagerService` methods the slash commands call,
  which run the same `permissions.check()`. An Angela request and a typed command
  are indistinguishable to the authorization layer.
- `ACTION_SPECS` is a closed catalogue. An action outside it is rejected before
  anything runs.
- Angela's remaining job is conversational: work out intent, ask when unclear,
  and report the outcome in her own voice. She cannot soften a refusal into a
  success — she narrates the `ActionResult` she is given.

### What Angela is told about herself

`persona.MANAGEMENT_CAPABILITIES` is the model-facing description of this
capability, injected into her system prompt by `gemini.generate_response()` via
`angela_bridge.capability_briefing()`. It appends the **live** configured rank
and branch names, so the names she quotes are ones that actually exist.

It returns `""` while the system is unconfigured — she is not told she can do
this until she genuinely can, or she would promise members actions that cannot
run.

The rules it sets are the conversational half of the security model:

1. She does not decide authorization and must never call someone "authorized".
2. The sheet is the record; she submits requests against it.
3. **She must never claim an action was performed unless the system reported back
   that it succeeded.** This is the important one — see below.
4. A refusal is stated plainly, with its reason, no workarounds offered.
5. Acting on someone else requires a real `@mention`; no guessing at names.
6. Rank/branch names must match the configured lists; no closest-match guessing.

Rule 3 exists because of a specific failure mode. `_INTENT_HINTS` is a regex gate
that decides whether a message is routed to the Role Manager at all. If it misses
a genuine request, the message falls through to normal conversation — where
Angela now knows she has these powers and could describe an action that never
ran. The gate is therefore deliberately wide (a false positive costs one cheap
flash-lite call; a false negative costs a fabricated success), with rule 3 as the
backstop.

---

## Setup

1. `cp roles_config.example.json roles_config.json` and fill in real values.
   The loader reads `roles_config.json`; `ROLES_CONFIG_PATH` overrides that
   location and is otherwise unnecessary.
2. Add secrets to `config.json` / environment:
   `ROLES_TOKEN`, `ROBLOX_API_KEY`, `ROBLOX_OAUTH_CLIENT_ID`,
   `ROBLOX_OAUTH_CLIENT_SECRET` (plus `ROVER_API_KEY` only if you switch
   `registration.verification_method` back to `ROVER`).
3. Start the process. Without `ROLES_TOKEN` the roles bot is skipped entirely and
   nothing else changes.
4. `/roles status` reports what is still missing. `/roles reload` re-reads the
   config without a restart.

Prefer not to run a separate bot? Call `roles.commands.register_commands(tree)`
against an existing `CommandTree` and `roles.bot.attach_to(client)` from that
bot's `on_ready`. Note the role manager must be a member of the main server *and*
every branch server.

---

## Still to be supplied

Everything below is a placeholder. `/roles status` prints the live version of
this list.

- 14 normal rank definitions + Owner/CEO + special ranks (names, order, categories)
- Special-rank group ordering (`special_group_order`)
- `default_registration_rank`
- Main Discord server ID and per-rank role IDs
- Branch names, branch server IDs, branch role IDs
- Roblox group ID and rank+branch → group role mappings
- `ROLES_TOKEN`, `ROBLOX_API_KEY`, and `ROBLOX_OAUTH_CLIENT_ID` / `ROBLOX_OAUTH_CLIENT_SECRET`
- Roblox OAuth app approval (private mode caps at 10 unique users)
- **Every permission rule** — the hierarchy is entirely undefined and all
  authorization is currently denied
- **The `#/#/#` LOA format** — see `loa.py`; only the shape is validated, no
  meaning is assigned to the three fields
- `on_removal_roblox` — decide whether removed users should be demoted in the
  Roblox group (`SET_ROLE` + `removal_roblox_role_id`) or left alone (`IGNORE`)
