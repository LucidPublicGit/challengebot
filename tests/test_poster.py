"""Poster lookup: title clean + NSFW stand-in (no network)."""

from __future__ import annotations

import asyncio

from app.poster import (
    _vndb_is_nsfw,
    clean_title,
    fetch_poster,
    nsfw_poster,
    tags_are_nsfw,
)


def test_clean_title_strips_volume_noise():
    assert clean_title("呪術廻戦 Vol. 1") == "呪術廻戦"
    assert clean_title("ナルト vol. 14 (finished)") == "ナルト"
    assert clean_title("転スラ７３〜８３") == "転スラ"
    assert clean_title("薫る花は凛と咲く ep 1-3") == "薫る花は凛と咲く"


def test_vndb_nsfw_rule():
    assert _vndb_is_nsfw({}) is True
    assert _vndb_is_nsfw({"votecount": 0}) is True
    assert _vndb_is_nsfw({"votecount": 10, "sexual": 0.2, "violence": 0.1}) is False
    assert _vndb_is_nsfw({"votecount": 10, "sexual": 0.5, "violence": 0}) is True


def test_tadoku_nsfw_tag_uses_stand_in():
    assert tags_are_nsfw(["manga", "nsfw"]) is True
    assert tags_are_nsfw(["#NSFW"]) is True
    assert tags_are_nsfw(["manga"]) is False
    standin = nsfw_poster()
    assert standin and standin[:8] == b"\x89PNG\r\n\x1a\n"
    got = asyncio.run(fetch_poster("Whatever", tags=["vn", "nsfw"]))
    assert got == standin
