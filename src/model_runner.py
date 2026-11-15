#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQL model runner: builds the star schema in dependency order and tests it.

A small, dependency-free take on what dbt does, sized to this project. It exists
because the alternative (a pile of ``psql -f`` calls in a Makefile) cannot
express the three things that make a warehouse trustworthy: build order, an
incremental load, and tests that run after every build.

How it works
------------
Each file in ``sql/models/`` is one model and starts with a header::

    -- model: fact_sales_daily
    -- kind: incremental          (table | scd2 | incremental | snapshot | view)
    -- target: mart.fact_sales_daily
    -- depends_on: dim_date, dim_store, dim_product

The runner orders models by ``depends_on`` (a topological sort that rejects
cycles and unknown names), executes each in its own transaction with a fixed set
of bind parameters, and records every build in ``ops.model_runs``.

Parameters every model can reference::

    :lookback_days   how many days before the watermark an incremental reprocesses
    :full_refresh    rebuild incrementals from the beginning
    :as_of           the effective date for SCD2 version changes
    :snapshot_date   the date a periodic snapshot is stamped with

After the build, ``sql/models/tests.sql`` runs. Each test is a query returning the
rows that VIOLATE an expectation; zero rows is a pass. A failing ``error`` test
makes the run exit non-zero.

Usage
-----
    python src/model_runner.py                          # build + test
    python src/model_runner.py --select fact_sales_daily+   # a model and its dependents
    python src/model_runner.py --full-refresh           # rebuild incrementals
    python src/model_runner.py --tests-only
    python src/model_runner.py --list                   # show the build order, no database

Dependencies: SQLAlchemy 2.x and psycopg2 (only when actually connecting).
Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbutil  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_DIR = PROJECT_ROOT / "sql" / "models"
TESTS_FILE = "tests.sql"
DEFAULT_LOOKBACK_DAYS = 3

KINDS = ("table", "scd2", "incremental", "snapshot", "view")

_HEADER_RE = re.compile(r"^--\s*(model|kind|target|depends_on|description)\s*:\s*(.*)$")
_TEST_RE = re.compile(
    r"^--\s*test\s*:\s*(?P<name>[\w]+)\s*\|\s*model\s*=\s*(?P<model>[\w]+)"
    r"\s*\|\s*severity\s*=\s*(?P<severity>error|warn)\s*$")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class ModelError(RuntimeError):
    """A model file is malformed, or the dependency graph is invalid."""


@dataclass
class Model:
    name: str
    kind: str
    target: str
    depends_on: List[str]
    description: str
    sql: str
    path: Path


@dataclass
class ModelTest:
    name: str
    model: str
    severity: str
    sql: str


@dataclass
class ModelResult:
    name: str
    kind: str
    status: str
    rows_affected: Optional[int]
    target_rows: Optional[int]
    duration_ms: int
    error: Optional[str] = None


@dataclass
class TestResult:
    name: str
    model: str
    severity: str
    failing_rows: int
    status: str
    sample: List[Any] = field(default_factory=list)


def parse_model(path: Path) -> Model:
    """Read one model file: header comments, then the SQL (the whole file)."""
    text = path.read_text(encoding="utf-8")
    header: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            if stripped:
                break          # the header ends at the first line of real SQL
            continue
        match = _HEADER_RE.match(stripped)
        if match and match.group(1) not in header:
            header[match.group(1)] = match.group(2).strip()

    for required in ("model", "kind", "target"):
        if not header.get(required):
            raise ModelError(f"{path.name}: missing '-- {required}:' header")
    if header["kind"] not in KINDS:
        raise ModelError(f"{path.name}: kind must be one of {KINDS}, "
                         f"got {header['kind']!r}")

    depends = [d.strip() for d in header.get("depends_on", "").split(",") if d.strip()]
    return Model(name=header["model"], kind=header["kind"], target=header["target"],
                 depends_on=depends, description=header.get("description", ""),
                 sql=text, path=path)


def load_models(models_dir: Path = DEFAULT_MODELS_DIR) -> Dict[str, Model]:
    """Parse every model file; model names must be unique."""
    models: Dict[str, Model] = {}
    for path in sorted(models_dir.glob("*.sql")):
        if path.name == TESTS_FILE:
            continue
        model = parse_model(path)
        if model.name in models:
            raise ModelError(f"duplicate model name {model.name!r} "
                             f"({models[model.name].path.name} and {path.name})")
        models[model.name] = model
    if not models:
        raise ModelError(f"no models found in {models_dir}")
    return models


def parse_tests(path: Path) -> List[ModelTest]:
    """Split ``tests.sql`` into tests on their ``-- test:`` marker lines."""
    if not path.is_file():
        return []
    tests: List[ModelTest] = []
    current: Optional[Dict[str, Any]] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _TEST_RE.match(line.strip())
        if match:
            if current:
                tests.append(_finish_test(current))
            current = {**match.groupdict(), "lines": []}
        elif current is not None:
            current["lines"].append(line)
    if current:
        tests.append(_finish_test(current))
    return tests


def _finish_test(raw: Dict[str, Any]) -> ModelTest:
    sql = "\n".join(raw["lines"]).strip()
    sql = re.sub(r";\s*$", "", sql)       # the runner wraps it; a trailing ; would break that
    if not re.search(r"\bselect\b", sql, re.IGNORECASE):
        raise ModelError(f"test {raw['name']!r} has no SELECT")
    return ModelTest(raw["name"], raw["model"], raw["severity"], sql)


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------

def topological_order(models: Dict[str, Model]) -> List[str]:
    """Kahn's algorithm. Ties break alphabetically so the order is deterministic."""
    for model in models.values():
        for dep in model.depends_on:
            if dep not in models:
                raise ModelError(f"model {model.name!r} depends on unknown "
                                 f"model {dep!r}")

    remaining = {name: set(m.depends_on) for name, m in models.items()}
    ordered: List[str] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise ModelError("dependency cycle among: "
                             + ", ".join(sorted(remaining)))
        for name in ready:
            ordered.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def select_models(models: Dict[str, Model], selectors: Optional[Sequence[str]]) -> List[str]:
    """Resolve ``--select`` expressions into an ordered list of model names.

    ``name`` selects one model; ``name+`` also selects everything downstream of
    it. Output is always in build order.
    """
    order = topological_order(models)
    if not selectors:
        return order

    chosen: set = set()
    for selector in selectors:
        with_downstream = selector.endswith("+")
        name = selector.rstrip("+")
        if name not in models:
            raise ModelError(f"--select: unknown model {name!r}")
        chosen.add(name)
        if with_downstream:
            frontier = {name}
            while frontier:
                nxt = {n for n, m in models.items()
                       if set(m.depends_on) & frontier and n not in chosen}
                chosen |= nxt
                frontier = nxt
    return [name for name in order if name in chosen]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class ModelRunner:
    """Builds models and runs tests against one database."""

    def __init__(self, engine: Any, models_dir: Path = DEFAULT_MODELS_DIR,
                 run_id: Optional[str] = None) -> None:
        self.engine = engine
        self.models_dir = models_dir
        self.models = load_models(models_dir)
        self.tests = parse_tests(models_dir / TESTS_FILE)
        self.run_id = run_id or os.environ.get("PIPELINE_RUN_ID")

    # -- recording ----------------------------------------------------------

    def _record(self, sql: str, params: Dict[str, Any]) -> None:
        """Best-effort write to ops.*: losing a log row must not fail a build."""
        from sqlalchemy import text
        try:
            with self.engine.begin() as connection:
                connection.execute(text(sql), params)
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not record to ops.*: {type(exc).__name__})")

    # -- build --------------------------------------------------------------

    def build(self, select: Optional[Sequence[str]] = None, *,
              full_refresh: bool = False,
              lookback_days: int = DEFAULT_LOOKBACK_DAYS,
              as_of: Optional[date] = None,
              snapshot_date: Optional[date] = None) -> List[ModelResult]:
        """Build the selected models in dependency order; stop at the first failure."""
        from sqlalchemy import text

        names = select_models(self.models, select)
        today = date.today()
        params = {"lookback_days": int(lookback_days),
                  "full_refresh": bool(full_refresh),
                  "as_of": (as_of or today).isoformat(),
                  "snapshot_date": (snapshot_date or today).isoformat()}

        results: List[ModelResult] = []
        for name in names:
            model = self.models[name]
            started = datetime.now(timezone.utc)
            clock = time.perf_counter()
            status, error, rows, target_rows = "SUCCESS", None, None, None
            try:
                with self.engine.begin() as connection:
                    if full_refresh and model.kind == "incremental":
                        # The table may not exist on a first build; only
                        # truncate it when it does.
                        exists = connection.execute(
                            text("SELECT to_regclass(:t) IS NOT NULL"),
                            {"t": model.target}).scalar()
                        if exists:
                            connection.execute(text(f"TRUNCATE {model.target}"))
                    elif full_refresh and model.kind == "snapshot":
                        print(f"  note: {name} is a snapshot and is NOT truncated on "
                              f"--full-refresh (its history cannot be rebuilt)")
                    outcome = connection.execute(text(model.sql), params)
                    rows = outcome.rowcount if outcome.rowcount >= 0 else None
                    if model.kind != "view":
                        target_rows = connection.execute(
                            text(f"SELECT COUNT(*) FROM {model.target}")).scalar()
            except Exception as exc:  # noqa: BLE001
                status, error = "FAILED", f"{type(exc).__name__}: {exc}".splitlines()[0]

            duration_ms = int((time.perf_counter() - clock) * 1000)
            result = ModelResult(name, model.kind, status, rows, target_rows,
                                 duration_ms, error)
            results.append(result)
            self._record(
                "INSERT INTO ops.model_runs (run_id, model_name, kind, status, "
                "rows_affected, started_at, duration_ms, full_refresh, error) "
                "VALUES (CAST(:run_id AS UUID), :name, :kind, :status, :rows, "
                ":started, :ms, :full, :error)",
                {"run_id": self.run_id, "name": name, "kind": model.kind,
                 "status": status, "rows": rows, "started": started,
                 "ms": duration_ms, "full": full_refresh, "error": error})
            if status == "FAILED":
                break
        return results

    # -- tests --------------------------------------------------------------

    def test(self, select: Optional[Sequence[str]] = None) -> List[TestResult]:
        """Run the tests whose model is in scope. Zero violating rows = pass."""
        from sqlalchemy import text

        scope = set(select_models(self.models, select))
        results: List[TestResult] = []
        for test in self.tests:
            if test.model not in self.models:
                raise ModelError(f"test {test.name!r} targets unknown model "
                                 f"{test.model!r}")
            if test.model not in scope:
                continue
            try:
                with self.engine.connect() as connection:
                    failing = connection.execute(text(
                        f"SELECT COUNT(*) FROM ({test.sql}) AS violations")).scalar()
                    sample = []
                    if failing:
                        sample = [tuple(r) for r in connection.execute(text(
                            f"SELECT * FROM ({test.sql}) AS violations LIMIT 3"))]
                status = "FAIL" if failing else "PASS"
            except Exception as exc:  # noqa: BLE001
                # A test that cannot run is a failed test, never a silent pass.
                failing, status = 1, "FAIL"
                sample = [f"{type(exc).__name__}: {exc}".splitlines()[0]]
            results.append(TestResult(test.name, test.model, test.severity,
                                      int(failing), status, sample))
            self._record(
                "INSERT INTO ops.model_test_results (run_id, test_name, model_name, "
                "severity, status, failing_rows) "
                "VALUES (CAST(:run_id AS UUID), :name, :model, :sev, :status, :n)",
                {"run_id": self.run_id, "name": test.name, "model": test.model,
                 "sev": test.severity, "status": status, "n": int(failing)})
        return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_build(results: Iterable[ModelResult]) -> None:
    print()
    print(f"{'MODEL':<22}{'KIND':<13}{'STATUS':<9}{'UPSERTED':>10}{'IN TARGET':>11}{'MS':>8}")
    print("-" * 73)
    for r in results:
        rows = "-" if r.rows_affected is None else f"{r.rows_affected:,}"
        target = "-" if r.target_rows is None else f"{r.target_rows:,}"
        print(f"{r.name:<22}{r.kind:<13}{r.status:<9}{rows:>10}{target:>11}{r.duration_ms:>8}")
        if r.error:
            print(f"    ERROR: {r.error}")


def print_tests(results: Iterable[TestResult]) -> None:
    results = list(results)
    print()
    print(f"{'TEST':<54}{'SEV':<7}{'RESULT':<8}{'VIOLATIONS':>10}")
    print("-" * 79)
    for r in results:
        print(f"{r.name:<54}{r.severity:<7}{r.status:<8}{r.failing_rows:>10}")
        for row in r.sample:
            print(f"    e.g. {row}")
    passed = sum(1 for r in results if r.status == "PASS")
    print("-" * 79)
    print(f"{passed}/{len(results)} tests passed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and test the SQL models.")
    parser.add_argument("--dsn", default=None, help="SQLAlchemy DSN (or RETAIL_DB_DSN).")
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    parser.add_argument("--select", nargs="+", metavar="MODEL",
                        help="Models to build; append + for downstream dependents.")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Rebuild incremental models from the beginning.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help="Days before the watermark an incremental reprocesses "
                             f"to absorb late and corrected data (default {DEFAULT_LOOKBACK_DAYS}).")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="Effective date for SCD2 version changes (default today).")
    parser.add_argument("--snapshot-date", type=date.fromisoformat, default=None,
                        help="Date to stamp the stock snapshot with (default today).")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--tests-only", action="store_true")
    parser.add_argument("--fail-on-warn", action="store_true",
                        help="Treat failing warn-severity tests as a failed run.")
    parser.add_argument("--list", action="store_true",
                        help="Print the build order and exit; needs no database.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        models = load_models(args.models_dir)
        order = select_models(models, args.select)
    except ModelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for position, name in enumerate(order, 1):
            m = models[name]
            deps = ", ".join(m.depends_on) or "-"
            print(f"{position:>2}. {name:<22}{m.kind:<13} depends on: {deps}")
        return 0

    engine = dbutil.get_engine(args.dsn)
    try:
        dbutil.ensure_schemas(engine)
        runner = ModelRunner(engine, args.models_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot prepare the database: {type(exc).__name__}: "
              f"{str(exc).splitlines()[0]}", file=sys.stderr)
        return 1

    exit_code = 0
    if not args.tests_only:
        built = runner.build(args.select, full_refresh=args.full_refresh,
                             lookback_days=args.lookback_days, as_of=args.as_of,
                             snapshot_date=args.snapshot_date)
        print_build(built)
        if any(r.status == "FAILED" for r in built):
            return 1

    if not args.skip_tests:
        tested = runner.test(args.select)
        print_tests(tested)
        failed_errors = [t for t in tested if t.status == "FAIL" and t.severity == "error"]
        failed_warns = [t for t in tested if t.status == "FAIL" and t.severity == "warn"]
        if failed_errors or (args.fail_on_warn and failed_warns):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
