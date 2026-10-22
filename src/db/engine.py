"""
Engine factory and schema-state helpers (Phase 6).
SQLite by default; any SQLAlchemy URL (PostgreSQL via DATABASE_URL) works the same way:
nothing here or in the models depends on SQLite-only features, except the optional
foreign-key PRAGMA, which is applied only when the dialect is SQLite.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine

SCHEMA_VERSION = "6"
LEGACY_ADS_TABLE = "legacy_ads_phase5"
LEGACY_ANALYSES_TABLE = "legacy_analyses_phase5"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def default_database_url() -> str:
    """DATABASE_URL if set (PostgreSQL or any SQLAlchemy URL), else the local SQLite file."""
    return os.environ.get("DATABASE_URL") or f"sqlite:///{DATA_DIR / 'meta_ads.db'}"


def make_engine(url: str) -> Engine:
    """Creates an engine; on SQLite, enables foreign keys on every connection."""
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(url, connect_args={"check_same_thread": False} if is_sqlite else {})
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def has_table(engine: Engine, name: str) -> bool:
    return inspect(engine).has_table(name)


def column_names(engine: Engine, table: str) -> set:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def is_legacy_schema(engine: Engine) -> bool:
    """True when 'ads' exists with the pre-Phase-6 flat layout (no competitor_id column)."""
    return has_table(engine, "ads") and "competitor_id" not in column_names(engine, "ads")


def schema_version(engine: Engine) -> str:
    """Returns the recorded schema_version ('' if the table or key is missing)."""
    if not has_table(engine, "schema_meta"):
        return ""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM schema_meta WHERE key = 'schema_version'")).fetchone()
    return row[0] if row else ""
