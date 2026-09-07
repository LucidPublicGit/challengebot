"""Goal evaluation + synthetic logs (no DB)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.verify import evaluate_logs, synthetic_logs_for_goal


def test_param_normalize_and_aliases():
    from app.services.verify import evaluate_logs

    logs = [
        {
            "id": "a",
            "amount": 10,
            "unit": "volumes",
            "tags": ["manga"],
            "series": "OnePiecee",
        }
    ]
    # space / case normalize
    r = evaluate_logs(
        logs,
        goal_kind="volumes",
        goal_amount=5,
        media_filter="manga",
        param="One Piece",
    )
    assert r.ok, r.detail
    # explicit alias
    r2 = evaluate_logs(
        logs,
        goal_kind="volumes",
        goal_amount=5,
        media_filter="manga",
        param="One Piece",
        aliases=["totally-wrong"],
    )
    assert r2.ok
    # force include by id ignores bad param
    r3 = evaluate_logs(
        logs,
        goal_kind="volumes",
        goal_amount=5,
        media_filter="manga",
        param="Naruto",
        include_log_ids=["a"],
    )
    assert r3.ok


def test_characters():
    logs = [
        {
            "id": "1",
            "amount": 20000,
            "unit": "characters",
            "activity": "reading",
            "tags": ["vn"],
            "description": "Game X",
        },
        {
            "id": "2",
            "amount": 15000,
            "unit": "characters",
            "activity": "reading",
            "tags": ["vn"],
            "description": "Game X",
        },
    ]
    r = evaluate_logs(
        logs,
        goal_kind="characters",
        goal_amount=30000,
        media_filter="vn",
        param="Game X",
    )
    assert r.ok
    assert r.progress == 35000


def test_minutes_youtube():
    logs = [
        {
            "id": "1",
            "duration_seconds": 3600,
            "tags": ["youtube"],
            "description": "topic A",
        },
        {
            "id": "2",
            "amount": 60,
            "unit": "minutes",
            "tags": ["youtube"],
            "description": "topic A",
        },
        {
            "id": "3",
            "duration_seconds": 3600,
            "tags": ["youtube"],
            "description": "topic B",
        },
    ]
    r = evaluate_logs(
        logs,
        goal_kind="minutes",
        goal_amount=180,
        media_filter="youtube",
    )
    assert r.progress == 180.0
    assert r.ok


def test_volumes_from_pages():
    logs = [
        {
            "id": "1",
            "amount": 900,
            "unit": "comic pages",
            "tags": ["manga"],
            "description": "One Piece",
        }
    ]
    r = evaluate_logs(
        logs,
        goal_kind="volumes",
        goal_amount=5,
        media_filter="manga",
        pages_per_volume=180,
        param="One Piece",
    )
    assert r.ok
    assert abs(r.progress - 5.0) < 0.01


def test_episodes_unit():
    logs = [
        {
            "id": "1",
            "amount": 8,
            "unit": "episodes",
            "tags": ["anime"],
            "description": "Show S01",
            "activity": "listening",
        }
    ]
    r = evaluate_logs(
        logs,
        goal_kind="episodes",
        goal_amount=8,
        media_filter="anime",
        param="Show",
    )
    assert r.ok
    assert r.progress == 8


def test_immersion_hours():
    logs = [
        {
            "id": "1",
            "duration_seconds": 4 * 3600,
            "tags": ["anime"],
            "description": "mix",
            "activity": "listening",
        },
        {
            "id": "2",
            "duration_seconds": 4 * 3600,
            "tags": ["youtube"],
            "description": "mix",
            "activity": "listening",
        },
    ]
    r = evaluate_logs(
        logs,
        goal_kind="immersion_hours",
        goal_amount=8,
        media_filter="any",
    )
    assert r.ok
    assert abs(r.progress - 8.0) < 0.01


def test_resolve_content_from_logs_and_param():
    from app.services.verify import resolve_content

    logs = [
        {"id": "1", "description": "Steins;Gate S01E01", "series": ""},
        {"id": "2", "description": "Steins;Gate S01E02"},
        {"id": "3", "description": "Other Show"},
    ]
    c = resolve_content(
        param="fallback",
        logs=logs,
        matched_ids=["1", "2"],
    )
    assert c and "Steins;Gate" in c
    assert resolve_content(param="  VN X  ") == "VN X"
    assert resolve_content(note="done 8h") == "done 8h"


def test_honor_complete_work():
    r = evaluate_logs(
        [],
        goal_kind="complete_work",
        goal_amount=1,
        media_filter="book",
    )
    assert not r.ok
    assert r.method == "honor"


def test_synthetic_pass_fail():
    logs = synthetic_logs_for_goal(
        goal_kind="characters",
        goal_amount=30000,
        media_filter="vn",
        param="Game X",
        meet_goal=True,
    )
    r = evaluate_logs(
        logs,
        goal_kind="characters",
        goal_amount=30000,
        media_filter="vn",
        param="Game X",
    )
    assert r.ok
    logs_fail = synthetic_logs_for_goal(
        goal_kind="characters",
        goal_amount=30000,
        media_filter="vn",
        param="Game X",
        meet_goal=False,
    )
    r2 = evaluate_logs(
        logs_fail,
        goal_kind="characters",
        goal_amount=30000,
        media_filter="vn",
        param="Game X",
    )
    assert not r2.ok


def test_synthetic_volumes_and_episodes():
    for kind, amount, media in (
        ("volumes", 5, "manga"),
        ("episodes", 8, "anime"),
        ("minutes", 180, "youtube"),
        ("immersion_hours", 8, "any"),
    ):
        logs = synthetic_logs_for_goal(
            goal_kind=kind,
            goal_amount=amount,
            media_filter=media,
            param="Test",
            meet_goal=True,
        )
        r = evaluate_logs(
            logs,
            goal_kind=kind,
            goal_amount=amount,
            media_filter=media,
            param="Test",
        )
        assert r.ok, (kind, r.detail)


def test_param_filters_out():
    logs = [
        {
            "id": "1",
            "amount": 50000,
            "unit": "characters",
            "tags": ["vn"],
            "description": "Other Game",
        }
    ]
    r = evaluate_logs(
        logs,
        goal_kind="characters",
        goal_amount=30000,
        media_filter="vn",
        param="Steins;Gate",
    )
    assert not r.ok
    assert r.progress == 0


def test_multi_work_param_slash_or_match():
    """Focus 'A/B' should match logs titled A or B (not require literal A/B)."""
    logs = [
        {
            "id": "1",
            "amount": 4,
            "unit": "episodes",
            "tags": ["anime"],
            "description": "kamen rider",
        },
        {
            "id": "2",
            "amount": 4,
            "unit": "episodes",
            "tags": ["anime"],
            "description": "madoka",
        },
        {
            "id": "3",
            "amount": 99,
            "unit": "episodes",
            "tags": ["anime"],
            "description": "unrelated show",
        },
    ]
    r = evaluate_logs(
        logs,
        goal_kind="episodes",
        goal_amount=8,
        media_filter="anime",
        param="kamen rider/madoka",
    )
    assert r.ok, r.detail
    assert r.progress == 8
    assert set(r.matched_log_ids) == {"1", "2"}

    # bare short slash titles stay whole (Fate/stay night style)
    from app.services.verify import _expand_param_needles

    needles = _expand_param_needles("Fate/stay night")
    assert needles == ["Fate/stay night"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_verify ok")
