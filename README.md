# Challengebot

Discord bot + admin UI for yearly immersion challenges. Users log on **tadoku.app**; the bot verifies clears against Tadoku when possible.

The current repository is [LucidPublicGit/challengebot](https://github.com/LucidPublicGit/challengebot).
`ChallengeTracker` is the former repository name. The Mac mini checkout is
`/Users/lucid/DevEnvPublic/challengebot`; its compatibility path
`/Users/lucid/DevEnv/ChallengeTracker` points to that checkout.

For the Mac mini deployment, run Docker Compose from the current checkout with
`docker compose -f docker-compose.yml -f compose.mac.yaml up -d --build`.

## Features

- Slash commands: list / start / status / confirm / abandon / link / leaderboard
- **Tadoku-required** confirm for measurable goals; **honor** only for `verify: honor` (e.g. full novel)
- Exclusive Daily slot (positive window up to 24h), concurrent distinct Monthly/Yearly challenges with personal UTC anniversaries; evergreen Specials retain custom windows
- Versioned catalog editions, safe retirement/history, captured attempt rules and audited scoring adjustments
- Up to three duration badges: current-year Daily 日 sweep, lifetime Monthly 月 and Yearly 年
- SQLite durability (`./data`), survive restarts
- Admin web UI: attempts, users, challenges, settings (roles, contest id, re-attempt, announce channel)
- Pixel Arcade achievements: dated unlocks, hidden-artwork stubs, and editable admin definitions
- Docker Compose

## Quick start

```powershell
cd challengebot
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

1. `/challenge link` → paste your Tadoku UUID or profile URL.
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
- Retired challenges remain resolvable for history and announcements; retirement blocks new attempts, not valid in-flight completion. Published entries are not hard-deleted.
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
- The 18 approved definitions live in [`achievements/defaults.yaml`](achievements/defaults.yaml)
  with explicit permanent IDs, rules, artwork filenames, visibility, and order.
  The versioned YAML is validated and seeded once. Later restarts do not overwrite
  edits or restore removed definitions. Rules support distinct clears, media
  format counts, and one clear from each configured group of challenge IDs.
  Group rules explicitly list IDs; update them when adding relevant challenges.
  Failures, abandoned attempts, and repeats never advance clear milestones.
- **Export / import achievement catalog** on the admin Achievements page downloads
  the live definitions as YAML (including removed entries, never member activity).
  SQLite stays authoritative: editing the default YAML does not change an existing
  database. To apply changes, explicitly import a reviewed catalog.
  Imports require confirmation and merge atomically by ID; omitted entries are
  untouched, `enabled: false` hides an entry, and `enabled: true` can restore it.
  Rules, challenge IDs, and artwork are validated before any writes. Invalid files
  make no changes. Catalogs are UTF-8 YAML, up to 512 KB / 1000 entries.
  The export does not bundle images; back up uploaded PNGs separately and make
  referenced artwork available on the destination before importing. Keep IDs
  unchanged when renaming, reordering, or retiring achievements.
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
| `/challenge admin user:` → Probe Tadoku link | Inspect the selected member's binding and recent real logs |
| Admin panel → Preview real (dry) | Diagnostic confirmation with real logs, without changing attempt status |
| Admin panel → Fake pass / Fake fail (dry) | Synthetic diagnostics only; cannot issue completions or points |
| Admin panel → Review completion attestation | Enter attempt ID, actual completion time, reason and optional work; review effects before applying |
| Admin panel → Void attempt id | Reason and reviewed correction preview before voiding |

Diagnostics never award clears and do not activate/expire global attempts or persist matching edits. Synthetic `apply=True` and direct forced/synthetic confirmations are rejected. Administrative attestation preserves evidence, rejects future dates, and requires a separate explicit reason for intentional window exceptions. Apply rechecks the preview author's current admin permissions. It does not automatically announce or grant external rewards; old synthetic award history is retained.

## Settings (UI or env)

Admin **Settings** page (DB overrides env). Highlights:

| Area | Keys |
|------|------|
| Core | `tadoku_contest_id`, channels, `allow_reattempt`, `allow_honor_override`, pages/episode, rewards |
| Status | milestones on/off, fracs (`25,50,75`), long-window days, poll/interval, final-hour public/DM, auto-complete |
| Cards | per-kind announce toggles (`announce_fail` **default off**), congrats / new-challenger / shame, highlight kinds |
| Digest | enabled, hour, timezone |

## New year

1. Publish `challenges/2027.yaml` with **new edition IDs** and explicit `identity_id` links for renewals; never move an existing edition ID to another year or infer identity from its title.
2. Set publication/availability deliberately. Annual eligibility returns only for a published, available new edition; retirement does not undo itself on January 1.
3. Admin → Challenges → merge YAML. Supply a reason and review the captured definition/roster impact before applying. Omitted entries/fields remain unchanged; retire editions explicitly. Startup seeds only missing entries, preserving admin edits and retirement.
4. Existing attempts retain their original edition and deadline across January 1. Special definitions are evergreen and are not regenerated.

## Lifecycle migration and scoring

Read [the implementation handoff](docs/challenge-lifecycle-and-badges-implementation.md) before deploying this schema change. Back up SQLite using its backup API plus catalog/artwork assets, rehearse on a copy, and compare private before/after reports. Do not run a live migration just to inspect the schema.

The lifecycle admin page provides authenticated scoring previews and audited apply/revert operations. `points: null` inherits the duration formula. Daily uses its difficulty base (1/2/3/4); Weekly/Monthly/Yearly use `ceil(multiplier × (base + 1) / 2)` with multipliers 2/3/4, giving Yearly awards of 4/6/8/10; an integer is an explicit absolute override. Weekly has a personal seven-day window; Monthly/Yearly use clamped calendar anniversaries. Global and selected edition/identity/year formulas can be reviewed before applying. On upgrade, the revised formula includes applicable open long attempts while retaining prior snapshots and completed awards. Retroactive adjustments retain original awards and signed ledger deltas. A score change does not change completion-based badges. Headers show 日/週/月/年: current-year Daily and lifetime Weekly/Monthly/Yearly.

Catalog YAML merges also require a captured reason/preview/apply workflow. Paste external YAML or choose a bounded source inside `challenges/`; apply never reopens a mutable file. Existing point overrides cannot be silently changed/cleared, and batches publish only final roster sets, not intermediate requirements.

Historical Daily roster repairs are available under `/lifecycle/rosters` with member-progress/bonus previews and audited apply. Ordinary catalog changes and retirement/restoration also require reviewed impact previews; future lifecycle boundaries are forecast separately without granting awards early. Scheduled restoration and explicit cancellation are supported. Discord catalog and void controls now collect an audit reason and offer a private, complete preview before applying.

Tests install a collection-time guard restricting SQLite to memory/OS temporary paths and skip the repository runtime `.env`. Keep test fixtures disposable; never point tests at a live database.

Tadoku fetches are bounded. Incomplete pages, unavailable endpoints, and uncertain log timestamps produce **incomplete evidence**, not authoritative failure or partial progress. Long-window verification uses user-wide logs rather than assuming one contest covers a month/year. Honor-based complete-work challenges, including Tales/Trails, do not depend on Tadoku.

## Layout

```text
app/          bot + FastAPI admin + verify + tadoku client
challenges/   YAML catalogs
achievements/ versioned default achievement catalog
data/         SQLite volume (gitignored)
```

Single container / single SQLite writer — do not scale replicas.

## Sharing and security

See [SECURITY.md](SECURITY.md) for deployment and disclosure guidance.
Never share `.env`, `data/`, database backups, logs, agent sessions, or demo previews.
Only share a sanitized release snapshot, not the original development Git history.

## Tests (local, no Discord token required)

```powershell
cd challengebot
python tests/run_all.py
# or: pip install pytest && python -m pytest tests -q
```

Covers goal math, 2026 catalog seed, attempt lifecycle, expire, honor confirm,
admin fake/force dry-run + apply, and mocked real-Tadoku preview.
