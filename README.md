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
4. Bot perms: Send Messages, Manage Roles (if rewarding), Use App Commands  
5. Installation → enable **Guild Install**  
6. Optional `ADMIN_ROLE_IDS` (guild owner + Administrator always allowed)

### User flow

1. `/challenge link user_id:<tadoku uuid or profile url>`  
2. `/challenge list` → `/challenge start tier:Easy number:4 param:Some VN`  
3. Log immersion on tadoku.app inside the window  
4. `/challenge confirm` — bot pulls logs and checks the goal  

## Settings (UI or env)

| Key | Meaning |
|-----|---------|
| `tadoku_contest_id` | Default Deep Weeb Club 2026 |
| `allow_reattempt` | Completing the same challenge again |
| `reward_roles_json` | `{"easy":"roleId","any_clear":"…","clear_5":"…"}` |
| `announce_channel_id` | Public completion messages |
| `pages_per_volume` / `episode_minutes` | Heuristics for manga/anime auto-count |
| `admin_role_ids` | Extra admin roles for `/challenge admin_*` |

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
