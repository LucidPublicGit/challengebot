"""Catalog seed + lookup."""

from __future__ import annotations

from app.catalog import (
    get_challenge,
    get_challenge_by_tier_num,
    list_challenges,
    needs_focus_param,
    special_points_note,
    special_points_suffix,
    sweep_bonus,
)
from tests.helpers import cleanup_db, setup_tmp_db


def test_sweep_bonus_scale():
    assert sweep_bonus("easy") == 2
    assert sweep_bonus("medium") == 4
    assert sweep_bonus("hard") == 6
    assert sweep_bonus("ota_master") == 8
    assert sweep_bonus("special") == 0


def test_seed_2026_count():
    path = setup_tmp_db()
    try:
        items = list_challenges(year=2026)
        assert len(items) == 31  # Tales/Trails is the new Easy Monthly edition.
        tiers = {c["tier"] for c in items}
        assert tiers == {"easy", "medium", "hard", "ota_master", "special"}
        assert {tier: sum(c["tier"] == tier for c in items) for tier in tiers} == {
            "easy": 7, "medium": 6, "hard": 7, "ota_master": 6, "special": 5
        }
        assert sum(1 for c in items if c["tier"] == "special") == 5
        for level, tier, walk_hours, endurance_hours, walk_num, endurance_num in (
            ("Easy", "easy", 2, 1, 5, 6),
            ("Medium", "medium", 4, 2, 5, 6),
            ("Hard", "hard", 6, 4, 6, 7),
            ("Ota-Master", "ota_master", 8, 6, 5, 6),
        ):
            key = "ota" if tier == "ota_master" else tier
            for kind, hours, number in (
                ("walk", walk_hours, walk_num),
                ("endurance", endurance_hours, endurance_num),
            ):
                ch = get_challenge(f"physical-{kind}-{key}")
                assert ch["tier"] == tier and ch["number"] == number
                assert ch["title"] == f"{'Walking' if kind == 'walk' else 'Endurance'} Immersion {level}"
                assert ch["goal_amount"] == hours and f"{hours} hour" in ch["description"]
                assert ch["time_limit_hours"] == 24 and ch["verify"] == "honor"
                assert ch["points"] is None
                assert special_points_note(ch) == ""
        for cid, count in (
            ("easy-1", 3), ("medium-1", 6), ("hard-2", 8), ("ota-2", 12),
        ):
            ch = get_challenge(cid)
            assert ch["title"] == f"Manga {count}"
            assert ch["goal_amount"] == count
            assert f"{count} full manga volumes" in ch["description"]
        op = get_challenge("medium-7")
        assert op is not None and op["tier"] == "special"
        assert int(op.get("allows_concurrent") or 0) == 1
        assert int(op.get("points") or 0) == 2
        jp = get_challenge("easy-5")
        assert jp["tier"] == "special" and int(jp.get("points") or 0) == 1
        umezu = get_challenge("medium-8")
        assert umezu["tier"] == "special" and int(umezu.get("points") or 0) == 2
        assert int(jp.get("allows_concurrent") or 0) == 0
        assert special_points_note(jp) == "1 pt"
        assert special_points_suffix(op) == " · **2 pts**"
        assert special_points_note(get_challenge("easy-1")) == ""
    finally:
        cleanup_db(path)


def test_lookup_and_verify_modes():
    path = setup_tmp_db()
    try:
        vn = get_challenge_by_tier_num("easy", 4, year=2026)
        assert vn is not None
        assert vn["goal_kind"] == "characters"
        assert vn["verify"] == "tadoku"
        assert vn["param_label"] is None
        assert needs_focus_param(vn) is False

        immersion = get_challenge("hard-5")
        assert immersion is not None
        assert immersion["verify"] == "honor"
        assert immersion["goal_kind"] == "immersion_hours"
        assert needs_focus_param(immersion) is False

        novel = get_challenge("hard-1")
        assert novel is not None
        assert novel["verify"] == "honor"
        assert novel["goal_kind"] == "complete_work"
        assert needs_focus_param(novel) is True
    finally:
        cleanup_db(path)


def test_admin_catalog_crud():
    from app.bot_commands import _challenge_payload, _next_challenge_number
    from app.services import attempts as att_svc

    path = setup_tmp_db()
    try:
        n = _next_challenge_number(2026, "easy")
        assert n >= 1
        cid = f"easy-{n}"
        r = att_svc.admin_upsert_challenge(
            {
                "id": cid,
                "year": 2026,
                "tier": "easy",
                "number": n,
                "title": "Admin CRUD Probe",
                "goal_kind": "volumes",
                "goal_amount": 3,
                "media_filter": "manga",
                "verify": "tadoku",
                "time_limit_hours": 12,
            }
        )
        assert r["ok"], r
        ch = get_challenge(cid)
        assert ch is not None and ch["title"] == "Admin CRUD Probe"
        r2 = att_svc.admin_upsert_challenge(
            _challenge_payload(ch, title="Renamed", verify="honor")
        )
        assert r2["ok"], r2
        ch2 = get_challenge(cid)
        assert ch2["title"] == "Renamed" and ch2["verify"] == "honor"
        att_svc.admin_delete_challenge(cid)
        assert get_challenge(cid)['lifecycle_state'] == 'retired'
        assert cid not in {c['id'] for c in list_challenges()}
    finally:
        cleanup_db(path)


def test_special_mixed_points():
    from app.db import db_session, utcnow_iso
    from app.services import attempts as att

    path = setup_tmp_db()
    try:
        now = utcnow_iso()
        with db_session() as conn:
            for cid in ("easy-5", "medium-8", "medium-7", "physical-walk-easy"):
                conn.execute(
                    "INSERT INTO attempts("
                    "discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    ("u", cid, "completed", now, now),
                )
        assert att.challenge_points("u") == 6  # 1 + 2 + 2 + easy 1
        assert att.completed_tiers("u") == []
        assert att.highest_completed_tier("u") is None
    finally:
        cleanup_db(path)


def test_specials_survive_new_year():
    import tempfile
    from pathlib import Path

    import yaml

    from app.catalog import upsert_challenges_from_yaml

    path = setup_tmp_db()
    try:
        assert get_challenge("medium-6")["tier"] == "special"
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "2027.yaml"
            catalog.write_text(yaml.safe_dump({
                "year": 2027,
                "challenges": [
                    {"id": "easy-1-2027", "identity_id": "easy-1", "tier": "easy", "number": 1,
                     "title": "2027 Manga", "goal_kind": "volumes",
                     "goal_amount": 3, "media_filter": "manga",
                     "verify": "tadoku"},
                    {"id": "medium-6", "tier": "medium", "number": 6,
                     "title": "Should not clobber Toriyama",
                     "goal_kind": "volumes", "goal_amount": 6,
                     "media_filter": "manga", "verify": "tadoku"},
                ],
            }), encoding="utf8")
            upsert_challenges_from_yaml(catalog, replace_year=True)
        toriyama = get_challenge("medium-6")
        assert toriyama["tier"] == "special"
        assert toriyama["title"] == "鳥山明先生の作品"
        assert get_challenge("easy-1-2027")["title"] == "2027 Manga"
        assert get_challenge("easy-1")["year"] == 2026  # Original edition is immutable.
        specials = list_challenges(year=2027, tier="special")
        assert {c["id"] for c in specials} >= {
            "easy-5", "medium-5", "medium-6", "medium-7", "medium-8",
        }
        found = get_challenge_by_tier_num("special", 5, year=2027)
        assert found is not None and found["id"] == "medium-6"
    finally:
        cleanup_db(path)


if __name__ == "__main__":
    test_sweep_bonus_scale()
    test_seed_2026_count()
    test_lookup_and_verify_modes()
    test_admin_catalog_crud()
    test_special_mixed_points()
    test_specials_survive_new_year()
    print("test_catalog ok")
