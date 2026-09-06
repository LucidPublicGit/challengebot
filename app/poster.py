"""Best-effort cover art for challenge focus (AniList / VNDB / optional MAL).

Inspired by ApathyWorks/TadokuBot poster_client — failures always return None.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 6.0
_MAX_Q = 80
_CACHE: dict[tuple[str, str], Optional[bytes]] = {}
_CACHE_MAX = 128

# Strip volume / episode noise so search hits better.
_NOISE_RE = re.compile(
    r"""
    (?:
        \b(?:vol\.?|volume|v)\s*\d+
        | \b(?:ch\.?|chapter)\s*\d+
        | \b(?:ep\.?|episode|e)\s*\d+
        | \bS\d{1,2}E\d{1,3}\b
        | [〜~\-–—]\s*\d+
        | \d+\s*[〜~\-–—]\s*\d+
    )
    """,
    re.I | re.VERBOSE,
)


def clean_title(title: str) -> str:
    t = " ".join((title or "").split())
    t = _NOISE_RE.sub(" ", t)
    return " ".join(t.split()).strip()[:_MAX_Q]


def _media_category(media_filter: str | None) -> str:
    m = (media_filter or "any").lower().strip()
    if m in ("vn", "visual_novel", "visual novel"):
        return "vn"
    if m in ("manga", "comic"):
        return "manga"
    if m in ("anime",):
        return "anime"
    if m in ("book", "novel", "ln", "light_novel", "reading"):
        return "book"
    if m in ("youtube", "video"):
        return "skip"
    return "any"  # try anime/manga/vn broadly


async def fetch_poster(
    title: str,
    media_filter: str | None = None,
    *,
    use_cache: bool = True,
) -> Optional[bytes]:
    """Return cover image bytes for *title*, or None."""
    q = clean_title(title)
    if not q or len(q) < 2:
        return None
    cat = _media_category(media_filter)
    if cat == "skip":
        return None
    key = (cat, q.lower())
    if use_cache and key in _CACHE:
        return _CACHE[key]

    result: Optional[bytes] = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            url = await _image_url(client, cat, q)
            if url:
                result = await _download(client, url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("poster fetch failed for %r: %s", q, exc)
        result = None

    if use_cache:
        if len(_CACHE) >= _CACHE_MAX:
            # drop an arbitrary old entry
            try:
                _CACHE.pop(next(iter(_CACHE)))
            except StopIteration:
                pass
        _CACHE[key] = result
    return result


async def _download(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    data = r.content
    if not data or len(data) < 200 or len(data) > 5_000_000:
        return None
    # crude image sniff
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF"):
        # still allow webp etc.
        if b"ftyp" not in data[:20] and data[:4] != b"RIFF":
            pass
    return data


async def _image_url(client: httpx.AsyncClient, cat: str, title: str) -> Optional[str]:
    if cat == "vn":
        return await _vndb_url(client, title)
    if cat == "manga":
        return (
            await _mal_url(client, "manga", title)
            or await _anilist_url(client, title, media_type="MANGA")
        )
    if cat == "anime":
        return (
            await _mal_url(client, "anime", title)
            or await _anilist_url(client, title, media_type="ANIME")
        )
    if cat == "book":
        return await _anilist_url(client, title, media_type="MANGA")  # novels often under MANGA/NOVEL
    # any: try anime then manga then vn
    return (
        await _anilist_url(client, title, media_type="ANIME")
        or await _anilist_url(client, title, media_type="MANGA")
        or await _vndb_url(client, title)
        or await _mal_url(client, "anime", title)
        or await _mal_url(client, "manga", title)
    )


async def _anilist_url(
    client: httpx.AsyncClient, title: str, *, media_type: str = "ANIME"
) -> Optional[str]:
    query = """
    query ($search: String, $type: MediaType) {
      Media(search: $search, type: $type, sort: SEARCH_MATCH) {
        coverImage { large extraLarge }
      }
    }
    """
    try:
        r = await client.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": title, "type": media_type}},
        )
        if r.status_code != 200:
            return None
        media = (r.json().get("data") or {}).get("Media") or {}
        cover = media.get("coverImage") or {}
        return cover.get("extraLarge") or cover.get("large")
    except Exception:  # noqa: BLE001
        return None


async def _vndb_url(client: httpx.AsyncClient, title: str) -> Optional[str]:
    # VNDB API v2
    body = {
        "filters": ["search", "=", title],
        "fields": "image.url",
        "results": 1,
    }
    try:
        r = await client.post(
            "https://api.vndb.org/kana/vn",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results") or []
        if not results:
            return None
        img = results[0].get("image") or {}
        return img.get("url")
    except Exception:  # noqa: BLE001
        return None


async def _mal_url(client: httpx.AsyncClient, media: str, title: str) -> Optional[str]:
    client_id = (os.environ.get("MAL_CLIENT_ID") or "").strip()
    if not client_id:
        return None
    try:
        r = await client.get(
            f"https://api.myanimelist.net/v2/{media}",
            params={"q": title, "limit": 1, "fields": "main_picture"},
            headers={"X-MAL-CLIENT-ID": client_id},
        )
        if r.status_code != 200:
            return None
        nodes = r.json().get("data") or []
        if not nodes:
            return None
        pic = (nodes[0].get("node") or {}).get("main_picture") or {}
        return pic.get("large") or pic.get("medium")
    except Exception:  # noqa: BLE001
        return None
