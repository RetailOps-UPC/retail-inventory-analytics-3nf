# -*- coding: utf-8 -*-
"""
End-to-end tests: the REAL pipeline, against a throwaway PostgreSQL.

Unit tests prove each stage in isolation. These prove the stages compose:
generate -> quality gate -> load -> star schema -> forecast -> report, run twice,
with a failure injected. The second run is the important one: a pipeline that is
not idempotent works perfectly until the day somebody re-runs it.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

import dq
import pipeline as pl

pytestmark = pytest.mark.integration

SEED = 4242
END = date(2026, 11, 14)


def make_ctx(warehouse, tmp_path):
    return pl.Context(
        dsn=warehouse.url.render_as_string(hide_password=False),
        raw_dir=tmp_path / "raw", days=50, seed=SEED, end_date=END,
        etl_log=tmp_path / "etl.log", quarantine_dir=tmp_path / "quarantine",
        quality_runs_dir=tmp_path / "qruns", quality_history=tmp_path / "history.jsonl",
        quality_report=tmp_path / "report.md", out_dir=tmp_path / "out")


def run_all(ctx, **kwargs):
    outcomes = pl.build_pipeline().run(ctx, sleep=lambda s: None, **kwargs)
    return {o.name: o for o in outcomes}


def assert_all_ok(outcomes):
    bad = {n: (o.status, o.error, o.detail.get("output_tail", "")[-400:])
           for n, o in outcomes.items() if o.status != "SUCCESS"}
    assert not bad, f"pipeline stages did not succeed: {bad}"


@pytest.fixture
def full_run(warehouse, tmp_path):
    ctx = make_ctx(warehouse, tmp_path)
    return ctx, run_all(ctx)


# ---------------------------------------------------------------------------
# One full run
# ---------------------------------------------------------------------------

def test_the_whole_pipeline_runs_end_to_end(full_run):
    _, outcomes = full_run
    assert_all_ok(outcomes)
    assert set(outcomes) == {"bootstrap", "generate", "etl", "models", "forecast", "dq_report"}


def test_every_layer_of_the_warehouse_is_populated(full_run, scalar):
    assert_all_ok(full_run[1])
    for table in ("daily_sales", "goods_receipt_items", "inventory_batches", "waste_log",
                  "mart.fact_sales_daily", "mart.fact_waste", "mart.fact_receipt_line",
                  "mart.fact_stock_position", "ml.demand_forecast",
                  "ml.replenishment_recommendation", "ml.forecast_backtest"):
        assert scalar(f"SELECT COUNT(*) FROM {table}") > 0, f"{table} is empty"


def test_the_star_schema_reconciles_to_the_source_after_a_real_load(full_run, scalar):
    assert_all_ok(full_run[1])
    assert scalar("SELECT SUM(units_sold) FROM daily_sales") == \
           scalar("SELECT SUM(units_sold) FROM mart.fact_sales_daily")
    assert scalar("SELECT SUM(total_loss_amount) FROM waste_log") == \
           scalar("SELECT SUM(total_loss_amount) FROM mart.fact_waste")


def test_the_quality_gate_results_are_persisted_to_the_database(full_run, scalar):
    assert_all_ok(full_run[1])
    assert scalar("SELECT COUNT(*) FROM ops.dq_runs WHERE NOT dry_run") == 1
    assert scalar("SELECT COUNT(*) FROM ops.dq_rule_results") > 20
    assert scalar("SELECT SUM(detected) FROM ops.dq_ground_truth") == \
           scalar("SELECT SUM(injected) FROM ops.dq_ground_truth") > 0, \
        "the gate missed a planted defect"


def test_no_audit_row_was_lost_to_a_key_collision(full_run, scalar):
    """F01 runs once per FK column; the audit table must still hold every rule once."""
    assert_all_ok(full_run[1])
    assert scalar("SELECT COUNT(*) FROM (SELECT rule_id, dataset FROM ops.dq_rule_results "
                  "GROUP BY 1, 2 HAVING COUNT(*) > 1) d") == 0
    assert scalar("SELECT rows_evaluated FROM ops.dq_rule_results "
                  "WHERE rule_id = 'F01' AND dataset = 'goods_receipts.csv'") > \
           scalar("SELECT COUNT(DISTINCT grn_number) FROM goods_receipt_headers"), \
        "the merged F01 count should span every FK column, not just the last"


def test_the_trigger_was_respected_not_bypassed(full_run, scalar):
    assert_all_ok(full_run[1])
    assert scalar("SELECT COUNT(*) FROM waste_log WHERE total_loss_amount > 50 "
                  "AND (authorized_by IS NULL OR BTRIM(authorized_by) = '')") == 0


def test_the_quality_report_is_rendered_from_the_run(full_run):
    ctx, outcomes = full_run
    assert_all_ok(outcomes)
    text = ctx.quality_report.read_text(encoding="utf-8")
    assert "Gate versus ground truth" in text
    assert "planted defects caught" in text
    assert str(SEED) in text


def test_the_datamart_was_refreshed_by_the_load(full_run, scalar):
    assert_all_ok(full_run[1])
    assert scalar("SELECT COUNT(*) FROM mv_store_category_waste_summary") > 0


def test_forecasts_are_calibrated_and_beat_the_baseline(full_run, scalar):
    assert_all_ok(full_run[1])
    holdout, baseline, coverage = (float(x) for x in (
        scalar("SELECT holdout_wape FROM ml.forecast_runs"),
        scalar("SELECT baseline_wape FROM ml.forecast_runs"),
        scalar("SELECT interval_coverage FROM ml.forecast_runs")))
    assert holdout < baseline
    assert 0.65 <= coverage <= 0.95


# ---------------------------------------------------------------------------
# Idempotence: the property that matters most
# ---------------------------------------------------------------------------

SNAPSHOT_QUERIES = {
    "daily_sales": "SELECT md5(string_agg(s::TEXT, '|' ORDER BY sale_id)) FROM daily_sales s",
    "receipt_items": ("SELECT md5(string_agg(grn_item_id || ':' || grn_id || ':' || product_id, "
                      "'|' ORDER BY grn_item_id)) FROM goods_receipt_items"),
    "batch_links": ("SELECT md5(string_agg(batch_code || ':' || COALESCE(grn_item_id::TEXT, '-'), "
                    "'|' ORDER BY batch_code)) FROM inventory_batches"),
    "waste": "SELECT md5(string_agg(waste_code || ':' || total_loss_amount, '|' ORDER BY waste_code)) FROM waste_log",
    "fact_sales": ("SELECT md5(string_agg(f::TEXT, '|' ORDER BY f.date_key, f.store_key, f.product_key)) "
                   "FROM mart.fact_sales_daily f"),
    "fact_waste": "SELECT md5(string_agg(f::TEXT, '|' ORDER BY f.waste_id)) FROM mart.fact_waste f",
}


def test_running_the_whole_pipeline_twice_changes_nothing(warehouse, tmp_path, scalar):
    """The bug this pins down: the receipt-item load once did DELETE+INSERT, which
    violated inventory_batches.grn_item_id on the SECOND run and rolled back."""
    ctx = make_ctx(warehouse, tmp_path)
    assert_all_ok(run_all(ctx))
    before = {name: scalar(sql) for name, sql in SNAPSHOT_QUERIES.items()}

    second = run_all(ctx)
    assert_all_ok(second)

    after = {name: scalar(sql) for name, sql in SNAPSHOT_QUERIES.items()}
    assert after == before, {n: (before[n], after[n]) for n in before if before[n] != after[n]}


def test_a_second_run_does_not_duplicate_forecast_or_snapshot_rows(warehouse, tmp_path, scalar):
    ctx = make_ctx(warehouse, tmp_path)
    run_all(ctx)
    counts = {t: scalar(f"SELECT COUNT(*) FROM {t}") for t in (
        "ml.forecast_runs", "ml.demand_forecast", "ml.replenishment_recommendation",
        "mart.fact_stock_position")}
    run_all(ctx)
    assert {t: scalar(f"SELECT COUNT(*) FROM {t}") for t in counts} == counts


def test_receipt_item_ids_are_stable_across_runs(warehouse, tmp_path, scalar):
    ctx = make_ctx(warehouse, tmp_path)
    run_all(ctx)
    ids = scalar("SELECT array_agg(grn_item_id ORDER BY grn_item_id) FROM goods_receipt_items")
    run_all(ctx)
    assert scalar("SELECT array_agg(grn_item_id ORDER BY grn_item_id) FROM goods_receipt_items") == ids


def test_part_of_the_graph_can_be_rerun_alone(full_run, warehouse, tmp_path, scalar):
    ctx, first = full_run
    assert_all_ok(first)
    before = scalar("SELECT COUNT(*) FROM mart.fact_sales_daily")
    partial = run_all(ctx, from_task="models", skip=["dq_report"])
    assert set(partial) == {"models", "forecast"}
    assert_all_ok(partial)
    assert scalar("SELECT COUNT(*) FROM mart.fact_sales_daily") == before


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_a_failed_etl_skips_models_forecast_and_report(warehouse, tmp_path):
    ctx = make_ctx(warehouse, tmp_path)         # raw_dir is empty and generate is skipped
    outcomes = run_all(ctx, skip=["generate"])

    assert outcomes["bootstrap"].status == "SUCCESS"
    assert outcomes["etl"].status == "FAILED"
    for name in ("models", "forecast", "dq_report"):
        assert outcomes[name].status == "SKIPPED", name
        assert "etl" in outcomes[name].error


def test_a_failed_etl_leaves_the_warehouse_untouched(warehouse, tmp_path, scalar):
    before = scalar("SELECT COUNT(*) FROM daily_sales")
    run_all(make_ctx(warehouse, tmp_path), skip=["generate"])
    assert scalar("SELECT COUNT(*) FROM daily_sales") == before
    # models was skipped, so the star schema was never even created: absent, not empty.
    assert scalar("SELECT to_regclass('mart.fact_sales_daily') IS NULL") is True


def test_a_corrupt_dataset_is_quarantined_not_loaded(warehouse, tmp_path, scalar):
    ctx = make_ctx(warehouse, tmp_path)
    assert_all_ok(run_all(ctx))
    # No row with an expiry before its manufacturing date may exist in the database.
    assert scalar("SELECT COUNT(*) FROM inventory_batches "
                  "WHERE expiration_date < manufacturing_date") == 0
    assert list((tmp_path / "quarantine").glob("batches_rejected_*.csv"))


# ---------------------------------------------------------------------------
# The run record
# ---------------------------------------------------------------------------

def test_a_run_can_be_recorded_in_the_ops_schema(warehouse, tmp_path, scalar):
    from datetime import datetime, timezone
    ctx = make_ctx(warehouse, tmp_path)
    started = datetime.now(timezone.utc)
    pipeline = pl.build_pipeline()
    outcomes = pipeline.run(ctx, only=["bootstrap"], sleep=lambda s: None)
    finished = datetime.now(timezone.utc)
    summary = pl.run_summary(ctx, outcomes, started, finished, {"only": ["bootstrap"]})

    pl.record_to_database(warehouse, summary, outcomes, started, finished)

    assert scalar("SELECT COUNT(*) FROM ops.pipeline_runs") == 1
    assert scalar("SELECT status FROM ops.pipeline_runs") == "SUCCESS"
    assert scalar("SELECT task_name FROM ops.task_runs") == "bootstrap"
    assert scalar("SELECT COUNT(*) FROM ops.v_run_history") == 1


def test_the_history_file_gains_one_line_per_run(warehouse, tmp_path):
    ctx = make_ctx(warehouse, tmp_path)
    run_all(ctx)
    run_all(ctx)
    entries = dq.read_history(ctx.quality_history)
    assert len(entries) == 2
    assert entries[0]["recorded_at"] <= entries[1]["recorded_at"]
    assert all(not e["dry_run"] and e["status"] == "SUCCESS" for e in entries)
