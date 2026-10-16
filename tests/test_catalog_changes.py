"""Catalog changes survive restarts, no-op imports, and deletions."""

import tempfile
from pathlib import Path

import yaml

from app.catalog import get_challenge, upsert_challenges_from_yaml
from app.catalog_changes import changes_text, list_changes, mark_announced
from app.db import db_session, init_db
from app.services.attempts import admin_delete_challenge, admin_upsert_challenge
from tests.helpers import ROOT, cleanup_db, setup_tmp_db


def test_initial_report_and_idempotent_import():
    path = setup_tmp_db()
    try:
        changes = list_changes(pending_only=True)
        assert len(changes) == 12
        assert sum(c["change"] == "new" for c in changes) == 8
        assert sum(c["change"] == "updated" for c in changes) == 4
        assert all(c["before"] is None and c["note"] for c in changes)
        body = changes_text(changes)
        assert "**New challenges**" in body and "**Modified challenges**" in body
        assert "Manga 3" in body and "Walking Immersion Easy" in body
        upsert_challenges_from_yaml(ROOT / "challenges/2026.yaml")
        upsert_challenges_from_yaml(ROOT / "challenges/2026.yaml", replace_year=True)
        init_db(path)
        assert len(list_changes()) == 12
    finally:
        cleanup_db(path)


def test_crud_and_import_track_real_diffs():
    path = setup_tmp_db()
    try:
        ids = tuple(c["id"] for c in list_changes())
        mark_announced(ids)
        before = get_challenge("easy-1")
        admin_upsert_challenge({**before, "goal_amount": 4, "description": "Read four volumes."})
        event = list_changes(pending_only=True)[0]
        assert event["before"] == before and event["after"]["goal_amount"] == 4
        assert "goal_amount: 3.0 → 4.0" in changes_text([event])
        admin_upsert_challenge(get_challenge("easy-1"))
        assert len(list_changes(pending_only=True)) == 1
        admin_delete_challenge("easy-2")
        event = list_changes()[0]
        assert event["change"] == "retired" and event["before"]["title"] == "Shows Sprint"
        assert event["after"]["lifecycle_state"] == "retired"
        assert get_challenge("easy-2")["lifecycle_state"] == "retired"
        admin_delete_challenge("easy-2")
        assert len(list_changes(pending_only=True)) == 2
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.yaml"
            data = yaml.safe_load((ROOT / "challenges/2026.yaml").read_text(encoding="utf8"))
            data["challenges"] = [c for c in data["challenges"] if c["id"] != "easy-3"]
            data["challenges"][0]["title"] = "Imported manga change"
            catalog.write_text(yaml.safe_dump(data), encoding="utf8")
            upsert_challenges_from_yaml(catalog, replace_year=True)
            events = list_changes()
            assert any(c["challenge_id"] == "easy-3" and c["change"] == "retired" for c in events)
            assert get_challenge("easy-2")["lifecycle_state"] == "retired"
            assert not any(c["challenge_id"] == "easy-2" and c["change"] == "new" for c in events)
            assert events[0]["before_json"] or events[0]["after_json"]
    finally:
        cleanup_db(path)


def test_history_and_catalog_rollback_together():
    path = setup_tmp_db()
    try:
        from app.catalog_changes import record_change
        count = len(list_changes())
        before = get_challenge("easy-1")
        try:
            with db_session() as conn:
                conn.execute("UPDATE challenge_defs SET title='Rollback' WHERE id='easy-1'")
                record_change(conn, before, {**before, "title": "Rollback"})
                raise ValueError("rollback")
        except ValueError:
            pass
        assert get_challenge("easy-1") == before
        assert len(list_changes()) == count
    finally:
        cleanup_db(path)


def test_first_import_recovers_available_previous_definition():
    path = setup_tmp_db()
    try:
        with db_session() as conn:
            conn.execute("DELETE FROM challenge_changes")
            conn.execute("DELETE FROM meta WHERE key='catalog_change_tracking'")
            conn.execute("UPDATE challenge_defs SET goal_amount=2 WHERE id='easy-1'")
        upsert_challenges_from_yaml(ROOT / "challenges/2026.yaml")
        manga = next(c for c in list_changes() if c["challenge_id"] == "easy-1")
        assert manga["change"] == "updated"
        assert manga["before"]["goal_amount"] == 2
        assert manga["after"]["goal_amount"] == 3
        assert "captured pre-import" in manga["note"]
    finally:
        cleanup_db(path)
