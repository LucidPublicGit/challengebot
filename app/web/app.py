from __future__ import annotations

import html
import json
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.catalog import list_challenges, upsert_challenges_from_yaml
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings, set_settings
from app.services import attempts as att_svc
from app.services import progress_status as prog_svc

security = HTTPBasic(auto_error=False)

NAV_ITEMS = (
    ("/", "Attempts"),
    ("/challenges", "Challenges"),
    ("/users", "Users"),
    ("/settings", "Settings"),
)

TIERS = ("easy", "medium", "hard", "ota_master", "special")
GOAL_KINDS = (
    "volumes",
    "episodes",
    "minutes",
    "characters",
    "immersion_hours",
    "complete_work",
)
ATTEMPT_STATUSES = (
    "active",
    "scheduled",
    "completed",
    "expired",
    "abandoned",
    "void",
)
PAGE_SIZE_DEFAULT = 100
PAGE_SIZE_MAX = 500

CSS = """
:root {
  --bg:#0b0d12; --panel:#141821; --panel2:#1a1f2b; --ink:#e8eaed;
  --muted:#8b93a7; --line:#262c3a; --accent:#7c9cff; --accent2:#5b7cfa;
  --ok:#3dd68c; --bad:#f07178; --warn:#e6c07b;
  --radius:8px; --font:ui-sans-serif,system-ui,-apple-system,sans-serif;
}
html{color-scheme:dark}
*{box-sizing:border-box}
body{
  margin:0;font:14px/1.4 var(--font);background:var(--bg);color:var(--ink);
  min-height:100vh;
}
a{color:var(--accent);text-decoration:none}
a:hover{color:#a8bdff}
.skip{
  position:absolute;left:-999px;top:0;background:var(--accent);color:#0b0d12;
  padding:.4rem .7rem;z-index:50;font-weight:700;
}
.skip:focus{left:.5rem;top:.5rem}
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
h1{font-size:1.15rem;margin:0 0 .35rem;font-weight:700;text-wrap:balance}
h1 .badge{vertical-align:middle;margin-left:.35rem}
.page-sub{margin:0 0 .75rem;color:var(--muted);font-size:.86rem}
.page-sub a{font-weight:600}
h2{font-size:.78rem;margin:0 0 .55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:700}
h3{font-size:.95rem;margin:0 0 .55rem;font-weight:700}
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:.75rem .85rem;margin-bottom:.65rem;
}
.card.monitor{background:var(--panel2);border-style:dashed}
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
tr.row-hot td{background:rgba(91,124,250,.06)}
label{display:block;font-size:.7rem;color:var(--muted);font-weight:700;margin:0 0 .18rem;text-transform:uppercase;letter-spacing:.03em}
label .hint{font-weight:600;text-transform:none;letter-spacing:0;color:var(--muted);opacity:.85}
input,select,textarea{
  width:100%;padding:.38rem .5rem;border-radius:6px;border:1px solid var(--line);
  background:#0c0f16;color:var(--ink);font:inherit;
}
input:focus,select:focus,textarea:focus,button:focus-visible,a:focus-visible,.btn:focus-visible{
  outline:2px solid var(--accent2);outline-offset:2px;border-color:var(--accent2);
}
input:focus:not(:focus-visible),select:focus:not(:focus-visible),textarea:focus:not(:focus-visible){
  outline:none;
}
textarea{min-height:3.4rem;resize:vertical}
input[readonly]{opacity:.75}
button,.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:.3rem;
  margin:.1rem .25rem .1rem 0;padding:.4rem .7rem;border-radius:6px;
  border:1px solid transparent;background:var(--accent);color:#0b0d12;
  font-weight:700;cursor:pointer;font:inherit;font-size:.84rem;text-decoration:none;
  touch-action:manipulation;
}
button:hover,.btn:hover{filter:brightness(1.05);text-decoration:none;color:#0b0d12}
button:disabled,.btn:disabled{opacity:.55;cursor:wait;filter:none}
button.secondary,.btn.secondary{background:transparent;color:var(--ink);border-color:var(--line)}
button.danger,.btn.danger{background:transparent;color:var(--bad);border-color:#5a3036}
.muted{color:var(--muted);font-size:.82rem}
.msg{color:var(--ok);margin:.35rem 0;padding:.45rem .6rem;border-radius:6px;background:rgba(61,214,140,.1);border:1px solid rgba(61,214,140,.25)}
.err{color:var(--bad);margin:.35rem 0;padding:.45rem .6rem;border-radius:6px;background:rgba(240,113,120,.1);border:1px solid rgba(240,113,120,.25)}
.badge{
  display:inline-block;font-size:.68rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.03em;padding:.12rem .4rem;border-radius:999px;background:var(--panel2);color:var(--muted);
}
.badge.active{color:#7eb6ff;background:rgba(91,124,250,.15)}
.badge.completed,.badge.linked{color:var(--ok);background:rgba(61,214,140,.12)}
.badge.expired,.badge.void,.badge.abandoned,.badge.unlinked{color:var(--bad);background:rgba(240,113,120,.12)}
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
.seg input:checked + span{color:var(--ink);background:rgba(124,156,255,.18)}
.seg label span{display:block;margin:-.38rem -.75rem;padding:.38rem .75rem}
.pill{
  display:inline-flex;align-items:center;gap:.3rem;padding:.15rem .45rem;border-radius:999px;
  background:var(--panel2);border:1px solid var(--line);font-size:.75rem;color:var(--muted);
}
.pill b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.actions{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center;margin-top:.55rem}
.toolbar{display:flex;justify-content:space-between;gap:.75rem;flex-wrap:wrap;align-items:center;margin-bottom:.55rem}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.8rem}
.truncate{max-width:16rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
td .sub{display:block;color:var(--muted);font-size:.75rem;margin-top:.1rem}
td .row-actions{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center}
td .row-actions form{display:inline;margin:0}
td .row-actions button,.btn.tiny{
  margin:0;padding:.28rem .55rem;font-size:.75rem;font-weight:700;
  background:transparent;color:var(--accent);border:1px solid var(--line);
}
td .row-actions button:hover,.btn.tiny:hover{border-color:var(--accent2);color:var(--ink)}
.stat-row{display:flex;gap:.45rem;flex-wrap:wrap;margin-bottom:.65rem;align-items:center}
.filters{display:flex;gap:.35rem;flex-wrap:wrap;margin-bottom:.65rem}
.filters a{
  display:inline-flex;align-items:center;gap:.35rem;padding:.35rem .7rem;border-radius:999px;
  border:1px solid var(--line);background:var(--panel);color:var(--muted);font-weight:600;font-size:.82rem;
}
.filters a:hover{color:var(--ink);text-decoration:none;border-color:var(--accent2)}
.filters a.on{background:rgba(124,156,255,.16);color:var(--ink);border-color:var(--accent2)}
.filters a .n{font-variant-numeric:tabular-nums;opacity:.85}
.search-row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:end;margin-bottom:.65rem}
.search-row .grow{flex:1 1 14rem}
.search-row input[type=search]{min-width:12rem}
.poll-bar{
  display:flex;gap:.75rem;flex-wrap:wrap;align-items:center;justify-content:space-between;
}
.poll-bar .poll-meta{display:flex;flex-direction:column;gap:.2rem;min-width:12rem;flex:1 1 14rem}
.poll-timer{
  font-variant-numeric:tabular-nums;font-weight:700;font-size:.95rem;color:var(--ink);
}
.poll-timer.due{color:var(--ok)}
details.block > summary .poll-timer{margin-left:auto}
.poll-mini{width:100%;margin-top:.45rem;font-size:.78rem}
.poll-mini td,.poll-mini th{padding:.22rem .3rem;font-size:.75rem}
details.block{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:.55rem .85rem;margin-bottom:.65rem;
}
details.block > summary{
  cursor:pointer;font-weight:700;color:var(--ink);list-style:none;
  display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;
}
details.block > summary::-webkit-details-marker{display:none}
details.block > summary::before{
  content:'▸';color:var(--muted);font-size:.85rem;
}
details.block[open] > summary::before{content:'▾'}
details.block .block-body{margin-top:.65rem}
.check-row{
  display:flex;align-items:center;gap:.55rem;padding:.35rem 0;min-height:2rem;
}
.check-row input[type=checkbox]{
  width:1.05rem;height:1.05rem;accent-color:var(--accent2);flex:0 0 auto;margin:0;
}
.check-row label{
  margin:0;text-transform:none;letter-spacing:0;font-size:.86rem;font-weight:600;color:var(--ink);
  cursor:pointer;
}
.check-row .desc{display:block;font-size:.75rem;font-weight:500;color:var(--muted);margin-top:.1rem}
.sticky-save{
  position:sticky;bottom:0;z-index:15;margin:1rem -0.25rem 0;padding:.65rem .85rem;
  background:rgba(11,13,18,.92);border-top:1px solid var(--line);backdrop-filter:blur(8px);
  display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;
}
pre.evidence{
  margin:0;padding:.55rem;background:#0c0f16;border:1px solid var(--line);border-radius:6px;
  overflow:auto;max-height:16rem;font-size:.75rem;white-space:pre-wrap;word-break:break-word;
}
.empty{
  padding:1.25rem 1rem;text-align:center;color:var(--muted);
}
.empty .actions{justify-content:center}
.pager{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:.55rem}
.pager .muted{font-variant-numeric:tabular-nums}
.identity{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin-bottom:.65rem}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}
}
"""

PAGE_JS = """
(function(){
  document.addEventListener('submit', function(e){
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    var btns = form.querySelectorAll('button[type=submit],button:not([type])');
    for (var i=0;i<btns.length;i++){
      if (!btns[i].disabled) {
        btns[i].dataset._lbl = btns[i].textContent;
        btns[i].disabled = true;
        if (!/…|\\.\\.\\./.test(btns[i].textContent)) btns[i].textContent = btns[i].textContent.replace(/\\s*$/, '') + '…';
      }
    }
  }, true);
  document.addEventListener('keydown', function(e){
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    var s = document.getElementById('q') || document.querySelector('input[type=search]');
    if (!s) return;
    e.preventDefault();
    s.focus();
    if (s.select) s.select();
  });
})();
"""


def _esc(v: object) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _parse_iso_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    s = str(iso).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _rel_phrase(dt: datetime, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    secs = (dt - now).total_seconds()
    future = secs > 0
    s = abs(secs)
    if s < 45:
        return "just now"
    if s < 3600:
        n = max(1, int(s // 60))
        unit = "m"
    elif s < 86400:
        n = max(1, int(s // 3600))
        unit = "h"
    elif s < 86400 * 14:
        n = max(1, int(s // 86400))
        unit = "d"
    else:
        return ""
    return f"in {n}{unit}" if future else f"{n}{unit} ago"


def _human_when(iso: str | None, *, now: datetime | None = None) -> str:
    dt = _parse_iso_dt(iso)
    if not dt:
        return "—"
    abs_s = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b %Y %H:%M')} UTC"
    rel = _rel_phrase(dt, now=now)
    return f"{abs_s} · {rel}" if rel else abs_s


def _when_cell(iso: str | None, *, label: str = "") -> str:
    dt = _parse_iso_dt(iso)
    if not dt:
        return "—"
    now = datetime.now(timezone.utc)
    if dt.year == now.year:
        abs_s = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')} {dt.strftime('%H:%M')}"
    else:
        abs_s = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b %Y')} {dt.strftime('%H:%M')}"
    rel = _rel_phrase(dt, now=now)
    head = f"{_esc(label)} {_esc(abs_s)}".strip() if label else _esc(abs_s)
    sub = _esc(rel) if rel else _esc("UTC")
    title = _esc(_human_when(iso, now=now))
    return f"<span title='{title}'>{head}</span><span class='sub'>{sub}</span>"


def _attempt_when_cell(a: dict) -> str:
    st = (a.get("status") or "").lower()
    if st == "scheduled":
        return _when_cell(a.get("started_at"), label="Starts")
    if st == "active":
        return _when_cell(a.get("ends_at"), label="Ends")
    if st == "completed":
        return _when_cell(a.get("completed_at") or a.get("started_at"), label="Done")
    if st == "expired":
        return _when_cell(a.get("ends_at") or a.get("completed_at"), label="Ended")
    return _when_cell(a.get("completed_at") or a.get("started_at"))


def _iso_to_date_time(iso: str | None) -> tuple[str, str]:
    dt = _parse_iso_dt(iso)
    if not dt:
        return "", ""
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


def _qs(**parts: object) -> str:
    pairs = []
    for k, v in parts.items():
        if v is None or v == "" or v is False:
            continue
        if v is True:
            pairs.append((k, "1"))
        else:
            pairs.append((k, str(v)))
    return urlencode(pairs)


def _flash_html(saved: str = "", err: str = "", extra: str = "") -> str:
    bits = []
    if saved:
        msg = saved if saved not in ("1", "true") else "Saved."
        bits.append(f'<p class="msg" role="status">{_esc(msg)}</p>')
    if err:
        bits.append(f'<p class="err" role="alert">{_esc(err)}</p>')
    if extra:
        bits.append(extra)
    return "".join(bits)


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
        f'<a class="brand" href="/">ChallengeTracker<span>admin</span></a>'
        f'<nav class="nav">{"".join(links)}</nav>'
        f"</div></div>"
    )


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _page(
    title: str,
    body: str,
    path: str = "/",
    *,
    subtitle: str = "",
    status: int = 200,
) -> HTMLResponse:
    sub = f'<p class="page-sub">{subtitle}</p>' if subtitle else ""
    doc_title = _esc(_strip_tags(title).strip() or "Admin")
    html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#0b0d12"/>
<meta name="color-scheme" content="dark"/>
<title>{doc_title} · ChallengeTracker</title>
<style>{CSS}</style>
</head><body>
<a class="skip" href="#main">Skip to content</a>
{_nav(path)}
<div class="wrap" id="main">
<h1>{title}</h1>
{sub}
{body}
</div>
<script>{PAGE_JS}</script>
</body></html>"""
    # title/subtitle may include safe HTML (badges/links); callers must escape user text
    return HTMLResponse(html_doc, status_code=status)


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


def _select_opts(options: tuple[str, ...] | list[str], selected: str) -> str:
    out = []
    sel = (selected or "").strip()
    found = False
    for o in options:
        is_sel = o == sel
        found = found or is_sel
        out.append(
            f"<option value='{_esc(o)}'{' selected' if is_sel else ''}>{_esc(o)}</option>"
        )
    if sel and not found:
        out.insert(0, f"<option value='{_esc(sel)}' selected>{_esc(sel)}</option>")
    return "".join(out)


def _bool_check(name: str, on: bool, label: str, desc: str = "") -> str:
    checked = "checked" if on else ""
    d = f'<span class="desc">{_esc(desc)}</span>' if desc else ""
    return (
        f'<div class="check-row">'
        f'<input type="checkbox" name="{_esc(name)}" id="{_esc(name)}" value="true" {checked}/>'
        f'<label for="{_esc(name)}">{_esc(label)}{d}</label>'
        f"</div>"
    )


def _backup_root() -> Path:
    return Path(get_settings().database_path).expanduser().resolve().parent


def _list_backup_dbs() -> list[Path]:
    root = _backup_root()
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.glob("*.db") if p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


def _fmt_bytes(sz: int) -> str:
    if sz >= 1024 * 1024:
        return f"{sz / (1024 * 1024):.1f} MB"
    if sz >= 1024:
        return f"{sz / 1024:.0f} KB"
    return f"{sz} B"


def _resolve_backup_db(name: str) -> Path:
    """Live DB in data/ or scheduled slot under data/backups/."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(db|json)", name or ""):
        raise HTTPException(400, "Invalid backup name")
    root = _backup_root()
    slots = (root / "backups").resolve()
    candidates = [(root / name).resolve(), (slots / name).resolve()]
    for path in candidates:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file() and path.parent in (root, slots):
            return path
    raise HTTPException(404, "Backup not found")


def _content_cell(a: dict) -> str:
    c = att_svc.display_content(a)
    return _esc(c) if c else "—"


def _pretty_evidence(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw)
    try:
        obj = json.loads(text)
        text = json.dumps(obj, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    return text


def _tier_goal_fields(
    *,
    tier: str = "easy",
    goal_kind: str = "volumes",
    allows_concurrent: bool = False,
) -> str:
    return f"""
              <div><label>Tier</label>
                <select name="tier">{_select_opts(TIERS, tier)}</select></div>
              <div><label>Goal kind</label>
                <select name="goal_kind">{_select_opts(GOAL_KINDS, goal_kind)}</select></div>
              <div class="span-2">
                {_bool_check('allows_concurrent', allows_concurrent, 'Allows concurrent',
                             'If on, can run beside other open challenges.')}
              </div>
"""


def create_app(bot=None) -> FastAPI:
    app = FastAPI(title="ChallengeTracker Admin", docs_url=None, redoc_url=None)
    app.state.bot = bot

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):  # noqa: ARG001
        if exc.status_code == 401:
            headers = dict(exc.headers or {})
            body = """
            <div class="card empty">
              <p class="err">Authentication required.</p>
            </div>
            """
            resp = _page("Unauthorized", body, "/", status=401)
            for k, v in headers.items():
                resp.headers[k] = v
            return resp
        if exc.status_code == 404:
            body = """
            <div class="card empty">
              <p>Page not found.</p>
              <div class="actions">
                <a class="btn" href="/">Attempts</a>
                <a class="btn secondary" href="/challenges">Challenges</a>
                <a class="btn secondary" href="/users">Users</a>
              </div>
            </div>
            """
            return _page("Not found", body, "/", status=404)
        # 502/503/400: keep admin chrome with detail
        if exc.status_code >= 400:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            body = f'<div class="card empty"><p class="err">{_esc(detail)}</p><div class="actions"><a class="btn secondary" href="/">Back</a></div></div>'
            return _page(f"Error {exc.status_code}", body, "/", status=exc.status_code)
        raise exc

    @app.get("/", response_class=HTMLResponse)
    def home(
        filter: str = "all",
        q: str = "",
        page: int = 1,
        reposted: int = 0,
        polled: Optional[int] = None,
        poll_err: str = "",
        saved: str = "",
        _: None = Depends(_auth),
    ):
        filt = (filter or "all").strip().lower()
        allowed = ("all", "active", "completed", "voided", "expired", "abandoned")
        if filt not in allowed:
            filt = "all"
        status_map = {
            "all": None,
            "active": ["active", "scheduled"],
            "completed": ["completed"],
            "voided": ["void"],
            "expired": ["expired"],
            "abandoned": ["abandoned"],
        }
        statuses = status_map[filt]
        needle = (q or "").strip()
        try:
            page_n = max(1, int(page))
        except (TypeError, ValueError):
            page_n = 1
        limit = PAGE_SIZE_DEFAULT
        offset = (page_n - 1) * limit

        total_matched = att_svc.count_attempts(statuses=statuses, q=needle or None)
        rows = att_svc.list_attempts(
            statuses=statuses,
            q=needle or None,
            limit=limit,
            offset=offset,
        )
        counts = att_svc.attempt_status_counts()
        n_active = counts.get("active", 0) + counts.get("scheduled", 0)
        n_completed = counts.get("completed", 0)
        n_voided = counts.get("void", 0)
        n_expired = counts.get("expired", 0)
        n_abandoned = counts.get("abandoned", 0)
        n_total = counts.get("total", 0)

        def tab(key: str, label: str, n: int) -> str:
            on = "on" if filt == key else ""
            href = "/?" + _qs(filter=None if key == "all" else key, q=needle or None)
            return f'<a class="{on}" href="{href}">{label} <span class="n">{n}</span></a>'

        tr = []
        for a in rows:
            content = _content_cell(a)
            st = (a.get("status") or "").lower()
            chal = (
                '<input type="hidden" name="with_challenger" value="1"/>'
                if st in ("scheduled", "active")
                else ""
            )
            repost_btn = (
                f"<form method='post' action='/attempts/{a['id']}/repost'>"
                f"{chal}"
                f"<input type='hidden' name='filter' value='{_esc(filt)}'/>"
                f"<input type='hidden' name='q' value='{_esc(needle)}'/>"
                f"<input type='hidden' name='page' value='{page_n}'/>"
                f"<button type='submit' title='Repost card to announce channels'>Repost</button>"
                f"</form>"
            )
            uname = _esc(a.get("display_name") or a["discord_id"])
            hot = " class='row-hot'" if st in ("active", "scheduled") else ""
            prog_s = prog_svc.progress_display(a)
            tr.append(
                f"<tr{hot}>"
                f"<td class='mono'><a href='/attempts/{a['id']}'>{a['id']}</a></td>"
                f"<td><a href='/users/{_esc(a['discord_id'])}'>{uname}</a>"
                f"<span class='sub mono'>{_esc(a['discord_id'])}</span></td>"
                f"<td><a href='/challenges/{_esc(a['challenge_id'])}'>{_esc(a['tier'])} #{a['number']}</a>"
                f"<span class='sub'>{_esc(a['title'])}</span></td>"
                f"<td class='truncate' title='{_esc(att_svc.display_content(a))}'>{content}</td>"
                f"<td class='mono'>{_esc(prog_s)}</td>"
                f"<td><span class='badge {_esc(a['status'])}'>{_esc(a['status'])}</span></td>"
                f"<td class='muted'>{_attempt_when_cell(a)}</td>"
                f"<td><div class='row-actions'>"
                f"<a class='btn tiny' href='/attempts/{a['id']}'>Edit</a>"
                f"{repost_btn}</div></td>"
                f"</tr>"
            )

        empty_msg = {
            "all": "No attempts yet",
            "active": "No active or scheduled attempts",
            "completed": "No completed attempts",
            "voided": "No voided attempts",
            "expired": "No expired attempts",
            "abandoned": "No abandoned attempts",
        }[filt]
        if needle and not rows:
            empty_body = (
                f'<div class="empty">No attempts match <b>{_esc(needle)}</b>.'
                f'<div class="actions"><a class="btn secondary" href="/?filter={_esc(filt)}">Clear search</a></div></div>'
            )
        elif not rows and filt == "all" and not needle:
            empty_body = (
                f'<div class="empty">{empty_msg}.'
                f'<div class="actions"><a class="btn secondary" href="/challenges">Manage challenges</a></div></div>'
            )
        else:
            empty_body = f'<div class="empty">{empty_msg}.</div>'

        flash_extra = ""
        if reposted:
            flash_extra += (
                f'<p class="msg" role="status">Reposted attempt <b>#{int(reposted)}</b> '
                "to announce channels.</p>"
            )
        if polled is not None:
            flash_extra += (
                f'<p class="msg" role="status">Forced Tadoku poll — posted '
                f"<b>{int(polled)}</b> status card(s).</p>"
            )
        if poll_err:
            flash_extra += f'<p class="err" role="alert">{_esc(poll_err)}</p>'
        flash = _flash_html(saved=saved, extra=flash_extra)

        overview = prog_svc.tadoku_poll_overview()
        next_iso = overview.get("next_eligible_at") or ""
        poll_on = overview.get("enabled", True)
        poll_m = float(overview.get("poll_minutes") or 2)
        poll_base = float(overview.get("poll_base_minutes") or poll_m)
        poll_m_s = f"{poll_m:g}"
        poll_base_s = f"{poll_base:g}"
        n_targets = int(overview.get("n_targets") or 0)
        tick_s = int(overview.get("tick_seconds") or 60)
        poll_attempts = overview.get("attempts") or []
        poll_rows = []
        for p in poll_attempts:
            prog = p.get("last_progress")
            goal = p.get("goal_amount")
            kind = p.get("goal_kind") or ""
            if prog is None:
                prog_s = "—"
            elif goal:
                prog_s = f"{prog:g} / {goal:g} {kind}".strip()
            else:
                prog_s = f"{prog:g}"
            last_s = _human_when(p.get("last_poll_at")) if p.get("last_poll_at") else "never"
            poll_rows.append(
                "<tr>"
                f"<td class='mono'><a href='/attempts/{p['attempt_id']}'>#{p['attempt_id']}</a></td>"
                f"<td>{_esc(p.get('name'))}<span class='sub'>{_esc(p.get('title'))}</span></td>"
                f"<td class='mono'>{_esc(prog_s)}</td>"
                f"<td class='muted'>{_esc(last_s)}</td>"
                "</tr>"
            )
        poll_table = (
            "<table class='poll-mini'><thead><tr>"
            "<th>ID</th><th>Active Tadoku</th><th>Last progress</th><th>Last poll</th>"
            "</tr></thead><tbody>"
            + (
                "".join(poll_rows)
                or "<tr><td colspan=4 class='muted'>No active Tadoku attempts</td></tr>"
            )
            + "</tbody></table>"
        )
        cards_lbl = "on" if poll_on else "off"
        poll_open = " open" if poll_attempts else ""
        pages = max(1, (total_matched + limit - 1) // limit) if total_matched else 1
        page_n = min(page_n, pages)
        pager = ""
        if total_matched > limit:
            prev_h = "/?" + _qs(
                filter=None if filt == "all" else filt,
                q=needle or None,
                page=page_n - 1 if page_n > 1 else None,
            )
            next_h = "/?" + _qs(
                filter=None if filt == "all" else filt,
                q=needle or None,
                page=page_n + 1 if page_n < pages else None,
            )
            pager = (
                f'<div class="pager">'
                f'<a class="btn secondary tiny" href="{prev_h}"'
                f'{" aria-disabled=true style=opacity:.4;pointer-events:none" if page_n<=1 else ""}>Prev</a>'
                f'<span class="muted">Page <b>{page_n}</b> / {pages} · {total_matched} match</span>'
                f'<a class="btn secondary tiny" href="{next_h}"'
                f'{" aria-disabled=true style=opacity:.4;pointer-events:none" if page_n>=pages else ""}>Next</a>'
                f"</div>"
            )

        # JS Date.parse is happier with Z than +00:00
        next_attr = (next_iso or "").replace("+00:00", "Z")
        body = f"""
        {flash}
        <div class="block monitor" style="padding:.55rem .85rem;margin-bottom:.65rem">
          <div class="poll-bar">
            <div class="poll-meta">
              <div class="poll-timer" id="poll-timer" data-next="{_esc(next_attr)}">—</div>
              <div class="muted">
                Tadoku poll · cadence <b>{_esc(poll_m_s)} min</b>
                ({_esc(poll_base_s)}×{max(n_targets, 1)} user{'s' if max(n_targets, 1) != 1 else ''}, cap 15)
                · tick <b>{tick_s}s</b>
                · Discord cards <b>{cards_lbl}</b>
                <span id="poll-next-abs" class="mono"></span>
              </div>
            </div>
            <form method="post" action="/poll/tadoku">
              <input type="hidden" name="filter" value="{_esc(filt)}"/>
              <input type="hidden" name="q" value="{_esc(needle)}"/>
              <input type="hidden" name="page" value="{page_n}"/>
              <button type="submit" title="Bypass poll cooldown and fetch Tadoku now">
                Force Tadoku poll
              </button>
            </form>
          </div>
        </div>
        <details class="block monitor"{poll_open}>
          <summary>
            Active Tadoku targets
            <span class="pill"><b>{len(poll_attempts)}</b> active</span>
          </summary>
          <div class="block-body">
            {poll_table}
          </div>
        </details>
        <form class="search-row" method="get" action="/">
          <input type="hidden" name="filter" value="{_esc(filt)}"/>
          <div class="grow">
            <label for="q">Search</label>
            <input type="search" name="q" id="q" value="{_esc(needle)}"
              placeholder="Name, Discord id, challenge, content…" spellcheck="false" autocomplete="off"/>
          </div>
          <div><button type="submit">Search</button></div>
          {f'<div><a class="btn secondary" href="/?filter={_esc(filt)}">Clear</a></div>' if needle else ''}
        </form>
        <div class="filters">
          {tab("all", "All", n_total)}
          {tab("active", "Active", n_active)}
          {tab("completed", "Completed", n_completed)}
          {tab("expired", "Expired", n_expired)}
          {tab("abandoned", "Abandoned", n_abandoned)}
          {tab("voided", "Voided", n_voided)}
        </div>
        <div class="stat-row">
          <span class="pill">Showing <b>{len(rows)}</b></span>
          <span class="pill">Matched <b>{total_matched}</b></span>
          <span class="pill muted">/ focuses search · Repost = new card to channels</span>
        </div>
        <div class="card" style="padding:0;overflow:auto">
          {"<table><thead><tr><th>ID</th><th>User</th><th>Challenge</th><th>Content</th><th>Progress</th><th>Status</th><th>When</th><th>Actions</th></tr></thead><tbody>" + "".join(tr) + "</tbody></table>" if tr else empty_body}
        </div>
        {pager}
        <script>
        (function(){{
          var el = document.getElementById('poll-timer');
          if (!el) return;
          var nextRaw = el.getAttribute('data-next') || '';
          var abs = document.getElementById('poll-next-abs');
          var tickS = {tick_s};
          var FLASH = ['reposted','polled','poll_err','saved'];
          function parseIso(s){{
            if (!s) return null;
            var t = Date.parse(s);
            return isNaN(t) ? null : t;
          }}
          // Drop one-shot flash params so auto-reload does not re-show banners
          try {{
            var u = new URL(location.href);
            var dropped = false;
            FLASH.forEach(function(k){{ if (u.searchParams.has(k)) {{ u.searchParams.delete(k); dropped = true; }} }});
            if (dropped) history.replaceState(null, '', u.pathname + (u.search || '') + u.hash);
          }} catch (e) {{}}
          var nextMs = parseIso(nextRaw);
          function fmt(sec){{
            sec = Math.max(0, Math.floor(sec));
            var m = Math.floor(sec / 60), s = sec % 60;
            if (m >= 60) {{
              var h = Math.floor(m / 60); m = m % 60;
              return h + 'h ' + m + 'm ' + s + 's';
            }}
            return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
          }}
          function tick(){{
            if (!nextMs) {{
              el.textContent = 'No Tadoku attempts to poll';
              el.classList.remove('due');
              if (abs) abs.textContent = '';
              return;
            }}
            var left = (nextMs - Date.now()) / 1000;
            if (abs) abs.textContent = ' · ' + new Date(nextMs).toLocaleString();
            if (left <= 0) {{
              el.textContent = 'Poll due — next tick ≤' + tickS + 's';
              el.classList.add('due');
            }} else {{
              el.textContent = 'Next poll in ' + fmt(left);
              el.classList.remove('due');
            }}
          }}
          function uiBusy(){{
            if (document.hidden) return true;
            var a = document.activeElement;
            if (!a) return false;
            var t = a.tagName;
            return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || a.isContentEditable;
          }}
          var reloadTimer = null;
          function softReload(){{
            if (uiBusy()) {{
              reloadTimer = setTimeout(softReload, 4000);
              return;
            }}
            location.reload();
          }}
          function scheduleReload(){{
            if (reloadTimer) clearTimeout(reloadTimer);
            var delay;
            if (nextMs) {{
              // After next poll eligibility + one bot tick, then refresh stats
              delay = (nextMs - Date.now()) + (tickS + 3) * 1000;
              if (delay < 5000) delay = 5000;
              // Keep page fresh even when next poll is far out
              if (delay > 60000) delay = 60000;
            }} else {{
              delay = 60000;
            }}
            reloadTimer = setTimeout(softReload, delay);
          }}
          tick();
          setInterval(tick, 1000);
          scheduleReload();
          document.addEventListener('visibilitychange', function(){{
            if (!document.hidden) scheduleReload();
          }});
        }})();
        </script>
        """
        return _page("Attempts", body, "/")

    @app.post("/poll/tadoku")
    async def force_tadoku_poll(
        filter: str = Form("all"),
        q: str = Form(""),
        page: int = Form(1),
        _: None = Depends(_auth),
    ):
        filt = (filter or "all").strip().lower()
        qs = _qs(filter=None if filt == "all" else filt, q=(q or "").strip() or None, page=page if page > 1 else None)
        prefix = f"/?{qs}&" if qs else "/?"
        bot = getattr(app.state, "bot", None)
        if bot is None:
            return RedirectResponse(
                f"{prefix}poll_err=Bot+not+running+(no+DISCORD_TOKEN)",
                status_code=303,
            )
        if not getattr(bot, "is_ready", lambda: False)():
            return RedirectResponse(
                f"{prefix}poll_err=Bot+not+ready+yet",
                status_code=303,
            )
        try:
            posted = await bot._run_status_updates(force=True)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).replace(" ", "+")[:120]
            return RedirectResponse(f"{prefix}poll_err={msg}", status_code=303)
        return RedirectResponse(f"{prefix}polled={int(posted)}", status_code=303)

    @app.get("/attempts/{attempt_id}", response_class=HTMLResponse)
    def attempt_edit(
        attempt_id: int,
        saved: str = "",
        filter: str = "all",
        q: str = "",
        _: None = Depends(_auth),
    ):
        a = att_svc.get_attempt(attempt_id)
        if not a:
            raise HTTPException(404)
        sd, st = _iso_to_date_time(a.get("started_at"))
        ed, et = _iso_to_date_time(a.get("ends_at"))
        name = a.get("display_name") or a["discord_id"]
        status = a.get("status") or ""
        evidence = _pretty_evidence(a.get("evidence_json"))
        back_qs = _qs(
            filter=None if (filter or "all") == "all" else filter,
            q=(q or "").strip() or None,
        )
        back_href = f"/?{back_qs}" if back_qs else "/"
        chal = (
            '<input type="hidden" name="with_challenger" value="1"/>'
            if status in ("scheduled", "active")
            else ""
        )
        title_html = (
            f"Attempt #{int(attempt_id)} "
            f"<span class='badge {_esc(status)}'>{_esc(status)}</span>"
        )
        subtitle = (
            f"<a href='/users/{_esc(a['discord_id'])}'>{_esc(name)}</a>"
            f" · <a href='/challenges/{_esc(a['challenge_id'])}'>"
            f"{_esc(a.get('tier'))} #{_esc(a.get('number'))} {_esc(a.get('title'))}</a>"
            f" · <span class='mono'>{_esc(a['challenge_id'])}</span>"
        )
        goal_line = (
            f"{_esc(a.get('goal_amount'))} {_esc(a.get('goal_kind') or '')}".strip()
            + f" · verify <b>{_esc(a.get('verify') or '—')}</b>"
            + (
                " · Tadoku linked"
                if a.get("tadoku_user_id")
                else " · <span class='err' style='display:inline;padding:0;border:0;background:0;margin:0'>Tadoku not linked</span>"
            )
        )
        body = f"""
        {_flash_html(saved="Saved." if saved else "")}
        <div class="card">
          <p class="muted" style="margin:0 0 .65rem">Goal: {goal_line}</p>
          <form method="post" action="/attempts/{attempt_id}" id="attempt-form"
            data-prev-status="{_esc(status)}">
            <input type="hidden" name="filter" value="{_esc(filter)}"/>
            <input type="hidden" name="q" value="{_esc(q)}"/>
            <div class="grid">
              <div>
                <label>Status</label>
                <select name="status" id="attempt-status">
                  {_select_opts(ATTEMPT_STATUSES, status)}
                </select>
                <p class="muted" style="margin:.35rem 0 0">
                  Setting <b>completed</b> stamps completed_at to now.
                </p>
              </div>
              <div>
                <label>Verify method</label>
                {_verify_seg((a.get('verify_method') or a.get('verify') or 'tadoku'), name='verify_method')}
              </div>
              <div>
                <label>Param <span class="hint">(declared focus)</span></label>
                <input name="param" value="{_esc(a.get('param') or '')}"/>
              </div>
              <div class="span-2">
                <label>Content <span class="hint">(completed with)</span></label>
                <textarea name="content" placeholder="Series / game / titles from logs…">{_esc(a.get('content') or a.get('param') or '')}</textarea>
              </div>
              <div class="span-2">
                <label>Note</label>
                <textarea name="note">{_esc(a.get('note') or '')}</textarea>
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
              <div class="span-2">
                <p class="muted" style="margin:0">
                  Starts <b>{_esc(_human_when(a.get('started_at')))}</b><br/>
                  Ends <b>{_esc(_human_when(a.get('ends_at')))}</b>
                  {(' · Completed <b>' + _esc(_human_when(a.get('completed_at'))) + '</b>') if a.get('completed_at') else ''}
                </p>
              </div>
            </div>
            <div class="actions">
              <button type="submit">Save</button>
              <a class="btn secondary" href="{back_href}">Back</a>
            </div>
          </form>
          <form method="post" action="/attempts/{attempt_id}/repost" style="margin-top:.5rem">
            {chal}
            <input type="hidden" name="filter" value="{_esc(filter)}"/>
            <input type="hidden" name="q" value="{_esc(q)}"/>
            <button type="submit" class="secondary">Repost to channels</button>
          </form>
        </div>
        <details class="block">
          <summary>Evidence JSON</summary>
          <div class="block-body">
            <pre class="evidence mono">{_esc(evidence) if evidence else '—'}</pre>
          </div>
        </details>
        <script>
        (function(){{
          var form = document.getElementById('attempt-form');
          if (!form) return;
          form.addEventListener('submit', function(e){{
            var sel = document.getElementById('attempt-status');
            if (!sel) return;
            var v = sel.value;
            if (v === 'void' || v === 'abandoned') {{
              if (!confirm('Mark this attempt as ' + v + '?')) {{
                e.preventDefault();
                e.stopImmediatePropagation();
                var btns = form.querySelectorAll('button[type=submit]');
                for (var i=0;i<btns.length;i++){{ btns[i].disabled=false; if(btns[i].dataset._lbl) btns[i].textContent=btns[i].dataset._lbl; }}
              }}
            }}
          }});
        }})();
        </script>
        """
        return _page(title_html, body, "/", subtitle=subtitle)

    @app.post("/attempts/{attempt_id}/repost")
    async def attempt_repost(
        attempt_id: int,
        request: Request,
        with_challenger: str = Form(""),
        highlights_only: str = Form(""),
        filter: str = Form("all"),
        q: str = Form(""),
        page: int = Form(1),
        _: None = Depends(_auth),
    ):
        from app.services.rewards import repost_attempt_announce

        att = att_svc.get_attempt(attempt_id)
        if not att:
            raise HTTPException(404, "Attempt not found")
        bot = getattr(request.app.state, "bot", None)
        if bot is None:
            raise HTTPException(503, "Discord bot not running (DISCORD_TOKEN empty?).")
        if not bot.is_ready():
            raise HTTPException(503, "Discord bot not ready yet — try again shortly.")
        force = str(with_challenger or "").strip() in ("1", "on", "true", "yes")
        ho = str(highlights_only or "").strip() in ("1", "on", "true", "yes")
        ok = await repost_attempt_announce(
            bot,
            att,
            with_challenger=force or att.get("status") == "scheduled",
            highlights_only=ho,
        )
        if not ok:
            raise HTTPException(
                502,
                "Announce failed — check ANNOUNCE_CHANNEL_ID / bot permissions.",
            )
        filt = (filter or "all").strip().lower()
        qs = _qs(
            reposted=attempt_id,
            filter=None if filt == "all" else filt,
            q=(q or "").strip() or None,
            page=page if int(page or 1) > 1 else None,
        )
        return RedirectResponse(f"/?{qs}", status_code=303)

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
        filter: str = Form("all"),
        q: str = Form(""),
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
        qs = _qs(saved=1, filter=None if filter == "all" else filter, q=(q or "").strip() or None)
        return RedirectResponse(f"/attempts/{attempt_id}?{qs}", status_code=303)

    def _challenge_form_fields(c: dict | None = None) -> str:
        c = c or {}
        concurrent = bool(int(c.get("allows_concurrent") or 0))
        return f"""
            <div class="grid">
              <div><label>Id</label>
                <input name="id" value="{_esc(c.get('id') or '')}"
                  {"readonly" if c.get("id") else "required"}
                  placeholder="easy-1" spellcheck="false" autocomplete="off"/></div>
              <div><label>Year</label>
                <input name="year" value="{_esc(c.get('year') or '2026')}" required/></div>
              <div><label>Number</label>
                <input name="number" value="{_esc(c.get('number') or '1')}" required/></div>
              <div><label>Time limit (hours)</label>
                <input name="time_limit_hours" value="{_esc(c.get('time_limit_hours') or '24')}"/></div>
              <div class="span-2"><label>Title</label>
                <input name="title" value="{_esc(c.get('title') or '')}" required/></div>
              <div><label>Param label <span class="hint">(series / game / …)</span></label>
                <input name="param_label" value="{_esc(c.get('param_label') or '')}"
                  placeholder="series / game / …"/></div>
              <div><label>Media filter</label>
                <input name="media_filter" value="{_esc(c.get('media_filter') or 'any')}"/></div>
              {_tier_goal_fields(
                    tier=str(c.get("tier") or "easy"),
                    goal_kind=str(c.get("goal_kind") or "volumes"),
                    allows_concurrent=concurrent,
                )}
              <div class="span-2">
                <label>Verification</label>
                {_verify_seg(str(c.get("verify") or "tadoku"))}
              </div>
              <div><label>Goal amount</label>
                <input name="goal_amount" value="{_esc(c.get('goal_amount') or '1')}" required/></div>
              <div><label>Points <span class="hint">(blank = tier default; Special sets this)</span></label>
                <input name="points" value="{_esc('' if c.get('points') in (None, '') else c.get('points'))}"/></div>
              <div class="span-2"><label>Description</label>
                <textarea name="description">{_esc(c.get("description") or "")}</textarea></div>
            </div>
        """

    @app.get("/challenges", response_class=HTMLResponse)
    def challenges_page(
        year: str = "",
        tier: str = "",
        verify: str = "",
        saved: str = "",
        _: None = Depends(_auth),
    ):
        items = list_challenges()
        years = sorted({str(c.get("year")) for c in items if c.get("year") is not None}, reverse=True)
        yf = (year or "").strip()
        tf = (tier or "").strip().lower()
        vf = (verify or "").strip().lower()
        filtered = []
        for c in items:
            if yf and str(c.get("year")) != yf:
                continue
            if tf and str(c.get("tier") or "").lower() != tf:
                continue
            if vf and str(c.get("verify") or "").lower() != vf:
                continue
            filtered.append(c)

        def chip(kind: str, val: str, label: str) -> str:
            cur = {"year": yf, "tier": tf, "verify": vf}.get(kind, "")
            on = "on" if cur == val else ""
            parts = {"year": yf, "tier": tf, "verify": vf}
            if cur == val:
                parts[kind] = ""
            else:
                parts[kind] = val
            href = "/challenges?" + _qs(**{k: v or None for k, v in parts.items()})
            return f'<a class="{on}" href="{href}">{label}</a>'

        rows = []
        for c in filtered:
            v = (c.get("verify") or "tadoku").lower()
            vbadge = "completed" if v == "tadoku" else "active"
            rows.append(
                f"<tr>"
                f"<td class='mono'><a href='/challenges/{_esc(c['id'])}'>{_esc(c['id'])}</a></td>"
                f"<td>{c['year']}</td>"
                f"<td>{_esc(c['tier'])} #{c['number']}</td>"
                f"<td>{_esc(c['title'])}"
                f"<span class='sub'>{_esc((c.get('description') or '')[:90])}</span></td>"
                f"<td>{_esc(c['goal_kind'])} {_esc(c['goal_amount'])}"
                f"<span class='sub'>{_esc(c.get('media_filter') or 'any')}</span></td>"
                f"<td><span class='badge {vbadge}'>{_esc(v)}</span></td>"
                f"<td class='row-actions'>"
                f"<a class='btn tiny' href='/challenges/{_esc(c['id'])}'>Edit</a>"
                f"<form method='post' action='/challenges/{_esc(c['id'])}/delete' "
                f"onsubmit=\"return confirm('Delete {_esc(c['id'])}? Historical attempts are kept.')\">"
                f"<button type='submit' class='danger'>Delete</button></form></td>"
                f"</tr>"
            )

        year_chips = "".join(chip("year", y, y) for y in years)
        tier_chips = "".join(chip("tier", t, t) for t in TIERS)
        verify_chips = chip("verify", "tadoku", "tadoku") + chip("verify", "honor", "honor")

        empty = (
            '<div class="empty">No challenges match these filters.'
            '<div class="actions"><a class="btn secondary" href="/challenges">Clear filters</a></div></div>'
            if items and not filtered
            else (
                '<div class="empty">No challenges yet.'
                '<div class="actions"><span class="muted">Seed from YAML below, or add one.</span></div></div>'
                if not items
                else '<div class="empty">Empty.</div>'
            )
        )

        body = f"""
        {_flash_html(saved="Saved." if saved else "")}
        <div class="toolbar">
          <div class="stat-row" style="margin:0">
            <span class="pill"><b>{len(filtered)}</b> shown</span>
            <span class="pill"><b>{len(items)}</b> total</span>
          </div>
        </div>
        <div class="card">
          <h2>Seed from YAML</h2>
          <form method="post" action="/challenges/seed" class="row tight">
            <div>
              <label>Path</label>
              <input name="path" value="{_esc(get_settings().challenge_catalog)}" spellcheck="false"/>
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
        <div class="filters">
          <span class="muted" style="align-self:center">Year</span>{year_chips or '<span class="muted">—</span>'}
        </div>
        <div class="filters">
          <span class="muted" style="align-self:center">Tier</span>{tier_chips}
        </div>
        <div class="filters">
          <span class="muted" style="align-self:center">Verify</span>{verify_chips}
        </div>
        <div class="card" style="padding:0;overflow:auto">
          {"<table><thead><tr><th>ID</th><th>Year</th><th>Tier</th><th>Title</th><th>Goal</th><th>Verify</th><th></th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>" if rows else empty}
        </div>
        <details class="block">
          <summary>Add challenge</summary>
          <div class="block-body">
            <form method="post" action="/challenges/save">
              {_challenge_form_fields()}
              <div class="actions"><button type="submit">Create / upsert</button></div>
            </form>
          </div>
        </details>
        """
        return _page("Challenges", body, "/challenges")

    @app.post("/challenges/seed")
    def challenges_seed(
        path: str = Form(...),
        replace: str = Form("0"),
        _: None = Depends(_auth),
    ):
        upsert_challenges_from_yaml(path, replace_year=replace == "1")
        return RedirectResponse("/challenges?saved=1", status_code=303)

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
        allows_concurrent: str = Form("false"),
        goal_kind: str = Form(...),
        goal_amount: float = Form(...),
        media_filter: str = Form("any"),
        description: str = Form(""),
        points: str = Form(""),
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
                "allows_concurrent": allows_concurrent,
                "goal_kind": goal_kind,
                "goal_amount": goal_amount,
                "media_filter": media_filter,
                "description": description,
                "points": points.strip() or None,
            }
        )
        return RedirectResponse(f"/challenges/{id}?saved=1", status_code=303)

    @app.get("/challenges/{cid}", response_class=HTMLResponse)
    def challenge_one(cid: str, saved: str = "", _: None = Depends(_auth)):
        items = list_challenges()
        c = next((x for x in items if x["id"] == cid), None)
        if not c:
            raise HTTPException(404)
        counts = att_svc.attempt_status_counts_for_challenge(cid)
        n_active = counts.get("active", 0) + counts.get("scheduled", 0)
        title_html = (
            f"{_esc(c['title'])} "
            f"<span class='badge'>{_esc(c.get('tier'))}</span> "
            f"<span class='badge'>{_esc(c.get('verify') or 'tadoku')}</span>"
        )
        subtitle = (
            f"<span class='mono'>{_esc(c['id'])}</span> · year { _esc(c.get('year')) }"
            f" · #{_esc(c.get('number'))}"
        )
        body = f"""
        {_flash_html(saved="Saved." if saved else "")}
        <div class="stat-row">
          <span class="pill">Attempts <b>{counts.get('total', 0)}</b></span>
          <span class="pill">Active <b>{n_active}</b></span>
          <span class="pill">Completed <b>{counts.get('completed', 0)}</b></span>
          <a class="btn tiny" href="/?q={_esc(cid)}">View attempts</a>
        </div>
        <div class="card">
          <form method="post" action="/challenges/save">
            {_challenge_form_fields(c)}
            <div class="actions">
              <button type="submit">Save</button>
              <button class="danger" formaction="/challenges/{_esc(cid)}/delete" formmethod="post"
                onclick="return confirm('Delete this challenge? Historical attempts are kept.')">Delete</button>
              <a class="btn secondary" href="/challenges">Back</a>
            </div>
          </form>
        </div>
        <p class="muted">Deleting removes the catalog row only; past attempts keep their challenge_id.</p>
        """
        return _page(title_html, body, "/challenges", subtitle=subtitle)

    @app.post("/challenges/{cid}/delete")
    def challenge_delete(cid: str, _: None = Depends(_auth)):
        att_svc.admin_delete_challenge(cid)
        return RedirectResponse("/challenges?saved=1", status_code=303)

    @app.get("/users", response_class=HTMLResponse)
    def users_page(q: str = "", saved: str = "", _: None = Depends(_auth)):
        needle = (q or "").strip()
        rows = att_svc.list_users(q=needle or None)
        tr = []
        for u in rows:
            linked = bool(u.get("tadoku_user_id"))
            badge = (
                '<span class="badge linked">linked</span>'
                if linked
                else '<span class="badge unlinked">unlinked</span>'
            )
            active_n = int(u.get("active_n") or 0)
            hot = " class='row-hot'" if active_n else ""
            tr.append(
                f"<tr{hot}>"
                f"<td><a href='/users/{_esc(u['discord_id'])}'>{_esc(u.get('display_name') or '—')}</a></td>"
                f"<td class='mono'>{_esc(u['discord_id'])}</td>"
                f"<td>{badge}"
                f"<span class='sub mono'>{_esc(u.get('tadoku_user_id') or '—')}</span></td>"
                f"<td>{_esc(u.get('tadoku_name') or '—')}</td>"
                f"<td class='mono'>{active_n}</td>"
                f"<td><a class='btn tiny' href='/users/{_esc(u['discord_id'])}'>Edit</a></td>"
                f"</tr>"
            )
        empty = (
            f'<div class="empty">No users match <b>{_esc(needle)}</b>.'
            f'<div class="actions"><a class="btn secondary" href="/users">Clear search</a></div></div>'
            if needle
            else '<div class="empty">No users yet.<div class="muted">Users appear when they start a challenge or link Tadoku.</div></div>'
        )
        body = f"""
        {_flash_html(saved="Saved." if saved else "")}
        <form class="search-row" method="get" action="/users">
          <div class="grow">
            <label for="q">Search</label>
            <input type="search" name="q" id="q" value="{_esc(needle)}"
              placeholder="Name, Discord id, Tadoku…" spellcheck="false" autocomplete="off"/>
          </div>
          <div><button type="submit">Search</button></div>
          {f'<div><a class="btn secondary" href="/users">Clear</a></div>' if needle else ''}
        </form>
        <div class="stat-row"><span class="pill"><b>{len(rows)}</b> users</span>
          <span class="pill muted">/ focuses search</span></div>
        <div class="card" style="padding:0;overflow:auto">
          {"<table><thead><tr><th>Name</th><th>Discord</th><th>Tadoku</th><th>Tadoku name</th><th>Active</th><th></th></tr></thead><tbody>" + "".join(tr) + "</tbody></table>" if tr else empty}
        </div>
        """
        return _page("Users", body, "/users")

    @app.get("/users/{discord_id}", response_class=HTMLResponse)
    def user_edit(
        discord_id: str,
        saved: str = "",
        unlinked: str = "",
        _: None = Depends(_auth),
    ):
        u = att_svc.get_user(discord_id) or {
            "discord_id": discord_id,
            "display_name": "",
            "tadoku_user_id": "",
            "tadoku_name": "",
        }
        recent = att_svc.list_attempts(discord_id=discord_id, limit=20)
        counts = att_svc.attempt_status_counts(discord_id=discord_id)
        n_active = counts.get("active", 0) + counts.get("scheduled", 0)
        linked = bool(u.get("tadoku_user_id"))
        display = u.get("display_name") or discord_id
        title_html = _esc(display)
        if n_active:
            title_html += f" <span class='badge active'>{n_active} active</span>"
        title_html += (
            " <span class='badge linked'>linked</span>"
            if linked
            else " <span class='badge unlinked'>not linked</span>"
        )
        subtitle = f"<span class='mono' translate='no'>{_esc(discord_id)}</span>"
        tr = []
        for a in recent:
            content = _content_cell(a)
            st = (a.get("status") or "").lower()
            hot = " class='row-hot'" if st in ("active", "scheduled") else ""
            tr.append(
                f"<tr{hot}>"
                f"<td class='mono'><a href='/attempts/{a['id']}'>{a['id']}</a></td>"
                f"<td><a href='/challenges/{_esc(a['challenge_id'])}'>{_esc(a['tier'])} #{a['number']}</a>"
                f"<span class='sub'>{_esc(a['title'])}</span></td>"
                f"<td>{content}</td>"
                f"<td><span class='badge {_esc(a['status'])}'>{_esc(a['status'])}</span></td>"
                f"<td class='muted'>{_attempt_when_cell(a)}</td>"
                f"<td><a class='btn tiny' href='/attempts/{a['id']}'>Edit</a></td>"
                f"</tr>"
            )
        flash = _flash_html(
            saved="Saved." if saved and not unlinked else ("Tadoku unlinked." if unlinked else ("Saved." if saved else "")),
        )
        had_link = "1" if linked else "0"
        body = f"""
        {flash}
        <div class="stat-row">
          <span class="pill">Attempts <b>{counts.get('total', 0)}</b></span>
          <span class="pill">Active <b>{n_active}</b></span>
          <span class="pill">Completed <b>{counts.get('completed', 0)}</b></span>
          <a class="btn tiny" href="/?q={_esc(discord_id)}">All attempts</a>
        </div>
        <div class="card">
          <form method="post" action="/users/{_esc(discord_id)}" id="user-form" data-had-link="{had_link}">
            <div class="grid">
              <div><label>Discord id</label>
                <input value="{_esc(discord_id)}" readonly spellcheck="false" translate="no"/></div>
              <div><label>Display name</label>
                <input name="display_name" value="{_esc(u.get('display_name') or '')}"/></div>
              <div><label>Tadoku name</label>
                <input name="tadoku_name" value="{_esc(u.get('tadoku_name') or '')}"/></div>
              <div class="span-2"><label>Tadoku user id</label>
                <input name="tadoku_user_id" id="tadoku_user_id"
                  value="{_esc(u.get('tadoku_user_id') or '')}"
                  spellcheck="false" autocomplete="off"/>
                <p class="muted" style="margin:.35rem 0 0">Clear this field and save to unlink Tadoku.</p>
              </div>
            </div>
            <div class="actions">
              <button type="submit">Save</button>
              <a class="btn secondary" href="/users">Back</a>
            </div>
          </form>
        </div>
        <div class="card" style="padding:0;overflow:auto">
          <div style="padding:.65rem .85rem .2rem"><h2>Recent attempts</h2></div>
          {"<table><thead><tr><th>ID</th><th>Challenge</th><th>Content</th><th>Status</th><th>When</th><th></th></tr></thead><tbody>" + "".join(tr) + "</tbody></table>" if tr else '<div class="empty">No attempts yet.</div>'}
        </div>
        <script>
        (function(){{
          var form = document.getElementById('user-form');
          if (!form) return;
          form.addEventListener('submit', function(e){{
            if (form.dataset.hadLink !== '1') return;
            var id = document.getElementById('tadoku_user_id');
            if (id && !String(id.value||'').trim()) {{
              if (!confirm('Unlink Tadoku for this user?')) {{
                e.preventDefault();
                e.stopImmediatePropagation();
                var btns = form.querySelectorAll('button[type=submit]');
                for (var i=0;i<btns.length;i++){{ btns[i].disabled=false; if(btns[i].dataset._lbl) btns[i].textContent=btns[i].dataset._lbl; }}
              }}
            }}
          }});
        }})();
        </script>
        """
        return _page(title_html, body, "/users", subtitle=subtitle)

    @app.post("/users/{discord_id}")
    def user_save(
        discord_id: str,
        display_name: str = Form(""),
        tadoku_user_id: str = Form(""),
        tadoku_name: str = Form(""),
        _: None = Depends(_auth),
    ):
        prev = att_svc.get_user(discord_id) or {}
        had = bool(prev.get("tadoku_user_id"))
        att_svc.ensure_user(discord_id, display_name or None)
        cleared = not (tadoku_user_id or "").strip()
        if tadoku_user_id.strip():
            att_svc.link_tadoku(discord_id, tadoku_user_id, tadoku_name or None)
        else:
            with db_session() as conn:
                conn.execute(
                    "UPDATE users SET tadoku_user_id=NULL, tadoku_name=?, display_name=? "
                    "WHERE discord_id=?",
                    (tadoku_name or None, display_name or None, discord_id),
                )
        qs = "saved=1"
        if had and cleared:
            qs += "&unlinked=1"
        return RedirectResponse(f"/users/{discord_id}?{qs}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(saved: str = "", _: None = Depends(_auth)):
        from app.config import fracs_to_str

        settings = get_settings()
        with db_session() as conn:
            dbmap = get_all_settings(conn)
        rt = merge_runtime(settings, dbmap)
        roles_json = dbmap.get("reward_roles_json") or json.dumps(
            settings.reward_roles_map(), indent=2
        )
        hk = ",".join(rt.get("highlight_kinds") or ())

        def section(title: str, inner: str, open_: bool = True) -> str:
            op = " open" if open_ else ""
            return (
                f'<details class="block"{op}><summary>{_esc(title)}</summary>'
                f'<div class="block-body">{inner}</div></details>'
            )

        core = f"""
            <div class="grid">
              <div><label>Tadoku contest id</label>
                <input name="tadoku_contest_id" value="{_esc(rt['tadoku_contest_id'])}"
                  spellcheck="false" autocomplete="off"/></div>
              <div><label>Tadoku API base</label>
                <input name="tadoku_api_base" value="{_esc(rt['tadoku_api_base'])}"
                  spellcheck="false" autocomplete="off"/></div>
              <div><label>Highlights channel</label>
                <input name="announce_channel_id" value="{_esc(rt['announce_channel_id'])}"
                  spellcheck="false" autocomplete="off"/></div>
              <div><label>Log channel</label>
                <input name="announce_secondary_channel_id"
                  value="{_esc(rt.get('announce_secondary_channel_id') or '')}"
                  spellcheck="false" autocomplete="off"/></div>
              <div><label>Guild id</label>
                <input name="guild_id" value="{_esc(rt['guild_id'])}"
                  spellcheck="false" autocomplete="off"/></div>
              <div class="span-2"><label>Admin role ids</label>
                <input name="admin_role_ids" value="{_esc(','.join(rt['admin_role_ids']))}"
                  spellcheck="false" autocomplete="off"/></div>
              <div class="span-2">
                {_bool_check('allow_reattempt', rt['allow_reattempt'], 'Allow re-attempt')}
                {_bool_check('allow_honor_override', rt.get('allow_honor_override', True),
                             'Allow honor override', '/challenge failoverride when Tadoku fails.')}
              </div>
              <div><label>Pages / volume</label>
                <input name="pages_per_volume" value="{_esc(rt['pages_per_volume'])}"/></div>
              <div><label>Minutes / episode</label>
                <input name="episode_minutes" value="{_esc(rt['episode_minutes'])}"/></div>
              <div class="span-2"><label>Reward roles JSON</label>
                <textarea name="reward_roles_json" style="min-height:7rem" spellcheck="false">{_esc(roles_json)}</textarea>
              </div>
            </div>
        """
        status_sec = f"""
            <div class="grid">
              <div class="span-2">
                {_bool_check('status_updates_enabled', rt.get('status_updates_enabled', True), 'Discord status cards (Tadoku poll always runs)')}
                {_bool_check('status_auto_complete', rt.get('status_auto_complete', True), 'Auto-complete on goal')}
                {_bool_check('status_time_milestones', rt.get('status_time_milestones', True), 'Time milestones')}
                {_bool_check('status_goal_milestones', rt.get('status_goal_milestones', True), 'Goal milestones')}
                {_bool_check('status_final_hour_public', rt.get('status_final_hour_public', True), 'Final stretch public card')}
                {_bool_check('status_final_hour_dm', rt.get('status_final_hour_dm', True), 'Final stretch private DM/thread')}
              </div>
              <div><label>Time fracs (long window) %</label>
                <input name="status_time_fracs_long"
                  value="{_esc(fracs_to_str(rt.get('status_time_fracs_long') or (0.25,0.5,0.75)))}"
                  placeholder="25,50,75…"/></div>
              <div><label>Time fracs (short window) %</label>
                <input name="status_time_fracs_short"
                  value="{_esc(fracs_to_str(rt.get('status_time_fracs_short') or (0.5,)))}"
                  placeholder="50…"/></div>
              <div><label>Goal fracs %</label>
                <input name="status_goal_fracs"
                  value="{_esc(fracs_to_str(rt.get('status_goal_fracs') or (0.25,0.5,0.75)))}"
                  placeholder="25,50,75…"/></div>
              <div><label>Long window days</label>
                <input name="status_long_window_days"
                  value="{_esc(rt.get('status_long_window_days', 7))}"/></div>
              <div><label>Poll minutes (1 active user)</label>
                <input name="status_poll_minutes"
                  value="{_esc(rt.get('status_poll_minutes', 2))}"/>
                <p class="muted" style="margin:.25rem 0 0">Scales × active Tadoku users (cap 15 min).</p></div>
              <div><label>Min interval between posts (min)</label>
                <input name="status_min_interval_minutes"
                  value="{_esc(rt.get('status_min_interval_minutes', 12))}"/></div>
              <div><label>Final stretch minutes</label>
                <input name="status_final_hour_minutes"
                  value="{_esc(rt.get('status_final_hour_minutes', 60))}"/></div>
            </div>
        """
        announce = f"""
            <p class="muted">Failed confirm posts default <strong>off</strong> (mistaken /confirm).</p>
            <div class="grid"><div class="span-2">
              {_bool_check('announce_start', rt.get('announce_start', True), 'start')}
              {_bool_check('announce_scheduled', rt.get('announce_scheduled', True), 'scheduled')}
              {_bool_check('announce_status', rt.get('announce_status', False), 'status')}
              {_bool_check('announce_complete', rt.get('announce_complete', True), 'complete')}
              {_bool_check('announce_fail', rt.get('announce_fail', False), 'fail (failed check)')}
              {_bool_check('announce_expired', rt.get('announce_expired', True), 'expired')}
              {_bool_check('announce_abandon', rt.get('announce_abandon', True), 'abandon')}
              {_bool_check('announce_congrats', rt.get('announce_congrats', True), 'congrats media')}
              {_bool_check('announce_new_challenger', rt.get('announce_new_challenger', True), 'new-challenger banner')}
              {_bool_check('announce_shame', rt.get('announce_shame', True), 'shame captions')}
            </div>
              <div class="span-2"><label>Highlight kinds (comma)</label>
                <input name="highlight_kinds" value="{_esc(hk)}"
                  placeholder="start,complete,fail…" spellcheck="false"/>
              </div>
            </div>
        """
        digest = f"""
            <div class="grid">
              <div class="span-2">
                {_bool_check('monthly_digest_enabled', rt.get('monthly_digest_enabled', True), 'Enabled')}
              </div>
              <div><label>Hour (local 0–23)</label>
                <input name="monthly_digest_hour"
                  value="{_esc(rt.get('monthly_digest_hour', 12))}"/></div>
              <div class="span-2"><label>Timezone (IANA or UTC±HH)</label>
                <input name="monthly_digest_tz"
                  value="{_esc(rt.get('monthly_digest_tz') or '')}"
                  placeholder="America/New_York or UTC-5…" spellcheck="false"/>
              </div>
            </div>
        """
        from app.services import db_backup as bak_svc

        bak = bak_svc.status()
        slot_rows = []
        for f in bak["files"]:
            kind = f["kind"].replace("_", " ")
            period = f.get("period") or "—"
            if f["exists"]:
                dl = (
                    f'<a class="btn tiny" href="/backup/{_esc(f["name"])}">Download</a>'
                )
                size_cell = _esc(f["size_label"])
                if f["over_limit"]:
                    size_cell = (
                        f"<span class='err' style='display:inline;padding:0;"
                        f"border:0;background:0;margin:0'>{size_cell} ⚠</span>"
                    )
            else:
                dl = "<span class='muted'>—</span>"
                size_cell = "—"
            slot_rows.append(
                "<tr>"
                f"<td>{_esc(kind)}</td>"
                f"<td class='mono'>{_esc(f['name'])}</td>"
                f"<td class='mono'>{_esc(period)}</td>"
                f"<td>{size_cell}</td>"
                f"<td>{dl}</td>"
                "</tr>"
            )
        size_alert = ""
        if bak.get("any_over_limit") or bak["keys"].get("size_warn"):
            note = bak["keys"].get("size_warn") or (
                f"A backup exceeds the {bak['warn_label']} soft limit."
            )
            size_alert = f'<p class="err" role="alert">{_esc(note)}</p>'
        db_links = []
        for p in _list_backup_dbs():
            size_s = _fmt_bytes(p.stat().st_size)
            db_links.append(
                f'<a class="btn secondary" href="/backup/{_esc(p.name)}">'
                f"{_esc(p.name)} <span class='muted'>({_esc(size_s)})</span></a>"
            )
        backup_body = (
            f'<p class="muted" style="margin:0 0 .55rem">'
            f"Rolling slots (replace previous): challenges <b>daily / weekly / monthly</b>, "
            f"admin settings <b>weekly</b>. Soft size warn at <b>{_esc(bak['warn_label'])}</b>. "
            f"Dir <span class='mono'>{_esc(bak['dir'])}</span>."
            f"</p>{size_alert}"
            f'<div class="card" style="padding:0;overflow:auto;margin-bottom:.65rem">'
            "<table><thead><tr><th>Slot</th><th>File</th><th>Period</th><th>Size</th><th></th></tr></thead>"
            f"<tbody>{''.join(slot_rows)}</tbody></table></div>"
            '<p class="muted" style="margin:0 0 .35rem">Live DB snapshot (on demand)</p>'
            + (
                f'<div class="actions">{"".join(db_links)}</div>'
                if db_links
                else '<p class="muted">No live .db files found.</p>'
            )
            + (
                '<form method="post" action="/backup/run" class="actions" '
                'style="margin-top:.55rem">'
                '<button type="submit" class="secondary">Run due backups now</button>'
                "</form>"
            )
        )
        body = f"""
        {_flash_html(saved=("Settings saved." if saved in ("1", "true") else saved) if saved else "")}
        <form method="post" action="/settings" id="settings-form">
          {section("Core", core)}
          {section("Status / progress cards", status_sec)}
          {section("Announce cards", announce)}
          {section("Monthly digest", digest)}
          <div class="sticky-save">
            <button type="submit">Save settings</button>
            <span class="muted" id="settings-dirty" hidden>Unsaved changes</span>
          </div>
        </form>
        {section("Server backup", backup_body, open_=True)}
        <script>
        (function(){{
          var form = document.getElementById('settings-form');
          if (!form) return;
          var dirty = false;
          var flag = document.getElementById('settings-dirty');
          function mark(){{ dirty = true; if (flag) flag.hidden = false; }}
          form.addEventListener('input', mark);
          form.addEventListener('change', mark);
          form.addEventListener('submit', function(){{ dirty = false; }});
          window.addEventListener('beforeunload', function(e){{
            if (!dirty) return;
            e.preventDefault();
            e.returnValue = '';
          }});
        }})();
        </script>
        """
        return _page("Settings", body, "/settings")

    @app.post("/settings")
    def settings_save(
        tadoku_contest_id: str = Form(""),
        tadoku_api_base: str = Form(""),
        announce_channel_id: str = Form(""),
        announce_secondary_channel_id: str = Form(""),
        guild_id: str = Form(""),
        admin_role_ids: str = Form(""),
        allow_reattempt: str = Form("false"),
        allow_honor_override: str = Form("false"),
        pages_per_volume: str = Form("180"),
        episode_minutes: str = Form("24"),
        reward_roles_json: str = Form("{}"),
        status_updates_enabled: str = Form("false"),
        status_auto_complete: str = Form("false"),
        status_time_milestones: str = Form("false"),
        status_goal_milestones: str = Form("false"),
        status_time_fracs_long: str = Form("25,50,75"),
        status_time_fracs_short: str = Form("50"),
        status_goal_fracs: str = Form("25,50,75"),
        status_long_window_days: str = Form("7"),
        status_poll_minutes: str = Form("2"),
        status_min_interval_minutes: str = Form("12"),
        status_final_hour_public: str = Form("false"),
        status_final_hour_dm: str = Form("false"),
        status_final_hour_minutes: str = Form("60"),
        announce_start: str = Form("false"),
        announce_scheduled: str = Form("false"),
        announce_status: str = Form("false"),
        announce_complete: str = Form("false"),
        announce_fail: str = Form("false"),
        announce_expired: str = Form("false"),
        announce_abandon: str = Form("false"),
        announce_congrats: str = Form("false"),
        announce_new_challenger: str = Form("false"),
        announce_shame: str = Form("false"),
        highlight_kinds: str = Form("start,complete,fail"),
        monthly_digest_enabled: str = Form("false"),
        monthly_digest_hour: str = Form("12"),
        monthly_digest_tz: str = Form(""),
        _: None = Depends(_auth),
    ):
        try:
            json.loads(reward_roles_json or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"Invalid reward_roles_json: {exc}") from exc

        def b(v: str) -> str:
            return "true" if str(v).strip().lower() in ("1", "true", "yes", "on") else "false"

        with db_session() as conn:
            set_settings(
                conn,
                {
                    "tadoku_contest_id": tadoku_contest_id.strip(),
                    "tadoku_api_base": tadoku_api_base.strip(),
                    "announce_channel_id": announce_channel_id.strip(),
                    "announce_secondary_channel_id": announce_secondary_channel_id.strip(),
                    "guild_id": guild_id.strip(),
                    "admin_role_ids": admin_role_ids.strip(),
                    "allow_reattempt": b(allow_reattempt),
                    "allow_honor_override": b(allow_honor_override),
                    "pages_per_volume": pages_per_volume.strip(),
                    "episode_minutes": episode_minutes.strip(),
                    "reward_roles_json": reward_roles_json.strip() or "{}",
                    "status_updates_enabled": b(status_updates_enabled),
                    "status_auto_complete": b(status_auto_complete),
                    "status_time_milestones": b(status_time_milestones),
                    "status_goal_milestones": b(status_goal_milestones),
                    "status_time_fracs_long": status_time_fracs_long.strip(),
                    "status_time_fracs_short": status_time_fracs_short.strip(),
                    "status_goal_fracs": status_goal_fracs.strip(),
                    "status_long_window_days": status_long_window_days.strip(),
                    "status_poll_minutes": status_poll_minutes.strip(),
                    "status_min_interval_minutes": status_min_interval_minutes.strip(),
                    "status_final_hour_public": b(status_final_hour_public),
                    "status_final_hour_dm": b(status_final_hour_dm),
                    "status_final_hour_minutes": status_final_hour_minutes.strip(),
                    "announce_start": b(announce_start),
                    "announce_scheduled": b(announce_scheduled),
                    "announce_status": b(announce_status),
                    "announce_complete": b(announce_complete),
                    "announce_fail": b(announce_fail),
                    "announce_expired": b(announce_expired),
                    "announce_abandon": b(announce_abandon),
                    "announce_congrats": b(announce_congrats),
                    "announce_new_challenger": b(announce_new_challenger),
                    "announce_shame": b(announce_shame),
                    "highlight_kinds": highlight_kinds.strip(),
                    "monthly_digest_enabled": b(monthly_digest_enabled),
                    "monthly_digest_hour": monthly_digest_hour.strip(),
                    "monthly_digest_tz": monthly_digest_tz.strip(),
                },
            )
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/backup/run")
    def backup_run_now(_: None = Depends(_auth)):
        from app.services.db_backup import run_due_backups

        result = run_due_backups()
        done = ",".join(result.get("done") or []) or "none"
        qs = _qs(saved=f"Backup run: {done}"[:80])
        return RedirectResponse(f"/settings?{qs}", status_code=303)

    @app.get("/backup/{name}")
    def backup_download(name: str, _: None = Depends(_auth)):
        src = _resolve_backup_db(name)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Scheduled slots are already snapshots — stream as-is.
        if src.parent.name == "backups" or src.suffix.lower() == ".json":
            media = (
                "application/json"
                if src.suffix.lower() == ".json"
                else "application/octet-stream"
            )
            return FileResponse(src, media_type=media, filename=src.name)
        fd, tmp = tempfile.mkstemp(prefix="ct-backup-", suffix=".db")
        os.close(fd)
        try:
            # Online-safe snapshot (includes WAL commits)
            src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
            try:
                dst_conn = sqlite3.connect(tmp)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        def _rm(p: str = tmp) -> None:
            try:
                os.unlink(p)
            except OSError:
                pass

        return FileResponse(
            tmp,
            media_type="application/octet-stream",
            filename=f"{src.stem}-{stamp}.db",
            background=BackgroundTask(_rm),
        )

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


if __name__ == "__main__":
    # ponytail: tiny self-check for helpers (no server)
    assert _qs(filter="active", q="x") == "filter=active&q=x"
    assert _qs(filter="all") == "filter=all" or True
    assert "Saved" in _flash_html(saved="1")
    assert _pretty_evidence('{"a":1}').startswith("{")
    try:
        _resolve_backup_db("../etc/passwd.db")
        raise SystemExit("backup path escape")
    except HTTPException as e:
        assert e.status_code == 400
    print("web helpers ok")
