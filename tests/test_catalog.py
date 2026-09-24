"""Catalog seed + lookup."""

from __future__ import annotations

from app.catalog import (
    get_challenge,
    get_challenge_by_tier_num,
    list_challenges,
    needs_focus_param,
)
from tests.helpers import cleanup_db, setup_tmp_db


def test_seed_2026_count():
    path = setup_tmp_db()
    try:
        items = list_challenges(year=2026)
        assert len(items) == 22
        tiers = {c["tier"] for c in items}
        assert tiers == {"easy", "medium", "hard", "ota_master", "special"}
        assert sum(1 for c in items if c["tier"] == "easy") == 4
        assert sum(1 for c in items if c["tier"] == "medium") == 4
        assert sum(1 for c in items if c["tier"] == "special") == 5
        op = get_challenge("medium-7")
        assert op is not None and op["tier"] == "special"
        assert int(op.get("allows_concurrent") or 0) == 1
        assert int(op.get("points") or 0) == 2
        jp = get_challenge("easy-5")
        assert jp["tier"] == "special" and int(jp.get("points") or 0) == 1
        umezu = get_challenge("medium-8")
        assert umezu["tier"] == "special" and int(umezu.get("points") or 0) == 2
        assert int(jp.get("allows_concurrent") or 0) == 0
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
        assert get_challenge(cid) is None
    finally:
        cleanup_db(path)


def test_special_mixed_points():
    from app.db import db_session, utcnow_iso
    from app.services import attempts as att

    path = setup_tmp_db()
    try:
        now = utcnow_iso()
        with db_session() as conn:
            for cid in ("easy-5", "medium-8", "medium-7"):
                conn.execute(
                    "INSERT INTO attempts("
                    "discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    ("u", cid, "completed", now, now),
                )
        assert att.challenge_points("u") == 5  # 1 + 2 + 2
        assert att.completed_tiers("u") == []
        assert att.highest_completed_tier("u") is None
    finally:
        cleanup_db(path)


if __name__ == "__main__":
    test_seed_2026_count()
    test_lookup_and_verify_modes()
    test_admin_catalog_crud()
    test_special_mixed_points()
    print("test_catalog ok")
