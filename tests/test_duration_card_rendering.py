"""Synthetic card previews and duration badge layout checks; no database/network."""

import asyncio
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from app import challenge_card as card


PREVIEWS = Path(__file__).resolve().parents[1] / "data/card_previews/duration"
TIERS = ["easy", "medium", "hard", "ota_master"]
NAMES = [
    "Synthetic Challenger",
    "A Very Long Synthetic Display Name " * 5,
    "読書家のチャレンジャー / アパシー " * 6,
    "Мария / 日本語",
]
BADGES = [
    [],
    [{"duration_group": "daily", "tier": "easy", "year": 2026}],
    [{"duration_group": "monthly", "tier": "medium", "year": None}],
    [{"duration_group": "yearly", "tier": "hard", "year": None}],
    [{"duration_group": duration, "tier": "ota_master"}
     for duration in ("daily", "monthly", "yearly")],
]


@pytest.fixture(autouse=True)
def no_database():
    with patch("sqlite3.connect", side_effect=AssertionError("Rendering must not open a DB")):
        yield


def test_duration_selection_matches_service_contract_and_legacy_fallback():
    assert card._profile_duration_badges(None, TIERS) == [("daily", "ota_master")]
    assert card._profile_duration_badges([], TIERS) == []
    assert card._profile_duration_badges({}, TIERS) == []
    assert card._profile_duration_badges({
        "yearly": ["easy", "ota_master", "hard", "ota_master"],
        "monthly": " HARD ", "daily": ["medium", "easy"],
        "special": "ota_master",
    }) == [("daily", "medium"), ("monthly", "hard"), ("yearly", "ota_master")]
    rows = [
        {"duration_group": duration, "tier": tier}
        for duration in ("yearly", "daily", "monthly")
        for tier in ("special", "hard", "easy", "unknown", "medium", "ota_master")
    ]
    assert card._profile_duration_badges(rows) == [
        ("daily", "ota_master"), ("monthly", "ota_master"), ("yearly", "ota_master"),
    ]
    assert card._profile_duration_badges({"monthly": None, "daily": ["special"]}) == []


@pytest.mark.parametrize("duration,glyph", [("daily", "日"), ("monthly", "月"), ("yearly", "年")])
def test_duration_glyphs_are_drawn_with_real_font_support(duration, glyph):
    font = card._font(40, bold=True)
    assert "".join(run for _, run in card._shape(glyph, font)) == glyph, (
        "Install a CJK font from the renderer's font stack to verify badge glyphs"
    )
    for compact in (False, True):
        with patch.object(card._Draw, "text", autospec=True) as draw:
            card._hanko_stamp("ota_master", 2, compact=compact, duration=duration)
        assert draw.call_args.args[2] == glyph
        assert draw.call_args.kwargs["fill"] == card.ACCENT_GOLD


@pytest.mark.parametrize("name_index", range(len(NAMES)))
@pytest.mark.parametrize("badge_index", range(len(BADGES)))
def test_profile_duration_layout_and_mobile_previews(name_index, badge_index):
    name, badges = NAMES[name_index], BADGES[badge_index]
    texts = []
    original = card._Draw.text

    def text(draw, xy, value, *args, **kwargs):
        texts.append((draw, xy, value, kwargs))
        return original(draw, xy, value, *args, **kwargs)

    achievements = [
        {"earned": True, "title": "Synthetic First Clear"},
        {"earned": False, "title": "Hidden artwork"},
    ]
    with patch.object(card._Draw, "text", text), patch.object(
        card, "_draw_duration_badges", wraps=card._draw_duration_badges,
    ) as draw_badges:
        png = asyncio.run(card.render_userinfo_card(
            display_name=name, duration_badges=badges,
            # An explicit empty result must suppress stale legacy badges.
            cleared_tiers=TIERS,
            best_tier="ota_master" if any(b["duration_group"] == "daily" for b in badges) else None,
            achievements=achievements,
            active=[{"title": "Synthetic long challenge", "tier": "easy", "number": 1}],
            completed=[{"title": "Synthetic completed challenge", "tier": "hard", "number": 2}],
        ))
    image = Image.open(BytesIO(png))
    assert image.width == 560 and image.height > 560
    name_draw, name_xy, shown_name, name_kw = next(t for t in texts if t[1] == (196, 56))
    expected = card._profile_duration_badges(badges)
    assert len(expected) <= 3
    if expected:
        draw_badges.assert_called_once()
        _, badge_x, badge_mid, drawn, scale = draw_badges.call_args.args
        assert drawn == expected
        assert name_xy[0] + name_draw.textlength(shown_name, font=name_kw["font"]) <= badge_x - 10 * scale
        assert badge_x + card._duration_badges_width(drawn, scale) == (560 - 28) * scale
        for duration, tier in drawn:
            stamp = card._hanko_stamp(tier, scale, duration=duration)
            assert badge_mid - stamp.height / 2 >= 0
            assert badge_mid + stamp.height / 2 < 118 * scale
    else:
        draw_badges.assert_not_called()
    assert "Achievements" in [t[2] for t in texts]
    assert "Hidden artwork" not in [t[2] for t in texts]
    if not any(b["duration_group"] == "daily" for b in badges):
        assert "UNRANKED" in [t[2] for t in texts]
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    stem = f"profile_name{name_index}_badges{badge_index}"
    image.save(PREVIEWS / f"{stem}.png")
    mobile = image.resize((320, round(image.height * 320 / image.width)), Image.Resampling.LANCZOS)
    mobile.save(PREVIEWS / f"{stem}_mobile.png")
    assert mobile.width == 320


def test_long_badges_preserve_daily_styling_and_achievement_section_pixels():
    common = dict(display_name="Synthetic Challenger", best_tier="hard",
                  achievements=[{"title": "Synthetic achievement", "earned": True}] * 7)
    daily = Image.open(BytesIO(asyncio.run(card.render_userinfo_card(
        **common, duration_badges={"daily": "hard"},
    ))))
    all_durations = Image.open(BytesIO(asyncio.run(card.render_userinfo_card(
        **common, duration_badges={"daily": "hard", "monthly": "ota_master", "yearly": "ota_master"},
    ))))
    assert daily.size == all_durations.size
    # Only the name/badge row may change: Daily chrome, tier pill, statistics,
    # achievements (including the second row), and challenge lists stay identical.
    assert daily.crop((0, 80, 560, daily.height)).tobytes() == all_durations.crop((0, 80, 560, daily.height)).tobytes()
    assert daily.crop((0, 0, 90, 118)).tobytes() == all_durations.crop((0, 0, 90, 118)).tobytes()


@pytest.mark.parametrize("sweeps", [[], ["easy"], TIERS])
def test_leaderboard_and_event_stamps_are_daily_and_compact(sweeps):
    name = NAMES[2]
    common = dict(display_name=name, challenge_title="Synthetic Monthly Challenge",
                  tier="easy", number=1, avatar_bytes=None, focus="Synthetic work",
                  cleared_tiers=sweeps, name_tier="hard")
    with patch.object(card, "_hanko_stamp", wraps=card._hanko_stamp) as stamp:
        outputs = {
            "event": asyncio.run(card.render_challenge_card(
                **common, kind="complete", line_primary="Cleared", line_secondary="Monthly", footer="",
            )),
            "unified_start": asyncio.run(card.render_unified_start_card(
                **common, include_challenger=False,
            )),
            "leaderboard": asyncio.run(card.render_leaderboard_card(
                title="Synthetic board", entries=[{
                    "rank": 4, "name": name, "points": 42, "best_tier": "hard",
                    "cleared_tiers": sweeps,
                    "duration_badges": BADGES[-1],  # Never repurposed as Daily sweeps.
                }],
            )),
        }
    expected_tier = "ota_master" if len(sweeps) == 4 else "easy"
    if sweeps:
        assert stamp.call_args_list
        for call in stamp.call_args_list:
            assert call.args[0] == expected_tier
            assert call.kwargs.get("duration", "daily") == "daily"
    else:
        stamp.assert_not_called()
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    for kind, png in outputs.items():
        image = Image.open(BytesIO(png))
        image.save(PREVIEWS / f"{kind}_daily{len(sweeps)}.png")
        image.resize((320, round(image.height * 320 / image.width)), Image.Resampling.LANCZOS).save(
            PREVIEWS / f"{kind}_daily{len(sweeps)}_mobile.png",
        )
