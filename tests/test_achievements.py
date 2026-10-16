"""Achievement receipts, administration, and locked-artwork privacy checks."""

import asyncio
import json
import re
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.catalog import list_challenges
from app.config import Settings
from app.db import db_session, init_db
from app.services import achievements as ach
from app.services import attempts as att
from tests.helpers import cleanup_db, setup_tmp_db


def _attempt(cid="easy-1", date="2026-02-01T12:00:00+00:00", user="u", status="completed"):
    att.ensure_user(user)
    with db_session() as conn:
        aid = conn.execute(
            "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at) "
            "VALUES (?,?,'active','2026-01-01T00:00:00+00:00','2026-12-31T23:59:00+00:00')",
            (user, cid),
        ).lastrowid
    result = att.admin_update_attempt(aid, status=status,
                                     completed_at=date if status == "completed" else None)
    assert result["ok"], result
    return aid


def _earned(user="u"):
    return {item["id"]: item for item in ach.for_user(user) if item["earned"]}


def _token(response):
    return re.search(r'name="token" value="([^"]+)"', response.text).group(1)


def test_achievement_yaml_seed_ids_and_restart_preservation():
    with TemporaryDirectory() as directory:
        directory = Path(directory)
        document = yaml.safe_load(ach.DEFAULT_CATALOG.read_text(encoding="utf-8"))
        assert len(document["achievements"]) == 18
        # Array order must never define identity or artwork assignments.
        document["achievements"].reverse()
        catalog = directory / "defaults.yaml"
        catalog.write_text(yaml.safe_dump(document), encoding="utf-8")
        path = str(directory / "seed.db")
        with patch.object(ach, "DEFAULT_CATALOG", catalog):
            init_db(path)
            definitions = ach.list_definitions()
            assert definitions[0]["id"] == "achievement-001"
            assert definitions[0]["title"] == "First Clear"
            assert definitions[-1]["id"] == "achievement-018"
            assert definitions[-1]["image_file"] == "achievement-018.png"
            # Legacy databases with an existing ID but missing seed marker retain edits.
            definitions[0]["title"] = "An admin edit"
            ach.save_definition(definitions[0])
            with db_session() as conn:
                conn.execute("DELETE FROM meta WHERE key='achievements_seeded'")
            init_db(path)
            assert ach.list_definitions()[0]["title"] == "An admin edit"
            ach.remove_definition("achievement-002")
        # Seeded databases do not even read a changed/missing default catalog.
        with patch.object(ach, "DEFAULT_CATALOG", directory / "absent.yaml"):
            init_db(path)
            assert ach.list_definitions()[0]["title"] == "An admin edit"
            assert "achievement-002" not in {d["id"] for d in ach.list_definitions()}


def test_achievement_catalog_roundtrip_atomic_import_and_validation():
    path = setup_tmp_db()
    try:
        _attempt(user="private-member")
        ach.remove_definition("achievement-002")
        with db_session() as conn:
            known = {r["id"] for r in conn.execute("SELECT id FROM challenge_defs")}
        for definition in ach.parse_catalog(ach.DEFAULT_CATALOG.read_bytes()):
            ach._definition_values(definition, known)
        original = yaml.safe_load(ach.export_catalog())
        assert len(original["achievements"]) == 18
        assert not original["achievements"][1]["enabled"]
        assert all(set(d) == ach.CATALOG_FIELDS for d in original["achievements"])
        assert "private-member" not in ach.export_catalog()
        def receipts():
            return {aid: (d["unlocked_at"], d["source_attempt_id"])
                    for aid, d in _earned("private-member").items()}

        before = receipts()
        with patch.object(ach, "sync_all", wraps=ach.sync_all) as sync:
            assert ach.import_catalog(ach.export_catalog().encode()) == 18
            sync.assert_called_once()
        assert receipts() == before
        assert yaml.safe_load(ach.export_catalog()) == original

        # A one-entry merge leaves omitted definitions and receipts alone.
        item = {**original["achievements"][1], "title": "Restored", "enabled": True}
        assert ach.import_catalog(yaml.safe_dump(dict(version=1, achievements=[item])).encode()) == 1
        assert len(ach.list_definitions()) == 18
        assert receipts() == before

        baseline = ach.export_catalog()
        good = {**original["achievements"][0], "title": "Must not be saved"}
        bad_entries = [
            {**good, "rule": {"kind": "groups", "groups": [["missing-challenge"]]}},
            {**good, "id": "other", "image_file": "../private.png"},
            {**good, "id": "other", "image_file": "missing.png"},
            {**good, "id": "other", "rule": {"kind": "clears", "count": True}},
            {**good, "id": "other", "enabled": "false"},
            {**good, "id": "other", "position": 0.5},
            {**good, "id": ""},
            {**good, "id": "other", "source_attempt_id": 1},
            good,  # Duplicate ID.
        ]
        invalid = [yaml.safe_dump(dict(version=1, achievements=[good, bad])).encode()
                   for bad in bad_entries]
        invalid += [
            baseline.replace("title: First Clear\n", "title: First Clear\n  title: Duplicate\n").encode(),
            b"version: [", b"\xff", b"!!python/object/apply:os.system ['echo unsafe']",
            b"version: 1\nversion: 1\nachievements: []",
            b"version: 2\nachievements: []", b"version: 1\nachievements: []",
            b"x" * (ach.MAX_CATALOG_BYTES + 1),
        ]
        for content in invalid:
            try:
                ach.import_catalog(content)
            except ValueError:
                pass
            else:
                raise AssertionError("Accepted an invalid catalog.")
            assert ach.export_catalog() == baseline
        # A failure during reconciliation rolls back every definition write too.
        with patch.object(ach, "sync_all", side_effect=RuntimeError("reconciliation failed")):
            try:
                ach.import_catalog(yaml.safe_dump(dict(version=1, achievements=[good])).encode())
            except RuntimeError:
                pass
            else:
                raise AssertionError("Expected reconciliation failure.")
        assert ach.export_catalog() == baseline
    finally:
        cleanup_db(path)


def test_achievement_web_catalog_export_import_auth_and_confirmation():
    from app.web.app import create_app

    path = setup_tmp_db()
    try:
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")):
            client = TestClient(create_app(), headers={"Origin": "http://testserver"})
            assert client.get("/achievements/catalog/export.yaml").status_code == 401
            assert client.post("/achievements/catalog/import", files={"catalog": ("c.yaml", b"invalid")}).status_code == 401
            client.auth = ("admin", "secret")
            exported = client.get("/achievements/catalog/export.yaml")
            assert exported.status_code == 200
            assert exported.headers["cache-control"] == "no-store"
            assert "attachment" in exported.headers["content-disposition"]
            document = yaml.safe_load(exported.text)
            document["achievements"][0]["title"] = "Imported title"
            payload = yaml.safe_dump(document).encode()

            def post(token="", confirmed="yes", content=payload):
                return client.post("/achievements/catalog/import", data=dict(token=token, confirmed=confirmed),
                                   files={"catalog": ("catalog.yaml", content, "application/yaml")})

            assert post().status_code == 403
            assert post(_token(client.get("/achievements")), confirmed="").status_code == 400
            token = _token(client.get("/achievements"))
            imported = post(token)
            assert imported.status_code == 200 and "Imported 18 definitions." in imported.text
            assert ach.list_definitions()[0]["title"] == "Imported title"
            assert post(token).status_code == 403
            before = ach.export_catalog()
            assert post(_token(client.get("/achievements")), content=b"invalid").status_code == 400
            assert post(_token(client.get("/achievements")),
                        content=b"x" * (ach.MAX_CATALOG_BYTES + 1)).status_code == 400
            assert ach.export_catalog() == before
    finally:
        cleanup_db(path)


def test_achievement_rules_dates_and_user_isolation():
    path = setup_tmp_db()
    try:
        assert len(ach.for_user("u")) == 18
        assert not _earned()
        text = ach.dump_text()
        assert "ID: achievement-001" in text and "First Clear" in text
        assert ach.dump_text([]) == "No achievements found."
        _attempt(status="expired")
        _attempt(status="abandoned")
        assert not _earned()  # Failures never contribute.
        _attempt()
        _attempt(date="2026-03-01T12:00:00Z")  # Repeats never advance milestones.
        _attempt("easy-2", "2026-02-02T12:00:00Z")
        _attempt("easy-3", "2026-02-03T12:00:00Z")
        _attempt("easy-4", "2026-02-04T12:00:00Z")
        _attempt("physical-walk-easy", "2026-02-05T12:00:00Z")
        earned = _earned()
        assert earned["achievement-001"]["unlocked_at"] == "2026-02-01T12:00:00+00:00"
        assert earned["achievement-002"]["unlocked_at"] == "2026-02-05T12:00:00+00:00"
        assert "achievement-005" in earned and "achievement-006" not in earned
        assert "achievement-007" in earned and "achievement-008" in earned
        assert "achievement-009" not in earned
        assert not _earned("someone-else")
        _attempt("physical-endurance-easy", "2026-02-06T12:00:00Z")
        _attempt("hard-1", "2026-02-07T12:00:00Z")
        assert {"achievement-006", "achievement-009", "achievement-016"} <= set(_earned())
        items = ach.for_user("u")
        assert items[0]["unlocked_at"] == "2026-02-07T12:00:00+00:00"
        assert items[-1]["earned"] == 0
    finally:
        cleanup_db(path)


def test_achievement_completion_corrections_and_stable_ids():
    path = setup_tmp_db()
    try:
        aid = _attempt()
        original = _earned()["achievement-001"]["unlocked_at"]
        assert att.admin_update_attempt(aid, status="completed", note="Edited note")["ok"]
        assert att.get_attempt(aid)["completed_at"] == original
        with db_session() as conn:
            snapshot = json.loads(conn.execute(
                "SELECT challenge_snapshot_json FROM attempts WHERE id=?", (aid,)
            ).fetchone()[0])
            assert snapshot["id"] == "easy-1" and snapshot["media_filter"] == "manga"
            conn.execute("UPDATE challenge_defs SET media_filter='any' WHERE id='easy-1'")
        _attempt("easy-2")
        assert "achievement-007" in _earned()  # Historical media survives catalog edits.
        definition = next(d for d in ach.list_definitions() if d["id"] == "achievement-001")
        definition["title"] = "A New Title"
        definition["description"] = "A revised requirement description."
        assert ach.save_definition(definition) == "achievement-001"
        assert _earned()["achievement-001"]["unlocked_at"] == original
        assert att.admin_update_attempt(aid, completed_at="2026-01-02T05:00:00Z")["ok"]
        assert _earned()["achievement-001"]["unlocked_at"] == "2026-01-02T05:00:00+00:00"
        assert not att.admin_update_attempt(aid, completed_at="2025-12-01")["ok"]
        assert not att.admin_update_attempt(aid, completed_at="bad date")["ok"]
        assert att.admin_update_attempt(aid, status="void")["ok"]
        assert "achievement-007" not in _earned()
        ach.remove_definition("achievement-001")
        init_db(path)
        assert all(d["id"] != "achievement-001" for d in ach.list_definitions())
        assert len(ach.list_definitions(include_removed=True)) == 18
    finally:
        cleanup_db(path)


def test_achievement_atomic_completion_and_legacy_backfill():
    path = setup_tmp_db()
    try:
        aid = _attempt(status="active")
        result = att._complete(att.get_attempt(aid), method="honor", evidence={}, note=None)
        assert result["ok"]
        assert result["attempt"]["completed_at"] == result["completed_at"]
        assert _earned()["achievement-001"]["unlocked_at"] == result["completed_at"]
        with db_session() as conn:
            snapshot = json.loads(conn.execute(
                "SELECT challenge_snapshot_json FROM attempts WHERE id=?", (aid,)
            ).fetchone()[0])
            assert snapshot["year"] == 2026 and snapshot["category"] == "ota"
            assert snapshot["goal_kind"] == "volumes" and snapshot["goal_amount"] == 3
        assert not att._complete(att.get_attempt(aid), method="honor", evidence={}, note=None)["ok"]
        att.ensure_user("legacy")
        with db_session() as conn:
            conn.execute(
                "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at) "
                "VALUES ('legacy','easy-1','completed','2025-01-01','2025-01-02')"
            )
        init_db(path)
        assert _earned("legacy")["achievement-001"]["unlocked_at"] is None
        with db_session() as conn:
            legacy = conn.execute("SELECT * FROM attempts WHERE discord_id='legacy'").fetchone()
            assert legacy["completed_at"] is None
            assert json.loads(legacy["challenge_snapshot_json"])["media_filter"] == "manga"
        assert att.admin_update_attempt(legacy["id"], status="completed", note="Legacy edit")["ok"]
        assert _earned("legacy")["achievement-001"]["unlocked_at"] is None
    finally:
        cleanup_db(path)


def test_specials_do_not_grant_ladder_achievements():
    path = setup_tmp_db()
    try:
        _attempt("medium-6", "2026-02-01T12:00:00+00:00")
        earned = _earned()
        assert "achievement-014" in earned and "achievement-010" in earned
        assert "achievement-001" not in earned
        assert "achievement-017" not in earned
        _attempt("easy-1", "2026-02-01T12:00:00+00:00")
        earned = _earned()
        assert earned["achievement-001"]["unlocked_at"] == "2026-02-01T12:00:00+00:00"
        assert "achievement-017" not in earned
    finally:
        cleanup_db(path)


def test_achievement_quarter_century_and_rule_validation():
    path = setup_tmp_db()
    try:
        challenges = list_challenges()
        for challenge in challenges[:24]:
            _attempt(challenge["id"])
        assert "achievement-018" not in _earned()
        _attempt(challenges[24]["id"])
        assert "achievement-018" in _earned()
        for rule in [
            {"kind": "fails", "count": 1}, {"kind": "clears", "count": True},
            {"kind": "clears", "count": 0}, {"kind": "formats", "count": 6},
            {"kind": "groups", "groups": []}, {"kind": "groups", "groups": [["../private"]]},
        ]:
            try:
                ach.validate_rule(rule)
            except ValueError:
                continue
            raise AssertionError(f"Accepted an invalid rule: {rule}")
    finally:
        cleanup_db(path)


def test_achievement_admin_crud_auth_csrf_and_uploads():
    from app.web.app import create_app

    path = setup_tmp_db()
    uploaded = None
    try:
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")):
            client = TestClient(create_app(), headers={"Origin": "http://testserver"})
            assert client.get("/achievements").status_code == 401
            assert client.get("/api/achievements").status_code == 401
            assert client.get("/achievements/achievement-001/image").status_code == 401
            client.auth = ("admin", "secret")
            page = client.get("/achievements")
            assert "Achievements" in page.text and "achievement-018" in page.text
            image = BytesIO()
            Image.new("RGBA", (32, 32), (150, 128, 226, 255)).save(image, "PNG")
            data = dict(title="<b>New Achievement</b>", description="Complete one challenge.",
                        rule_json='{"kind":"clears","count":1}', token=_token(page))
            response = client.post("/achievements", data=data, files={"image": ("art.png", image.getvalue(), "image/png")})
            assert response.status_code == 200
            assert "&lt;b&gt;New Achievement&lt;/b&gt;" in response.text
            aid = response.url.path.rsplit("/", 1)[-1]
            assert not aid.startswith("new-achievement")
            item = next(d for d in ach.list_definitions() if d["id"] == aid)
            uploaded = ach.image_path(item["image_file"])
            assert uploaded.is_file()
            assert client.post("/achievements", data=data).status_code == 403
            _attempt()
            before = next(d for d in ach.for_user("u") if d["id"] == aid)["unlocked_at"]
            edit = client.get(f"/achievements/{aid}")
            saved = client.post(f"/achievements/{aid}", data={
                **data, "title": "Renamed", "token": _token(edit), "image_file": item["image_file"],
            })
            assert saved.status_code == 200
            assert next(d for d in ach.for_user("u") if d["id"] == aid)["unlocked_at"] == before
            assert "First Clear" in client.get("/users/u").text
            payload = client.get("/api/achievements?discord_id=u").json()
            assert next(d for d in payload if d["id"] == aid)["earned"]
            remove_tokens = re.findall(r'name="token" value="([^"]+)"', saved.text)
            assert client.post(f"/achievements/{aid}/remove", data={"token": remove_tokens[-1]}).status_code == 200
            assert not any(d["id"] == aid for d in ach.for_user("u"))
            bad = {**data, "token": _token(client.get("/achievements")), "image_file": "../private.png"}
            assert client.post("/achievements", data=bad).status_code == 400
            bad = {**data, "token": _token(client.get("/achievements")), "rule_json": '{"kind":"failures","count":1}'}
            assert client.post("/achievements", data=bad).status_code == 400
    finally:
        if uploaded:
            uploaded.unlink(missing_ok=True)
        cleanup_db(path)


def test_achievement_cards_hide_artwork_and_publish_clean_copy():
    from app.challenge_card import render_achievement_list, render_userinfo_card

    path = setup_tmp_db()
    try:
        _attempt()
        entries = ach.for_user("u")
        image_calls, text_calls = [], []
        original_open, original_text = Image.open, ImageDraw.ImageDraw.text

        def open_image(fp, *args, **kwargs):
            image_calls.append(str(fp))
            return original_open(fp, *args, **kwargs)

        def text(draw, xy, value, *args, **kwargs):
            text_calls.append(str(value))
            return original_text(draw, xy, value, *args, **kwargs)

        with patch.object(Image, "open", open_image), patch.object(ImageDraw.ImageDraw, "text", text):
            card = asyncio.run(render_userinfo_card(display_name="Challenger", achievements=entries))
            listing = asyncio.run(render_achievement_list(display_name="Challenger", achievements=entries))
        assert card.startswith(b"\x89PNG") and listing.startswith(b"\x89PNG")
        real_art = [p for p in image_calls if "achievement-" in p]
        assert real_art and all("achievement-001.png" in p for p in real_art)
        assert "Achievements" in text_calls
        assert not any("Newest" in t or "Earn an" in t or t in ("Locked", "Unlocked") for t in text_calls)
        assert "Getting Rolling" in text_calls  # Requirements are discoverable.
        small = original_open(BytesIO(card))
        base = original_open(BytesIO(asyncio.run(render_userinfo_card(display_name="Challenger"))))
        assert small.height - base.height == 280  # Three readable rows, no blank spacer.
        expanded = original_open(BytesIO(asyncio.run(render_userinfo_card(
            display_name="Challenger", achievements=entries + [entries[-1]],
        ))))
        assert expanded.height - small.height == 82  # A 19th tile wraps to a fourth row.
        assert "Date unavailable" not in text_calls
        for item in entries:
            item["description"] = "A long requirement with words that must wrap correctly. " * 6
        assert asyncio.run(render_achievement_list(display_name="A" * 100, achievements=entries)).startswith(b"\x89PNG")
    finally:
        cleanup_db(path)


def test_achievement_web_completion_dates_and_discord_pagination():
    from app.web.app import create_app
    from app.bot_commands import AchievementListView, challenge_achievements, challenge_group

    path = setup_tmp_db()
    try:
        aid = _attempt(date="2026-02-01T12:34:56Z")
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")):
            client = TestClient(create_app(), headers={"Origin": "http://testserver"})
            client.auth = ("admin", "secret")
            page = client.get(f"/attempts/{aid}")
            # Legacy completed_at is a confirmation-time proxy, not a known
            # actual completion. Never prefill the latter from the former.
            assert 'name="completed_time" value=""' in page.text
            before = att.get_attempt(aid)
            assert before["actual_completed_at"] is None
            assert client.post(f"/attempts/{aid}", data={
                "status": "completed", "note": "Missing audit reason",
            }).status_code == 400
            response = client.post(f"/attempts/{aid}", data={
                "status": "completed", "note": "Note edited through the web UI.",
                "completed_date": "2026-02-01", "completed_time": "12:34:56",
                "reason": "Attest known actual completion date",
            })
            assert response.status_code == 200
            assert 'name="token"' in response.text
            # Preview must not mutate dates, evidence, achievements or totals.
            assert att.get_attempt(aid)["completed_at"] == before["completed_at"]
            assert att.get_attempt(aid)["actual_completed_at"] is None
            assert client.post(f"/attempts/{aid}", data={
                "status": "completed", "completed_date": "not-a-date",
                "reason": "Reject malformed date",
            }).status_code == 400

        async def check():
            view = AchievementListView(1, "u", "Challenger")
            file, embeds = await view.card()
            assert file.filename == "achievements.png"
            file.close()
            assert view.previous.disabled and not view.next_page.disabled
            view.page = 100
            file, embeds = await view.card()
            file.close()
            assert view.page == 4 and not view.previous.disabled and view.next_page.disabled
            assert challenge_achievements.name == "achievements"
            assert len(challenge_group.commands) <= 25

        asyncio.run(check())
    finally:
        cleanup_db(path)
