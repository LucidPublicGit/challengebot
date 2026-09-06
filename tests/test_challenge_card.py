"""PNG challenge cards render without Discord."""

from __future__ import annotations

import asyncio

from app.challenge_card import render_challenge_card, render_leaderboard_card


def test_challenge_card_png():
    png = asyncio.run(
        render_challenge_card(
            kind="complete",
            display_name="Tester",
            challenge_title="VN 30k",
            tier="easy",
            number=4,
            avatar_bytes=None,
            focus="Game X",
            line_primary="Cleared.",
            line_secondary="Method: tadoku_poll",
            footer="ChallengeTracker",
        )
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 500


def test_leaderboard_card_png():
    png = asyncio.run(
        render_leaderboard_card(
            title="Challenge clears",
            entries=[
                {"rank": 1, "name": "Alice", "score": 12, "avatar": None},
                {"rank": 2, "name": "Bob", "score": 7, "avatar": None},
                {"rank": 3, "name": "Cara", "score": 3, "avatar": None},
                {"rank": 4, "name": "Dan", "score": 1, "avatar": None},
            ],
            footer="4 ranked",
        )
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 500
