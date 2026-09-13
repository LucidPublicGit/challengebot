# ChallengeTracker

Discord bot + admin UI for yearly immersion challenges. Users log on **tadoku.app**; the bot verifies clears against Tadoku when possible.

## Features

- Slash commands: list / start / status / confirm / abandon / link / leaderboard
- **Tadoku-required** confirm for measurable goals; **honor** only for `verify: honor` (e.g. full novel)
- One active challenge per user, rolling timer (default 24h)
- SQLite durability (`./data`), survive restarts
- Admin web UI: attempts, users, challenges, settings (roles, contest id, re-attempt, announce channel)
- Docker Compose

## Quick start

```powershell
cd ChallengeTracker
copy .env.example .env
# Edit .env: DISCORD_TOKEN (required), DISCORD_GUILD_ID (recommended)
# optional: UI_PASSWORD, ANNOUNCE_CHANNEL_ID, ADMIN_ROLE_IDS, DISCORD_CLIENT_ID

.\rebuild.bat          # build + start (after code changes)
# .\start.bat          # start only
# .\status.bat         # ps + health + recent logs
# .\logs.bat           # follow logs
# .\stop.bat           # compose down
```

Scripts live under `scripts\*.ps1`; the `.bat` wrappers bypass execution policy.

**Never commit `.env`.** Only `.env.example` is tracked.

- Bot: Discord slash commands (guild-synced when `DISCORD_GUILD_ID` set)
- Admin UI: http://localhost:8090 (HTTP Basic if `UI_PASSWORD` set)

### Discord app

1. Create application → Bot → token  
2. Privileged intent: **Server Members** (for reward roles)  
3. OAuth2 scopes: `bot`, `applications.commands`  
4. Bot perms: Send Messages, Create Private Threads, Send Messages in Threads, Manage Roles (if rewarding), Use App Commands  
5. Installation → enable **Guild Install**  
6. Optional `ADMIN_ROLE_IDS` (guild owner + Administrator always allowed)

### User flow

1. `/challenge link user_id:<tadoku uuid or profile url>`  
2. `/challenge list` → `/challenge start tier:Easy number:4 param:Some VN`  
3. Log immersion on tadoku.app inside the window  
4. `/challenge confirm` — bot pulls Tadoku logs immediately (no form); edit labels later with `/challenge content`  

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

## Tests (local, no Discord token required)

```powershell
cd ChallengeTracker
python tests/run_all.py
# or: pip install pytest && python -m pytest tests -q
```

Covers goal math, 2026 catalog seed, attempt lifecycle, expire, honor confirm,
admin fake/force dry-run + apply, and mocked real-Tadoku preview.
