"""Identity REST fallback helpers (no network)."""

from __future__ import annotations

import base64

from app.discord_http import (
    identity_fallback_kind,
    is_cloudflare_block,
    stub_app,
    stub_user,
    user_id_from_bot_token,
)

UID = "1545936864177946697"


def _token(uid: str = UID) -> str:
    part = base64.b64encode(uid.encode()).decode().rstrip("=")
    return f"{part}.AAAAAA.{'x' * 38}"


def test_user_id_from_bot_token():
    token = _token()
    assert user_id_from_bot_token(token) == UID
    assert user_id_from_bot_token("Bot " + token) == UID
    user = stub_user(UID)
    assert user["id"] == UID and user["bot"] is True
    app = stub_app(UID)
    assert app["id"] == UID and app["owner"]["id"] == "0"


def test_user_id_rejects_garbage():
    part = base64.b64encode(b"nope").decode().rstrip("=")
    try:
        user_id_from_bot_token(f"{part}.AAAAAA.xxx")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_cloudflare_block_html_not_json():
    html = (
        "<html><head><title>Internal Server Error</title></head>"
        "<body><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'>"
    )
    assert is_cloudflare_block(500, html, "text/html")
    assert is_cloudflare_block(500, html, None)
    assert not is_cloudflare_block(
        500, '{"message": "500: Internal Server Error", "code": 0}'
    )
    assert not is_cloudflare_block(401, html, "text/html")
    assert not is_cloudflare_block(500, "", None)


def test_identity_fallback_kind():
    assert (
        identity_fallback_kind("GET", "/users/@me", own_id=UID) == "user"
    )
    assert identity_fallback_kind("GET", "/users/@me", own_id=None) is None
    assert (
        identity_fallback_kind(
            "GET", "/users/{user_id}", f"https://discord.com/api/v10/users/{UID}", UID
        )
        == "user"
    )
    assert (
        identity_fallback_kind(
            "GET",
            "/users/{user_id}",
            "https://discord.com/api/v10/users/1",
            UID,
        )
        is None
    )
    assert (
        identity_fallback_kind("GET", "/oauth2/applications/@me", own_id=UID) == "app"
    )
    assert identity_fallback_kind("GET", "/gateway/bot") == "gateway"
    assert identity_fallback_kind("POST", "/users/@me", own_id=UID) is None
    assert identity_fallback_kind("GET", "/guilds/1", own_id=UID) is None
