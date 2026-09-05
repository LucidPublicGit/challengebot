"""Catalog seed + lookup."""

from __future__ import annotations

from app.catalog import get_challenge, get_challenge_by_tier_num, list_challenges
from tests.helpers import cleanup_db, setup_tmp_db


def test_seed_2026_count():
    path = setup_tmp_db()
    try:
        items = list_challenges(year=2026)
        assert len(items) == 17
        tiers = {c["tier"] for c in items}
        assert tiers == {"easy", "medium", "hard", "ota_master"}
    finally:
        cleanup_db(path)


def test_lookup_and_verify_modes():
    path = setup_tmp_db()
    try:
        vn = get_challenge_by_tier_num("easy", 4, year=2026)
        assert vn is not None
        assert vn["goal_kind"] == "characters"
        assert vn["verify"] == "tadoku"
        assert vn["param_label"] == "game"

        immersion = get_challenge("hard-5")
        assert immersion is not None
        assert immersion["verify"] == "honor"
        assert immersion["goal_kind"] == "immersion_hours"

        novel = get_challenge("hard-1")
        assert novel is not None
        assert novel["verify"] == "honor"
        assert novel["goal_kind"] == "complete_work"
    finally:
        cleanup_db(path)


if __name__ == "__main__":
    test_seed_2026_count()
    test_lookup_and_verify_modes()
    print("test_catalog ok")
