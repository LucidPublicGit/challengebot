"""Fail-closed test isolation: no runtime DB or .env access.

SQLite connections are restricted to in-memory databases and OS temporary roots.
This guard is active during collection as well as test execution. Tests can still
mock sqlite3.connect normally; the original callable they restore is this guard.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import unquote, urlsplit, parse_qs

import pytest

from app import config, db

_ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.TemporaryDirectory(prefix='challengebot-test-safety-')
_SAFE_DB = Path(_TMP.name) / 'default.sqlite'
_CONNECT = sqlite3.connect
_LOAD_DOTENV = config._load_dotenv
_TEMP_ROOTS = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()}
_OLD_ENV_DB = os.environ.get('DATABASE_PATH')


def _safe_connect(database, *args, **kwargs):
    name = os.fsdecode(database)
    if name not in ('', ':memory:'):
        if name.startswith('file:'):
            uri = urlsplit(name)
            if parse_qs(uri.query).get('mode') == ['memory'] or uri.path == ':memory:':
                return _CONNECT(database, *args, **kwargs)
            path = Path(unquote(uri.path)).resolve()
        else:
            path = Path(name).resolve()
        if not any(path.is_relative_to(root) for root in _TEMP_ROOTS):
            raise AssertionError(f'Tests may open SQLite only under OS temp roots, never runtime data: {path}')
    return _CONNECT(database, *args, **kwargs)


def _safe_dotenv(path='.env'):
    if Path(path).resolve() == _ROOT / '.env':
        return
    return _LOAD_DOTENV(path)


sqlite3.connect = _safe_connect
config._load_dotenv = _safe_dotenv
os.environ['DATABASE_PATH'] = str(_SAFE_DB)
config.get_settings.cache_clear()
db.init_db(str(_SAFE_DB))


@pytest.fixture(autouse=True)
def _isolate_default_database():
    # A helper may configure and delete its own fixture DB; the next test must
    # not inherit that stale path, let alone fall back to ./data/challenges.db.
    db.configure(str(_SAFE_DB))
    yield


def pytest_unconfigure(config):
    sqlite3.connect = _CONNECT
    from app import config as settings_config
    settings_config._load_dotenv = _LOAD_DOTENV
    if _OLD_ENV_DB is None:
        os.environ.pop('DATABASE_PATH', None)
    else:
        os.environ['DATABASE_PATH'] = _OLD_ENV_DB
    settings_config.get_settings.cache_clear()
    _TMP.cleanup()
