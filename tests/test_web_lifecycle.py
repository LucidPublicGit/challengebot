"""Admin lifecycle integration checks using disposable SQLite only."""

import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import catalog
from app.config import Settings
from app.db import db_session
from app.services import attempts, badges, lifecycle, scoring
from app.web.app import create_app
from tests.helpers import cleanup_db, setup_tmp_db


@pytest.fixture
def client():
    path = setup_tmp_db()
    settings = Settings(ui_password="isolated-web-test")
    try:
        with patch("app.web.app.get_settings", return_value=settings), patch("app.web.lifecycle.get_settings", return_value=settings):
            with TestClient(create_app(), headers={"Origin": "http://testserver"}) as http:
                http.auth = ("admin", "isolated-web-test")
                yield http
    finally:
        cleanup_db(path)


def _monthly(cid="web-tales", number=909):
    result = attempts.admin_upsert_challenge(dict(
        id=cid, identity_id=cid, year=2026, tier="easy", number=number,
        title="Web Tales <script>", goal_kind="complete_work", goal_amount=1,
        duration_group="monthly", repeat_policy="annual", verify="honor", time_limit_hours=24,
    ))
    assert result["ok"], result
    return catalog.get_challenge(cid)


def _completed(challenge):
    attempts.ensure_user("web-member", "Isolated member")
    start = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
    end = lifecycle.calculate_deadline(challenge, start)
    completed = "2026-02-10T12:00:00+00:00"
    with db_session() as conn:
        aid = conn.execute(
            "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at,confirmed_at,param,content,verify_method) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("web-member", challenge["id"], "completed", start.isoformat(), end.isoformat(), completed, completed, completed, "Tales", "Tales", "honor"),
        ).lastrowid
        lifecycle.capture_attempt(conn, aid, challenge)
        scoring.sync_user(conn, "web-member")
        badges.sync_user(conn, "web-member")
    return aid


def _token(response):
    assert response.status_code == 200, response.text
    match = re.search(r'name="token" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def _policy(challenge, **changes):
    return dict(policy_target="override", application="retroactive", edition_ids=challenge["edition_id"],
                date_field="started_at", points_override="5", reason="Correct Tales award", **changes)


def _apply(client, response, **extra):
    return client.post("/lifecycle/apply", data=dict(token=_token(response), confirmed="yes", **extra))


def _award(aid):
    with db_session() as conn:
        return dict(conn.execute("SELECT * FROM award_ledger WHERE source_key=?", (f"completion:{aid}",)).fetchone())


def test_lifecycle_auth_and_csrf_before_service_access(client):
    for route in ("/lifecycle", "/lifecycle/rosters", "/lifecycle/history", "/lifecycle/attempts/1/history", "/lifecycle/challenges/easy-1/history"):
        assert client.get(route, auth=("wrong", "wrong")).status_code == 401
    with patch("app.web.lifecycle._service") as service:
        for route in ("/lifecycle/scoring/preview", "/lifecycle/rosters/preview", "/lifecycle/apply", "/lifecycle/revert/preview", "/lifecycle/challenges/easy-1/preview", "/attempts/1"):
            response = client.post(route, headers={"Origin": "https://evil.invalid"})
            assert response.status_code == 403
        service.assert_not_called()
    response = client.get("/lifecycle")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert client.post("/lifecycle/apply", data={"token": "forged", "confirmed": "yes"}).status_code == 403


def test_catalog_controls_and_non_destructive_routes(client):
    challenge = _monthly()
    page = client.get("/challenges/web-tales")
    assert page.status_code == 200
    for field in ("duration_group", "repeat_policy", "identity_id", "available_from", "available_until", "reason"):
        assert f'name="{field}"' in page.text
    assert "Web Tales &lt;script&gt;" in page.text
    assert "Web Tales <script>" not in page.text
    assert "Preview lifecycle change" in page.text and ">Delete<" not in page.text
    assert client.post("/challenges/web-tales/delete").status_code == 409
    assert catalog.get_challenge(challenge["id"])
    with patch('app.services.catalog_import.preview_import') as importer:
        assert client.post("/challenges/seed", data={"path": "unused", "replace": "1"}).status_code == 400
        importer.assert_not_called()


def test_retire_restore_and_stale_lifecycle_preview(client):
    challenge = _monthly()
    aid = _completed(challenge)
    data = dict(action="retire", identity="0", reason="Retire old edition")
    preview = client.post("/lifecycle/challenges/web-tales/preview", data=data)
    with db_session() as conn:
        conn.execute("UPDATE challenge_defs SET title='Concurrent edit' WHERE id='web-tales'")
    assert _apply(client, preview).status_code == 409
    assert catalog.get_challenge("web-tales")["lifecycle_state"] == "active"
    preview = client.post("/lifecycle/challenges/web-tales/preview", data=data)
    token = _token(preview)
    assert client.post("/lifecycle/apply", data={"token": token}).status_code == 400
    response = client.post("/lifecycle/apply", data={"token": token, "confirmed": "yes"})
    assert response.status_code == 200
    assert client.post("/lifecycle/apply", data={"token": token, "confirmed": "yes"}).text == response.text
    assert catalog.get_challenge("web-tales")["lifecycle_state"] == "retired"
    assert not any(c["id"] == "web-tales" for c in catalog.list_challenges())
    assert "web-tales" in client.get("/challenges?state=retired&year=2026").text
    assert _award(aid)["effective_points"] == 10
    assert badges.duration_badges("web-member")[0]["duration_group"] == "monthly"
    preview = client.post("/lifecycle/challenges/web-tales/preview", data={**data, "action": "restore", "reason": "Restore edition"})
    assert _apply(client, preview).status_code == 200
    assert catalog.get_challenge("web-tales")["lifecycle_state"] == "active"


def test_real_scoring_preview_apply_idempotency_and_revert(client):
    challenge = _monthly()
    aid = _completed(challenge)
    before_badges = badges.badge_history("web-member")
    preview = client.post("/lifecycle/scoring/preview", data=_policy(challenge))
    assert "Previous total" in preview.text and "-5" in preview.text
    assert _award(aid)["effective_points"] == 10
    # Apply ignores client-injected replacement policy and reason.
    response = _apply(client, preview, points_override="999", reason="Forged reason")
    assert response.status_code == 200, response.text
    op = re.search(r'Operation ID: <code>([^<]+)', response.text).group(1)
    assert _award(aid)["original_points"] == 10
    assert _award(aid)["effective_points"] == 5
    assert _apply(client, preview).status_code == 200
    with db_session() as conn:
        row = conn.execute("SELECT reason,actor FROM admin_operations WHERE operation_id=?", (op,)).fetchone()
        assert row["reason"] == "Correct Tales award" and row["actor"] == "web:admin"
        assert conn.execute("SELECT COUNT(*) FROM point_adjustments WHERE operation_id=?", (op,)).fetchone()[0] == 1
    assert badges.badge_history("web-member") == before_badges
    revert = client.post("/lifecycle/revert/preview", data={"operation_id": op, "reason": "Undo corrected award"})
    assert _apply(client, revert).status_code == 200
    assert _apply(client, revert).status_code == 200
    assert _award(aid)["effective_points"] == 10
    assert catalog.get_challenge("web-tales")["points"] is None
    assert client.get("/lifecycle/history").status_code == 200


def test_scoring_rejects_stale_preview_and_empty_scope(client):
    challenge = _monthly()
    aid = _completed(challenge)
    data = _policy(challenge)
    assert client.post("/lifecycle/scoring/preview", data={**data, "edition_ids": ""}).status_code == 400
    assert client.post("/lifecycle/scoring/preview", data={**data, "reason": " "}).status_code == 400
    assert client.post("/lifecycle/scoring/preview", data={**data, "date_field": "confirmed_at"}).status_code == 400
    assert client.post("/lifecycle/scoring/preview", data={**data, "points_override": "NaN"}).status_code == 400
    preview = client.post("/lifecycle/scoring/preview", data=data)
    with db_session() as conn:
        conn.execute("UPDATE attempts SET note='Concurrent correction' WHERE id=?", (aid,))
    assert _apply(client, preview).status_code == 409
    assert _award(aid)["effective_points"] == 10


def test_future_policy_and_explicit_clear_override(client):
    challenge = _monthly()
    aid = _completed(challenge)
    preview = client.post("/lifecycle/scoring/preview", data={**_policy(challenge), "application": "future"})
    assert _apply(client, preview).status_code == 200
    assert _award(aid)["effective_points"] == 10
    assert catalog.get_challenge("web-tales")["points"] == 5
    data = {**_policy(challenge), "points_override": "", "clear_override": "yes", "application": "future"}
    assert _apply(client, client.post("/lifecycle/scoring/preview", data=data)).status_code == 200
    assert catalog.get_challenge("web-tales")["points"] is None
    assert _award(aid)["effective_points"] == 10


def test_global_policy_requires_explicit_all_scope(client):
    data = dict(policy_target="global", application="future", multiplier_monthly="5", date_field="started_at", reason="New monthly default")
    assert client.post("/lifecycle/scoring/preview", data=data).status_code == 400
    assert client.post("/lifecycle/scoring/preview", data={**data, "all_editions": "yes", "years": "2026"}).status_code == 400
    assert _apply(client, client.post("/lifecycle/scoring/preview", data={**data, "all_editions": "yes"})).status_code == 200
    with db_session() as conn:
        assert scoring.current_policy(conn)["multipliers"]["monthly"] == 5


@pytest.mark.parametrize("scope_field", ["edition_ids", "identity_ids", "years"])
@pytest.mark.parametrize("application", ["future", "retroactive"])
def test_scoped_formula_preview_apply_and_revert(client, scope_field, application):
    challenge = _monthly()
    other = _monthly("web-other", 910)
    if scope_field == "years":
        # Year scopes must leave other catalog years untouched.
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET year=2025 WHERE id=?", (other["id"],))
            conn.execute("UPDATE challenge_editions SET year=2025 WHERE id=?", (other["edition_id"],))
        other = catalog.get_challenge(other["id"])
    aid, other_aid = _completed(challenge), _completed(other)
    value = {"edition_ids": challenge["edition_id"], "identity_ids": challenge["identity_id"], "years": "2026"}[scope_field]
    data = dict(policy_target="scoped", application=application, multiplier_monthly="5",
                date_field="started_at", reason="Scoped inherited monthly formula", **{scope_field: value})
    preview = client.post("/lifecycle/scoring/preview", data=data)
    assert _award(aid)["effective_points"] == 10
    response = _apply(client, preview)
    assert response.status_code == 200, response.text
    assert _apply(client, preview).status_code == 200
    assert _award(aid)["effective_points"] == (5 if application == "retroactive" else 10)
    assert _award(other_aid)["effective_points"] == 10
    assert catalog.get_challenge(challenge["id"])["points"] is None
    with db_session() as conn:
        assert scoring.capture_snapshot(conn, challenge)["points"] == 5
        assert scoring.capture_snapshot(conn, other)["points"] == 10
        assert scoring.current_policy(conn)["multipliers"]["monthly"] == 10
    op = re.search(r'Operation ID: <code>([^<]+)', response.text).group(1)
    revert = client.post("/lifecycle/revert/preview", data={"operation_id": op, "reason": "Undo scoped formula"})
    assert _apply(client, revert).status_code == 200
    assert _award(aid)["effective_points"] == 10
    with db_session() as conn:
        assert scoring.capture_snapshot(conn, challenge)["points"] == 10


def test_scoped_formula_requires_unambiguous_scope(client):
    challenge = _monthly()
    data = dict(policy_target="scoped", application="future", multiplier_monthly="5",
                date_field="started_at", reason="Review scope")
    for extra in ({}, {"all_editions": "yes"}, {"years": "2026", "all_editions": "yes"},
                  {"edition_ids": "unknown"}, {"identity_ids": "unknown"},
                  {"edition_ids": challenge["edition_id"], "points_override": "5"}):
        assert client.post("/lifecycle/scoring/preview", data={**data, **extra}).status_code == 400
    page = client.get("/lifecycle")
    assert 'value="scoped"' in page.text
    assert "Date filters below restrict historical attempts only" in page.text


def test_weekly_duration_and_multiplier_are_editable_in_web(client):
    page = client.get('/challenges')
    assert page.status_code == 200
    assert '<option value="weekly"' in page.text
    data = dict(id='web-weekly', year='2026', tier='easy', number='991', title='Weekly test',
                goal_kind='complete_work', goal_amount='1', duration_group='weekly', repeat_policy='annual',
                verify='honor', time_limit_hours='24', reason='Publish Weekly')
    response = _apply(client, client.post('/challenges/save', data=data))
    assert response.status_code == 200, response.text
    challenge = catalog.get_challenge('web-weekly')
    assert challenge['duration_group'] == 'weekly'
    assert 'duration=weekly' in client.get('/challenges').text
    data = dict(policy_target='scoped', application='future', edition_ids=challenge['edition_id'],
                multiplier_weekly='6', date_field='started_at', reason='Scoped Weekly multiplier')
    assert _apply(client, client.post('/lifecycle/scoring/preview', data=data)).status_code == 200
    with db_session() as conn:
        assert scoring.capture_snapshot(conn, challenge)['points'] == 6


def test_attempt_edits_use_real_correction_preview_and_history(client):
    aid = _completed(_monthly())
    page = client.get(f"/attempts/{aid}")
    assert page.status_code == 200 and "Preview correction" in page.text
    data = dict(content="Tales corrected <script>", reason="Correct selected work", completed_date="2026-02-10", completed_time="12:00:00")
    preview = client.post(f"/attempts/{aid}", data=data)
    assert "Tales corrected &lt;script&gt;" in preview.text
    assert attempts.get_attempt(aid)["content"] == "Tales"
    assert _apply(client, preview).status_code == 200
    assert attempts.get_attempt(aid)["content"] == "Tales corrected <script>"
    assert _award(aid)["effective_points"] == 10
    history = client.get(f"/lifecycle/attempts/{aid}/history")
    assert history.status_code == 200 and "Correct selected work" in history.text
    assert client.get("/lifecycle/challenges/web-tales/history").status_code == 200


def test_correction_deadline_choices_and_stale_guard(client):
    aid = _completed(_monthly())
    data = dict(started_date="2026-01-30", started_time="12:00", reason="Correct start", recompute_deadline="yes")
    preview = client.post(f"/attempts/{aid}", data=data)
    assert _apply(client, preview).status_code == 200
    assert attempts.get_attempt(aid)["original_started_at"].startswith("2026-01-31")
    assert attempts.get_attempt(aid)["started_at"].startswith("2026-01-30")
    preview = client.post(f"/attempts/{aid}", data={"note": "Correction", "reason": "Audited note"})
    with db_session() as conn:
        conn.execute("UPDATE attempts SET content='Changed by another admin' WHERE id=?", (aid,))
    assert _apply(client, preview).status_code == 409


def test_new_catalog_fields_reach_service_and_daily_validation(client):
    data = dict(id="web-created", year="2027", tier="easy", number="900", title="Created in web",
                goal_kind="complete_work", goal_amount="1", duration_group="daily", repeat_policy="annual", reason="Publish challenge",
                time_limit_hours="25", verify="honor", available_from="2027-01-01T00:00:00+00:00")
    assert client.post("/challenges/save", data=data).status_code == 400
    response = client.post("/challenges/save", data={**data, "duration_group": "monthly"}, follow_redirects=False)
    assert response.status_code == 200, response.text
    assert catalog.get_challenge('web-created') is None
    assert 'roster_impact' in response.text
    applied = _apply(client, response)
    assert applied.status_code == 200, applied.text
    assert _apply(client, response).text == applied.text
    saved = catalog.get_challenge("web-created")
    assert saved["duration_group"] == "monthly" and saved["repeat_policy"] == "annual"
    assert saved["available_from"] == "2027-01-01T00:00:00+00:00"
