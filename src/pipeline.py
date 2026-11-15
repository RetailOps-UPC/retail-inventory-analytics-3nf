#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pipeline orchestrator: one command runs the whole platform, in order, with
retries, failure isolation and a record of what happened.

The stages
----------
::

    bootstrap --+
                +--> etl --+--> models --> forecast
    generate ---+          |
                           +--> dq_report

``bootstrap``  checks the database is reachable (retrying while a container
               boots) and applies the idempotent ``ops`` and ``ml`` schemas.
``generate``   simulates the store network and writes the raw CSVs.
``etl``        extracts, runs the quality gate, quarantines bad rows, upserts.
``models``     builds the star schema incrementally, then runs the model tests.
``forecast``   forecasts demand, projects expiry, recommends orders.
``dq_report``  renders the versioned data-quality report.

Behaviour that makes it an orchestrator rather than a script
-------------------------------------------------------------
* **Dependencies, not a fixed list.** A task runs when the tasks it depends on
  succeeded. If ``etl`` fails, ``models`` and ``forecast`` are SKIPPED, because
  forecasting on a half-loaded warehouse is worse than not forecasting. Tasks
  that do not depend on the failure (``dq_report`` after a failed ``models``)
  still run.
* **Retries only where retrying can help.** A database that is still starting
  raises a transient error and is retried with exponential backoff. A failed
  quality gate or a failed model test does not: running it again will fail the
  same way, and quietly retrying a data problem hides it.
* **A record of every run**, in ``ops.pipeline_runs`` / ``ops.task_runs`` when the
  database is up, and always in ``data/processed/pipeline_runs.jsonl``.
* **Selective runs.** ``--only``, ``--from`` and ``--skip`` re-run part of the
  graph without repeating hours of upstream work.

Usage
-----
    python src/pipeline.py                       # everything
    python src/pipeline.py --list                # show the graph and exit
    python src/pipeline.py --from models         # models and everything downstream
    python src/pipeline.py --only forecast dq_report
    python src/pipeline.py --skip generate       # reuse the existing CSVs
    python src/pipeline.py --retries 3 --retry-delay 2

Dependencies: those of the stages it runs (pandas, numpy, SQLAlchemy, psycopg2).
Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbutil  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_PROCESSED = PROJECT_ROOT / "data" / "processed"
DEFAULT_RUN_LOG = DEFAULT_PROCESSED / "pipeline_runs.jsonl"
DEFAULT_TASK_LOGS = DEFAULT_PROCESSED / "pipeline_logs"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PipelineError(RuntimeError):
    """The graph itself is invalid (unknown task, cycle)."""


class TaskFailed(RuntimeError):
    """A task failed for a reason a retry will not fix."""


class Transient(RuntimeError):
    """A task failed for a reason that may clear on its own; safe to retry."""


def is_transient(exc: BaseException) -> bool:
    """Whether retrying could plausibly succeed.

    Connection trouble and timeouts are transient. Everything else, in
    particular a failed quality gate or a failed assertion, is not: it will
    fail the same way again, and retrying it would only hide the problem.
    """
    if isinstance(exc, (Transient, ConnectionError, TimeoutError)):
        return True
    return type(exc).__name__ in {"OperationalError", "InterfaceError"}


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """Everything a task needs, assembled once per run."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    dsn: str = field(default_factory=dbutil.resolve_dsn)
    raw_dir: Path = DEFAULT_RAW_DIR
    days: int = 60
    seed: Optional[int] = None
    end_date: Optional[date] = None
    policy: str = "heuristic"
    lookback_days: int = 3
    full_refresh: bool = False
    horizon: int = 14
    service_z: float = 1.65
    strategy: str = "ensemble"
    etl_log: Path = PROJECT_ROOT / "docs" / "etl_execution.log"
    quarantine_dir: Path = PROJECT_ROOT / "data" / "quarantine"
    quality_runs_dir: Path = PROJECT_ROOT / "data" / "quality_runs"
    quality_history: Path = PROJECT_ROOT / "docs" / "data_quality" / "history.jsonl"
    quality_report: Path = PROJECT_ROOT / "docs" / "data_quality_report.md"
    out_dir: Path = DEFAULT_PROCESSED
    _engine: Any = None

    @property
    def engine(self) -> Any:
        if self._engine is None:
            self._engine = dbutil.get_engine(self.dsn)
        return self._engine


@dataclass
class Task:
    name: str
    description: str
    depends_on: Tuple[str, ...]
    func: Callable[[Context], Dict[str, Any]]


@dataclass
class Outcome:
    name: str
    status: str                       # SUCCESS | FAILED | SKIPPED
    attempts: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

class Pipeline:
    """A validated task graph and the logic to run all or part of it."""

    def __init__(self, tasks: Sequence[Task]) -> None:
        self.tasks: Dict[str, Task] = {}
        for task in tasks:
            if task.name in self.tasks:
                raise PipelineError(f"duplicate task name {task.name!r}")
            self.tasks[task.name] = task
        for task in tasks:
            for dep in task.depends_on:
                if dep not in self.tasks:
                    raise PipelineError(f"task {task.name!r} depends on unknown task {dep!r}")
        self._order = self._topological()

    def _topological(self) -> List[str]:
        remaining = {n: set(t.depends_on) for n, t in self.tasks.items()}
        ordered: List[str] = []
        while remaining:
            ready = sorted(n for n, deps in remaining.items() if not deps)
            if not ready:
                raise PipelineError("dependency cycle among: " + ", ".join(sorted(remaining)))
            for name in ready:
                ordered.append(name)
                del remaining[name]
            for deps in remaining.values():
                deps.difference_update(ready)
        return ordered

    @property
    def order(self) -> List[str]:
        return list(self._order)

    def downstream_of(self, name: str) -> Set[str]:
        """Every task that depends on ``name``, directly or through others."""
        found: Set[str] = set()
        frontier = {name}
        while frontier:
            nxt = {n for n, t in self.tasks.items()
                   if set(t.depends_on) & frontier and n not in found}
            found |= nxt
            frontier = nxt
        return found

    def plan(self, only: Optional[Sequence[str]] = None,
             from_task: Optional[str] = None,
             skip: Optional[Sequence[str]] = None) -> List[str]:
        """The tasks a run would execute, in order."""
        for name in list(only or []) + list(skip or []) + ([from_task] if from_task else []):
            if name not in self.tasks:
                raise PipelineError(f"unknown task {name!r}; known: {', '.join(self._order)}")
        chosen = set(self._order)
        if only:
            chosen = set(only)
        if from_task:
            chosen &= {from_task} | self.downstream_of(from_task)
        chosen -= set(skip or [])
        return [n for n in self._order if n in chosen]

    def run(self, ctx: Context, *, only: Optional[Sequence[str]] = None,
            from_task: Optional[str] = None, skip: Optional[Sequence[str]] = None,
            retries: int = 0, retry_delay: float = 1.0,
            sleep: Callable[[float], None] = time.sleep,
            on_event: Optional[Callable[[str], None]] = None,
            capture: bool = True,
            log_dir: Optional[Path] = None) -> List[Outcome]:
        """Execute the planned tasks; skip whatever depends on a failure."""
        emit = on_event or (lambda message: None)
        planned = self.plan(only, from_task, skip)
        #: task -> the task that actually failed. A task that FAILED is its own
        #: root cause; a task that was SKIPPED inherits its blocker's. Reporting
        #: the root cause, not the nearest skipped neighbour, is what tells the
        #: reader where to look: "forecast skipped: models" is a dead end,
        #: "forecast skipped: etl" is where the problem is.
        root_cause: Dict[str, str] = {}
        outcomes: List[Outcome] = []

        for position, name in enumerate(planned, 1):
            task = self.tasks[name]
            blockers = [d for d in task.depends_on if d in root_cause]
            if blockers:
                roots = sorted({root_cause[d] for d in blockers})
                root_cause[name] = roots[0]      # its dependents are blocked too
                reason = f"upstream failed: {', '.join(roots)}"
                outcomes.append(Outcome(name, "SKIPPED", error=reason))
                emit(f"[{position}/{len(planned)}] {name:<11} SKIPPED ({reason})")
                continue

            outcome = self._run_task(task, ctx, retries, retry_delay, sleep, capture, log_dir)
            outcomes.append(outcome)
            if outcome.status == "FAILED":
                root_cause[name] = name
            suffix = f", {outcome.attempts} attempts" if outcome.attempts > 1 else ""
            emit(f"[{position}/{len(planned)}] {name:<11} {outcome.status:<8} "
                 f"{outcome.duration_ms / 1000:6.1f}s{suffix}"
                 + (f"  {outcome.error}" if outcome.error else ""))
        return outcomes

    def _run_task(self, task: Task, ctx: Context, retries: int, retry_delay: float,
                  sleep: Callable[[float], None], capture: bool,
                  log_dir: Optional[Path]) -> Outcome:
        started = datetime.now(timezone.utc)
        clock = time.perf_counter()
        outcome = Outcome(task.name, "FAILED", started_at=started)
        buffer = io.StringIO()

        for attempt in range(1, retries + 2):
            outcome.attempts = attempt
            try:
                with (contextlib.redirect_stdout(buffer) if capture
                      else contextlib.nullcontext()):
                    outcome.detail = task.func(ctx) or {}
                outcome.status, outcome.error = "SUCCESS", None
                break
            except Exception as exc:  # noqa: BLE001 - the record must capture anything
                message = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
                outcome.status, outcome.error = "FAILED", message.strip()
                if attempt <= retries and is_transient(exc):
                    sleep(retry_delay * (2 ** (attempt - 1)))
                    continue
                break

        outcome.finished_at = datetime.now(timezone.utc)
        outcome.duration_ms = int((time.perf_counter() - clock) * 1000)
        if capture and log_dir is not None and buffer.getvalue():
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"{task.name}.log").write_text(buffer.getvalue(), encoding="utf-8")
        if outcome.status == "FAILED" and capture:
            tail = "\n".join(buffer.getvalue().splitlines()[-12:])
            if tail:
                outcome.detail = {**outcome.detail, "output_tail": tail}
        return outcome


# ---------------------------------------------------------------------------
# Task implementations
# ---------------------------------------------------------------------------

def task_bootstrap(ctx: Context) -> Dict[str, Any]:
    """Confirm the database answers, then apply the idempotent schemas."""
    from sqlalchemy import text
    engine = ctx.engine
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        # Surface connection failures as transient: a container that is still
        # booting is the commonest cause, and it clears by itself.
        raise Transient(f"database not reachable: {type(exc).__name__}") from exc
    return {"schemas_applied": dbutil.ensure_schemas(engine)}


def task_generate(ctx: Context) -> Dict[str, Any]:
    import data_generator as gen
    argv = ["--out", str(ctx.raw_dir), "--days", str(ctx.days), "--policy", ctx.policy]
    if ctx.seed is not None:
        argv += ["--seed", str(ctx.seed)]
    if ctx.end_date is not None:
        argv += ["--end-date", ctx.end_date.isoformat()]
    code = gen.main(argv)
    if code != 0:
        raise TaskFailed(f"data generator exited with {code}")
    manifest = json.loads((ctx.raw_dir / "_manifest.json").read_text(encoding="utf-8"))
    return {"seed": manifest["seed"], "window": f"{manifest['start_date']}..{manifest['end_date']}",
            "rows": sum(manifest["row_counts"].values()),
            "planted_defects": sum(manifest["anomaly_counts"].values())}


def task_etl(ctx: Context) -> Dict[str, Any]:
    import etl_pipeline as etl
    os.environ["PIPELINE_RUN_ID"] = ctx.run_id
    code = etl.main(["--dsn", ctx.dsn, "--raw-dir", str(ctx.raw_dir),
                     "--log-file", str(ctx.etl_log),
                     "--quarantine-dir", str(ctx.quarantine_dir),
                     "--quality-runs-dir", str(ctx.quality_runs_dir),
                     "--quality-history", str(ctx.quality_history)])
    if code != 0:
        raise TaskFailed(f"ETL exited with {code}; see {ctx.etl_log}")
    # The load already succeeded. The quality history is an audit by-product
    # that the ETL writes best-effort, so its absence must not fail the task.
    try:
        history = [json.loads(line) for line in
                   ctx.quality_history.read_text(encoding="utf-8").splitlines() if line.strip()]
        last = history[-1]
    except (OSError, ValueError, IndexError):
        return {"quality_history": "not available"}
    truth = last.get("ground_truth", {})
    return {"rows_extracted": last["totals"]["rows_extracted"],
            "rows_quarantined": last["totals"]["rows_quarantined"],
            "acceptance_rate": last["totals"]["acceptance_rate"],
            "defects_caught": f"{sum(v['detected'] for v in truth.values())}/"
                              f"{sum(v['injected'] for v in truth.values())}"}


def task_models(ctx: Context) -> Dict[str, Any]:
    from model_runner import ModelRunner
    runner = ModelRunner(ctx.engine, run_id=ctx.run_id)
    built = runner.build(full_refresh=ctx.full_refresh, lookback_days=ctx.lookback_days)
    failed = [r for r in built if r.status == "FAILED"]
    if failed:
        raise TaskFailed(f"model {failed[0].name} failed: {failed[0].error}")
    tests = runner.test()
    broken = [t for t in tests if t.status == "FAIL" and t.severity == "error"]
    if broken:
        raise TaskFailed("model tests failed: " + ", ".join(t.name for t in broken))
    return {"models_built": len(built),
            "rows_upserted": sum(r.rows_affected or 0 for r in built),
            "tests_passed": f"{sum(1 for t in tests if t.status == 'PASS')}/{len(tests)}"}


def task_forecast(ctx: Context) -> Dict[str, Any]:
    from datetime import timedelta

    import pandas as pd

    import forecasting as fc
    sales = fc.load_sales_history(ctx.engine)
    if sales.empty:
        raise TaskFailed("no sales history in mart.fact_sales_daily; did the models run?")
    start = pd.to_datetime(sales["sale_date"]).max().date() + timedelta(days=1)
    result = fc.plan(sales, fc.load_products(ctx.engine), fc.load_lots(ctx.engine, start),
                     horizon=ctx.horizon, service_z=ctx.service_z, strategy=ctx.strategy)
    fc.write_csvs(result, ctx.out_dir)
    fc.persist(ctx.engine, result, run_id=ctx.run_id,
               parameters={"horizon": ctx.horizon, "service_z": ctx.service_z,
                           "strategy": ctx.strategy})
    summary = result.forecast.summary()
    return {"series": len(result.forecast.series),
            "holdout_wape": round(summary["holdout_wape"], 4) if summary["holdout_wape"] else None,
            "interval_coverage": round(summary["coverage"], 4) if summary["coverage"] else None,
            "lots_projected_to_expire": sum(1 for p in result.projections if p.expired > 1e-9)}


def task_dq_report(ctx: Context) -> Dict[str, Any]:
    import dq_report
    path = dq_report.generate(ctx.quality_history, ctx.quality_runs_dir, ctx.quality_report)
    return {"report": str(path)}


def build_pipeline() -> Pipeline:
    return Pipeline([
        Task("bootstrap", "Check the database and apply the ops/ml schemas", (), task_bootstrap),
        Task("generate", "Simulate the store network and write the raw CSVs", (), task_generate),
        Task("etl", "Extract, quality-gate, quarantine and upsert", ("bootstrap", "generate"), task_etl),
        Task("models", "Build the star schema incrementally and test it", ("etl",), task_models),
        Task("forecast", "Forecast demand, project expiry, recommend orders", ("models",), task_forecast),
        Task("dq_report", "Render the versioned data-quality report", ("etl",), task_dq_report),
    ])


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------

def overall_status(outcomes: Sequence[Outcome]) -> str:
    return "FAILED" if any(o.status in ("FAILED", "SKIPPED") for o in outcomes) else "SUCCESS"


def run_summary(ctx: Context, outcomes: Sequence[Outcome], started: datetime,
                finished: datetime, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": ctx.run_id,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 2),
        "status": overall_status(outcomes),
        "parameters": parameters,
        "tasks": [{"name": o.name, "status": o.status, "attempts": o.attempts,
                   "duration_ms": o.duration_ms, "detail": o.detail, "error": o.error}
                  for o in outcomes],
    }


def append_run_log(summary: Dict[str, Any], path: Path = DEFAULT_RUN_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")


def record_to_database(engine: Any, summary: Dict[str, Any],
                       outcomes: Sequence[Outcome], started: datetime,
                       finished: datetime) -> None:
    """Write the run to ops.pipeline_runs / ops.task_runs. Raises on failure."""
    from sqlalchemy import text
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO ops.pipeline_runs (run_id, started_at, finished_at, status, "
            "  trigger, parameters) "
            "VALUES (CAST(:run_id AS UUID), :started, :finished, :status, 'cli', "
            "        CAST(:params AS JSONB)) "
            "ON CONFLICT (run_id) DO NOTHING"),
            {"run_id": summary["run_id"], "started": started, "finished": finished,
             "status": summary["status"],
             "params": json.dumps(summary["parameters"], default=str)})
        rows = [{"run_id": summary["run_id"], "name": o.name, "attempt": max(o.attempts, 1),
                 "status": o.status, "started": o.started_at or started,
                 "finished": o.finished_at or finished, "ms": o.duration_ms,
                 "detail": json.dumps(o.detail, default=str) if o.detail else None,
                 "error": o.error} for o in outcomes]
        if rows:
            connection.execute(text(
                "INSERT INTO ops.task_runs (run_id, task_name, attempt, status, "
                "  started_at, finished_at, duration_ms, detail, error) "
                "VALUES (CAST(:run_id AS UUID), :name, :attempt, :status, :started, "
                "        :finished, :ms, CAST(:detail AS JSONB), :error)"), rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the retail data platform end to end.")
    parser.add_argument("--only", nargs="+", metavar="TASK", help="Run exactly these tasks.")
    parser.add_argument("--from", dest="from_task", metavar="TASK",
                        help="Run this task and everything downstream of it.")
    parser.add_argument("--skip", nargs="+", metavar="TASK", help="Leave these tasks out.")
    parser.add_argument("--list", action="store_true", help="Show the task graph and exit.")
    parser.add_argument("--plan", action="store_true",
                        help="Show what would run, in order, without running it.")
    parser.add_argument("--retries", type=int, default=2,
                        help="Extra attempts for TRANSIENT failures only (default 2).")
    parser.add_argument("--retry-delay", type=float, default=2.0,
                        help="Seconds before the first retry; doubles each time (default 2).")
    parser.add_argument("--verbose", action="store_true",
                        help="Stream each task's output instead of capturing it.")
    parser.add_argument("--dsn", default=None, help="SQLAlchemy DSN (or RETAIL_DB_DSN).")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--end-date", type=date.fromisoformat, default=None)
    parser.add_argument("--policy", choices=("heuristic", "level", "forecast"),
                        default="heuristic", help="Replenishment policy of the simulated stores.")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Rebuild incremental models from the beginning.")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--horizon", type=int, default=14)
    parser.add_argument("--service-z", type=float, default=1.65)
    parser.add_argument("--strategy", choices=("ensemble", "per_series"), default="ensemble")
    return parser.parse_args(argv)


def print_graph(pipeline: Pipeline) -> None:
    for position, name in enumerate(pipeline.order, 1):
        task = pipeline.tasks[name]
        deps = ", ".join(task.depends_on) or "-"
        print(f"{position}. {name:<11} {task.description}")
        print(f"   {'':<11} depends on: {deps}")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    pipeline = build_pipeline()

    if args.list:
        print_graph(pipeline)
        return 0

    try:
        planned = pipeline.plan(args.only, args.from_task, args.skip)
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not planned:
        print("ERROR: nothing to run.", file=sys.stderr)
        return 2
    if args.plan:
        print("Would run, in order: " + " -> ".join(planned))
        return 0

    ctx = Context(dsn=dbutil.resolve_dsn(args.dsn), raw_dir=args.raw_dir, days=args.days,
                  seed=args.seed, end_date=args.end_date, policy=args.policy,
                  lookback_days=args.lookback_days, full_refresh=args.full_refresh,
                  horizon=args.horizon, service_z=args.service_z, strategy=args.strategy)
    parameters = {"only": args.only, "from": args.from_task, "skip": args.skip,
                  "days": args.days, "seed": args.seed, "policy": args.policy,
                  "full_refresh": args.full_refresh, "retries": args.retries}

    print(f"Pipeline run {ctx.run_id}")
    print("Plan: " + " -> ".join(planned))
    started = datetime.now(timezone.utc)
    outcomes = pipeline.run(ctx, only=args.only, from_task=args.from_task, skip=args.skip,
                            retries=args.retries, retry_delay=args.retry_delay,
                            on_event=print, capture=not args.verbose,
                            log_dir=DEFAULT_TASK_LOGS / ctx.run_id[:8])
    finished = datetime.now(timezone.utc)

    summary = run_summary(ctx, outcomes, started, finished, parameters)
    append_run_log(summary)
    try:
        if any(o.name == "bootstrap" and o.status == "SUCCESS" for o in outcomes) or \
                "bootstrap" not in planned:
            record_to_database(ctx.engine, summary, outcomes, started, finished)
    except Exception as exc:  # noqa: BLE001 - the audit row must never mask the result
        print(f"(run not recorded in ops.*: {type(exc).__name__})")

    print()
    for outcome in outcomes:
        for key, value in outcome.detail.items():
            if key != "output_tail":
                print(f"  {outcome.name:<10} {key}: {value}")
        if outcome.status == "FAILED" and outcome.detail.get("output_tail"):
            print(f"  --- {outcome.name} output (last lines) ---")
            print("  " + outcome.detail["output_tail"].replace("\n", "\n  "))
    print(f"\nRun {summary['status']} in {summary['duration_s']:.1f}s "
          f"(record: data/processed/pipeline_runs.jsonl)")
    return 0 if summary["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
