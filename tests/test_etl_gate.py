# -*- coding: utf-8 -*-
"""
Tests for the ETL quality gate (rules, quarantine, scoring) and its CLI.

The gate is the part of the pipeline that decides what the warehouse is allowed
to believe, so each rule is exercised with a row that SHOULD fail and a row that
should NOT: a rule that never fires and a rule that always fires both look like
"passing" in a happy-path test.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import data_generator as gen
import dq
import etl_pipeline as etl

D = date(2026, 9, 1)


def gate():
    report = etl.ETLReport()
    return etl.QualityGate(report), report


def master_frames():
    """Parent tables every child dataset's foreign keys resolve against."""
    return {
        "stores.csv": pd.DataFrame({
            "store_code": ["TB-101"], "store_name": ["Miraflores"], "store_format": ["FLAGSHIP"],
            "district": ["Miraflores"], "city": ["Lima"], "square_meters": [110.0]}),
        "product_categories.csv": pd.DataFrame({
            "category_code": ["CAT-1"], "category_name": ["Dairy"], "storage_temp_type": ["REFRIGERATED"]}),
        "suppliers.csv": pd.DataFrame({
            "supplier_code": ["SUP-1"], "tax_id_ruc": ["20100017491"], "company_name": ["Gloria"],
            "payment_terms_days": [30], "lead_time_days": [1]}),
        "products.csv": pd.DataFrame({
            "sku_code": ["SKU-1"], "product_name": ["Yogurt"], "category_code": ["CAT-1"],
            "unit_cost": [4.2], "retail_price": [6.9], "shelf_life_days": [21],
            "min_safety_stock": [10], "reorder_point": [25]}),
        "waste_reasons.csv": pd.DataFrame({
            "reason_code": ["WR-EXP"], "reason_description": ["Expired"],
            "loss_classification": ["EXPIRATION"]}),
    }


def receipts(*rows):
    cols = ["grn_number", "store_code", "supplier_code", "dispatch_guide_ref",
            "received_timestamp", "received_by_user", "status", "sku_code",
            "qty_dispatched", "qty_accepted", "unit_purchase_cost", "temperature_celsius", "notes"]
    frame = pd.DataFrame(rows, columns=cols)
    frame["received_timestamp"] = pd.Timestamp("2026-09-01 07:00")
    return frame


def receipt(grn, sku="SKU-1", dispatched=50.0, accepted=50.0, notes=np.nan, temp=4.5):
    return (grn, "TB-101", "SUP-1", "GR-1", None, "Ana", "AUDITED", sku,
            dispatched, accepted, 4.2, temp, notes)


def batch_frame(*rows):
    cols = ["batch_code", "store_code", "sku_code", "grn_number", "initial_qty", "current_qty",
            "manufacturing_date", "expiration_date", "batch_status"]
    return pd.DataFrame(rows, columns=cols)


def waste_frame(*rows):
    cols = ["waste_code", "store_code", "sku_code", "batch_code", "reason_code", "qty_discarded",
            "unit_cost_at_loss", "logged_timestamp", "logged_by_user", "authorized_by", "notes"]
    frame = pd.DataFrame(rows, columns=cols)
    frame["logged_timestamp"] = pd.Timestamp("2026-09-02 21:00")
    return frame


def result(report, rule, dataset):
    found = [r for r in dq.merge_results(report.rule_results)
             if r.rule_id == rule and r.dataset == dataset]
    assert len(found) == 1, f"{rule}/{dataset}: expected 1 result, got {len(found)}"
    return found[0]


# ---------------------------------------------------------------------------
# Every evaluation is recorded, including clean ones
# ---------------------------------------------------------------------------

def test_clean_data_records_every_rule_as_evaluated_with_zero_failures():
    g, report = gate()
    frames = {**master_frames(),
              "goods_receipts.csv": receipts(receipt("G1"), receipt("G2")),
              "batches.csv": batch_frame(("L1", "TB-101", "SKU-1", "G1", 50.0, 20.0, D, date(2026, 9, 20), "ACTIVE")),
              "waste_log.csv": waste_frame(("W1", "TB-101", "SKU-1", "L1", "WR-EXP", 2.0, 4.2, None, "Ana", "Boss", ""))}
    g.run(frames)

    assert not g.quarantine, "clean data must quarantine nothing"
    for rule, dataset in [("R01", "goods_receipts.csv"), ("R02", "goods_receipts.csv"),
                          ("B01", "batches.csv"), ("W03", "waste_log.csv")]:
        outcome = result(report, rule, dataset)
        assert outcome.evaluated > 0 and outcome.failed == 0, (rule, dataset)
    assert all(r.failed == 0 for r in report.rule_results if r.severity == "ERROR")


def test_a_rule_that_finds_nothing_still_appears_in_the_results():
    """The bug this guards: 'no findings' used to mean 'rule did not run'."""
    g, report = gate()
    g.run({**master_frames(), "goods_receipts.csv": receipts(receipt("G1"))})
    assert any(r.rule_id == "R02" and r.failed == 0 for r in report.rule_results)
    assert not [f for f in report.findings if f.rule_id == "R02"], \
        "a clean rule must not produce a log-worthy finding"


def test_every_warning_rule_is_recorded_too():
    g, report = gate()
    g.run({**master_frames(), "goods_receipts.csv": receipts(receipt("G1")),
           "batches.csv": batch_frame(("L1", "TB-101", "SKU-1", "G1", 50.0, 20.0, D, date(2026, 9, 20), "ACTIVE"))})
    recorded = {r.rule_id for r in report.rule_results}
    assert {"R04", "R05", "R06", "B05", "B06"} <= recorded


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------

def test_negative_received_quantity_is_quarantined_with_its_key():
    g, report = gate()
    frames = {**master_frames(), "goods_receipts.csv": receipts(receipt("G1"), receipt("G2", accepted=-5.0))}
    clean = g.run(frames)
    assert result(report, "R02", "goods_receipts.csv").failed == 1
    assert g.failed_keys[("R02", "goods_receipts.csv")] == {("G2", "SKU-1")}
    assert list(clean["goods_receipts.csv"]["grn_number"]) == ["G1"]
    assert (g.quarantine["goods_receipts.csv"]["_reject_rule"] == "R02").all()


def test_expiry_before_manufacturing_is_quarantined():
    g, report = gate()
    frames = {**master_frames(),
              "batches.csv": batch_frame(("OK", "TB-101", "SKU-1", "", 10.0, 5.0, D, date(2026, 9, 20), "ACTIVE"),
                                         ("BAD", "TB-101", "SKU-1", "", 10.0, 5.0, D, date(2026, 8, 20), "ACTIVE"))}
    clean = g.run(frames)
    assert g.failed_keys[("B01", "batches.csv")] == {("BAD",)}
    assert list(clean["batches.csv"]["batch_code"]) == ["OK"]


def test_undocumented_shortage_is_flagged_but_a_documented_one_is_not():
    g, report = gate()
    frames = {**master_frames(), "goods_receipts.csv": receipts(
        receipt("G1", dispatched=50, accepted=45, notes="5 unidades rechazadas por daño"),
        receipt("G2", dispatched=50, accepted=45, notes=np.nan),
        receipt("G3", dispatched=50, accepted=50))}
    g.run(frames)
    assert g.failed_keys[("R04", "goods_receipts.csv")] == {("G1", "SKU-1"), ("G2", "SKU-1")}, \
        "R04 flags every shortage"
    assert g.failed_keys[("R06", "goods_receipts.csv")] == {("G2", "SKU-1")}, \
        "R06 flags only the one nobody explained"


def test_over_receipt_is_rejected():
    g, report = gate()
    g.run({**master_frames(), "goods_receipts.csv": receipts(receipt("G1", dispatched=10, accepted=12))})
    assert result(report, "R03", "goods_receipts.csv").failed == 1


def test_material_waste_without_an_authorizer_is_rejected_but_small_waste_is_not():
    g, report = gate()
    big = ("W1", "TB-101", "SKU-1", "", "WR-EXP", 20.0, 4.2, None, "Ana", np.nan, "")     # 84.00
    small = ("W2", "TB-101", "SKU-1", "", "WR-EXP", 5.0, 4.2, None, "Ana", np.nan, "")     # 21.00
    clean = g.run({**master_frames(), "waste_log.csv": waste_frame(big, small)})
    assert g.failed_keys[("W03", "waste_log.csv")] == {("W1",)}
    assert list(clean["waste_log.csv"]["waste_code"]) == ["W2"]


def test_the_waste_threshold_is_strictly_greater_than():
    g, report = gate()
    exactly = ("W1", "TB-101", "SKU-1", "", "WR-EXP", 10.0, 5.0, None, "Ana", np.nan, "")   # exactly 50.00
    g.run({**master_frames(), "waste_log.csv": waste_frame(exactly)})
    assert result(report, "W03", "waste_log.csv").failed == 0


def test_self_authorized_material_waste_is_rejected():
    g, report = gate()
    row = ("W1", "TB-101", "SKU-1", "", "WR-EXP", 20.0, 4.2, None, "Ana", " ANA ", "")
    g.run({**master_frames(), "waste_log.csv": waste_frame(row)})
    assert result(report, "W04", "waste_log.csv").failed == 1


def test_duplicate_natural_key_keeps_the_first_occurrence():
    g, report = gate()
    clean = g.run({**master_frames(), "goods_receipts.csv": receipts(receipt("G1"), receipt("G1", dispatched=99))})
    assert result(report, "G02", "goods_receipts.csv").failed == 1
    assert len(clean["goods_receipts.csv"]) == 1
    assert clean["goods_receipts.csv"]["qty_dispatched"].iloc[0] == 50.0


def test_null_in_a_mandatory_column_is_rejected():
    g, report = gate()
    frame = receipts(receipt("G1"), receipt("G2"))
    frame.loc[1, "qty_dispatched"] = np.nan
    clean = g.run({**master_frames(), "goods_receipts.csv": frame})
    assert result(report, "G01", "goods_receipts.csv").failed == 1
    assert list(clean["goods_receipts.csv"]["grn_number"]) == ["G1"]


def test_orphan_foreign_key_is_rejected():
    g, report = gate()
    frame = receipts(receipt("G1"))
    frame.loc[0, "store_code"] = "TB-999"
    clean = g.run({**master_frames(), "goods_receipts.csv": frame})
    assert result(report, "F01", "goods_receipts.csv").failed >= 1
    assert clean["goods_receipts.csv"].empty


def test_later_rules_only_evaluate_rows_that_survived_earlier_ones():
    """A row rejected by R01 must not be counted again by R02 and R03."""
    g, report = gate()
    g.run({**master_frames(), "goods_receipts.csv": receipts(
        receipt("G1"), receipt("G2", dispatched=-4.0), receipt("G3"))})
    assert result(report, "R01", "goods_receipts.csv").evaluated == 3
    assert result(report, "R02", "goods_receipts.csv").evaluated == 2


def test_fatal_severity_aborts_the_run():
    g, _ = gate()
    mask = pd.Series([True])
    with pytest.raises(etl.FatalDataQualityError):
        g._apply("stores.csv", master_frames()["stores.csv"], mask, "G01",
                 etl.Severity.FATAL, "boom", ("store_code",))


# ---------------------------------------------------------------------------
# Missing inputs
# ---------------------------------------------------------------------------

def test_a_missing_required_column_is_a_fatal_contract_violation(tmp_path):
    (tmp_path / "stores.csv").write_text("store_code,store_name\nTB-1,X\n", encoding="utf-8")
    with pytest.raises(etl.FatalDataQualityError, match="missing"):
        etl.Extractor(tmp_path, etl.ETLReport()).run()


def test_a_missing_raw_directory_is_fatal(tmp_path):
    with pytest.raises(etl.FatalDataQualityError, match="not found"):
        etl.Extractor(tmp_path / "nope", etl.ETLReport()).run()


# ---------------------------------------------------------------------------
# End to end on generated data (no database: --dry-run)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("raw")
    gen.main(["--out", str(out), "--end-date", "2026-11-14", "--days", "60"])
    return out


def run_etl(raw_dir, tmp_path, *extra):
    args = ["--dry-run", "--raw-dir", str(raw_dir),
            "--log-file", str(tmp_path / "etl.log"),
            "--quarantine-dir", str(tmp_path / "quarantine"),
            "--quality-runs-dir", str(tmp_path / "qruns"),
            "--quality-history", str(tmp_path / "history.jsonl"), *extra]
    return etl.main(args), tmp_path / "history.jsonl"


def test_the_gate_catches_every_defect_the_generator_planted(raw_dir, tmp_path):
    code, history = run_etl(raw_dir, tmp_path)
    assert code == 0
    [entry] = dq.read_history(history)
    truth = entry["ground_truth"]
    assert truth, "no ground truth was scored: is the manifest being read?"
    for kind, counts in truth.items():
        assert counts["detected"] == counts["injected"], \
            f"{kind}: caught {counts['detected']} of {counts['injected']}"
    assert sum(c["injected"] for c in truth.values()) == 12


def test_the_dry_run_quarantines_rows_and_writes_them_to_disk(raw_dir, tmp_path):
    run_etl(raw_dir, tmp_path)
    rejected = list((tmp_path / "quarantine").glob("*_rejected_*.csv"))
    assert {p.name.split("_rejected_")[0] for p in rejected} == {"goods_receipts", "batches", "waste_log"}
    frame = pd.read_csv(next(p for p in rejected if p.name.startswith("batches")))
    assert (frame["_reject_rule"] == "B01").all()


def test_the_history_line_has_dimension_scores_and_source(raw_dir, tmp_path):
    _, history = run_etl(raw_dir, tmp_path)
    [entry] = dq.read_history(history)
    assert entry["dry_run"] is True
    assert entry["source"]["seed"] == gen.DEFAULT_SEED
    assert 0.99 < entry["totals"]["acceptance_rate"] < 1.0
    assert set(entry["dimension_pass_rate"]) >= {"completeness", "validity", "consistency"}
    assert all(0.9 < v <= 1.0 for v in entry["dimension_pass_rate"].values())


def test_fail_on_error_turns_quarantined_rows_into_a_nonzero_exit(raw_dir, tmp_path):
    code, _ = run_etl(raw_dir, tmp_path, "--fail-on-error")
    assert code == 1


def test_a_pristine_dataset_passes_the_strict_gate(tmp_path):
    clean = tmp_path / "clean"
    gen.main(["--out", str(clean), "--end-date", "2026-11-14", "--days", "30",
              "--no-inject-anomalies"])
    code, history = run_etl(clean, tmp_path, "--fail-on-error")
    [entry] = dq.read_history(history)
    assert entry["totals"]["rows_quarantined"] == 0
    assert code == 0


def test_a_failed_run_is_recorded_as_failed_with_a_reason(tmp_path):
    code, history = run_etl(tmp_path / "does-not-exist", tmp_path)
    assert code == 1
    [entry] = dq.read_history(history)
    assert entry["status"] == "FAILED"
    record = json.loads(next((tmp_path / "qruns").glob("*.json")).read_text(encoding="utf-8"))
    assert "not found" in record["aborted_reason"]


def test_a_failed_audit_write_never_changes_the_exit_code(raw_dir, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(dq, "write_run_record", boom)
    code, _ = run_etl(raw_dir, tmp_path)
    assert code == 0, "losing the audit row must not fail a load that already succeeded"
