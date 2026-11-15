# -*- coding: utf-8 -*-
"""
Shared database helpers for the pipeline's Python entry points.

Kept deliberately small: a DSN resolver, an engine factory and a function that
applies a SQL file. Everything that needs a connection (the ETL, the model
runner, the forecaster, the orchestrator) goes through here, so the
connection settings live in exactly one place.

SQLAlchemy is imported lazily so that modules which can run without a database
(the quality gate in ``--dry-run``, the SQL model parser) do not require it.

Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = PROJECT_ROOT / "sql"

#: Fallback DSN matching the credentials in docker-compose.yml.
DEFAULT_DSN = ("postgresql+psycopg2://retail_admin:retail_secret"
               "@localhost:5433/retail_inventory_db")

#: Idempotent schema files the pipeline applies on start. They are IF NOT EXISTS
#: throughout, which is what lets the pipeline work against a database volume
#: that was initialised before these files were added.
SCHEMA_FILES = ("06_ops_schema.sql", "07_ml_schema.sql")


def resolve_dsn(explicit: Optional[str] = None) -> str:
    """Explicit argument, then ``RETAIL_DB_DSN``, then the compose default."""
    return explicit or os.environ.get("RETAIL_DB_DSN") or DEFAULT_DSN


def get_engine(dsn: Optional[str] = None) -> Any:
    """Create a SQLAlchemy engine. ``pool_pre_ping`` survives a booting container."""
    from sqlalchemy import create_engine
    return create_engine(resolve_dsn(dsn), future=True, pool_pre_ping=True)


def apply_sql_file(engine: Any, path: Path) -> None:
    """Execute a whole SQL file in one transaction.

    The file is sent verbatim through a raw DBAPI cursor with NO parameters:
    no bind-parameter parsing and no ``%`` interpolation. That matters here,
    because the procedures in ``sql/04`` contain ``RAISE ... '%'`` placeholders,
    and psycopg2 would otherwise read them as parameter markers and fail.
    Only for files that take no parameters.
    """
    sql = path.read_text(encoding="utf-8")
    with engine.begin() as connection:
        with connection.connection.cursor() as cursor:
            cursor.execute(sql)


def ensure_schemas(engine: Any, sql_dir: Path = SQL_DIR) -> list:
    """Apply the idempotent ops and ml schema files; return those applied."""
    applied = []
    for name in SCHEMA_FILES:
        path = sql_dir / name
        if path.is_file():
            apply_sql_file(engine, path)
            applied.append(name)
    return applied
