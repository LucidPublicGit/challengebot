from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from app.http_limits import bounded_request

logger = logging.getLogger(__name__)
DEFAULT_BASE = "https://tadoku.app/api/internal/immersion"


class IncompleteLogsError(RuntimeError):
    """The provider did not establish complete evidence for the requested window.

    Callers must show an unavailable/incomplete result, never a definitive failure
    or a progress total based on this partial response.
    """


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


async def _fetch_logs(
    path: str, *, api_base: str, page_size: int, max_pages: int,
    user_id: str | None, since: datetime | None, until: datetime | None,
    contest: bool,
) -> list[dict[str, Any]]:
    if page_size <= 0 or max_pages <= 0:
        raise ValueError("Pagination bounds must be positive")
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    visited: set[int] = set()
    page = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(max_pages):
            if page in visited:
                raise IncompleteLogsError("Tadoku repeated a pagination cursor; evidence is incomplete.")
            visited.add(page)
            params: dict[str, Any] = {"page": page, "page_size": page_size}
            if contest:
                params["include_deleted"] = "false"
            r = await bounded_request(client, "GET", f"{(api_base or DEFAULT_BASE).rstrip('/')}/{path}", params=params)
            if r.status_code != 200:
                logger.warning("tadoku logs unavailable: HTTP %s", r.status_code)
                raise IncompleteLogsError(f"Tadoku returned HTTP {r.status_code}; evidence is incomplete.")
            try:
                data = r.json()
                logs = data.get("logs", data.get("items"))
                if not isinstance(logs, list) or any(not isinstance(log, dict) for log in logs):
                    raise ValueError("missing log list")
            except (ValueError, TypeError, AttributeError) as exc:
                raise IncompleteLogsError("Tadoku returned an invalid log page.") from exc
            nxt = data.get("next_page")
            for log in logs:
                if user_id and str(log.get("user_id") or "") != str(user_id):
                    continue
                ident = str(log.get("id") or "")
                if ident:
                    if ident in seen_ids:
                        continue
                    seen_ids.add(ident)
                created = _parse_dt(log.get("created_at"))
                if (since or until) and (created is None or created.tzinfo is None):
                    raise IncompleteLogsError("A Tadoku log has an unknown timestamp; window coverage is uncertain.")
                if since and created and created < since:
                    continue
                # All attempt windows are half-open: [start, end).
                if until and created and created >= until:
                    continue
                out.append(log)
            if nxt is None and len(logs) < page_size:
                return out
            try:
                page = int(nxt) if nxt is not None else page + 1
            except (TypeError, ValueError) as exc:
                raise IncompleteLogsError("Tadoku returned an invalid pagination cursor.") from exc
    raise IncompleteLogsError(
        f"Tadoku evidence exceeded the {max_pages}-page safety limit; "
        "verification is unavailable, not failed. Use explicit manual verification or retry with a larger evidence budget."
    )


async def fetch_contest_logs(
    contest_id: str, *, api_base: str = DEFAULT_BASE, page_size: int = 50,
    max_pages: int = 40, user_id: str | None = None,
    since: datetime | None = None, until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch a complete contest result or explicitly reject incomplete evidence."""
    return await _fetch_logs(
        f"contests/{contest_id}/logs", api_base=api_base, page_size=page_size,
        max_pages=max_pages, user_id=user_id, since=since, until=until, contest=True,
    )


async def fetch_user_logs(
    user_id: str, *, api_base: str = DEFAULT_BASE, page_size: int = 50,
    max_pages: int = 20, since: datetime | None = None, until: datetime | None = None,
) -> list[dict[str, Any]]:
    """User-wide endpoint spans contests/years; no assumed date ordering."""
    return await _fetch_logs(
        f"users/{user_id}/logs", api_base=api_base, page_size=page_size,
        max_pages=max_pages, user_id=None, since=since, until=until, contest=False,
    )


async def resolve_logs_for_user(
    *, tadoku_user_id: str, contest_id: str, api_base: str,
    since: datetime, until: datetime,
) -> list[dict[str, Any]]:
    """Use cross-contest user evidence for long windows.

    A contest is not proof of a complete month/year. Preserve the legacy contest
    fallback only for short windows; never replace a truncated user result with
    an apparently authoritative partial contest result.
    """
    try:
        logs = await fetch_user_logs(tadoku_user_id, api_base=api_base, since=since, until=until)
    except IncompleteLogsError:
        # An unavailable endpoint can be retried later; falling back after any
        # partial fetch would conceal truncation or provider corruption.
        raise
    if logs or (until - since).total_seconds() > 24 * 3600:
        return logs
    return await fetch_contest_logs(
        contest_id, api_base=api_base, user_id=tadoku_user_id, since=since, until=until,
    )
