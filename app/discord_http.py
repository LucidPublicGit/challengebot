"""Work around Discord/Cloudflare HTML 500s on identity REST.

Cloudflare has returned HTML 500 (+ JS challenge) for GET /users/@me,
/oauth2/applications/@me, /gateway/bot, and GET /users/{this bot}.
Unauthenticated /gateway, guild REST, and websocket IDENTIFY still work.
JSON 5xx from Discord itself is not rewritten.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import discord
from discord.http import HTTPClient, Route

logger = logging.getLogger("challengetracker")

_patched = False


def user_id_from_bot_token(token: str) -> str:
    raw = token.replace("Bot ", "").strip()
    part = raw.split(".")[0]
    pad = "=" * ((4 - len(part) % 4) % 4)
    uid = base64.b64decode(part + pad).decode("ascii")
    if not uid.isdigit():
        raise ValueError("token id is not a snowflake")
    return uid


def stub_user(uid: str) -> dict[str, Any]:
    return {
        "id": str(uid),
        "username": "bot",
        "discriminator": "0",
        "global_name": None,
        "avatar": None,
        "bot": True,
    }


def stub_app(uid: str) -> dict[str, Any]:
    return {
        "id": str(uid),
        "name": "ChallengeTracker",
        "description": "",
        "icon": None,
        "bot_public": True,
        "bot_require_code_grant": False,
        "verify_key": "",
        "flags": 0,
        "owner": stub_user("0"),
    }


def stub_bot_gateway(url: str = "wss://gateway.discord.gg") -> dict[str, Any]:
    return {
        "url": url,
        "shards": 1,
        "session_start_limit": {
            "total": 1000,
            "remaining": 1000,
            "reset_after": 0,
            "max_concurrency": 1,
        },
    }


def is_cloudflare_block(
    status: int, text: str | None = None, content_type: str | None = None
) -> bool:
    if status < 500:
        return False
    if content_type and "html" in content_type.lower():
        return True
    low = (text or "")[:2000].lower()
    if not low:
        return False
    if low.lstrip().startswith("{"):
        return False
    return "<html" in low or "cdn-cgi" in low or "cloudflare" in low


def identity_fallback_kind(
    method: str, path: str, url: str = "", own_id: str | None = None
) -> str | None:
    if (method or "GET").upper() != "GET":
        return None
    if path in ("/oauth2/applications/@me", "/applications/@me"):
        return "app" if own_id else None
    if path == "/gateway/bot":
        return "gateway"
    if path == "/users/@me":
        return "user" if own_id else None
    if path == "/users/{user_id}":
        requested = (url or "").rsplit("/", 1)[-1]
        if own_id and requested == own_id:
            return "user"
    return None


def _own_id(http: HTTPClient) -> str | None:
    token = getattr(http, "token", None) or ""
    if not token:
        return None
    try:
        return user_id_from_bot_token(token)
    except Exception:  # noqa: BLE001
        return None


def _cloudflare_http_error(exc: discord.HTTPException) -> bool:
    ctype = ""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers:
        ctype = headers.get("Content-Type") or headers.get("content-type") or ""
    return is_cloudflare_block(exc.status, getattr(exc, "text", None), ctype)


async def _fallback(http: HTTPClient, orig, route: Route) -> dict[str, Any] | None:
    path = getattr(route, "path", "") or ""
    method = getattr(route, "method", "GET") or "GET"
    url = getattr(route, "url", "") or ""
    uid = _own_id(http)
    kind = identity_fallback_kind(method, path, url, uid)
    if kind == "app" and uid:
        return stub_app(uid)
    if kind == "gateway":
        data = await orig(http, Route("GET", "/gateway"))
        return stub_bot_gateway((data or {}).get("url") or "wss://gateway.discord.gg")
    if kind == "user" and uid:
        from app.config import get_settings

        gid = (get_settings().discord_guild_id or "").strip()
        if gid:
            try:
                member = await orig(
                    http,
                    Route(
                        "GET",
                        "/guilds/{guild_id}/members/{user_id}",
                        guild_id=gid,
                        user_id=uid,
                    ),
                )
                user = (member or {}).get("user")
                if user:
                    return user
            except Exception:  # noqa: BLE001
                logger.warning("guild-member fallback failed for bot user %s", uid)
        return stub_user(uid)
    return None


def patch_discord_http() -> None:
    global _patched
    if _patched:
        return
    _patched = True
    orig = HTTPClient.request

    async def request(self, route, *args, **kwargs):  # noqa: ANN001
        try:
            return await orig(self, route, *args, **kwargs)
        except discord.HTTPException as exc:
            if not _cloudflare_http_error(exc):
                raise
            alt = await _fallback(self, orig, route)
            if alt is None:
                raise
            logger.warning(
                "Cloudflare %s on %s %s; used identity fallback",
                exc.status,
                getattr(route, "method", "?"),
                getattr(route, "path", "?"),
            )
            return alt

    HTTPClient.request = request  # type: ignore[method-assign]
