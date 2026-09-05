from __future__ import annotations

import json
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.catalog import list_challenges, upsert_challenges_from_yaml
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings, set_settings
from app.services import attempts as att_svc

security = HTTPBasic(auto_error=False)

NAV = """
<nav class="nav">
  <a href="/">Attempts</a>
  <a href="/challenges">Challenges</a>
  <a href="/users">Users</a>
  <a href="/settings">Settings</a>
</nav>
"""

CSS = """
:root {
  --bg:#0f1115; --panel:#1a1d24; --ink:#e8eaed; --muted:#9aa0a6;
  --line:#2a2f3a; --accent:#7c9cff; --ok:#3dd68c; --bad:#f07178;
  --font: ui-sans-serif, system-ui, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin:0; font-family:var(--font); background:var(--bg); color:var(--ink);
  line-height:1.45; padding:1.25rem;
}
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.nav { display:flex; gap:1rem; margin-bottom:1.25rem; flex-wrap:wrap; }
.nav a { font-weight:600; }
h1 { font-size:1.35rem; margin:0 0 0.75rem; }
h2 { font-size:1.05rem; margin:1.25rem 0 0.5rem; color:var(--muted); }
.card {
  background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:0.9rem 1rem; margin-bottom:0.75rem;
}
table { width:100%; border-collapse:collapse; font-size:0.9rem; }
th, td { text-align:left; padding:0.4rem 0.5rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:0.78rem; text-transform:uppercase; }
label { display:block; font-size:0.78rem; color:var(--muted); font-weight:600; margin:0.45rem 0 0.2rem; }
input, select, textarea {
  width:100%; max-width:40rem; padding:0.4rem 0.5rem; border-radius:6px;
  border:1px solid var(--line); background:#0c0e12; color:var(--ink); font:inherit;
}
textarea { min-height:4rem; }
button, .btn {
  display:inline-block; margin-top:0.55rem; margin-right:0.35rem;
  padding:0.4rem 0.75rem; border-radius:6px; border:1px solid var(--line);
  background:var(--accent); color:#0b0d12; font-weight:700; cursor:pointer; font:inherit;
}
button.secondary { background:transparent; color:var(--ink); }
.msg { margin:0.5rem 0; color:var(--ok); }
.err { color:var(--bad); }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:0.75rem; }
@media (max-width:800px){ .grid { grid-template-columns:1fr; } }
.badge { font-size:0.72rem; font-weight:700; text-transform:uppercase; color:var(--muted); }
.badge.active { color:#5b9fff; }
.badge.completed { color:var(--ok); }
.badge.expired,.badge.void { color:var(--bad); }
.muted { color:var(--muted); font-size:0.85rem; }
"""


def _auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> None:
    settings = get_settings()
    password = settings.ui_password or ""
    if not password:
        return  # open (LAN/dev)
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


def _page(title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title} · ChallengeTracker</title>
<style>{CSS}</style>
</head><body>
{NAV}
<h1>{title}</h1>
{body}
</body></html>"""
    return HTMLResponse(html)


def create_app() -> FastAPI:
    app = FastAPI(title="ChallengeTracker Admin", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def home(_: None = Depends(_auth)):
        rows = att_svc.list_attempts(limit=100)
        tr = []
        for a in rows:
            tr.append(
                f"<tr>"
                f"<td>{a['id']}</td>"
                f"<td>{a.get('display_name') or a['discord_id']}<br>"
                f"<span class='muted'>{a['discord_id']}</span></td>"
                f"<td>{a['tier']} #{a['number']}<br>{a['title']}</td>"
                f"<td>{a.get('param') or '—'}</td>"
                f"<td><span class='badge {a['status']}'>{a['status']}</span></td>"
                f"<td class='muted'>{a['started_at']}<br>→ {a['ends_at']}</td>"
                f"<td><a href='/attempts/{a['id']}'>Edit</a></td>"
                f"</tr>"
            )
        body = f"""
        <p class="muted">Manual fix-up if the bot or Tadoku matching gets it wrong.</p>
        <div class="card"><table>
          <thead><tr>
            <th>ID</th><th>User</th><th>Challenge</th><th>Param</th>
            <th>Status</th><th>Window</th><th></th>
          </tr></thead>
          <tbody>{''.join(tr) or '<tr><td colspan=7>No attempts</td></tr>'}</tbody>
        </table></div>
        """
        return _page("Attempts", body)

    @app.get("/attempts/{attempt_id}", response_class=HTMLResponse)
    def attempt_edit(attempt_id: int, _: None = Depends(_auth)):
        rows = att_svc.list_attempts(limit=500)
        a = next((x for x in rows if x["id"] == attempt_id), None)
        if not a:
            raise HTTPException(404)
        body = f"""
        <div class="card">
          <form method="post" action="/attempts/{attempt_id}">
            <div class="grid">
              <div>
                <label>Status</label>
                <select name="status">
                  {''.join(
                    f"<option value='{s}' {'selected' if a['status']==s else ''}>{s}</option>"
                    for s in ('active','completed','expired','abandoned','void')
                  )}
                </select>
              </div>
              <div>
                <label>Verify method</label>
                <input name="verify_method" value="{a.get('verify_method') or ''}"/>
              </div>
              <div>
                <label>Param</label>
                <input name="param" value="{a.get('param') or ''}"/>
              </div>
              <div>
                <label>Note</label>
                <input name="note" value="{a.get('note') or ''}"/>
              </div>
              <div>
                <label>Started at (ISO UTC)</label>
                <input name="started_at" value="{a.get('started_at') or ''}"/>
              </div>
              <div>
                <label>Ends at (ISO UTC)</label>
                <input name="ends_at" value="{a.get('ends_at') or ''}"/>
              </div>
            </div>
            <p class="muted">User: {a['discord_id']} · Challenge: {a['challenge_id']}</p>
            <p class="muted">Evidence: <code>{(a.get('evidence_json') or '')[:500]}</code></p>
            <button type="submit">Save</button>
            <a class="btn secondary" href="/">Back</a>
          </form>
        </div>
        """
        return _page(f"Attempt #{attempt_id}", body)

    @app.post("/attempts/{attempt_id}")
    def attempt_save(
        attempt_id: int,
        status: str = Form(...),
        verify_method: str = Form(""),
        param: str = Form(""),
        note: str = Form(""),
        started_at: str = Form(""),
        ends_at: str = Form(""),
        _: None = Depends(_auth),
    ):
        att_svc.admin_update_attempt(
            attempt_id,
            status=status,
            verify_method=verify_method or None,
            param=param,
            note=note,
            started_at=started_at or None,
            ends_at=ends_at or None,
        )
        return RedirectResponse(f"/attempts/{attempt_id}", status_code=303)

    @app.get("/challenges", response_class=HTMLResponse)
    def challenges_page(_: None = Depends(_auth)):
        items = list_challenges()
        rows = []
        for c in items:
            rows.append(
                f"<tr>"
                f"<td><code>{c['id']}</code></td>"
                f"<td>{c['year']}</td>"
                f"<td>{c['tier']} #{c['number']}</td>"
                f"<td>{c['title']}</td>"
                f"<td>{c['goal_kind']} {c['goal_amount']}<br>"
                f"<span class='muted'>{c['media_filter']} · {c['verify']}</span></td>"
                f"<td><a href='/challenges/{c['id']}'>Edit</a></td>"
                f"</tr>"
            )
        body = f"""
        <div class="card">
          <form method="post" action="/challenges/seed" class="grid">
            <div>
              <label>Seed / reset from YAML path</label>
              <input name="path" value="{get_settings().challenge_catalog}"/>
            </div>
            <div>
              <label>Replace year rows first?</label>
              <select name="replace">
                <option value="0">No (upsert only)</option>
                <option value="1">Yes (delete year then seed)</option>
              </select>
            </div>
            <div><button type="submit">Run seed</button></div>
          </form>
          <p class="muted">For a new year: edit/add <code>challenges/YYYY.yaml</code>, seed with replace, or add rows below.</p>
        </div>
        <div class="card">
          <h2>Add challenge</h2>
          <form method="post" action="/challenges/save" class="grid">
            <div><label>id</label><input name="id" required placeholder="easy-1"/></div>
            <div><label>year</label><input name="year" value="2026" required/></div>
            <div><label>tier</label>
              <select name="tier">
                <option>easy</option><option>medium</option>
                <option>hard</option><option>ota_master</option>
              </select>
            </div>
            <div><label>number</label><input name="number" value="1" required/></div>
            <div><label>title</label><input name="title" required/></div>
            <div><label>param_label (empty = none)</label><input name="param_label"/></div>
            <div><label>verify</label>
              <select name="verify"><option>tadoku</option><option>honor</option></select>
            </div>
            <div><label>time_limit_hours</label><input name="time_limit_hours" value="24"/></div>
            <div><label>goal_kind</label>
              <select name="goal_kind">
                <option>volumes</option><option>episodes</option><option>minutes</option>
                <option>characters</option><option>immersion_hours</option>
                <option>complete_work</option>
              </select>
            </div>
            <div><label>goal_amount</label><input name="goal_amount" value="1" required/></div>
            <div><label>media_filter</label><input name="media_filter" value="any"/></div>
            <div style="grid-column:1/-1"><label>description</label>
              <textarea name="description"></textarea></div>
            <div><button type="submit">Create / upsert</button></div>
          </form>
        </div>
        <div class="card"><table>
          <thead><tr><th>ID</th><th>Year</th><th>Tier</th><th>Title</th><th>Goal</th><th></th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan=6>Empty</td></tr>'}</tbody>
        </table></div>
        """
        return _page("Challenges", body)

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
        return RedirectResponse("/challenges", status_code=303)

    @app.get("/challenges/{cid}", response_class=HTMLResponse)
    def challenge_one(cid: str, _: None = Depends(_auth)):
        items = list_challenges()
        c = next((x for x in items if x["id"] == cid), None)
        if not c:
            raise HTTPException(404)
        body = f"""
        <div class="card">
          <form method="post" action="/challenges/save" class="grid">
            <div><label>id</label><input name="id" value="{c['id']}" readonly/></div>
            <div><label>year</label><input name="year" value="{c['year']}"/></div>
            <div><label>tier</label><input name="tier" value="{c['tier']}"/></div>
            <div><label>number</label><input name="number" value="{c['number']}"/></div>
            <div><label>title</label><input name="title" value="{c['title']}"/></div>
            <div><label>param_label</label><input name="param_label" value="{c.get('param_label') or ''}"/></div>
            <div><label>verify</label><input name="verify" value="{c['verify']}"/></div>
            <div><label>time_limit_hours</label><input name="time_limit_hours" value="{c['time_limit_hours']}"/></div>
            <div><label>goal_kind</label><input name="goal_kind" value="{c['goal_kind']}"/></div>
            <div><label>goal_amount</label><input name="goal_amount" value="{c['goal_amount']}"/></div>
            <div><label>media_filter</label><input name="media_filter" value="{c['media_filter']}"/></div>
            <div style="grid-column:1/-1"><label>description</label>
              <textarea name="description">{c.get('description') or ''}</textarea></div>
            <div><button type="submit">Save</button>
              <button class="secondary" formaction="/challenges/{cid}/delete" formmethod="post">Delete</button>
            </div>
          </form>
        </div>
        """
        return _page(c["title"], body)

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
                f"<tr><td>{u.get('display_name') or '—'}</td>"
                f"<td><code>{u['discord_id']}</code></td>"
                f"<td><code>{u.get('tadoku_user_id') or '—'}</code></td>"
                f"<td>{u.get('tadoku_name') or '—'}</td>"
                f"<td><a href='/users/{u['discord_id']}'>Edit</a></td></tr>"
            )
        body = f"""
        <div class="card"><table>
          <thead><tr><th>Name</th><th>Discord</th><th>Tadoku id</th><th>Tadoku name</th><th></th></tr></thead>
          <tbody>{''.join(tr) or '<tr><td colspan=5>No users</td></tr>'}</tbody>
        </table></div>
        """
        return _page("Users", body)

    @app.get("/users/{discord_id}", response_class=HTMLResponse)
    def user_edit(discord_id: str, _: None = Depends(_auth)):
        u = att_svc.get_user(discord_id) or {
            "discord_id": discord_id,
            "display_name": "",
            "tadoku_user_id": "",
            "tadoku_name": "",
        }
        body = f"""
        <div class="card">
          <form method="post" action="/users/{discord_id}">
            <label>Display name</label>
            <input name="display_name" value="{u.get('display_name') or ''}"/>
            <label>Tadoku user id</label>
            <input name="tadoku_user_id" value="{u.get('tadoku_user_id') or ''}"/>
            <label>Tadoku name</label>
            <input name="tadoku_name" value="{u.get('tadoku_name') or ''}"/>
            <button type="submit">Save</button>
          </form>
        </div>
        """
        return _page(f"User {discord_id}", body)

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
            <label>Tadoku contest id (Deep Weeb default)</label>
            <input name="tadoku_contest_id" value="{rt['tadoku_contest_id']}"/>
            <label>Tadoku API base</label>
            <input name="tadoku_api_base" value="{rt['tadoku_api_base']}"/>
            <label>Announce channel id</label>
            <input name="announce_channel_id" value="{rt['announce_channel_id']}"/>
            <label>Guild id</label>
            <input name="guild_id" value="{rt['guild_id']}"/>
            <label>Admin role ids (comma-separated)</label>
            <input name="admin_role_ids" value="{','.join(rt['admin_role_ids'])}"/>
            <label>Allow re-attempt</label>
            <select name="allow_reattempt">
              <option value="true" {'selected' if rt['allow_reattempt'] else ''}>true</option>
              <option value="false" {'selected' if not rt['allow_reattempt'] else ''}>false</option>
            </select>
            <label>Pages per volume (manga estimate)</label>
            <input name="pages_per_volume" value="{rt['pages_per_volume']}"/>
            <label>Minutes per episode (anime estimate)</label>
            <input name="episode_minutes" value="{rt['episode_minutes']}"/>
            <label>Reward roles JSON</label>
            <textarea name="reward_roles_json">{roles_json}</textarea>
            <p class="muted">Keys: easy, medium, hard, ota_master, any_clear, clear_3, clear_10, … → role snowflake strings.</p>
            <button type="submit">Save settings</button>
          </form>
        </div>
        """
        return _page("Settings", body)

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
        # validate JSON
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
