from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://tadoku.app/api/internal/immersion"


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


async def fetch_contest_logs(
    contest_id: str,
    *,
    api_base: str = DEFAULT_BASE,
    page_size: int = 50,
    max_pages: int = 40,
    user_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    GET /contests/{id}/logs — often public (no cookie).
    Filters client-side by user_id and created_at window when provided.
    """
    base = (api_base or DEFAULT_BASE).rstrip("/")
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        page = 0
        for _ in range(max_pages):
            url = f"{base}/contests/{contest_id}/logs"
            params = {"page": page, "page_size": page_size, "include_deleted": "false"}
            r = await client.get(url, params=params)
            if r.status_code != 200:
                logger.warning("tadoku contest logs %s: %s", r.status_code, r.text[:200])
                break
            data = r.json()
            logs = data.get("logs") or data.get("items") or []
            if not logs:
                break
            for log in logs:
                if user_id and str(log.get("user_id") or "") != str(user_id):
                    continue
                created = _parse_dt(log.get("created_at"))
                if since and created and created < since:
                    continue
                if until and created and created > until:
                    continue
                out.append(log)
            # stop early if last page older than since
            oldest = min(
                (_parse_dt(x.get("created_at")) for x in logs),
                default=None,
            )
            nxt = data.get("next_page")
            if nxt is None and len(logs) < page_size:
                break
            if since and oldest and oldest < since and not user_id:
                # without user filter still may need more pages for other users
                pass
            page = int(nxt) if nxt is not None else page + 1
            if nxt is None and len(logs) < page_size:
                break
    return out


async def fetch_user_logs(
    user_id: str,
    *,
    api_base: str = DEFAULT_BASE,
    page_size: int = 50,
    max_pages: int = 20,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """GET /users/{id}/logs — may be public for profiles."""
    base = (api_base or DEFAULT_BASE).rstrip("/")
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        page = 0
        for _ in range(max_pages):
            url = f"{base}/users/{user_id}/logs"
            r = await client.get(url, params={"page": page, "page_size": page_size})
            if r.status_code != 200:
                logger.warning("tadoku user logs %s: %s", r.status_code, r.text[:200])
                break
            data = r.json()
            logs = data.get("logs") or data.get("items") or []
            if not logs:
                break
            stop = False
            for log in logs:
                created = _parse_dt(log.get("created_at"))
                if until and created and created > until:
                    continue
                if since and created and created < since:
                    stop = True
                    continue
                out.append(log)
            if stop and len(logs) < page_size:
                break
            if len(logs) < page_size:
                break
            page += 1
    return out


async def resolve_logs_for_user(
    *,
    tadoku_user_id: str,
    contest_id: str,
    api_base: str,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    """Prefer user endpoint; fall back to contest logs filtered by user."""
    logs = await fetch_user_logs(
        tadoku_user_id,
        api_base=api_base,
        since=since,
        until=until,
    )
    if logs:
        return logs
    return await fetch_contest_logs(
        contest_id,
        api_base=api_base,
        user_id=tadoku_user_id,
        since=since,
        until=until,
    )
