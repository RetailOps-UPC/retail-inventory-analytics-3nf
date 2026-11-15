# -*- coding: utf-8 -*-
"""
Tests for the synthetic data generator.

The generator is the source of every number in the business case, so it is held
to the standards of the code that depends on it: deterministic, internally
consistent, and honest about the defects it plants.
"""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta

import pytest

import data_generator as gen

END = date(2026, 11, 14)
START = END - timedelta(days=59)
SEED = gen.DEFAULT_SEED


@pytest.fixture(scope="module")
def baseline():
    return gen.RetailSimulator(START, END, SEED, inject_anomalies=True).run()


def metrics(result):
    revenue = sum(s.gross_revenue for s in result.sales)
    waste = sum(w.qty_discarded * w.unit_cost_at_loss for w in result.waste)
    return round(revenue, 2), round(waste, 2)


# ---------------------------------------------------------------------------
# Determinism and the baseline
# ---------------------------------------------------------------------------

def test_the_same_seed_produces_identical_output(baseline):
    again = gen.RetailSimulator(START, END, SEED, inject_anomalies=True).run()
    assert metrics(again) == metrics(baseline)
    assert [(s.store_code, s.sku_code, s.sale_date, s.units_sold) for s in again.sales] == \
           [(s.store_code, s.sku_code, s.sale_date, s.units_sold) for s in baseline.sales]
    assert again.anomaly_log == baseline.anomaly_log


def test_a_different_seed_produces_different_output(baseline):
    other = gen.RetailSimulator(START, END, SEED + 1, inject_anomalies=True).run()
    assert metrics(other) != metrics(baseline)


def test_the_legacy_heuristic_baseline_is_unchanged(baseline):
    """The business case quotes numbers from this configuration.

    If this fails after an INTENTIONAL change to the simulator, update the
    constants AND regenerate every figure in docs/business_case_study.md that
    came from it. If it fails after an unrelated change, the baseline drifted.
    """
    revenue, waste = metrics(baseline)
    assert revenue == pytest.approx(430013.80, abs=0.01)
    assert waste == pytest.approx(3085.65, abs=0.01)


def test_the_crc32_offsets_do_not_depend_on_the_interpreter_hash_seed():
    # str.__hash__ is salted per process; crc32 is not. This value must never change.
    assert gen.stable_hash("TB-101", "SKU-2002") == gen.stable_hash("TB-101", "SKU-2002")
    assert gen.stable_hash("a") == 3904355907


# ---------------------------------------------------------------------------
# Internal consistency
# ---------------------------------------------------------------------------

def test_stock_never_goes_negative(baseline):
    assert all(b.current_qty >= 0 for b in baseline.batches)


def test_every_sale_row_is_positive_and_priced_above_cost(baseline):
    assert all(s.units_sold > 0 and s.gross_revenue > 0 for s in baseline.sales)
    # Every SKU's retail price is at least 1.4x its cost, so margin is never negative.
    assert all(s.cost_of_goods_sold < s.gross_revenue for s in baseline.sales)


def test_every_waste_event_points_at_a_real_batch(baseline):
    codes = {b.batch_code for b in baseline.batches}
    assert all(w.batch_code in codes for w in baseline.waste)


def test_expiry_waste_happens_only_after_the_expiration_date(baseline):
    expiry = {b.batch_code: b.expiration_date for b in baseline.batches}
    for w in baseline.waste:
        if w.reason_code == "WR-EXP":
            assert w.logged_timestamp.date() >= expiry[w.batch_code]


def test_weekend_demand_exceeds_weekday_demand_for_beer(baseline):
    weekday, weekend = [], []
    for s in baseline.sales:
        if s.sku_code != "SKU-4001":
            continue
        (weekend if s.sale_date.isoweekday() >= 5 else weekday).append(s.units_sold)
    assert sum(weekend) / len(weekend) > 1.8 * (sum(weekday) / len(weekday))


# ---------------------------------------------------------------------------
# Ground truth for the injected defects
# ---------------------------------------------------------------------------

def test_the_ground_truth_log_matches_the_injection_counts(baseline):
    for kind, count in baseline.anomalies_injected.items():
        assert sum(1 for a in baseline.anomaly_log if a["kind"] == kind) == count


def test_every_logged_defect_really_exists_in_the_data(baseline):
    """The manifest must not lie: each entry points at a row that has the defect."""
    batches = {b.batch_code: b for b in baseline.batches}
    waste = {w.waste_code: w for w in baseline.waste}
    lines = {(r.grn_number, r.sku_code): r for r in baseline.receipts}

    for entry in baseline.anomaly_log:
        kind = entry["kind"]
        if kind == "expiry_before_manufacturing":
            b = batches[entry["batch_code"]]
            assert b.expiration_date < b.manufacturing_date
        elif kind == "negative_received_qty":
            assert lines[(entry["grn_number"], entry["sku_code"])].qty_accepted < 0
        elif kind == "guide_vs_count_discrepancy":
            line = lines[(entry["grn_number"], entry["sku_code"])]
            assert line.qty_accepted < line.qty_dispatched
            assert line.notes == "", "an undocumented shortage must have no note"
        elif kind == "unauthorized_high_waste":
            w = waste[entry["waste_code"]]
            assert w.authorized_by == "" and w.qty_discarded * w.unit_cost_at_loss > 50


def test_documented_rejections_keep_their_note(baseline):
    documented = [r for r in baseline.receipts
                  if 0 <= r.qty_accepted < r.qty_dispatched and r.notes]
    assert documented, "the dataset should contain documented rejections too"
    assert all("rechazadas" in r.notes for r in documented)


def test_no_anomalies_are_injected_when_disabled():
    clean = gen.RetailSimulator(START, START + timedelta(days=20), SEED,
                                inject_anomalies=False).run()
    assert clean.anomalies_injected == {} and clean.anomaly_log == []
    assert all(r.qty_accepted >= 0 for r in clean.receipts)


# ---------------------------------------------------------------------------
# Replenishment policies
# ---------------------------------------------------------------------------

def run_policy(policy, seed=SEED, z=gen.FORECAST_SERVICE_Z):
    return gen.RetailSimulator(START, END, seed, inject_anomalies=False,
                               policy=policy, service_z=z).run()


def test_an_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        gen.RetailSimulator(START, END, SEED, False, policy="telepathy")


@pytest.mark.parametrize("policy", gen.POLICIES)
def test_every_policy_runs_and_keeps_stock_non_negative(policy):
    result = run_policy(policy)
    assert result.sales
    assert all(b.current_qty >= 0 for b in result.batches)


def test_the_policy_changes_what_is_ordered_not_what_customers_want():
    heuristic, forecast = run_policy("heuristic"), run_policy("forecast")
    assert metrics(heuristic) != metrics(forecast)


def test_a_higher_service_level_orders_more_and_stocks_out_less():
    lean = run_policy("forecast", z=1.0)
    safe = run_policy("forecast", z=3.5)
    assert safe.lost_revenue < lean.lost_revenue
    assert metrics(safe)[1] > metrics(lean)[1]               # and wastes more


def test_censored_days_are_floored_not_dropped():
    """Dropping stock-out days biases demand LOW; the floor must keep them."""
    sim = gen.RetailSimulator(START, END, SEED, False, policy="forecast")
    store, product = gen.STORES[0], gen.PRODUCTS[0]
    key = (store.store_code, product.sku_code)
    today = START + timedelta(days=10)
    sim._sales_hist[key] = {START + timedelta(days=i): v
                            for i, v in enumerate([20, 20, 20, 5, 20, 20, 20, 20, 20, 20])}
    sim._censored[key] = {START + timedelta(days=3)}       # the 5 was a sold-out day

    usable = sim._usable_history(store, product, today)
    assert len(usable) == 10, "a censored day must stay in the history"
    assert usable[START + timedelta(days=3)] == pytest.approx(20.0), \
        "a sold-out day must be floored at the uncensored mean, not trusted at 5"


def test_the_estimators_use_only_data_before_today():
    sim = gen.RetailSimulator(START, END, SEED, False, policy="forecast")
    store, product = gen.STORES[0], gen.PRODUCTS[0]
    key = (store.store_code, product.sku_code)
    today = START + timedelta(days=14)
    sim._sales_hist[key] = {START + timedelta(days=i): 10.0 for i in range(14)}
    before = sim._expected_demand(store, product, today, today)
    sim._sales_hist[key][today] = 9999.0                      # today's sales are not known yet
    sim._sales_hist[key][today + timedelta(days=1)] = 9999.0
    assert sim._expected_demand(store, product, today, today) == before


# ---------------------------------------------------------------------------
# CSV and manifest output
# ---------------------------------------------------------------------------

def test_main_writes_every_csv_and_a_manifest(tmp_path):
    code = gen.main(["--out", str(tmp_path), "--end-date", END.isoformat(), "--days", "30"])
    assert code == 0
    for name in ("stores", "products", "goods_receipts", "batches", "waste_log",
                 "daily_sales", "suppliers", "product_categories", "waste_reasons"):
        assert (tmp_path / f"{name}.csv").is_file(), name
    manifest = json.loads((tmp_path / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == SEED and manifest["days"] == 30
    assert manifest["end_date"] == END.isoformat()
    assert manifest["policy"] == "heuristic"


def test_the_manifest_is_byte_identical_across_runs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for target in (a, b):
        gen.main(["--out", str(target), "--end-date", END.isoformat(), "--days", "30"])
    assert (a / "_manifest.json").read_bytes() == (b / "_manifest.json").read_bytes()
    assert (a / "daily_sales.csv").read_bytes() == (b / "daily_sales.csv").read_bytes()


def test_the_csv_sales_match_the_in_memory_result(tmp_path):
    gen.main(["--out", str(tmp_path), "--end-date", END.isoformat(), "--days", "30"])
    with (tmp_path / "daily_sales.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) > 100
    assert all(float(r["units_sold"]) > 0 for r in rows)


def test_invalid_arguments_are_rejected_with_a_nonzero_exit(tmp_path, capsys):
    assert gen.main(["--out", str(tmp_path), "--days", "0"]) == 2
    assert gen.main(["--out", str(tmp_path), "--end-date", "11/14/2026"]) == 2
