"""Runtime settings merge + announce toggles."""

from __future__ import annotations

from app.config import (
    announce_kind_enabled,
    fracs_to_str,
    get_settings,
    merge_runtime,
    parse_fracs,
)


def test_parse_fracs_percent_and_ratio():
    assert parse_fracs("25,50,75", ()) == (0.25, 0.5, 0.75)
    assert parse_fracs("0.5", ()) == (0.5,)
    assert parse_fracs("", (0.5,)) == (0.5,)
    assert fracs_to_str((0.25, 0.5)) == "25,50"


def test_announce_fail_default_off():
    rt = merge_runtime(get_settings(), {})
    assert rt["announce_fail"] is False
    assert announce_kind_enabled(rt, "fail") is False
    assert announce_kind_enabled(rt, "complete") is True
    assert announce_kind_enabled(rt, "active") is False  # maps to status, default off


def test_db_overrides_status_and_fail():
    rt = merge_runtime(
        get_settings(),
        {
            "announce_fail": "true",
            "status_time_fracs_long": "50",
            "status_updates_enabled": "false",
            "monthly_digest_hour": "9",
        },
    )
    assert rt["announce_fail"] is True
    assert rt["status_time_fracs_long"] == (0.5,)
    assert rt["status_updates_enabled"] is False
    assert rt["monthly_digest_hour"] == 9.0
