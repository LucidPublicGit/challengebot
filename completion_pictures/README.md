# Completion congrats pictures

Attached to **challenge complete** announce posts (alongside the PNG card).

## Layout

```
completion_pictures/
  easy/ medium/ hard/ ota_master/   ← tier pools
  content/<slug>/                   ← title-specific (preferred when focus matches)
  *.gif / *.png / …                 ← general fallback (root only)
```

**Pick order:** content match → tier folder → root general.  
Within a pool, least-used files win (usage in SQLite `congrats_usage`).

## Content slugs

Folder name is matched loosely against the attempt focus (aliases in `app/congrats.py`).

Examples: `one-piece`, `steins-gate`, `frieren`, `my-hero-academia`, `jujutsu-kaisen`, …

Each popular title has a generated **CLEARED** card (`cleared-*.jpg`) from AniList cover art.
Add your own reaction gifs too.

Drop any `.png` `.jpg` `.jpeg` `.gif` `.webp` under the right folder.  
Files over **8MB** are skipped (Discord attachment limits).

## Refresh library

```bash
python scripts/fetch_congrats.py
```

Pulls more otakugifs reactions + rebuilds missing series CLEARED cards.

## Docker

`completion_pictures` is bind-mounted read-only — add files on the host, no rebuild.
