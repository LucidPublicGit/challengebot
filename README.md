# ChallengeTracker

Discord bot + admin UI for yearly immersion challenges. Users log on **tadoku.app**; the bot verifies clears against Tadoku when possible.

## Features

- Slash commands: list / start / status / confirm / abandon / link / leaderboard
- **Tadoku-required** confirm for measurable goals; **honor** only for `verify: honor` (e.g. full novel)
- One active challenge per user, rolling timer (default 24h)
- SQLite durability (`./data`), survive restarts
- Admin web UI: attempts, users, challenges, settings (roles, contest id, re-attempt, announce channel)
- Pixel Arcade achievements: dated unlocks, hidden-artwork stubs, and editable admin definitions
- Docker Compose

## Quick start

```powershell
cd ChallengeTracker
copy .env.example .env
# Edit .env: DISCORD_TOKEN (required), DISCORD_GUILD_ID (recommended)
# Set UI_PASSWORD to a strong unique password (required for admin UI access).
# optional: ANNOUNCE_CHANNEL_ID, ADMIN_ROLE_IDS, DISCORD_CLIENT_ID

.\rebuild.bat          # build + start (after code changes)
# .\start.bat          # start only
# .\status.bat         # ps + health + recent logs
# .\logs.bat           # follow logs
# .\stop.bat           # compose down
```

Scripts live under `scripts\*.ps1`; the `.bat` wrappers bypass execution policy.

**Never commit `.env`.** Only `.env.example` is tracked.

- Bot: Discord slash commands (guild-synced when `DISCORD_GUILD_ID` set)
- Admin UI: http://localhost:8090 (HTTP Basic required; disabled without `UI_PASSWORD`).
  Docker publishes only to loopback. For remote administration use an SSH tunnel
  or an authenticated HTTPS reverse proxy, never expose plain HTTP Basic to the internet.
  Proxy deployments must preserve the browser-facing Host and scheme for same-origin
  write checks; trust forwarded headers only from the proxy.

### Discord app

1. Create application → Bot → token  
2. Privileged intent: **Server Members** (for reward roles)  
3. OAuth2 scopes: `bot`, `applications.commands`  
4. Bot perms: Send Messages, Create Private Threads, Send Messages in Threads, Manage Roles (if rewarding), Use App Commands  
5. Installation → enable **Guild Install**  
6. Optional `ADMIN_ROLE_IDS` (guild owner + Administrator always allowed)

### User flow

1. `/challenge link user_id:<tadoku uuid or profile url>`  
2. `/challenge list` → `/challenge start` (e.g. Easy #4 VN 30k — any VNs)  
3. Log immersion on tadoku.app inside the window  
4. `/challenge confirm` — bot pulls Tadoku logs immediately (no form); edit labels later with `/challenge content`  

### Catalog export and announcements

- `/challenge dump`: anyone can export **all years** as plain text, without buttons
  or embeds. Includes a `.txt` attachment for copying the entire catalog; optional `year`.
  Set `catalog:Achievements` for published achievement IDs and requirements instead.
- `/challenge announce`: admins can announce **new**, **updated**, or **retired**
  challenges. It does not add, edit, or delete catalog entries.
- Choose one `challenge_id` from the dump, or a `category` (Physical, Ota, All).
  Category announcements default to the latest year; optional `year` selects another.
  Add `message` for context and `title` for a custom heading.
- For challenges already removed, omit `challenge_id`/`category` and put their
  names and details in `message`. Announce removals before deleting to use catalog details.
- `image` selects a saved Physical banner in `app/images/announcements/`
  (the approved Japanese-only banner is option 6);
  `banner` accepts an uploaded image for future announcements (use one or the other).
- Default is a **private preview**. Run again with `publish:True` to post to the
  configured announcement channel, or specify `channel` in this server.
  Announcements never ping roles or everyone. The bot needs Send Messages,
  Embed Links, and Attach Files in the destination.

Example: `/challenge announce change:new category:physical image:6`
then repeat with `publish:True` after reviewing.

### Web announcements

Open **Announcements** in the admin UI (`/announcements`). Start blank for any
news, event, or catalog change, or choose **Physical challenges draft** to load the
current Physical challenges and the approved **体力チャレンジ** image.
Edit the title/message, select no image, the Physical banner, or upload your own,
then choose the Discord destination. The live preview sends nothing.
Check the confirmation box and click **Publish announcement** to post.

The page uses the existing admin authentication; set `UI_PASSWORD` before exposing
the UI. One-use forms expire after 30 minutes and prevent duplicate resubmissions.
If publishing fails partway through, check Discord before retrying.

Choose **Unannounced changes** to draft separate **New**, **Modified**, and
**Removed** sections from the durable catalog history. Successful publication of
that draft marks only its included changes announced; failures leave them pending.
Large batches are split across drafts. Blank and Physical-only drafts do not
automatically acknowledge the change queue.

Catalog edits from the web UI, slash commands, and YAML imports save before/after
definitions in the same transaction. Unchanged saves/imports create no events.
**Change history** retains removed definitions and shows announcement timestamps.
The first import establishes a baseline and records the reported 2026 rollout:
four modified manga-volume challenges and eight newly added Physical challenges.
Previous definitions are explicitly marked unavailable unless recoverable from
the database before that import. Earlier changes cannot be reconstructed.

### Achievements

- Admins can use `/achievement_admin add`, `/achievement_admin edit`, and
  `/achievement_admin remove` in a server. The guild owner, Administrator, and
  configured `ADMIN_ROLE_IDS` are checked on every invocation.
  Add requires `title`, `description`, `rule_json`, and a managed `image_file`
  (e.g. `achievement-001.png`). Edit takes `achievement_id` and optional fields;
  omitted fields are preserved. Remove requires `confirm:True` and keeps history.
  Restore with edit `published:True`. Upload new artwork through the web UI.
  Use `/challenge dump catalog:Achievements` to find published IDs.

- `/challenge userinfo` shows small earned icons and `?` stubs below Category
  progress. **View Achievements** opens a paginated list; `/achievements` also
  opens it directly. The existing `/challenge` group is at Discord's 25-child
  limit, so the new command is standalone.
- Names and requirements are visible in the list. Real artwork is attached only
  after that user earns it. Earned achievements appear newest first, followed
  by stubs in catalog order. There are no achievement status labels or progress
  counters, and leaderboard points are unchanged.
- Open **Achievements** (`/achievements`) in the authenticated admin web UI to
  add, rename, edit requirements/rules, upload replacement artwork, or remove
  an achievement. IDs are permanent and independent of names/images. Removal
  hides the definition while keeping its ID and receipts; change visibility to
  restore it. Set `UI_PASSWORD` before exposing the admin UI.
- The 18 approved definitions are seeded once. Later restarts do not overwrite
  edits or restore removed definitions. Rules support distinct clears, media
  format counts, and one clear from each configured group of challenge IDs.
  Group rules explicitly list IDs; update them when adding relevant challenges.
  Failures, abandoned attempts, and repeats never advance clear milestones.
- Unlocks use the earliest qualifying completion history, not the time a user
  opens the card. Completion and unlock dates are stored in UTC, with a source
  attempt and a snapshot of challenge metadata. Editing a completed attempt
  preserves its date; the attempt form can correct a missing or inaccurate
  completion date. Rule edits and moderation corrections recalculate eligibility.
- Legacy completions with missing timestamps still qualify, but their unlock
  dates remain unavailable rather than fabricated. Backfilled metadata uses the
  catalog currently stored in the database; lost historical metadata cannot be
  reconstructed.
- Uploaded artwork is normalized to a managed PNG. Runtime assets live in
  `app/images/achievements/`; replacements persist in `achievement_images/`
  beside SQLite, inside the existing Docker data volume. Back up that folder
  alongside the database. Locally generated demo cards can be refreshed with
  `python scripts/preview_achievements.py` without touching the real database.

### Admin tests (owner / Administrator / `ADMIN_ROLE_IDS` only)

| Command | Purpose |
|---------|---------|
| `/challenge test link` | Probe linked Tadoku id (or paste UUID) + fetch recent real logs |
| `/challenge test preview` | Dry-run confirm with **real** Tadoku logs (no complete) |
| `/challenge test fake` | Synthetic logs pass/fail; optional `apply` to mark complete as `test_fake` |
| `/challenge test accept` | One-shot: `force` / `fake_pass` / `real` + optional apply/announce/rewards |
| `/challenge admin_force_complete` | Force-complete active attempt (manual) |
| `/challenge admin_void` | Void an attempt by id |

Defaults are **dry-run** (`apply=false`) so you can validate without awarding clears. Fake modes never write to Tadoku.  

## Settings (UI or env)

Admin **Settings** page (DB overrides env). Highlights:

| Area | Keys |
|------|------|
| Core | `tadoku_contest_id`, channels, `allow_reattempt`, `allow_honor_override`, pages/episode, rewards |
| Status | milestones on/off, fracs (`25,50,75`), long-window days, poll/interval, final-hour public/DM, auto-complete |
| Cards | per-kind announce toggles (`announce_fail` **default off**), congrats / new-challenger / shame, highlight kinds |
| Digest | enabled, hour, timezone |

## New year

1. Add `challenges/2027.yaml`  
2. Admin → Challenges → seed path + optional replace  
3. Or edit challenges live in the UI  

## Layout

```text
app/          bot + FastAPI admin + verify + tadoku client
challenges/   YAML catalogs
data/         SQLite volume (gitignored)
```

Single container / single SQLite writer — do not scale replicas.

## Sharing and security

See [SECURITY.md](SECURITY.md) for deployment and disclosure guidance and
[the audit report](docs/security-audit.md) for findings and release limitations.
Never share `.env`, `data/`, database backups, logs, agent sessions, or demo previews.
Only share a sanitized release snapshot, not the original development Git history.

## Tests (local, no Discord token required)

```powershell
cd ChallengeTracker
python tests/run_all.py
# or: pip install pytest && python -m pytest tests -q
```

Covers goal math, 2026 catalog seed, attempt lifecycle, expire, honor confirm,
admin fake/force dry-run + apply, and mocked real-Tadoku preview.
