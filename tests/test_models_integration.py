# -*- coding: utf-8 -*-
"""
Integration tests for the SQL model layer, against a real PostgreSQL.

These exist because "the models ran without errors" proves nothing about the
properties that justify building them this way:

* an incremental load must pick up NEW and LATE data, and must be cheaper than
  a full rebuild;
* an SCD2 dimension must keep history and price each fact with the version that
  was valid when the fact happened;
* every build must be idempotent;
* the reconciliation tests must actually FAIL when the warehouse is wrong, or
  they are decoration.

Each test gets its own throwaway database (see conftest.py).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from model_runner import ModelRunner, main as runner_main

pytestmark = pytest.mark.integration


def build(warehouse, **kwargs):
    runner = ModelRunner(warehouse)
    results = runner.build(**kwargs)
    failed = [r for r in results if r.status == "FAILED"]
    assert not failed, f"model build failed: {failed[0].name}: {failed[0].error}"
    return runner, {r.name: r for r in results}


def seed_sales(execute, days: int = 40) -> None:
    """Add a longer run of POS history so incremental behaviour is observable."""
    execute(
        "INSERT INTO daily_sales (store_id, product_id, sale_date, units_sold, "
        "                         gross_revenue, cost_of_goods_sold) "
        "SELECT 1, 1, CURRENT_DATE - g, 10, 69.00, 42.00 "
        "  FROM generate_series(4, :n) AS g "
        "ON CONFLICT (store_id, product_id, sale_date) DO NOTHING",
        n=days + 3)


# ---------------------------------------------------------------------------
# Build and idempotence
# ---------------------------------------------------------------------------

def test_every_model_test_passes_on_the_seed_data(warehouse):
    runner, _ = build(warehouse)
    results = runner.test()
    assert results, "no model tests were discovered"
    failing = [(t.name, t.sample) for t in results if t.status == "FAIL"]
    assert not failing, f"model tests failed on clean data: {failing}"


def test_build_is_idempotent(warehouse, scalar):
    """Two builds leave the facts byte-identical: no duplicates, no drift."""
    checksum = (
        "SELECT md5(string_agg(f::TEXT, '|' ORDER BY f.date_key, f.store_key, "
        "f.product_key)) FROM mart.fact_sales_daily f")
    build(warehouse)
    first = scalar(checksum)
    first_rows = scalar("SELECT COUNT(*) FROM mart.fact_sales_daily")
    build(warehouse)
    assert scalar("SELECT COUNT(*) FROM mart.fact_sales_daily") == first_rows
    assert scalar(checksum) == first


# ---------------------------------------------------------------------------
# Incremental loading
# ---------------------------------------------------------------------------

def test_incremental_picks_up_new_and_late_rows(warehouse, execute, scalar):
    seed_sales(execute)
    build(warehouse)

    # A brand-new day, and a correction to a row inside the lookback window.
    execute("INSERT INTO daily_sales (store_id, product_id, sale_date, units_sold, "
            "gross_revenue, cost_of_goods_sold) VALUES (2, 4, CURRENT_DATE, 7, 34.30, 16.10)")
    execute("UPDATE daily_sales SET units_sold = 99 "
            " WHERE store_id = 1 AND product_id = 1 AND sale_date = CURRENT_DATE - 1")

    build(warehouse)  # default lookback window

    assert scalar("SELECT COUNT(*) FROM mart.fact_sales_daily f "
                  "JOIN mart.dim_date d USING (date_key) "
                  "WHERE d.full_date = CURRENT_DATE AND f.store_key = 2") == 1, \
        "a new day was not loaded"
    assert scalar("SELECT f.units_sold FROM mart.fact_sales_daily f "
                  "JOIN mart.dim_date d USING (date_key) "
                  "WHERE d.full_date = CURRENT_DATE - 1 AND f.store_key = 1 "
                  "  AND f.product_key = (SELECT product_key FROM mart.dim_product "
                  "                        WHERE product_id = 1 AND is_current)") == 99, \
        "a correction inside the lookback window was not applied"


def test_incremental_reprocesses_far_fewer_rows_than_a_full_rebuild(warehouse, execute):
    seed_sales(execute, days=60)
    _, first = build(warehouse)
    total = first["fact_sales_daily"].target_rows
    assert total > 40

    _, second = build(warehouse, lookback_days=3)
    reprocessed = second["fact_sales_daily"].rows_affected
    assert 0 < reprocessed < total / 4, (
        f"incremental touched {reprocessed} of {total} rows: it is not incremental")

    _, refreshed = build(warehouse, full_refresh=True)
    assert refreshed["fact_sales_daily"].rows_affected == total


def test_a_change_older_than_the_lookback_is_missed_until_a_full_refresh(
        warehouse, execute, scalar):
    """The documented trade-off of an incremental window, pinned down.

    Late data older than the lookback is NOT picked up by a normal run. That is
    the price of not rescanning history, and it is why --full-refresh exists.
    If this ever stops being true, the lookback logic changed and the docs lie.
    """
    seed_sales(execute, days=40)
    build(warehouse)

    execute("UPDATE daily_sales SET units_sold = 555 "
            " WHERE store_id = 1 AND product_id = 1 AND sale_date = CURRENT_DATE - 30")
    build(warehouse, lookback_days=3)
    stale = scalar("SELECT f.units_sold FROM mart.fact_sales_daily f "
                   "JOIN mart.dim_date d USING (date_key) "
                   "WHERE d.full_date = CURRENT_DATE - 30 AND f.store_key = 1")
    assert stale != 555, "an out-of-window change was unexpectedly reprocessed"

    build(warehouse, full_refresh=True)
    fixed = scalar("SELECT f.units_sold FROM mart.fact_sales_daily f "
                   "JOIN mart.dim_date d USING (date_key) "
                   "WHERE d.full_date = CURRENT_DATE - 30 AND f.store_key = 1")
    assert fixed == 555


# ---------------------------------------------------------------------------
# SCD type 2
# ---------------------------------------------------------------------------

def test_scd2_keeps_history_and_prices_each_fact_with_the_version_of_its_day(
        warehouse, execute, scalar):
    seed_sales(execute, days=10)
    build(warehouse, as_of=date.today() - timedelta(days=20))
    assert scalar("SELECT COUNT(*) FROM mart.dim_product WHERE product_id = 1") == 1

    # Reprice SKU 1 effective 5 days ago.
    change_day = date.today() - timedelta(days=5)
    execute("UPDATE products SET unit_cost = 4.9000, retail_price = 8.50 "
            " WHERE product_id = 1")
    build(warehouse, as_of=change_day, full_refresh=True)

    versions = scalar("SELECT COUNT(*) FROM mart.dim_product WHERE product_id = 1")
    assert versions == 2, "a price change must open a second version"
    assert scalar("SELECT COUNT(*) FROM mart.dim_product "
                  "WHERE product_id = 1 AND is_current") == 1

    # A sale BEFORE the change resolves to the old price; one AFTER to the new.
    price_on = ("SELECT p.retail_price FROM mart.fact_sales_daily f "
                "JOIN mart.dim_product p USING (product_key) "
                "JOIN mart.dim_date d USING (date_key) "
                "WHERE p.product_id = 1 AND f.store_key = 1 "
                "  AND d.full_date = CURRENT_DATE - :ago")
    assert float(scalar(price_on, ago=8)) == pytest.approx(6.90), \
        "history was rewritten: an old sale picked up the new price"
    assert float(scalar(price_on, ago=2)) == pytest.approx(8.50), \
        "a recent sale did not pick up the new price"


def test_scd2_unchanged_source_opens_no_new_version(warehouse, scalar):
    build(warehouse)
    build(warehouse)
    build(warehouse, as_of=date.today() + timedelta(days=30))
    assert scalar("SELECT COUNT(*) FROM mart.dim_product") == scalar(
        "SELECT COUNT(*) FROM products"), \
        "re-running with unchanged source created extra versions"


def test_scd2_second_change_on_the_same_day_overwrites_instead_of_versioning(
        warehouse, execute, scalar):
    build(warehouse)
    day = date.today() + timedelta(days=1)

    execute("UPDATE products SET retail_price = 7.00 WHERE product_id = 2")
    build(warehouse, as_of=day)
    execute("UPDATE products SET retail_price = 7.25 WHERE product_id = 2")
    build(warehouse, as_of=day)          # a typo fixed on the same day

    assert scalar("SELECT COUNT(*) FROM mart.dim_product WHERE product_id = 2") == 2, \
        "a same-day correction must not open a third, zero-length version"
    assert float(scalar("SELECT retail_price FROM mart.dim_product "
                        "WHERE product_id = 2 AND is_current")) == pytest.approx(7.25)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def test_snapshot_is_idempotent_per_day_and_accumulates_across_days(warehouse, scalar):
    day1 = date.today()
    day2 = day1 + timedelta(days=1)

    build(warehouse, snapshot_date=day1)
    one = scalar("SELECT COUNT(*) FROM mart.fact_stock_position")
    assert one > 0

    build(warehouse, snapshot_date=day1)
    assert scalar("SELECT COUNT(*) FROM mart.fact_stock_position") == one, \
        "re-running the same day duplicated the snapshot"

    build(warehouse, snapshot_date=day2)
    assert scalar("SELECT COUNT(*) FROM mart.fact_stock_position") > one, \
        "a new day must add a new slice of history"


def test_full_refresh_never_truncates_the_snapshot_history(warehouse, scalar):
    build(warehouse, snapshot_date=date.today())
    build(warehouse, snapshot_date=date.today() + timedelta(days=1))
    before = scalar("SELECT COUNT(*) FROM mart.fact_stock_position")

    build(warehouse, full_refresh=True)
    assert scalar("SELECT COUNT(*) FROM mart.fact_stock_position") >= before, \
        "--full-refresh destroyed snapshot history that cannot be rebuilt"


# ---------------------------------------------------------------------------
# The tests themselves must be able to fail
# ---------------------------------------------------------------------------

def test_reconciliation_catches_a_row_missing_from_the_warehouse(warehouse, execute):
    runner, _ = build(warehouse)
    execute("DELETE FROM mart.fact_sales_daily WHERE ctid IN "
            "(SELECT ctid FROM mart.fact_sales_daily LIMIT 1)")

    outcome = {t.name: t for t in runner.test()}
    assert outcome["fact_sales_row_count_matches_source"].status == "FAIL"
    assert outcome["fact_sales_reconciles_units"].status == "FAIL"
    assert outcome["fact_sales_reconciles_units"].severity == "error"


def test_reconciliation_catches_a_corrupted_amount(warehouse, execute):
    runner, _ = build(warehouse)
    execute("UPDATE mart.fact_waste SET total_loss_amount = total_loss_amount + 1 "
            " WHERE waste_id = (SELECT MIN(waste_id) FROM mart.fact_waste)")
    outcome = {t.name: t for t in runner.test()}
    assert outcome["fact_waste_reconciles_loss"].status == "FAIL"


def test_a_failing_error_test_makes_the_runner_exit_nonzero(warehouse, execute, monkeypatch):
    build(warehouse)
    execute("DELETE FROM mart.fact_waste")

    import dbutil
    monkeypatch.setattr(dbutil, "get_engine", lambda dsn=None: warehouse)
    assert runner_main(["--tests-only"]) == 1


def test_a_broken_model_stops_the_build_and_is_reported(warehouse, tmp_path):
    from shutil import copytree
    models = tmp_path / "models"
    copytree(__import__("model_runner").DEFAULT_MODELS_DIR, models)
    (models / "11_dim_store.sql").write_text(
        "-- model: dim_store\n-- kind: table\n-- target: mart.dim_store\n"
        "-- depends_on:\nSELECT * FROM table_that_does_not_exist;\n", encoding="utf-8")

    results = ModelRunner(warehouse, models).build()
    failed = [r for r in results if r.status == "FAILED"]
    assert failed and failed[0].name == "dim_store"
    assert "table_that_does_not_exist" in failed[0].error
    assert results[-1].status == "FAILED", "the build must stop at the first failure"
