"""Authenticated lifecycle administration; writes go through domain services.

Preview receipts are bounded, process-local, and expire after 30 minutes. A
restart requires a fresh preview, while committed operation IDs live in the
backend. Apply requests never accept a replacement policy or correction body.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request

from app import catalog
from app.config import get_settings
from app.db import db_session
from app.services import attempts
from app.web.app import _esc, _page

DURATIONS = ("daily", "monthly", "yearly", "special")
REPEATS = ("once_ever", "annual", "legacy")


def _service(name):
    # Lazy imports allow a disabled admin UI to start during a rolling upgrade.
    try:
        return importlib.import_module(f"app.services.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"app.services.{name}":
            raise
        raise HTTPException(503, f"The {name} service is not installed.") from exc


def _call(fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(409 if "stale" in message.lower() else 400, message) from exc
    if isinstance(result, dict) and result.get("ok") is False:
        message = str(result.get("error") or "Operation rejected.")
        raise HTTPException(409 if "stale" in message.lower() else 400, message)
    return result


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _details(title, value, *, open=False):
    return (f'<details class="block" {"open" if open else ""}><summary>{_esc(title)}</summary>'
            f'<div class="block-body"><pre class="evidence mono">{_esc(_json(value))}</pre></div></details>')


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _scoring_summary(preview):
    totals = preview.get("totals", {})
    rows = "".join(f'<tr><td>{_esc(member)}</td><td>{_esc(t["old_points"])}</td><td>{_esc(t["new_points"])}</td><td>{_esc(t["delta"])}</td></tr>' for member, t in totals.items())
    return (f'<p>{len(preview.get("changes", []))} completion awards change; '
            f'{len(totals)} members affected; {len(preview.get("open_attempts", []))} open attempts included.</p>'
            f'<p>Excluded explicit overrides: {_esc(", ".join(preview.get("excluded_overrides", [])) or "None")}.</p>'
            + ('<table><thead><tr><th>Member</th><th>Previous total</th><th>New total</th><th>Delta</th></tr></thead><tbody>' + rows + '</tbody></table>' if rows else '<p>No historical point totals change.</p>'))


def _reason(form):
    value = str(form.get("reason") or "").strip()
    if not value or len(value) > 2000:
        raise HTTPException(400, "Enter an audit reason (1–2000 characters).")
    return value


def _instant(raw, label):
    if not str(raw or "").strip():
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"Enter a valid {label}.") from exc
    # datetime-local fields are explicitly labeled UTC in the UI.
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt.tzinfo is None else dt.astimezone(timezone.utc).isoformat()


def _number(raw, label, *, integer=False):
    try:
        value = float(raw)
        if not math.isfinite(value) or value < 0 or (integer and value != int(value)):
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(400, f"{label} must be a finite nonnegative {'integer' if integer else 'number'}.") from exc
    return int(value) if integer else value


def _options(values, selected):
    return "".join(f'<option value="{_esc(v)}" {"selected" if v == selected else ""}>{_esc(v.replace("_", " ").title())}</option>' for v in values)


def catalog_fields(c):
    duration = c.get("duration_group") or ("special" if c.get("tier") == "special" else "daily")
    repeat = c.get("repeat_policy") or ("legacy" if c.get("id") else "once_ever")
    return f"""
      <div><label>Duration group</label><select name="duration_group">{_options(DURATIONS, duration)}</select>
        <p class="muted">Daily: configured hours, at most 24. Monthly / Yearly: one personal calendar month / year from actual start. Special: custom legacy window.</p></div>
      <div><label>Repeat eligibility</label><select name="repeat_policy">{_options(REPEATS, repeat)}</select>
        <p class="muted">Annual means once per published catalog-year edition; it does not republish a retired identity. Legacy preserves migrated exceptions.</p></div>
      <div><label>Permanent identity</label><input name="identity_id" value="{_esc(c.get('identity_id') or '')}" {"readonly" if c.get('id') else ''}/>
        <p class="muted">For a new annual edition, explicitly use its existing permanent identity. Blank creates a new identity.</p></div>
      <div><label>Edition</label><input value="{_esc(c.get('edition_id') or 'Assigned on creation')}" readonly/></div>
      <div><label>Available from (UTC)</label><input name="available_from" value="{_esc(c.get('available_from') or '')}" placeholder="2026-10-15T00:00:00+00:00"/></div>
      <div><label>Available until, exclusive (UTC)</label><input name="available_until" value="{_esc(c.get('available_until') or '')}" placeholder="2027-01-01T00:00:00+00:00"/></div>
      <div class="span-2"><p class="muted">Definition changes apply prospectively. Use <a href="/lifecycle">Scoring &amp; history</a> to preview scoring policies or historical adjustments. Existing attempts retain their accepted rules and deadlines.</p>
        <label>Reason for definition revision</label><textarea name="reason" maxlength="2000" required></textarea></div>
    """


def challenge_panel(c):
    cid = quote(str(c["id"]), safe="")
    return f"""
      <div class="card"><h2>Lifecycle</h2>
        <p><b>{_esc(c.get('lifecycle_state') or 'active')}</b> · {_esc(c.get('duration_group') or 'daily')}
        · {_esc(c.get('repeat_policy') or 'legacy')}</p>
        <p>Retirement blocks new starts. Open attempts retain their deadlines and may finish. Restoration does not renew an annual edition or change completion history.</p>
        <form method="post" action="/lifecycle/challenges/{cid}/preview">
          <div class="grid"><div><label>Action</label><select name="action"><option value="retire">Retire</option><option value="restore">Restore now / schedule restoration</option><option value="cancel_restore">Cancel pending restoration</option></select></div>
          <div><label>Scope</label><select name="identity"><option value="0">This edition only</option><option value="1">Permanent identity (all editions)</option></select></div>
          <div><label>Effective at (UTC; blank = now)</label><input name="effective_at" type="datetime-local" step="1"/></div>
          <div class="span-2"><label>Reason</label><textarea name="reason" required maxlength="2000"></textarea></div></div>
          <button type="submit">Preview lifecycle change</button>
        </form><p><a href="/lifecycle/challenges/{cid}/history">Definition and lifecycle history</a></p>
      </div>
    """


def correction_fields(a):
    return f"""
      <div class="span-2"><h2>Audited correction</h2><p class="muted">Preview verification and eligibility, points, badges and bonuses before applying. Original facts are retained. Confirmation and actual completion timestamps are separate.</p></div>
      <div><label>Corrected tier (blank = unchanged)</label><select name="correction_tier"><option value="">Unchanged</option>{_options(('easy', 'medium', 'hard', 'ota_master', 'special'), '')}</select></div>
      <div><label>Corrected duration (blank = unchanged)</label><select name="correction_duration_group"><option value="">Unchanged</option>{_options(DURATIONS, '')}</select></div>
      <div><label>Corrected category (blank = unchanged)</label><select name="correction_category"><option value="">Unchanged</option>{_options(('ota', 'physical'), '')}</select></div>
      <div><label>Confirmation timestamp (UTC; blank = unchanged)</label><input name="confirmed_at" value="{_esc(a.get('confirmed_at') or '')}"/></div>
      <div><label>Verification timestamp (UTC; blank = unchanged)</label><input name="verified_at" value="{_esc(a.get('verified_at') or '')}"/></div>
      <div class="span-2"><label>Evidence JSON replacement (blank = unchanged; existing evidence remains in history)</label><textarea name="evidence_json" rows="3"></textarea></div>
      <div><label><input type="checkbox" name="recompute_deadline" value="yes"/> Recompute deadline from corrected start</label><p class="muted">Unchecked retains the effective deadline. An explicit deadline edit is shown in the preview.</p></div>
      <div class="span-2"><label>Intentional date/eligibility exception reason (only if needed)</label><textarea name="exception_reason" maxlength="2000"></textarea></div>
      <div class="span-2"><label>Correction reason</label><textarea name="reason" required maxlength="2000"></textarea></div>
      <div class="span-2"><a href="/lifecycle/attempts/{int(a['id'])}/history">Original facts and correction history</a></div>
    """


def _history(table, field=None, value=None):
    # Names are internal constants, never identifiers from a request.
    with db_session() as conn:
        return [dict(r) for r in conn.execute(
            f'SELECT * FROM {table}' + (f' WHERE {field}=?' if field else '') + ' ORDER BY rowid DESC LIMIT 200',
            (value,) if field else (),
        )]


def _revert_state(operation_id):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM admin_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Scoring operation not found.")
        if row["operation_type"] not in ("scoring", "recalculation", "scoring_recalculation"):
            raise HTTPException(400, "Only scoring recalculation operations can be reverted here.")
        affected = [dict(r) for r in conn.execute(
            "SELECT p.*, a.discord_id, a.metadata_json, a.effective_points AS current_points, a.revision AS current_revision "
            "FROM point_adjustments p JOIN award_ledger a ON a.source_key=p.source_key "
            "WHERE p.operation_id=? ORDER BY p.source_key", (operation_id,))]
        # Policies and definitions can change even for a future-only operation
        # with no award adjustment. Include them in the web preview guard.
        policies = [dict(r) for r in conn.execute("SELECT * FROM scoring_policies ORDER BY id")]
        definitions = [dict(r) for r in conn.execute("SELECT * FROM challenge_defs ORDER BY id")]
        payload = json.loads(row["payload_json"])
        open_attempts = []
        for change in payload.get("open_attempts", []):
            current = conn.execute("SELECT * FROM attempts WHERE id=?", (change["attempt_id"],)).fetchone()
            open_attempts.append(dict(current) if current else None)
        for source in affected:
            source["compensated_points"] = source["current_points"] - source["delta"]
        return dict(operation=dict(row), affected_sources=affected, policies=policies,
                    definitions=definitions, open_attempts=open_attempts)


def _lifecycle_state(cid, identity):
    item = catalog.get_challenge(cid)
    if not item:
        raise HTTPException(404, "Challenge not found.")
    related = [c for c in catalog.list_challenges(include_retired=True) if c.get("identity_id") == item.get("identity_id")] if identity else [item]
    with db_session() as conn:
        parent = conn.execute("SELECT * FROM challenge_identities WHERE id=?", (item.get("identity_id"),)).fetchone()
        editions = [dict(r) for r in conn.execute("SELECT * FROM challenge_editions WHERE identity_id=? ORDER BY id", (item.get("identity_id"),))]
    # updated_at is changed by unrelated ensure_catalog_records calls. Immutable
    # revision IDs and material lifecycle fields are sufficient here.
    for edition in editions:
        edition.pop("updated_at", None)
    return dict(affected_editions=related, identity=dict(parent) if parent else None, editions=editions)


def register_lifecycle(app, auth):
    receipts = {}
    lock = threading.RLock()

    def actor():
        return f"web:{get_settings().ui_username or 'admin'}"

    def remember(kind, payload, reason, back, summary=None):
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with lock:
            for old in list(receipts):
                if receipts[old]["expires"] < now:
                    del receipts[old]
            if len(receipts) >= 256:
                del receipts[next(iter(receipts))]
            receipt = dict(kind=kind, payload=payload, reason=reason, back=back,
                           operation_id=f"web-{uuid.uuid4().hex}", actor=actor(), expires=now + 1800)
            receipts[token] = receipt
        body = f"""<div class="card"><h2>Review before applying</h2>
          <p>Operation <code>{receipt['operation_id']}</code></p><p>Reason: {_esc(reason)}</p>
          <p>The preview expires in 30 minutes. Concurrent changes require a fresh preview. Applying again uses the same operation ID.</p>
          {summary or ''}</div>{_details('Affected sources, scope and changes', payload, open=True)}
          <div class="card"><form method="post" action="/lifecycle/apply">
            <input type="hidden" name="token" value="{token}"/>
            <label><input type="checkbox" name="confirmed" value="yes" required/> I reviewed the scope, changed facts and affected awards.</label>
            <div class="actions"><button type="submit">Apply reviewed operation</button><a class="btn secondary" href="{_esc(back)}">Cancel</a></div>
          </form></div>"""
        return _page("Preview operation", body, "/lifecycle")

    def preview_catalog_definition(data, reason):
        preview = _call(_service('catalog_admin').preview_definition, data)
        affected = sum(len(members) for members in preview['roster_impact'].values())
        return remember('catalog', preview, reason, '/challenges',
                        f'<p>{affected} member/cohort progress or award projections change. This publishes prospective roster versions only; existing earned awards are preserved. Inspect the definition and every affected roster below.</p>')

    app.state.preview_catalog_definition = preview_catalog_definition

    def preview_catalog_import(data, reason):
        preview = _call(_service('catalog_import').preview_import, data)
        affected = sum(len(members) for members in preview['roster_impact'].values())
        return remember('catalog_import', preview, reason, '/challenges',
                        f'<p>Merge {len(preview["data"]["challenges"])} definitions; {affected} member/cohort projections change. Omitted definitions and histories remain untouched. Only final roster sets are published; no announcements are sent.</p>')

    app.state.preview_catalog_import = preview_catalog_import

    @app.get('/lifecycle/rosters')
    def roster_page(_: None = Depends(auth)):
        rows = _history('daily_rosters')
        superseded = {r['supersedes_roster_id'] for r in rows if r.get('supersedes_roster_id') is not None}
        options = ''.join(f'<option value="{int(r["id"])}">{int(r["year"])} · {_esc(r["tier"])} · version {int(r["version"])} · roster #{int(r["id"])}</option>' for r in rows if r['id'] not in superseded)
        body = f'''<div class="card"><h2>Historical Daily roster repair</h2>
          <p>This is an explicit historical correction, not an ordinary prospective catalog edit.
          Original requirements and receipts remain in history. A replacement version is appended;
          affected progress, awards and signed bonuses are recomputed for all members in this year/tier.
          Monthly/Yearly challenges cannot be added as Daily requirements. An empty roster never awards a sweep.</p>
          <form method="post" action="/lifecycle/rosters/preview"><div class="grid">
          <div><label>Roster to repair</label><select name="roster_id" required>{options}</select></div>
          <div class="span-2"><label>Replacement edition IDs (one per line or comma-separated; blank = empty roster)</label><textarea name="requirements" rows="6"></textarea></div>
          <div class="span-2"><label>Audit reason</label><textarea name="reason" required maxlength="2000"></textarea></div>
          <div class="span-2"><label><input type="checkbox" name="historical_repair" value="yes" required/> Intentionally re-evaluate historical Daily awards, including possible revocation and bonus adjustments</label></div>
          </div><button type="submit">Preview affected progress and awards</button></form></div>'''
        body += _details('Versioned requirements and replacement links (latest 200)', rows, open=True)
        return _page('Daily sweep rosters', body, '/lifecycle')

    @app.post('/lifecycle/rosters/preview')
    async def roster_preview(request: Request, _: None = Depends(auth)):
        form = await request.form()
        reason = _reason(form)
        if form.get('historical_repair') != 'yes':
            raise HTTPException(400, 'Explicit historical repair acknowledgement required.')
        try:
            roster_id = int(str(form.get('roster_id') or ''))
        except ValueError as exc:
            raise HTTPException(400, 'Select a valid roster ID.') from exc
        raw = str(form.get('requirements') or '')
        if len(raw) > 120000:
            raise HTTPException(400, 'Roster requirements are too large.')
        requirements = [x.strip() for x in raw.replace(',', '\n').splitlines() if x.strip()]
        preview = _call(_service('rosters').preview_repair, roster_id, requirements)
        delta = sum(p['points_delta'] for p in preview['impact'].values())
        return remember('roster', preview, reason, '/lifecycle/rosters',
                        f'<p>{len(preview["impact"])} members have changed progress or awards; total point delta {_esc(delta)}. Review every member\'s before/after badge, requirements and missing clears below. Unrelated durations are excluded.</p>')

    @app.get("/lifecycle")
    def lifecycle_page(_: None = Depends(auth)):
        items = catalog.list_challenges(include_retired=True)
        options = "".join(f'<option value="{_esc(c.get("edition_id") or c["id"])}">{_esc(c["year"])} · {_esc(c["id"])} · {_esc(c["title"])} ({_esc(c.get("lifecycle_state") or "active")})</option>' for c in items)
        identities = sorted({str(c.get("identity_id")) for c in items if c.get("identity_id")})
        identity_options = "".join(f'<option value="{_esc(identity)}">{_esc(identity)}</option>' for identity in identities)
        bases = "".join(f'<div><label>{_esc(tier.replace("_", " ").title())} base</label><input type="number" min="0" step="1" name="base_{tier}" placeholder="Unchanged"/></div>' for tier in ("easy", "medium", "hard", "ota_master"))
        multipliers = "".join(f'<div><label>{duration.title()} multiplier</label><input type="number" min="0" step="1" name="multiplier_{duration}" placeholder="Unchanged"/></div>' for duration in DURATIONS[:3])
        body = f"""
        <div class="card"><p><a href="/lifecycle/rosters">Preview historical Daily roster repair</a> · <a href="/lifecycle/history">Operation history</a></p><h2>Scoring policy</h2>
          <p>Daily / Monthly / Yearly defaults are difficulty base × 1 / 10 / 50. Supply only values to change. Absolute overrides remain distinct from inherited points. Point changes preserve clear counts, completion dates and completion-based badges.</p>
          <form method="post" action="/lifecycle/scoring/preview"><div class="grid">
            <div><label>Policy target</label><select name="policy_target"><option value="global">Shared defaults</option><option value="override">Selected edition override</option></select></div>
            <div><label>Application</label><select name="application"><option value="future">Future attempts only</option><option value="retroactive">Retroactive — explicit selected editions</option></select></div>
            <div class="span-2"><label><input type="checkbox" name="all_editions" value="yes"/> Shared defaults: explicitly select all catalog editions, including retired editions</label>
              <p class="muted">The service currently supports shared default changes across the whole catalog. For a selected edition use an absolute override. Scoped shared formula revisions are unavailable.</p></div>
            {bases}{multipliers}
            <div><label>Absolute points override</label><input type="number" name="points_override" min="0" step="1"/></div>
            <div><label><input type="checkbox" name="clear_override" value="yes"/> Explicitly clear override and return to formula inheritance</label></div>
            <div class="span-2"><label>Edition scope (select one or more for historical changes or overrides)</label><select multiple name="edition_ids" size="8">{options}</select>
              <p class="muted">Retired editions are included in these choices. Select exactly which editions may change. Shared defaults with future-only and no selection apply globally.</p></div>
            <div class="span-2"><label>Permanent identity scope (optional intersection with editions)</label><select multiple name="identity_ids" size="4">{identity_options}</select></div>
            <div><label>Catalog years (optional comma-separated intersection)</label><input name="years" placeholder="2025,2026"/></div>
            <div><label>Date-filter semantics</label><select name="date_field"><option value="started_at">Actual attempt start</option><option value="completed_at">Actual completion (unknown dates are excluded)</option></select></div>
            <div><label>Date from, inclusive (UTC)</label><input type="datetime-local" step="1" name="date_from"/></div>
            <div><label>Date until, exclusive (UTC)</label><input type="datetime-local" step="1" name="date_until"/></div>
            <div><label><input type="checkbox" name="include_open" value="yes"/> Include existing open attempts</label><p class="muted">Default: open attempts retain their accepted award.</p></div>
            <div><label><input type="checkbox" name="include_overrides" value="yes"/> Include explicit overrides in shared policy recalculation</label></div>
            <div class="span-2"><label>Reason</label><textarea name="reason" required maxlength="2000"></textarea></div>
          </div><button type="submit">Preview policy and awards</button></form></div>
          <div class="card"><h2>Revert a scoring operation</h2><p>Creates a compensating audited operation from recorded affected sources; intervening changes are checked by the service.</p>
          <form method="post" action="/lifecycle/revert/preview"><label>Original operation ID</label><input name="operation_id" required/>
          <label>Reason for revert</label><textarea name="reason" required maxlength="2000"></textarea><button type="submit">Review revert</button></form></div>
          <p><a href="/lifecycle/history">Scoring operation history</a> · <a href="/challenges?state=retired">Past / retired challenges</a></p>
        """
        return _page("Scoring & history", body, "/lifecycle")

    @app.post("/lifecycle/scoring/preview")
    async def scoring_preview(request: Request, _: None = Depends(auth)):
        form = await request.form()
        reason = _reason(form)
        target, application = form.get("policy_target"), form.get("application")
        if target not in ("global", "override") or application not in ("future", "retroactive"):
            raise HTTPException(400, "Choose a policy target and application scope.")
        editions = list(dict.fromkeys(str(x) for x in form.getlist("edition_ids") if str(x)))
        known = {str(c.get("edition_id") or c["id"]) for c in catalog.list_challenges(include_retired=True)}
        if set(editions) - known:
            raise HTTPException(400, "Unknown edition in scope.")
        if target == "override" and not editions:
            raise HTTPException(400, "Select explicit editions for a historical change or override.")
        scope = {"retroactive": application == "retroactive"}
        if editions:
            scope["edition_ids"] = editions
        identities = list(dict.fromkeys(str(x) for x in form.getlist("identity_ids") if str(x)))
        known_identities = {str(c.get("identity_id")) for c in catalog.list_challenges(include_retired=True)}
        if set(identities) - known_identities:
            raise HTTPException(400, "Unknown permanent identity in scope.")
        if identities:
            scope["identity_ids"] = identities
        if form.get("years"):
            try:
                scope["years"] = sorted({int(y.strip()) for y in str(form["years"]).split(",")})
                if any(y < 1 or y > 9999 for y in scope["years"]):
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, "Enter comma-separated catalog years.") from exc
        if form.get("date_field") not in ("started_at", "completed_at"):
            raise HTTPException(400, "Choose attempt start or actual completion for the date filter.")
        scope["date_field"] = form["date_field"]
        for key in ("date_from", "date_until"):
            value = _instant(form.get(key), key.replace("_", " "))
            if value:
                scope[key] = value
        if scope.get("date_from") and scope.get("date_until") and scope["date_from"] >= scope["date_until"]:
            raise HTTPException(400, "The exclusive date limit must follow the start.")
        policy = {}
        if target == "override":
            if form.get("clear_override") == "yes":
                if str(form.get("points_override") or "").strip():
                    raise HTTPException(400, "Choose either an absolute override or explicitly clear it.")
                policy["points_override"] = None
            else:
                policy["points_override"] = _number(form.get("points_override"), "Override", integer=True)
        else:
            for field, prefix, values in (("base_points", "base_", ("easy", "medium", "hard", "ota_master")), ("multipliers", "multiplier_", DURATIONS[:3])):
                changes = {key: _number(form[prefix + key], key, integer=True) for key in values if str(form.get(prefix + key) or "").strip()}
                if changes:
                    policy[field] = changes
            if not policy:
                raise HTTPException(400, "Supply at least one shared policy value to change.")
            if str(form.get("points_override") or "").strip() or form.get("clear_override"):
                raise HTTPException(400, "Choose the edition override target to change or clear an override.")
            if editions or identities or scope.get("years"):
                raise HTTPException(400, "Scoped shared formula changes are not supported by the service. Use a selected edition override or explicitly choose all editions for shared defaults.")
            if form.get("all_editions") != "yes":
                raise HTTPException(400, "Explicitly select all editions for a shared default change.")
        if application == "future" and form.get("include_open") == "yes":
            raise HTTPException(400, "To include existing open attempts, choose retroactive application and review its explicit scope.")
        preview = _call(_service("scoring").preview_recalculation, policy, scope,
                        include_open=form.get("include_open") == "yes", include_overrides=form.get("include_overrides") == "yes")
        return remember("scoring", preview, reason, "/lifecycle", _scoring_summary(preview))

    @app.post("/lifecycle/challenges/{cid}/preview")
    async def lifecycle_preview(cid: str, request: Request, _: None = Depends(auth)):
        form = await request.form()
        reason = _reason(form)
        action = form.get("action")
        if action not in ("retire", "restore", "cancel_restore") or form.get("identity") not in ("0", "1"):
            raise HTTPException(400, "Choose retirement/restoration and edition/identity scope.")
        item = catalog.get_challenge(cid)
        if not item:
            raise HTTPException(404, "Challenge not found.")
        identity = form["identity"] == "1"
        effective = _instant(form.get("effective_at"), "effective time")
        if action == 'cancel_restore':
            if effective:
                raise HTTPException(400, 'Cancellation is immediate; leave effective time blank.')
            state = _lifecycle_state(cid, identity)
            payload = dict(challenge_id=cid, action=action, identity=identity,
                           effective_at=None, affected_records=state, version=_fingerprint(state))
            return remember('lifecycle', payload, reason, f"/challenges/{quote(cid, safe='')}")
        preview = _call(_service('catalog_admin').preview_lifecycle, cid, action=action, identity=identity,
                        effective_at=effective, reason=reason)
        return remember('catalog_lifecycle', preview, reason, f"/challenges/{quote(cid, safe='')}",
                        '<p>Review current progress/award effects and, for a future boundary, the explicitly labeled forecast based only on already-known clears. Existing attempt deadlines and earned roster receipts are preserved.</p>')

    async def preview_attempt(request, attempt_id):
        from app.web.app import _date_time_to_iso, _iso_to_date_time

        form = await request.form()
        reason = _reason(form)
        item = attempts.get_attempt(attempt_id)
        if not item:
            raise HTTPException(404, "Attempt not found.")
        changes = {}
        for field in ("status", "verify_method", "param", "content", "note"):
            if field in form and str(form[field]) != str(item.get(field) or ""):
                changes[field] = str(form[field])
        for field, prefix in (("started_at", "started"), ("ends_at", "ends"), ("actual_completed_at", "completed")):
            if prefix + "_date" not in form:
                continue
            day, clock = str(form.get(prefix + "_date") or ""), str(form.get(prefix + "_time") or "")
            if (day, clock) == _iso_to_date_time(item.get(field), seconds=field == "actual_completed_at"):
                continue  # Preserve unknown facts and original sub-minute precision.
            if not day:
                raise HTTPException(400, "Use an explicit status correction to void a completion; dates cannot be silently erased.")
            value = _date_time_to_iso(day, clock)
            if not value:
                raise HTTPException(400, f"Enter a valid {prefix} date and time.")
            changes[field] = value
        for field in ("confirmed_at", "verified_at"):
            if str(form.get(field) or "").strip() and str(form[field]) != str(item.get(field) or ""):
                changes[field] = _instant(form[field], field)
        if str(form.get("evidence_json") or "").strip():
            try:
                value = json.loads(str(form["evidence_json"]))
                if not isinstance(value, (dict, list)):
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, "Evidence must be a JSON object or list.") from exc
            changes["evidence_json"] = json.dumps(value, ensure_ascii=False)
        for field in ("tier", "duration_group", "category"):
            if form.get("correction_" + field):
                changes[field] = str(form["correction_" + field])
        if not changes:
            raise HTTPException(400, "No facts changed. Enter a correction to preview.")
        preview = _call(_service("lifecycle").preview_correction, attempt_id, changes,
                        recompute_deadline=form.get("recompute_deadline") == "yes",
                        exception_reason=str(form.get("exception_reason") or "").strip() or None)
        return remember("correction", preview, reason, f"/attempts/{attempt_id}",
                        '<p>Dependency review: verification/eligibility → completion facts → points → badges/bonuses → profiles, leaderboard and achievement projections.</p>')

    app.state.preview_attempt_correction = preview_attempt

    @app.post("/lifecycle/apply")
    async def apply_preview(request: Request, _: None = Depends(auth)):
        form = await request.form()
        with lock:
            receipt = receipts.get(str(form.get("token") or ""))
            if not receipt or receipt["expires"] < time.monotonic() or receipt["actor"] != actor():
                raise HTTPException(403, "Preview expired or unavailable. Create a fresh preview.")
            if form.get("confirmed") != "yes":
                raise HTTPException(400, "Confirm the reviewed operation.")
            # Serialize repeated clicks, retaining the ID and result for retries.
            if "result" not in receipt:
                payload, kind = receipt["payload"], receipt["kind"]
                kwargs = dict(actor=receipt["actor"], reason=receipt["reason"], operation_id=receipt["operation_id"])
                if kind == "scoring":
                    result = _call(_service("scoring").apply_recalculation, payload, **kwargs)
                elif kind == "correction":
                    result = _call(_service("lifecycle").apply_correction, payload, **kwargs)
                elif kind == 'roster':
                    result = _call(_service('rosters').apply_repair, payload, **kwargs)
                elif kind == 'catalog':
                    result = _call(_service('catalog_admin').apply_definition, payload, **kwargs)
                elif kind == 'catalog_import':
                    result = _call(_service('catalog_import').apply_import, payload, **kwargs)
                elif kind == 'catalog_lifecycle':
                    result = _call(_service('catalog_admin').apply_lifecycle, payload, **kwargs)
                elif kind == "lifecycle":
                    if _fingerprint(_lifecycle_state(payload["challenge_id"], payload["identity"])) != payload["version"]:
                        raise HTTPException(409, "Stale lifecycle preview. Review the current editions again.")
                    fn = getattr(_service("lifecycle"), payload["action"] + "_challenge")
                    result = _call(fn, payload["challenge_id"], identity=payload["identity"], effective_at=payload["effective_at"], **kwargs)
                elif kind == "revert":
                    if _fingerprint(_revert_state(payload["original_operation_id"])) != payload["version"]:
                        raise HTTPException(409, "Stale revert preview. Review intervening scoring changes again.")
                    result = _call(_service("scoring").revert_operation, payload["original_operation_id"],
                                   actor=receipt["actor"], reason=receipt["reason"], revert_operation_id=receipt["operation_id"])
                else:
                    raise HTTPException(400, "Unsupported operation.")
                receipt["result"] = result
            body = f'<div class="card"><h2>Operation applied</h2><p>Operation ID: <code>{receipt["operation_id"]}</code></p><p>{_esc(receipt["reason"])}</p><a href="{_esc(receipt["back"])}">Back</a> · <a href="/lifecycle/history">History</a></div>' + _details("Result", receipt["result"], open=True)
        return _page("Operation applied", body, "/lifecycle")

    @app.post("/lifecycle/revert/preview")
    async def revert_preview(request: Request, _: None = Depends(auth)):
        form = await request.form()
        reason = _reason(form)
        original = str(form.get("operation_id") or "").strip()
        if not original or len(original) > 200:
            raise HTTPException(400, "Enter a valid original operation ID.")
        state = _revert_state(original)
        payload = dict(original_operation_id=original, version=_fingerprint(state),
                       original_operation=state["operation"], affected_sources=state["affected_sources"])
        return remember("revert", payload, reason, "/lifecycle",
                        '<p>The service checks intervening source changes when compensating the original operation. A conflict must be reviewed again; current totals are never blindly overwritten.</p>')

    @app.get("/lifecycle/history")
    def operation_history(_: None = Depends(auth)):
        return _page("Scoring operation history", '<p>Most recent 200 operations. Original awards and signed adjustments remain in the ledger.</p>' + _details("Operations", _history("admin_operations"), open=True), "/lifecycle")

    @app.get("/lifecycle/challenges/{cid}/history")
    def challenge_history(cid: str, _: None = Depends(auth)):
        item = catalog.get_challenge(cid)
        if not item:
            raise HTTPException(404)
        body = _details("Current edition", item, open=True)
        body += _details("Definition revisions", _history("definition_revisions", "challenge_id", cid))
        body += _details("Lifecycle events", _history("lifecycle_events", "identity_id", item.get("identity_id")))
        return _page("Challenge history", body, "/challenges")

    @app.get("/lifecycle/attempts/{attempt_id}/history")
    def attempt_history(attempt_id: int, _: None = Depends(auth)):
        item = attempts.get_attempt(attempt_id)
        if not item:
            raise HTTPException(404)
        body = _details("Original snapshots and current effective facts", item, open=True)
        body += _details("Correction and status events", _history("attempt_events", "attempt_id", attempt_id))
        return _page("Attempt correction history", body, "/")
