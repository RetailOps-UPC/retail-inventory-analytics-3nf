# -*- coding: utf-8 -*-
"""
Tests for the orchestrator's graph logic, retries and failure isolation.

These run on FAKE tasks, with no database and no sleeping, because what is being
tested is the orchestration itself: which tasks run, in what order, what happens
downstream of a failure, and which failures are worth retrying.
"""

from __future__ import annotations

import pytest

import pipeline as pl
from pipeline import Context, Pipeline, PipelineError, Task, TaskFailed, Transient


def task(name, deps=(), func=None):
    return Task(name, f"{name} task", tuple(deps), func or (lambda ctx: {"ran": name}))


def graph():
    """bootstrap + generate -> etl -> models -> forecast ; etl -> report."""
    return Pipeline([task("bootstrap"), task("generate"),
                     task("etl", ["bootstrap", "generate"]),
                     task("models", ["etl"]), task("forecast", ["models"]),
                     task("report", ["etl"])])


def run(pipeline, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return pipeline.run(Context(), **kwargs)


def statuses(outcomes):
    return {o.name: o.status for o in outcomes}


# ---------------------------------------------------------------------------
# Graph validation and ordering
# ---------------------------------------------------------------------------

def test_the_order_respects_every_dependency():
    p = graph()
    order = p.order
    for t in p.tasks.values():
        for dep in t.depends_on:
            assert order.index(dep) < order.index(t.name)


def test_a_cycle_is_rejected_at_construction():
    with pytest.raises(PipelineError, match="cycle"):
        Pipeline([task("a", ["b"]), task("b", ["a"])])


def test_an_unknown_dependency_is_rejected():
    with pytest.raises(PipelineError, match="unknown"):
        Pipeline([task("a", ["ghost"])])


def test_duplicate_task_names_are_rejected():
    with pytest.raises(PipelineError, match="duplicate"):
        Pipeline([task("a"), task("a")])


def test_downstream_includes_transitive_dependents_only():
    p = graph()
    assert p.downstream_of("etl") == {"models", "forecast", "report"}
    assert p.downstream_of("forecast") == set()
    assert p.downstream_of("generate") == {"etl", "models", "forecast", "report"}


def test_the_real_pipeline_graph_is_valid_and_etl_comes_before_models():
    real = pl.build_pipeline()
    assert real.order.index("etl") < real.order.index("models") < real.order.index("forecast")
    assert real.order.index("generate") < real.order.index("etl")
    assert real.order.index("bootstrap") < real.order.index("etl")


# ---------------------------------------------------------------------------
# Selecting what to run
# ---------------------------------------------------------------------------

def test_by_default_everything_runs_in_order():
    outcomes = run(graph())
    assert [o.name for o in outcomes] == graph().order
    assert set(statuses(outcomes).values()) == {"SUCCESS"}


def test_from_runs_a_task_and_everything_downstream():
    assert graph().plan(from_task="models") == ["models", "forecast"]
    planned = graph().plan(from_task="etl")
    # Independent branches (report, models) may run in either order; what is
    # guaranteed is the set, and that etl precedes everything that needs it.
    assert set(planned) == {"etl", "models", "forecast", "report"}
    assert planned[0] == "etl"
    assert planned.index("models") < planned.index("forecast")


def test_only_runs_exactly_the_named_tasks():
    planned = graph().plan(only=["forecast", "report"])
    assert set(planned) == {"forecast", "report"} and len(planned) == 2


def test_skip_removes_tasks():
    assert "generate" not in graph().plan(skip=["generate"])


def test_from_and_skip_combine():
    assert graph().plan(from_task="etl", skip=["report"]) == ["etl", "models", "forecast"]
    assert "report" not in graph().plan(from_task="etl", skip=["report"])


def test_an_unknown_task_name_in_a_selection_is_an_error():
    with pytest.raises(PipelineError, match="unknown"):
        graph().plan(only=["nope"])


def test_selecting_a_task_whose_upstream_is_not_selected_still_runs_it():
    """--only forecast must work: the user is asserting upstream is already done."""
    assert statuses(run(graph(), only=["forecast"])) == {"forecast": "SUCCESS"}


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

def failing(message="boom"):
    def func(ctx):
        raise TaskFailed(message)
    return func


def test_a_failure_skips_everything_downstream_of_it():
    p = Pipeline([task("bootstrap"), task("generate"),
                  task("etl", ["bootstrap", "generate"], failing()),
                  task("models", ["etl"]), task("forecast", ["models"]),
                  task("report", ["etl"])])
    result = statuses(run(p))
    assert result["etl"] == "FAILED"
    assert result["models"] == "SKIPPED" and result["forecast"] == "SKIPPED"
    assert result["report"] == "SKIPPED"
    assert result["bootstrap"] == result["generate"] == "SUCCESS"


def test_a_task_that_does_not_depend_on_the_failure_still_runs():
    p = Pipeline([task("etl"), task("models", ["etl"], failing()),
                  task("forecast", ["models"]), task("report", ["etl"])])
    result = statuses(run(p))
    assert result["models"] == "FAILED" and result["forecast"] == "SKIPPED"
    assert result["report"] == "SUCCESS", "an independent branch must survive a sibling's failure"


def test_a_skipped_task_says_which_upstream_failed():
    p = Pipeline([task("a", func=failing()), task("b", ["a"])])
    skipped = [o for o in run(p) if o.name == "b"][0]
    assert skipped.status == "SKIPPED" and "a" in skipped.error


def test_a_skipped_task_names_the_root_cause_not_the_nearest_skipped_neighbour():
    """forecast is blocked by models, but models was only SKIPPED: the thing that
    actually broke is etl, and that is what the reader needs to be told."""
    p = Pipeline([task("etl", func=failing()), task("models", ["etl"]),
                  task("forecast", ["models"])])
    result = {o.name: o for o in run(p)}
    assert result["models"].error == "upstream failed: etl"
    assert result["forecast"].error == "upstream failed: etl"


def test_a_failure_records_the_error_message():
    p = Pipeline([task("a", func=failing("the gate said no"))])
    outcome = run(p)[0]
    assert outcome.status == "FAILED" and "the gate said no" in outcome.error


def test_an_unexpected_exception_is_a_failure_not_a_crash():
    def func(ctx):
        raise KeyError("surprise")
    assert statuses(run(Pipeline([task("a", func=func)]))) == {"a": "FAILED"}


def test_overall_status_is_failed_if_anything_failed_or_was_skipped():
    ok = run(Pipeline([task("a")]))
    bad = run(Pipeline([task("a", func=failing()), task("b", ["a"])]))
    assert pl.overall_status(ok) == "SUCCESS"
    assert pl.overall_status(bad) == "FAILED"


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def flaky(failures, exc=Transient):
    state = {"calls": 0}

    def func(ctx):
        state["calls"] += 1
        if state["calls"] <= failures:
            raise exc("not yet")
        return {"calls": state["calls"]}
    return func, state


def test_a_transient_failure_is_retried_until_it_succeeds():
    func, state = flaky(2)
    outcome = run(Pipeline([task("a", func=func)]), retries=3)[0]
    assert outcome.status == "SUCCESS" and outcome.attempts == 3 and state["calls"] == 3


def test_retries_are_bounded():
    func, state = flaky(99)
    outcome = run(Pipeline([task("a", func=func)]), retries=2)[0]
    assert outcome.status == "FAILED" and state["calls"] == 3      # 1 try + 2 retries


def test_a_permanent_failure_is_never_retried():
    """Re-running a failed quality gate fails the same way and hides the problem."""
    func, state = flaky(99, exc=TaskFailed)
    outcome = run(Pipeline([task("a", func=func)]), retries=5)[0]
    assert outcome.status == "FAILED" and state["calls"] == 1 and outcome.attempts == 1


def test_retry_delay_doubles_each_time():
    func, _ = flaky(3)
    waits = []
    run(Pipeline([task("a", func=func)]), retries=3, retry_delay=2.0, sleep=waits.append)
    assert waits == [2.0, 4.0, 8.0]


def test_no_retries_by_default_in_the_library():
    func, state = flaky(1)
    assert run(Pipeline([task("a", func=func)]))[0].status == "FAILED"
    assert state["calls"] == 1


def test_transient_classification():
    assert pl.is_transient(Transient("x"))
    assert pl.is_transient(ConnectionError("x"))
    assert pl.is_transient(TimeoutError("x"))

    class OperationalError(Exception):
        pass

    assert pl.is_transient(OperationalError("db is starting"))
    assert not pl.is_transient(TaskFailed("gate failed"))
    assert not pl.is_transient(ValueError("bad input"))
    assert not pl.is_transient(AssertionError("test failed"))


# ---------------------------------------------------------------------------
# Output capture and records
# ---------------------------------------------------------------------------

def test_task_output_is_captured_and_written_to_a_log(tmp_path):
    def func(ctx):
        print("hello from the task")
        return {}
    run(Pipeline([task("a", func=func)]), log_dir=tmp_path)
    assert "hello from the task" in (tmp_path / "a.log").read_text(encoding="utf-8")


def test_a_failed_tasks_output_tail_is_kept_for_the_summary():
    def func(ctx):
        for i in range(30):
            print(f"line {i}")
        raise TaskFailed("died")
    outcome = run(Pipeline([task("a", func=func)]))[0]
    assert "line 29" in outcome.detail["output_tail"]
    assert "line 0" not in outcome.detail["output_tail"], "only the tail, not the whole log"


def test_output_is_not_captured_in_verbose_mode(capsys):
    def func(ctx):
        print("visible")
        return {}
    run(Pipeline([task("a", func=func)]), capture=False)
    assert "visible" in capsys.readouterr().out


def test_events_are_reported_with_a_position_and_status():
    events = []
    run(graph(), on_event=events.append)
    assert events[0].startswith("[1/6]") and "SUCCESS" in events[0]
    assert len(events) == 6


def test_run_summary_and_log_are_json_serialisable_and_appendable(tmp_path):
    import json
    from datetime import datetime, timezone
    p = Pipeline([task("a"), task("b", ["a"], failing())])
    ctx = Context()
    started = datetime.now(timezone.utc)
    outcomes = p.run(ctx)
    summary = pl.run_summary(ctx, outcomes, started, datetime.now(timezone.utc), {"x": 1})
    assert summary["status"] == "FAILED"
    log = tmp_path / "runs.jsonl"
    pl.append_run_log(summary, log)
    pl.append_run_log(summary, log)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["run_id"] == ctx.run_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_list_prints_the_graph_and_exits_zero(capsys):
    assert pl.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name in ("bootstrap", "generate", "etl", "models", "forecast", "dq_report"):
        assert name in out


def test_plan_shows_what_would_run_without_running_it(capsys):
    assert pl.main(["--plan", "--from", "models"]) == 0
    assert "models -> forecast" in capsys.readouterr().out


def test_an_unknown_task_is_a_usage_error(capsys):
    assert pl.main(["--only", "nope"]) == 2


def test_selecting_nothing_is_a_usage_error():
    assert pl.main(["--only", "etl", "--skip", "etl"]) == 2
