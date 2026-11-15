# -*- coding: utf-8 -*-
"""
Shared pytest fixtures.

Two kinds of test live here:

* UNIT tests need nothing but the standard library (and pandas for the ETL
  gate), so they run anywhere, including a laptop with no database.
* INTEGRATION tests (marked ``integration``) need PostgreSQL. Each one gets its
  OWN throwaway database, created from the real SQL files and dropped
  afterwards, so tests cannot see each other's rows and nothing touches the
  development database. If PostgreSQL is unreachable they SKIP rather than fail,
  so a missing database is visible but never mistaken for a regression.

Point the integration tests at a server with ``RETAIL_TEST_ADMIN_DSN`` (it must
be able to CREATE DATABASE); the default matches ``docker compose up -d``.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SQL = ROOT / "sql"
sys.path.insert(0, str(SRC))

ADMIN_DSN = os.environ.get(
    "RETAIL_TEST_ADMIN_DSN",
    "postgresql+psycopg2://retail_admin:retail_secret@localhost:5433/postgres")

#: Applied in this order to build a fresh warehouse, mirroring docker-compose.
SCHEMA_FILES = ("01_schema_ddl_3nf.sql", "02_seed_data.sql",
                "04_triggers_and_procedures.sql", "06_ops_schema.sql",
                "07_ml_schema.sql")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs a reachable PostgreSQL server")


@pytest.fixture(scope="session")
def admin_engine():
    """Autocommit engine on the maintenance database; skips if unreachable."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")
    from sqlalchemy import create_engine

    engine = create_engine(ADMIN_DSN, isolation_level="AUTOCOMMIT", future=True)
    try:
        engine.connect().close()
    except Exception:  # noqa: BLE001
        engine.dispose()
        pytest.skip("PostgreSQL is not reachable (start it: docker compose up -d)")
    yield engine
    engine.dispose()


@pytest.fixture
def warehouse(admin_engine):
    """A fresh database built from the project's SQL files. Dropped afterwards."""
    from sqlalchemy import create_engine, text

    import dbutil

    name = f"it_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))

    base = ADMIN_DSN.rsplit("/", 1)[0]
    engine = create_engine(f"{base}/{name}", future=True)
    try:
        for filename in SCHEMA_FILES:
            path = SQL / filename
            if path.is_file():
                dbutil.apply_sql_file(engine, path)
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.fixture
def scalar(warehouse):
    """Shortcut: run a query on the test warehouse and return the first value."""
    from sqlalchemy import text

    def run(sql, **params):
        with warehouse.connect() as connection:
            return connection.execute(text(sql), params).scalar()

    return run


@pytest.fixture
def execute(warehouse):
    """Shortcut: run a statement on the test warehouse inside a transaction."""
    from sqlalchemy import text

    def run(sql, **params):
        with warehouse.begin() as connection:
            return connection.execute(text(sql), params)

    return run
