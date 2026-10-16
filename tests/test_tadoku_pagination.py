"""Provider-only fixtures: never contact Tadoku or load a member database."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from app.tadoku.client import IncompleteLogsError, fetch_user_logs, resolve_logs_for_user

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2027, 1, 1, tzinfo=timezone.utc)


def run_fetch(pages, **kwargs):
    async def request(client, method, url, **params):
        return httpx.Response(200, json=pages[params["params"]["page"]])
    with patch("app.tadoku.client.bounded_request", request):
        return asyncio.run(fetch_user_logs("synthetic", since=START, until=END, **kwargs))


def test_full_last_page_cannot_silently_truncate():
    pages = [{"logs": [{"id": "one", "created_at": "2026-01-02T00:00:00Z"}]}]
    with pytest.raises(IncompleteLogsError, match="safety limit"):
        run_fetch(pages, page_size=1, max_pages=1)


def test_next_cursor_and_exclusive_deadline():
    pages = {
        0: {"logs": [{"id": "start", "created_at": "2026-01-01T00:00:00Z"}], "next_page": 3},
        3: {"logs": [{"id": "end", "created_at": "2027-01-01T00:00:00Z"}]},
    }
    assert [x["id"] for x in run_fetch(pages, page_size=2)] == ["start"]


def test_bad_timestamp_is_not_silently_counted():
    with pytest.raises(IncompleteLogsError, match="timestamp"):
        run_fetch([{"logs": [{"id": "bad", "created_at": "unknown"}]}])


def test_repeated_cursor_rejected():
    with pytest.raises(IncompleteLogsError, match="repeated"):
        run_fetch([{"logs": [], "next_page": 0}])


def test_no_assumed_date_sort_and_no_duplicate_credit():
    pages = [
        {"logs": [{"id": "old", "created_at": "2025-01-01T00:00:00Z"}], "next_page": 1},
        {"logs": [{"id": "in", "created_at": "2026-06-01T00:00:00Z"}], "next_page": 2},
        {"logs": [{"id": "in", "created_at": "2026-06-01T00:00:00Z"}]},
    ]
    assert [x["id"] for x in run_fetch(pages, page_size=2)] == ["in"]


def test_long_empty_user_result_never_uses_single_contest():
    async def empty(*args, **kwargs):
        return []
    with patch("app.tadoku.client.fetch_user_logs", empty), patch("app.tadoku.client.fetch_contest_logs") as contest:
        logs = asyncio.run(resolve_logs_for_user(tadoku_user_id="synthetic", contest_id="one", api_base="unused", since=START, until=END))
    assert logs == []
    contest.assert_not_called()


def test_http_failure_is_incomplete_evidence():
    async def unavailable(*args, **kwargs):
        return httpx.Response(503)
    with patch("app.tadoku.client.bounded_request", unavailable), pytest.raises(IncompleteLogsError, match="503"):
        asyncio.run(fetch_user_logs("synthetic"))
