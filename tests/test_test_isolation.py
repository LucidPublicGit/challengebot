"""The safety checks reject runtime paths before opening any real file."""
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pytest

from app import config



def test_runtime_database_connections_are_blocked_before_open():
    # These paths are not read or created: the connection guard rejects them.
    runtime = Path(__file__).resolve().parents[1] / 'data' / 'must-never-open.sqlite'
    with pytest.raises(AssertionError, match='never runtime data'):
        sqlite3.connect(runtime)
    with pytest.raises(AssertionError, match='never runtime data'):
        sqlite3.connect(runtime.as_uri() + '?mode=ro', uri=True)


def test_runtime_dotenv_is_skipped_without_reading_it():
    safety = sys.modules[config._load_dotenv.__module__]
    with patch.object(safety, '_LOAD_DOTENV', side_effect=AssertionError('Would read runtime configuration')):
        config._load_dotenv()
        config._load_dotenv(str(Path(__file__).resolve().parents[1] / '.env'))
