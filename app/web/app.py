from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.catalog import list_challenges, upsert_challenges_from_yaml
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings, set_settings
from app.services import attempts as att_svc

security = HTTPBasic(auto_error=False)

NAV_ITEMS = (
    ("/", "Attempts"),
    ("/challenges", "Challenges"),
    ("/users", "Users"),
    ("/settings", "Settings"),
)

CSS = """
:root {
  --bg:#0b0d12; --panel:#141821; --panel2:#1a1f2b; --ink:#e8eaed;
  --muted:#8b93a7; --line:#262c3a; --accent:#7c9cff; --accent2:#5b7cfa;
  --ok:#3dd68c; --bad:#f07178; --warn:#e6c07b;
  --radius:8px; --font:ui-sans-serif,system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}
body{
  margin:0;font:14px/1.4 var(--font);background:var(--bg);color:var(--ink);
  min-height:100vh;
}
a{color:var(--accent);text-decoration:none}
a:hover{color:#a8bdff}
.top{
  position:sticky;top:0;z-index:20;backdrop-filter:blur(10px);
  background:rgba(11,13,18,.88);border-bottom:1px solid var(--line);
}
.top-inner{
  max-width:1100px;margin:0 auto;padding:.55rem 1rem;
  display:flex;align-items:center;gap:1rem;flex-wrap:wrap;
}
.brand{font-weight:700;letter-spacing:.02em;color:var(--ink);white-space:nowrap}
.brand span{color:var(--muted);font-weight:600;margin-left:.35rem;font-size:.78rem}
.nav{display:flex;gap:.2rem;flex-wrap:wrap}
.nav a{
  color:var(--muted);padding:.35rem .7rem;border-radius:999px;font-weight:600;font-size:.86rem;
}
.nav a:hover{background:var(--panel2);color:var(--ink);text-decoration:none}
.nav a.active{background:var(--panel2);color:var(--ink);box-shadow:inset 0 0 0 1px var(--line)}
.wrap{max-width:1100px;margin:0 auto;padding:1rem}
h1{font-size:1.15rem;margin:0 0 .75rem;font-weight:700}
h2{font-size:.78rem;margin:0 0 .55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:700}
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:.75rem .85rem;margin-bottom:.65rem;
}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:end}
.row > *{flex:1 1 10rem}
.row.tight > *{flex:1 1 7rem}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.55rem .75rem}
.grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.55rem .75rem}
.span-2{grid-column:1/-1}
@media (max-width:820px){.grid,.grid-3{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th,td{text-align:left;padding:.38rem .4rem;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700;font-size:.7rem;text-transform:uppercase;letter-spacing:.03em}
tr:hover td{background:rgba(255,255,255,.015)}
label{display:block;font-size:.7rem;color:var(--muted);font-weight:700;margin:0 0 .18rem;text-transform:uppercase;letter-spacing:.03em}
input,select,textarea{
  width:100%;padding:.38rem .5rem;border-radius:6px;border:1px solid var(--line);
  background:#0c0f16;color:var(--ink);font:inherit;
}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent2)}
textarea{min-height:3.4rem;resize:vertical}
input[readonly]{opacity:.75}
button,.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:.3rem;
  margin:.1rem .25rem .1rem 0;padding:.4rem .7rem;border-radius:6px;
  border:1px solid transparent;background:var(--accent);color:#0b0d12;
  font-weight:700;cursor:pointer;font:inherit;font-size:.84rem;text-decoration:none;
}
button:hover,.btn:hover{filter:brightness(1.05);text-decoration:none;color:#0b0d12}
button.secondary,.btn.secondary{background:transparent;color:var(--ink);border-color:var(--line)}
button.danger,.btn.danger{background:transparent;color:var(--bad);border-color:#5a3036}
.muted{color:var(--muted);font-size:.82rem}
.msg{color:var(--ok);margin:.35rem 0}
.err{color:var(--bad)}
.badge{
  display:inline-block;font-size:.68rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.03em;padding:.12rem .4rem;border-radius:999px;background:var(--panel2);color:var(--muted);
}
.badge.active{color:#7eb6ff;background:rgba(91,124,250,.15)}
.badge.completed{color:var(--ok);background:rgba(61,214,140,.12)}
.badge.expired,.badge.void,.badge.abandoned{color:var(--bad);background:rgba(240,113,120,.12)}
.badge.scheduled{color:var(--warn);background:rgba(230,192,123,.14)}
.chip-row{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
.seg{
  display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#0c0f16;
}
.seg label{
  margin:0;padding:.38rem .75rem;cursor:pointer;color:var(--muted);text-transform:none;
  letter-spacing:0;font-size:.82rem;font-weight:600;border-right:1px solid var(--line);
}
.seg label:last-child{border-right:0}
.seg input{display:none}
.seg input:checked + span{
  color:var(--ink);background:rgba(124,156,255,.18);
}
.seg label span{display:block;margin:-.38rem -.75rem;padding:.38rem .75rem}
.pill{
  display:inline-flex;align-items:center;gap:.3rem;padding:.15rem .45rem;border-radius:999px;
  background:var(--panel2);border:1px solid var(--line);font-size:.75rem;color:var(--muted);
}
.pill b{color:var(--ink);font-weight:600}
.actions{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center;margin-top:.55rem}
.toolbar{display:flex;justify-content:space-between;gap:.75rem;flex-wrap:wrap;align-items:center;margin-bottom:.55rem}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.8rem}
.truncate{max-width:16rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td .sub{display:block;color:var(--muted);font-size:.75rem;margin-top:.1rem}
.stat-row{display:flex;gap:.45rem;flex-wrap:wrap;margin-bottom:.65rem}
"""


def _esc(v: object) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _iso_to_date_time(iso: str | None) -> tuple[str, str]:
    """Split stored ISO UTC into HTML date + time values."""
    if not iso:
        return "", ""
    s = str(iso).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return "", ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _date_time_to_iso(date_s: str, time_s: str) -> str | None:
    date_s = (date_s or "").strip()
    time_s = (time_s or "").strip()
    if not date_s:
        return None
    if not time_s:
        time_s = "00:00"
    if len(time_s.split(":")) == 2:
        time_s = time_s + ":00"
    try:
        dt = datetime.fromisoformat(f"{date_s}T{time_s}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.replace(microsecond=0).isoformat()


def _auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> None:
    settings = get_settings()
    password = settings.ui_password or ""
    if not password:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, settings.ui_username or "admin")
    pass_ok = secrets.compare_digest(credentials.password, password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Bad credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _nav(path: str) -> str:
    links = []
    for href, label in NAV_ITEMS:
        active = "active" if path == href or (href != "/" and path.startswith(href)) else ""
        links.append(f'<a class="{active}" href="{href}">{label}</a>')
    return (
        f'<div class="top"><div class="top-inner">'
        f'<div class="brand">ChallengeTracker<span>admin</span></div>'
        f'<nav class="nav">{"".join(links)}</nav>'
        f"</div></div>"
    )


def _page(title: str, body: str, path: str = "/") -> HTMLResponse:
    html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(title)} · ChallengeTracker</title>
<style>{CSS}</style>
</head><body>
{_nav(path)}
<div class="wrap">
<h1>{_esc(title)}</h1>
{body}
</div>
</body></html>"""
    return HTMLResponse(html_doc)


def _verify_seg(selected: str = "tadoku", name: str = "verify") -> str:
    selected = (selected or "tadoku").lower()
    opts = (("tadoku", "Tadoku"), ("honor", "Honor / manual"))
    parts = []
    for val, label in opts:
        checked = "checked" if selected == val else ""
        parts.append(
            f'<label><input type="radio" name="{name}" value="{val}" {checked}/>'
            f"<span>{label}</span></label>"
        )
    return f'<div class="seg">{"".join(parts)}</div>'


def _content_cell(a: dict) -> str:
    c = att_svc.display_content(a)
    return _esc(c) if c else "—"


def create_app() -> FastAPI:
    app = FastAPI(title="ChallengeTracker Admin", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def home(_: None = Depends(_auth)):
        rows = att_svc.list_attempts(limit=100)
        completed = sum(1 for a in rows if a.get("status") == "completed")
        active = sum(1 for a in rows if a.get("status") == "active")
        tr = []
        for a in rows:
            content = _content_cell(a)
            tr.append(
                f"<tr>"
                f"<td class='mono'>{a['id']}</td>"
                f"<td>{_esc(a.get('display_name') or a['discord_id'])}"
                f"<span class='sub mono'>{_esc(a['discord_id'])}</span></td>"
                f"<td>{_esc(a['tier'])} #{a['number']}"
                f"<span class='sub'>{_esc(a['title'])}</span></td>"
                f"<td class='truncate' title='{_esc(att_svc.display_content(a))}'>{content}</td>"
                f"<td><span class='badge { _esc(a['status']) }'>{_esc(a['status'])}</span></td>"
                f"<td class='muted mono'>{_esc(a.get('completed_at') or a['started_at'])}</td>"
                f"<td><a href='/attempts/{a['id']}'>Edit</a></td>"
                f"</tr>"
            )
        body = f"""
        <div class="stat-row">
          <span class="pill">Showing <b>{len(rows)}</b></span>
          <span class="pill">Active <b>{active}</b></span>
          <span class="pill">Completed <b>{completed}</b></span>
        </div>
        <div class="card" style="padding:0;overflow:auto">
          <table>
            <thead><tr>
              <th>ID</th><th>User</th><th>Challenge</th><th>Content</th>
              <th>Status</th><th>When</th><th></th>
            </tr></thead>
            <tbody>{''.join(tr) or '<tr><td colspan=7 class="muted">No attempts yet</td></tr>'}</tbody>
          </table>
        </div>
        """
        return _page("Attempts", body, "/")

    @app.get("/attempts/{attempt_id}", response_class=HTMLResponse)
    def attempt_edit(attempt_id: int, _: None = Depends(_auth)):
        rows = att_svc.list_attempts(limit=500)
        a = next((x for x in rows if x["id"] == attempt_id), None)
        if not a:
            raise HTTPException(404)
        sd, st = _iso_to_date_time(a.get("started_at"))
        ed, et = _iso_to_date_time(a.get("ends_at"))
        body = f"""
        <div class="card">
          <form method="post" action="/attempts/{attempt_id}">
            <div class="grid">
              <div>
                <label>Status</label>
                <select name="status">
                  {''.join(
                    f"<option value='{s}' {'selected' if a['status']==s else ''}>{s}</option>"
                    for s in ('active','scheduled','completed','expired','abandoned','void')
                  )}
                </select>
              </div>
              <div>
                <label>Verify method</label>
                <input name="verify_method" value="{_esc(a.get('verify_method') or '')}"/>
              </div>
              <div>
                <label>Param (declared focus)</label>
                <input name="param" value="{_esc(a.get('param') or '')}"/>
              </div>
              <div>
                <label>Content (completed with)</label>
                <input name="content" value="{_esc(a.get('content') or a.get('param') or '')}"
                  placeholder="Series / game / titles from logs"/>
              </div>
              <div class="span-2">
                <label>Note</label>
                <input name="note" value="{_esc(a.get('note') or '')}"/>
              </div>
              <div>
                <label>Start date (UTC)</label>
                <input type="date" name="started_date" value="{_esc(sd)}"/>
              </div>
              <div>
                <label>Start time (UTC)</label>
                <input type="time" name="started_time" value="{_esc(st)}" step="60"/>
              </div>
              <div>
                <label>End date (UTC)</label>
                <input type="date" name="ends_date" value="{_esc(ed)}"/>
              </div>
              <div>
                <label>End time (UTC)</label>
                <input type="time" name="ends_time" value="{_esc(et)}" step="60"/>
              </div>
            </div>
            <p class="muted" style="margin:.65rem 0 .2rem">
              User <span class="mono">{_esc(a['discord_id'])}</span>
              · Challenge <span class="mono">{_esc(a['challenge_id'])}</span>
            </p>
            <p class="muted mono" style="word-break:break-all">
              Evidence: {_esc((a.get('evidence_json') or '')[:600])}
            </p>
            <div class="actions">
              <button type="submit">Save</button>
              <a class="btn secondary" href="/">Back</a>
            </div>
          </form>
        </div>
        """
        return _page(f"Attempt #{attempt_id}", body, "/")

    @app.post("/attempts/{attempt_id}")
    def attempt_save(
        attempt_id: int,
        status: str = Form(...),
        verify_method: str = Form(""),
        param: str = Form(""),
        content: str = Form(""),
        note: str = Form(""),
        started_date: str = Form(""),
        started_time: str = Form(""),
        ends_date: str = Form(""),
        ends_time: str = Form(""),
        _: None = Depends(_auth),
    ):
        started_at = _date_time_to_iso(started_date, started_time)
        ends_at = _date_time_to_iso(ends_date, ends_time)
        att_svc.admin_update_attempt(
            attempt_id,
            status=status,
            verify_method=verify_method or None,
            param=param,
            content=content,
            note=note,
            started_at=started_at,
            ends_at=ends_at,
        )
        return RedirectResponse(f"/attempts/{attempt_id}", status_code=303)

    @app.get("/challenges", response_class=HTMLResponse)
    def challenges_page(_: None = Depends(_auth)):
        items = list_challenges()
        rows = []
        for c in items:
            v = (c.get("verify") or "tadoku").lower()
            vbadge = "completed" if v == "tadoku" else "active"
            rows.append(
                f"<tr>"
                f"<td class='mono'>{_esc(c['id'])}</td>"
                f"<td>{c['year']}</td>"
                f"<td>{_esc(c['tier'])} #{c['number']}</td>"
                f"<td>{_esc(c['title'])}"
                f"<span class='sub'>{_esc((c.get('description') or '')[:90])}</span></td>"
                f"<td>{_esc(c['goal_kind'])} {_esc(c['goal_amount'])}"
                f"<span class='sub'>{_esc(c.get('media_filter') or 'any')}</span></td>"
                f"<td><span class='badge {vbadge}'>{_esc(v)}</span></td>"
                f"<td class='row' style='flex-wrap:nowrap;gap:.35rem;align-items:center'>"
                f"<a href='/challenges/{_esc(c['id'])}'>Edit</a>"
                f"<form method='post' action='/challenges/{_esc(c['id'])}/delete' "
                f"style='margin:0' onsubmit=\"return confirm('Delete {_esc(c['id'])}?')\">"
                f"<button type='submit' class='danger' style='margin:0;padding:.2rem .45rem;font-size:.75rem'>"
                f"Delete</button></form></td>"
                f"</tr>"
            )
        body = f"""
        <div class="toolbar">
          <div class="stat-row" style="margin:0">
            <span class="pill"><b>{len(items)}</b> challenges</span>
          </div>
        </div>
        <div class="card">
          <h2>Seed from YAML</h2>
          <form method="post" action="/challenges/seed" class="row tight">
            <div>
              <label>Path</label>
              <input name="path" value="{_esc(get_settings().challenge_catalog)}"/>
            </div>
            <div>
              <label>Mode</label>
              <select name="replace">
                <option value="0">Upsert only</option>
                <option value="1">Replace year then seed</option>
              </select>
            </div>
            <div><button type="submit">Seed</button></div>
          </form>
        </div>
        <div class="card">
          <h2>Add challenge</h2>
          <form method="post" action="/challenges/save">
            <div class="grid">
              <div><label>id</label><input name="id" required placeholder="easy-1"/></div>
              <div><label>year</label><input name="year" value="2026" required/></div>
              <div><label>tier</label>
                <select name="tier">
                  <option>easy</option><option>medium</option>
                  <option>hard</option><option>ota_master</option>
                </select>
              </div>
              <div><label>number</label><input name="number" value="1" required/></div>
              <div class="span-2"><label>title</label><input name="title" required/></div>
              <div><label>param_label</label><input name="param_label" placeholder="series / game / …"/></div>
              <div><label>time_limit_hours</label><input name="time_limit_hours" value="24"/></div>
              <div class="span-2">
                <label>Verification</label>
                {_verify_seg('tadoku')}
              </div>
              <div><label>goal_kind</label>
                <select name="goal_kind">
                  <option>volumes</option><option>episodes</option><option>minutes</option>
                  <option>characters</option><option>immersion_hours</option>
                  <option>complete_work</option>
                </select>
              </div>
              <div><label>goal_amount</label><input name="goal_amount" value="1" required/></div>
              <div><label>media_filter</label><input name="media_filter" value="any"/></div>
              <div class="span-2"><label>description</label>
                <textarea name="description"></textarea></div>
            </div>
            <div class="actions"><button type="submit">Create / upsert</button></div>
          </form>
        </div>
        <div class="card" style="padding:0;overflow:auto">
          <table>
            <thead><tr>
              <th>ID</th><th>Year</th><th>Tier</th><th>Title</th>
              <th>Goal</th><th>Verify</th><th></th>
            </tr></thead>
            <tbody>{''.join(rows) or '<tr><td colspan=7 class="muted">Empty</td></tr>'}</tbody>
          </table>
        </div>
        """
        return _page("Challenges", body, "/challenges")

    @app.post("/challenges/seed")
    def challenges_seed(
        path: str = Form(...),
        replace: str = Form("0"),
        _: None = Depends(_auth),
    ):
        upsert_challenges_from_yaml(path, replace_year=replace == "1")
        return RedirectResponse("/challenges", status_code=303)

    @app.post("/challenges/save")
    def challenges_save(
        id: str = Form(...),
        year: int = Form(...),
        tier: str = Form(...),
        number: int = Form(...),
        title: str = Form(...),
        param_label: str = Form(""),
        verify: str = Form("tadoku"),
        time_limit_hours: float = Form(24),
        goal_kind: str = Form(...),
        goal_amount: float = Form(...),
        media_filter: str = Form("any"),
        description: str = Form(""),
        _: None = Depends(_auth),
    ):
        att_svc.admin_upsert_challenge(
            {
                "id": id,
                "year": year,
                "tier": tier,
                "number": number,
                "title": title,
                "param_label": param_label or None,
                "verify": verify,
                "time_limit_hours": time_limit_hours,
                "goal_kind": goal_kind,
                "goal_amount": goal_amount,
                "media_filter": media_filter,
                "description": description,
            }
        )
        return RedirectResponse(f"/challenges/{id}", status_code=303)

    @app.get("/challenges/{cid}", response_class=HTMLResponse)
    def challenge_one(cid: str, _: None = Depends(_auth)):
        items = list_challenges()
        c = next((x for x in items if x["id"] == cid), None)
        if not c:
            raise HTTPException(404)
        body = f"""
        <div class="card">
          <form method="post" action="/challenges/save">
            <div class="grid">
              <div><label>id</label><input name="id" value="{_esc(c['id'])}" readonly/></div>
              <div><label>year</label><input name="year" value="{_esc(c['year'])}"/></div>
              <div><label>tier</label><input name="tier" value="{_esc(c['tier'])}"/></div>
              <div><label>number</label><input name="number" value="{_esc(c['number'])}"/></div>
              <div class="span-2"><label>title</label><input name="title" value="{_esc(c['title'])}"/></div>
              <div><label>param_label</label>
                <input name="param_label" value="{_esc(c.get('param_label') or '')}"/></div>
              <div><label>time_limit_hours</label>
                <input name="time_limit_hours" value="{_esc(c['time_limit_hours'])}"/></div>
              <div class="span-2">
                <label>Verification</label>
                {_verify_seg(c.get('verify') or 'tadoku')}
              </div>
              <div><label>goal_kind</label>
                <input name="goal_kind" value="{_esc(c['goal_kind'])}"/></div>
              <div><label>goal_amount</label>
                <input name="goal_amount" value="{_esc(c['goal_amount'])}"/></div>
              <div><label>media_filter</label>
                <input name="media_filter" value="{_esc(c['media_filter'])}"/></div>
              <div class="span-2"><label>description</label>
                <textarea name="description">{_esc(c.get('description') or '')}</textarea></div>
            </div>
            <div class="actions">
              <button type="submit">Save</button>
              <button class="danger" formaction="/challenges/{_esc(cid)}/delete" formmethod="post"
                onclick="return confirm('Delete this challenge?')">Delete</button>
              <a class="btn secondary" href="/challenges">Back</a>
            </div>
          </form>
        </div>
        """
        return _page(c["title"], body, "/challenges")

    @app.post("/challenges/{cid}/delete")
    def challenge_delete(cid: str, _: None = Depends(_auth)):
        att_svc.admin_delete_challenge(cid)
        return RedirectResponse("/challenges", status_code=303)

    @app.get("/users", response_class=HTMLResponse)
    def users_page(_: None = Depends(_auth)):
        with db_session() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM users ORDER BY updated_at DESC"
                ).fetchall()
            ]
        tr = []
        for u in rows:
            tr.append(
                f"<tr>"
                f"<td>{_esc(u.get('display_name') or '—')}</td>"
                f"<td class='mono'>{_esc(u['discord_id'])}</td>"
                f"<td class='mono truncate'>{_esc(u.get('tadoku_user_id') or '—')}</td>"
                f"<td>{_esc(u.get('tadoku_name') or '—')}</td>"
                f"<td><a href='/users/{_esc(u['discord_id'])}'>Edit</a></td>"
                f"</tr>"
            )
        body = f"""
        <div class="stat-row"><span class="pill"><b>{len(rows)}</b> users</span></div>
        <div class="card" style="padding:0;overflow:auto">
          <table>
            <thead><tr><th>Name</th><th>Discord</th><th>Tadoku id</th><th>Tadoku name</th><th></th></tr></thead>
            <tbody>{''.join(tr) or '<tr><td colspan=5 class="muted">No users</td></tr>'}</tbody>
          </table>
        </div>
        """
        return _page("Users", body, "/users")

    @app.get("/users/{discord_id}", response_class=HTMLResponse)
    def user_edit(discord_id: str, _: None = Depends(_auth)):
        u = att_svc.get_user(discord_id) or {
            "discord_id": discord_id,
            "display_name": "",
            "tadoku_user_id": "",
            "tadoku_name": "",
        }
        recent = att_svc.list_attempts(discord_id=discord_id, limit=20)
        tr = []
        for a in recent:
            content = _content_cell(a)
            tr.append(
                f"<tr>"
                f"<td class='mono'>{a['id']}</td>"
                f"<td>{_esc(a['tier'])} #{a['number']} {_esc(a['title'])}</td>"
                f"<td>{content}</td>"
                f"<td><span class='badge {_esc(a['status'])}'>{_esc(a['status'])}</span></td>"
                f"<td><a href='/attempts/{a['id']}'>Edit</a></td>"
                f"</tr>"
            )
        body = f"""
        <div class="card">
          <form method="post" action="/users/{_esc(discord_id)}">
            <div class="grid">
              <div><label>Display name</label>
                <input name="display_name" value="{_esc(u.get('display_name') or '')}"/></div>
              <div><label>Tadoku name</label>
                <input name="tadoku_name" value="{_esc(u.get('tadoku_name') or '')}"/></div>
              <div class="span-2"><label>Tadoku user id</label>
                <input name="tadoku_user_id" value="{_esc(u.get('tadoku_user_id') or '')}"/></div>
            </div>
            <div class="actions">
              <button type="submit">Save</button>
              <a class="btn secondary" href="/users">Back</a>
            </div>
          </form>
        </div>
        <div class="card" style="padding:0;overflow:auto">
          <div style="padding:.65rem .85rem .2rem"><h2>Recent attempts</h2></div>
          <table>
            <thead><tr><th>ID</th><th>Challenge</th><th>Content</th><th>Status</th><th></th></tr></thead>
            <tbody>{''.join(tr) or '<tr><td colspan=5 class="muted">None</td></tr>'}</tbody>
          </table>
        </div>
        """
        return _page(f"User {discord_id}", body, "/users")

    @app.post("/users/{discord_id}")
    def user_save(
        discord_id: str,
        display_name: str = Form(""),
        tadoku_user_id: str = Form(""),
        tadoku_name: str = Form(""),
        _: None = Depends(_auth),
    ):
        att_svc.ensure_user(discord_id, display_name or None)
        if tadoku_user_id.strip():
            att_svc.link_tadoku(discord_id, tadoku_user_id, tadoku_name or None)
        else:
            with db_session() as conn:
                conn.execute(
                    "UPDATE users SET tadoku_user_id=NULL, tadoku_name=?, display_name=? "
                    "WHERE discord_id=?",
                    (tadoku_name or None, display_name or None, discord_id),
                )
        return RedirectResponse(f"/users/{discord_id}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(_: None = Depends(_auth)):
        settings = get_settings()
        with db_session() as conn:
            dbmap = get_all_settings(conn)
        rt = merge_runtime(settings, dbmap)
        roles_json = dbmap.get("reward_roles_json") or json.dumps(
            settings.reward_roles_map(), indent=2
        )
        body = f"""
        <div class="card">
          <form method="post" action="/settings">
            <div class="grid">
              <div><label>Tadoku contest id</label>
                <input name="tadoku_contest_id" value="{_esc(rt['tadoku_contest_id'])}"/></div>
              <div><label>Tadoku API base</label>
                <input name="tadoku_api_base" value="{_esc(rt['tadoku_api_base'])}"/></div>
              <div><label>Announce channel id</label>
                <input name="announce_channel_id" value="{_esc(rt['announce_channel_id'])}"/></div>
              <div><label>Guild id</label>
                <input name="guild_id" value="{_esc(rt['guild_id'])}"/></div>
              <div class="span-2"><label>Admin role ids</label>
                <input name="admin_role_ids" value="{_esc(','.join(rt['admin_role_ids']))}"
                  placeholder="comma-separated snowflakes"/></div>
              <div><label>Allow re-attempt</label>
                <select name="allow_reattempt">
                  <option value="true" {'selected' if rt['allow_reattempt'] else ''}>true</option>
                  <option value="false" {'selected' if not rt['allow_reattempt'] else ''}>false</option>
                </select>
              </div>
              <div><label>Pages / volume</label>
                <input name="pages_per_volume" value="{_esc(rt['pages_per_volume'])}"/></div>
              <div><label>Minutes / episode</label>
                <input name="episode_minutes" value="{_esc(rt['episode_minutes'])}"/></div>
              <div class="span-2"><label>Reward roles JSON</label>
                <textarea name="reward_roles_json" style="min-height:7rem">{_esc(roles_json)}</textarea>
                <p class="muted" style="margin:.35rem 0 0">
                  Keys: easy, medium, hard, ota_master, any_clear, clear_3, clear_10 → role ids.
                </p>
              </div>
            </div>
            <div class="actions"><button type="submit">Save settings</button></div>
          </form>
        </div>
        """
        return _page("Settings", body, "/settings")

    @app.post("/settings")
    def settings_save(
        tadoku_contest_id: str = Form(""),
        tadoku_api_base: str = Form(""),
        announce_channel_id: str = Form(""),
        guild_id: str = Form(""),
        admin_role_ids: str = Form(""),
        allow_reattempt: str = Form("true"),
        pages_per_volume: str = Form("180"),
        episode_minutes: str = Form("24"),
        reward_roles_json: str = Form("{}"),
        _: None = Depends(_auth),
    ):
        try:
            json.loads(reward_roles_json or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"Invalid reward_roles_json: {exc}") from exc
        with db_session() as conn:
            set_settings(
                conn,
                {
                    "tadoku_contest_id": tadoku_contest_id.strip(),
                    "tadoku_api_base": tadoku_api_base.strip(),
                    "announce_channel_id": announce_channel_id.strip(),
                    "guild_id": guild_id.strip(),
                    "admin_role_ids": admin_role_ids.strip(),
                    "allow_reattempt": allow_reattempt.strip().lower(),
                    "pages_per_volume": pages_per_volume.strip(),
                    "episode_minutes": episode_minutes.strip(),
                    "reward_roles_json": reward_roles_json.strip() or "{}",
                },
            )
        return RedirectResponse("/settings", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
