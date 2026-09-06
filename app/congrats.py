"""Pick a congrats image/gif for challenge completion posts.

Folder layout under CONGRATS_DIR (default ./completion_pictures):

  easy/ medium/ hard/ ota_master/   tier pools
  content/<slug>/                  per-title pools (one-piece, steins-gate, …)
  *                                root = general fallback

Selection order: content match → tier → general.
Within a pool, prefer least-used paths (SQLite congrats_usage).
"""

from __future__ import annotations

import logging
import random
import re
import unicodedata
from pathlib import Path
from typing import Optional

from app.db import db_session, utcnow_iso

logger = logging.getLogger(__name__)

_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# Discord bot attachments are unreliable above ~8MB on many guilds
_MAX_BYTES = 8_000_000

_TIER_DIRS = frozenset({"easy", "medium", "hard", "ota_master", "content"})  # noqa: F841

# Extra tokens so focus titles hit content folders / filenames
_ALIASES: dict[str, tuple[str, ...]] = {
    "my-hero-academia": ("mha", "boku-no-hero", "allmight", "all-might"),
    "one-piece": ("luffy",),
    "steins-gate": ("steinsgate", "okabe"),
    "gurren-lagann": ("ttgl", "gurren"),
    "one-punch-man": ("opm", "saitama"),
    "lucky-star": ("konata",),
    "haruhi": ("suzumiya",),
    "fate": ("tohsaka", "rin", "stay-night"),
    "frieren": ("sousou",),
    "dungeon-meshi": ("delicious-in-dungeon", "laios"),
    "berserk": ("guts",),
    "attack-on-titan": ("aot", "shingeki", "snk"),
    "jujutsu-kaisen": ("jjk", "gojo"),
    "demon-slayer": ("kimetsu", "tanjiro"),
    "spy-x-family": ("spyxfamily", "anya"),
    "chainsaw-man": ("csm", "denji"),
    "death-note": ("light-yagami",),
    "fullmetal-alchemist": ("fma", "edward-elric"),
    "hunter-x-hunter": ("hxh", "gon"),
    "dragon-ball": ("goku", "dbz"),
    "bocchi-the-rock": ("bocchi", "gotoh"),
    "oshi-no-ko": ("hoshino",),
    "evangelion": ("eva", "shinji", "asuka"),
    "re-zero": ("rezero", "emilia"),
    "konosuba": ("kazuma",),
    "jojo": ("jotaro", "jojo-s"),
    "mob-psycho": ("mob-psycho-100",),
    "solo-leveling": ("sung-jinwoo",),
    "naruto": ("sasuke", "uzumaki"),
    "bleach": ("ichigo",),
    "vinland-saga": ("thorfinn",),
    "violet-evergarden": ("violet",),
    "clannad": ("nagisa",),
    "cowboy-bebop": ("spike-spiegel", "bebop"),
}


def congrats_root() -> Path:
    import os

    raw = (os.environ.get("CONGRATS_DIR") or "./completion_pictures").strip()
    return Path(raw)


def slugify(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[;'’].*$", "", t)  # Steins;Gate → steins
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:64]


def _is_media(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in _EXTS:
        return False
    try:
        return 0 < path.stat().st_size <= _MAX_BYTES
    except OSError:
        return False


def _iter_dir_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    out: list[Path] = []
    try:
        for p in folder.iterdir():
            if _is_media(p):
                out.append(p.resolve())
    except OSError:
        pass
    return out


def _content_dirs(root: Path) -> list[Path]:
    base = root / "content"
    if not base.is_dir():
        return []
    try:
        return [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []


_STOP = frozenset(
    {
        "one",
        "man",
        "the",
        "and",
        "boy",
        "girl",
        "anime",
        "manga",
        "chan",
        "kun",
        "san",
        "ok",
        "thumbs",
        "up",
        "cool",
        "image",
        "maxresdefault",
        "sddefault",
    }
)


def _tokens(text: str) -> set[str]:
    s = slugify(text)
    parts = {p for p in s.split("-") if len(p) >= 4 and p not in _STOP}
    if s and s not in _STOP:
        parts.add(s)
    words = [w for w in s.split("-") if w]
    if len(words) >= 2:
        ac = "".join(w[0] for w in words if w)
        if len(ac) >= 3:
            parts.add(ac)
    # Only attach aliases when this text IS that title (not every filename)
    for key, extras in _ALIASES.items():
        if s == key or s in extras or key == s:
            parts.add(key)
            parts.update(extras)
        elif any(e == s for e in extras):
            parts.add(key)
            parts.update(extras)
    return parts


def _match_score(want: set[str], slug: str, name_stem: str = "") -> int:
    """Higher = better content hit. 0 = no match."""
    if not want:
        return 0
    have = _tokens(slug) | _tokens(name_stem)
    # full slug equality is strongest
    if slug and slug in want:
        return 100
    overlap = want & have
    if not overlap:
        return 0
    # prefer longer token hits (piece, steins, academia)
    return max(len(t) for t in overlap)


def _walk_media(root: Path) -> list[Path]:
    """All media under root (tier + content + general), max depth 3."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    try:
        for p in root.rglob("*"):
            if p.is_file() and _is_media(p):
                # stay inside congrats tree
                out.append(p.resolve())
    except OSError:
        pass
    return out


def _content_matches(root: Path, content: str) -> list[Path]:
    """Images from content/<slug>/ dirs or filenames that match focus title."""
    if not content or not content.strip():
        return []
    want = _tokens(content)
    slug = slugify(content)
    if slug:
        want = set(want) | {slug} | _tokens(slug)
    if not want:
        return []

    scored: list[tuple[int, Path]] = []
    seen: set[Path] = set()

    for d in _content_dirs(root):
        d_slug = slugify(d.name)
        sc = _match_score(want, d_slug)
        if sc <= 0 and slug and (slug == d_slug or slug.startswith(d_slug) or d_slug.startswith(slug)):
            sc = 80
        if sc > 0:
            for p in _iter_dir_images(d):
                if p not in seen:
                    seen.add(p)
                    scored.append((sc, p))

    for p in _walk_media(root):
        if p in seen:
            continue
        sc = _match_score(want, slugify(p.stem), p.stem)
        if sc > 0:
            seen.add(p)
            scored.append((sc, p))

    if not scored:
        return []
    best = max(s for s, _ in scored)
    # keep top tier of scores only (avoid weak secondary hits)
    return [p for s, p in scored if s >= best]


def _tier_pool(root: Path, tier: str) -> list[Path]:
    t = (tier or "").lower().strip().replace("-", "_")
    if t == "ota-master":
        t = "ota_master"
    return _iter_dir_images(root / t)


def _general_pool(root: Path) -> list[Path]:
    """Root-level media only (not inside tier/content subdirs)."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for p in root.iterdir():
            if p.is_file() and _is_media(p):
                out.append(p.resolve())
    except OSError:
        pass
    return out


def _usage_map(paths: list[Path]) -> dict[str, int]:
    if not paths:
        return {}
    keys = [str(p) for p in paths]
    with db_session() as conn:
        # Ensure table exists (init_db should have created it)
        try:
            rows = conn.execute(
                f"SELECT path, use_count FROM congrats_usage "
                f"WHERE path IN ({','.join('?' * len(keys))})",
                keys,
            ).fetchall()
        except Exception:  # noqa: BLE001
            return {k: 0 for k in keys}
    return {r["path"]: int(r["use_count"]) for r in rows}


def _mark_used(path: Path) -> None:
    key = str(path.resolve())
    now = utcnow_iso()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO congrats_usage(path, use_count, last_used) VALUES(?, 1, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "use_count = use_count + 1, last_used = excluded.last_used",
            (key, now),
        )


def pick_congrats(
    *,
    tier: str = "",
    content: str | None = None,
    mark: bool = True,
) -> Optional[Path]:
    """Return a congrats image path, or None if the library is empty."""
    root = congrats_root()
    if not root.is_dir():
        logger.debug("congrats dir missing: %s", root)
        return None

    pools: list[list[Path]] = []
    content_hits = _content_matches(root, content or "")
    if content_hits:
        pools.append(content_hits)
    tier_hits = _tier_pool(root, tier)
    if tier_hits:
        pools.append(tier_hits)
    general = _general_pool(root)
    if general:
        pools.append(general)

    if not pools:
        return None

    # Use the highest-priority non-empty pool; if every image there is
    # exhausted equally, still pick from it (cycles after unique pass).
    for pool in pools:
        # de-dupe
        uniq = list(dict.fromkeys(pool))
        usage = _usage_map(uniq)
        counts = [usage.get(str(p), 0) for p in uniq]
        min_c = min(counts)
        least = [p for p, c in zip(uniq, counts) if c == min_c]
        choice = random.choice(least)
        if mark:
            try:
                _mark_used(choice)
            except Exception as exc:  # noqa: BLE001
                logger.warning("congrats usage track failed: %s", exc)
        return choice
    return None


def as_discord_congrats(path: Path):
    """discord.File for a local congrats image."""
    import discord

    return discord.File(str(path), filename=path.name)
