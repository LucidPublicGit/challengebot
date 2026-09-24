"""Best-effort cover art (AniList / VNDB / MAL / Steam / Books / TMDB).

Ported from ApathyWorks/TadokuBot poster_client. Failures return None.
VNDB NSFW (and a tadoku `nsfw` tag) swap in images/anime-disgust.png.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
_MAX_Q = 64
_CACHE: dict[tuple[str, str], Optional[bytes]] = {}
_CACHE_MAX = 128
_NSFW = "nsfw"
_VNDB_NSFW_THRESHOLD = 0.4
_NSFW_POSTER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "images", "anime-disgust.png"
)


def clean_title(description: str) -> str:
    t = (description or "").strip()
    if not t:
        return ""
    t = re.sub(r"[\(（【\[].*?[\)）】\]]", " ", t)
    t = re.sub(r"[\(（【\[].*$", " ", t)
    t = re.sub(
        r"(?i)\b(?:(?:vol\.?|volume|ep\.?|episode|chapter|ch\.?|#)\s*[\d０-９]|s[\d０-９]+e[\d０-９]+).*$",
        "",
        t,
    )
    t = re.sub(r"第?\s*[\d０-９]+\s*(?:話|巻|章|集|冊).*$", "", t)
    t = re.sub(r"[\d０-９]+(?:\s*[〜~\-–ー]\s*[\d０-９]+)?\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" 　-–—・:：")
    return (t or (description or "").strip())[:_MAX_Q]


def tags_are_nsfw(tags: list | None) -> bool:
    return any(str(t).lower().lstrip("#") == "nsfw" for t in (tags or []))


def _category_from_tags(tags: list | None) -> Optional[str]:
    if not tags:
        return None
    have = {str(t).lower().lstrip("#") for t in tags}
    if "vn" in have:
        return "vn"
    if "game" in have:
        return "game"
    if "anime" in have:
        return "anime"
    if "manga" in have:
        return "manga"
    if "ln" in have:
        return "ln"
    if have & {"book", "audiobook"}:
        return "book"
    if have & {"tv", "movie", "show"}:
        return "screen"
    return None


def _media_category(media_filter: str | None) -> str:
    m = (media_filter or "any").lower().strip()
    if m in ("vn", "visual_novel", "visual novel"):
        return "vn"
    if m in ("game",):
        return "game"
    if m in ("manga", "comic"):
        return "manga"
    if m in ("anime",):
        return "anime"
    if m in ("shows", "show", "tv", "movie"):
        return "screen"
    if m in ("ln", "light_novel"):
        return "ln"
    if m in ("book", "novel", "reading"):
        return "book"
    if m in ("youtube", "video"):
        return "skip"
    return "any"


def _vndb_is_nsfw(image: dict) -> bool:
    if not image.get("votecount"):
        return True
    return (image.get("sexual") or 0) > _VNDB_NSFW_THRESHOLD or (
        image.get("violence") or 0
    ) > _VNDB_NSFW_THRESHOLD


def nsfw_poster() -> Optional[bytes]:
    try:
        with open(_NSFW_POSTER_PATH, "rb") as f:
            return f.read()
    except OSError as exc:
        logger.warning("NSFW stand-in missing at %s: %s", _NSFW_POSTER_PATH, exc)
        return None


async def fetch_poster(
    title: str,
    media_filter: str | None = None,
    *,
    use_cache: bool = True,
    tags: list | None = None,
) -> Optional[bytes]:
    """Return cover image bytes for *title*, or None."""
    if tags_are_nsfw(tags):
        return nsfw_poster()
    q = clean_title(title)
    if not q or len(q) < 2:
        return None
    cat = _category_from_tags(tags) or _media_category(media_filter)
    if cat == "skip":
        return None
    key = (cat, q.lower())
    if use_cache and key in _CACHE:
        return _CACHE[key]

    result: Optional[bytes] = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            url = await _image_url(client, cat, q)
            if url == _NSFW:
                result = nsfw_poster()
            elif url:
                result = await _download(client, url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("poster fetch failed for %r: %s", q, exc)
        result = None

    if use_cache:
        if len(_CACHE) >= _CACHE_MAX:
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
    return data


async def _image_url(client: httpx.AsyncClient, cat: str, title: str) -> Optional[str]:
    if cat == "vn":
        return await _vndb_url(client, title)
    if cat == "game":
        return await _vndb_url(client, title) or await _steam_url(client, title)
    if cat in ("anime", "manga"):
        media = "ANIME" if cat == "anime" else "MANGA"
        return await _anilist_url(client, title, media_type=media) or await _mal_url(
            client, cat, title
        )
    if cat == "ln":
        return await _anilist_url(
            client, title, media_type="MANGA", novel_only=True
        ) or await _google_books_url(client, title)
    if cat == "book":
        return await _anilist_url(client, title, media_type="MANGA") or await _google_books_url(
            client, title
        )
    if cat == "screen":
        return (
            await _anilist_url(client, title, media_type="ANIME")
            or await _mal_url(client, "anime", title)
            or await _tmdb_url(client, title)
        )
    return (
        await _anilist_url(client, title, media_type="ANIME")
        or await _anilist_url(client, title, media_type="MANGA")
        or await _vndb_url(client, title)
        or await _mal_url(client, "anime", title)
        or await _mal_url(client, "manga", title)
    )


async def _anilist_url(
    client: httpx.AsyncClient,
    title: str,
    *,
    media_type: str = "ANIME",
    novel_only: bool = False,
) -> Optional[str]:
    fmt = ", format: NOVEL" if novel_only else ""
    query = (
        f"query ($search: String) {{ Media(search: $search, type: {media_type}{fmt}) "
        f"{{ isAdult coverImage {{ extraLarge large medium }} }} }}"
    )
    try:
        r = await client.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": title}},
        )
        if r.status_code != 200:
            return None
        media = (r.json().get("data") or {}).get("Media") or {}
        if media.get("isAdult"):
            return _NSFW
        cover = media.get("coverImage") or {}
        return cover.get("extraLarge") or cover.get("large") or cover.get("medium")
    except Exception:  # noqa: BLE001
        return None


async def _vndb_url(client: httpx.AsyncClient, title: str) -> Optional[str]:
    body = {
        "filters": ["search", "=", title],
        "fields": "image.url, image.sexual, image.violence, image.votecount",
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
        url = img.get("url")
        if url and _vndb_is_nsfw(img):
            return _NSFW
        return url
    except Exception:  # noqa: BLE001
        return None


async def _steam_url(client: httpx.AsyncClient, title: str) -> Optional[str]:
    try:
        r = await client.get(
            "https://steamcommunity.com/actions/SearchApps/" + quote(title, safe="")
        )
        if r.status_code != 200:
            return None
        results = r.json() or []
        if not results:
            return None
        appid = results[0].get("appid")
        if not appid:
            return None
        base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"
        for filename in ("library_600x900.jpg", "header.jpg"):
            candidate = f"{base}/{filename}"
            head = await client.head(candidate)
            if head.status_code == 200:
                return candidate
    except Exception:  # noqa: BLE001
        return None
    return None


async def _tmdb_url(client: httpx.AsyncClient, title: str) -> Optional[str]:
    key = (os.environ.get("TMDB_API_KEY") or "").strip()
    if not key:
        return None
    try:
        r = await client.get(
            "https://api.themoviedb.org/3/search/multi",
            params={
                "api_key": key,
                "query": title,
                "include_adult": "false",
                "language": "ja-JP",
            },
        )
        if r.status_code != 200:
            return None
        for result in r.json().get("results") or []:
            if result.get("media_type") in ("movie", "tv") and result.get("poster_path"):
                return f"https://image.tmdb.org/t/p/w500{result['poster_path']}"
    except Exception:  # noqa: BLE001
        return None
    return None


async def _google_books_url(client: httpx.AsyncClient, title: str) -> Optional[str]:
    key = (os.environ.get("GOOGLE_BOOKS_API_KEY") or "").strip()
    if not key:
        return None
    try:
        r = await client.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": title, "maxResults": 1, "country": "US", "key": key},
        )
        if r.status_code != 200:
            return None
        items = r.json().get("items") or []
        if not items:
            return None
        links = items[0].get("volumeInfo", {}).get("imageLinks") or {}
        thumb = links.get("thumbnail") or links.get("smallThumbnail")
        return thumb.replace("http://", "https://") if thumb else None
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
