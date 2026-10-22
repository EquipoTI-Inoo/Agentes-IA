"""
Test isolation: every test runs against a temporary SQLite database, never data/meta_ads.db.
DATABASE_URL is set here, before any src module is imported, so src.database (SessionLocal)
and the global QuotaGuard both bind to the temporary file.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = Path(tempfile.mkdtemp(prefix="jinora_tests_"))
TEST_DB_PATH = _TMP_DIR / "test_meta_ads.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"

REAL_DB_PATH = (Path(__file__).resolve().parent.parent / "data" / "meta_ads.db").resolve()


@pytest.fixture(scope="session", autouse=True)
def _isolated_database():
    from src.ai.quota import get_quota_guard
    from src.database import engine

    assert Path(engine.url.database).resolve() == TEST_DB_PATH.resolve()
    assert Path(get_quota_guard().db_path).resolve() != REAL_DB_PATH
    yield
    engine.dispose()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
