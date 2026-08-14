"""Point the database at a throwaway file before anything imports `app.db`.

`app.db` builds its engine at import time from `DATABASE_URL`, so this has to run before the
first `from app import pipeline` anywhere in the suite. pytest imports conftest first, which is
what makes that ordering reliable.

Without this, `make test` would read the developer's real `DATABASE_URL` and write test scans
into it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "overwatch_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.setdefault("BUG_SOURCE", "seed")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _fresh_database():
    """One clean schema per session."""
    if _TMP_DB.exists():
        _TMP_DB.unlink()

    from app.db import init_db

    init_db()
    yield
    if _TMP_DB.exists():
        _TMP_DB.unlink()
