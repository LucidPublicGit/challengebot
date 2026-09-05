"""Minimal checks for goal evaluation."""

from app.services.verify import evaluate_logs


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


def test_honor_complete_work():
    r = evaluate_logs(
        [],
        goal_kind="complete_work",
        goal_amount=1,
        media_filter="book",
    )
    assert not r.ok
    assert r.method == "honor"


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


if __name__ == "__main__":
    test_characters()
    test_minutes_youtube()
    test_volumes_from_pages()
    test_honor_complete_work()
    test_param_filters_out()
    print("ok")
