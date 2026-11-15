# -*- coding: utf-8 -*-
"""
Tests for the data-quality scorecard (src/dq.py): rule catalog, dimension
roll-ups, ground-truth scoring and the versioned run history.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import dq

ROOT = Path(__file__).resolve().parents[1]


def rr(rule, dataset="d.csv", evaluated=100, failed=0, severity="ERROR"):
    return dq.RuleResult(rule, dataset, severity, evaluated, failed)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_every_rule_the_etl_uses_is_in_the_catalog():
    """A rule that fires but is not catalogued has no dimension and no title."""
    source = (ROOT / "src" / "etl_pipeline.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"([GRBWSF]\d\d)"', source))
    assert used, "found no rule ids in the ETL"
    missing = used - set(dq.RULES)
    assert not missing, f"rules used by the ETL but missing from dq.RULES: {sorted(missing)}"


def test_every_catalogued_rule_is_actually_implemented():
    source = (ROOT / "src" / "etl_pipeline.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"([GRBWSF]\d\d)"', source))
    assert set(dq.RULES) <= used, f"catalogued but never evaluated: {sorted(set(dq.RULES) - used)}"


def test_every_rule_belongs_to_a_known_dimension():
    assert all(spec.dimension in dq.DIMENSIONS for spec in dq.RULES.values())


def test_an_unknown_rule_falls_back_to_validity_instead_of_crashing():
    assert dq.dimension_of("ZZ99") == "validity"


# ---------------------------------------------------------------------------
# Rule results and dimension scores
# ---------------------------------------------------------------------------

def test_pass_rate_is_the_share_of_evaluated_rows_that_passed():
    assert rr("R02", evaluated=200, failed=2).pass_rate == pytest.approx(0.99)
    assert rr("R02", evaluated=200, failed=0).pass_rate == 1.0


def test_pass_rate_is_none_when_nothing_was_evaluated():
    assert rr("R02", evaluated=0).pass_rate is None


def test_dimension_scores_sum_row_checks_across_rules():
    scores = dq.dimension_scores([
        rr("R01", evaluated=100, failed=0),        # validity
        rr("R02", evaluated=100, failed=4),        # validity
        rr("R03", evaluated=100, failed=1),        # consistency
    ])
    assert scores["validity"] == {"evaluated": 200, "failed": 4, "pass_rate": pytest.approx(0.98)}
    assert scores["consistency"]["pass_rate"] == pytest.approx(0.99)
    assert "uniqueness" not in scores, "a dimension with no rules must not appear"


def test_merge_results_sums_checks_that_share_a_key():
    merged = dq.merge_results([rr("F01", "g.csv", 10, 1), rr("F01", "g.csv", 10, 0),
                               rr("F01", "g.csv", 10, 2), rr("R02", "g.csv", 10, 0)])
    f01 = next(r for r in merged if r.rule_id == "F01")
    assert (f01.evaluated, f01.failed) == (30, 3)
    assert len(merged) == 2


def test_merge_results_keeps_the_most_severe_severity():
    merged = dq.merge_results([rr("F01", severity="WARNING"), rr("F01", severity="ERROR"),
                               rr("F01", severity="WARNING")])
    assert merged[0].severity == "ERROR"


def test_merge_results_does_not_mutate_its_input():
    original = [rr("F01", evaluated=10, failed=1), rr("F01", evaluated=10, failed=1)]
    dq.merge_results(original)
    assert [(r.evaluated, r.failed) for r in original] == [(10, 1), (10, 1)]


def test_a_run_record_never_has_two_results_with_the_same_key():
    """The audit tables are keyed on (rule, dataset); duplicates would be dropped."""
    record = sample_record(results=[rr("F01", "g.csv", 10, 1), rr("F01", "g.csv", 10, 0)])
    keys = [(r["rule_id"], r["dataset"]) for r in record["rules"]]
    assert len(keys) == len(set(keys))
    assert record["rules"][0]["evaluated"] == 20


# ---------------------------------------------------------------------------
# Ground-truth scoring
# ---------------------------------------------------------------------------

def manifest(*anomalies):
    return {"anomalies": list(anomalies)}


def test_all_defects_caught_gives_full_recall():
    m = manifest({"kind": "expiry_before_manufacturing", "dataset": "batches", "batch_code": "L1"},
                 {"kind": "expiry_before_manufacturing", "dataset": "batches", "batch_code": "L2"})
    failed = {("B01", "batches.csv"): {("L1",), ("L2",)}}
    [result] = dq.score_ground_truth(m, failed)
    assert (result.injected, result.detected, result.unexpected) == (2, 2, 0)
    assert result.recall == 1.0 and result.precision == 1.0 and result.missed == []


def test_a_missed_defect_is_named():
    m = manifest({"kind": "unauthorized_high_waste", "waste_code": "W1"},
                 {"kind": "unauthorized_high_waste", "waste_code": "W2"})
    [result] = dq.score_ground_truth(m, {("W03", "waste_log.csv"): {("W1",)}})
    assert result.recall == 0.5
    assert result.missed == ["W2"]


def test_detection_requires_the_same_row_not_just_the_same_count():
    """Catching two OTHER rows must not count as catching the two injected ones."""
    m = manifest({"kind": "negative_received_qty", "grn_number": "G1", "sku_code": "S1"},
                 {"kind": "negative_received_qty", "grn_number": "G2", "sku_code": "S2"})
    failed = {("R02", "goods_receipts.csv"): {("G8", "S8"), ("G9", "S9")}}
    [result] = dq.score_ground_truth(m, failed)
    assert result.detected == 0 and result.recall == 0.0
    assert result.unexpected == 2 and result.precision == 0.0


def test_the_key_includes_every_column_of_a_composite_key():
    m = manifest({"kind": "guide_vs_count_discrepancy", "grn_number": "G1", "sku_code": "S1"})
    wrong_sku = {("R06", "goods_receipts.csv"): {("G1", "S2")}}
    assert dq.score_ground_truth(m, wrong_sku)[0].detected == 0
    right = {("R06", "goods_receipts.csv"): {("G1", "S1")}}
    assert dq.score_ground_truth(m, right)[0].detected == 1


def test_a_rule_that_never_fired_has_zero_recall_not_an_error():
    m = manifest({"kind": "expiry_before_manufacturing", "batch_code": "L1"})
    [result] = dq.score_ground_truth(m, {})
    assert result.recall == 0.0 and result.missed == ["L1"]


def test_extra_flags_are_counted_as_unexpected_but_do_not_reduce_recall():
    m = manifest({"kind": "unauthorized_high_waste", "waste_code": "W1"})
    failed = {("W03", "waste_log.csv"): {("W1",), ("W7",), ("W8",)}}
    [result] = dq.score_ground_truth(m, failed)
    assert result.recall == 1.0 and result.unexpected == 2
    assert result.precision == pytest.approx(1 / 3)


def test_scoring_without_a_manifest_returns_nothing():
    assert dq.score_ground_truth(None, {}) == []
    assert dq.score_ground_truth({"anomalies": []}, {}) == []


def test_unknown_anomaly_kinds_are_ignored():
    assert dq.score_ground_truth(manifest({"kind": "cosmic_ray", "x": "1"}), {}) == []


def test_every_generator_defect_type_has_a_responsible_rule():
    import data_generator as gen
    budget = set(gen.RetailSimulator(gen.date.today(), gen.date.today(), 1, True)._anomaly_budget)
    assert budget <= set(dq.GROUND_TRUTH_RULES), \
        f"defects with no detecting rule to score: {sorted(budget - set(dq.GROUND_TRUTH_RULES))}"
    assert all(spec.rule_id in dq.RULES for spec in dq.GROUND_TRUTH_RULES.values())


# ---------------------------------------------------------------------------
# Run record and history
# ---------------------------------------------------------------------------

def sample_record(**overrides):
    base = dict(
        run_id="11111111-2222-3333-4444-555555555555",
        started_at=datetime(2026, 11, 15, 10, 0, 0), finished_at=datetime(2026, 11, 15, 10, 0, 5),
        status="SUCCESS", dry_run=False,
        extracted={"a.csv": 100, "b.csv": 50}, quarantined={"a.csv": 3}, loaded={"a": 97},
        results=[rr("R02", "a.csv", 100, 3), rr("R01", "a.csv", 97, 0)],
        ground_truth=[dq.GroundTruthResult("negative_received_qty", "R02", 3, 3)],
        manifest={"seed": 7, "policy": "heuristic", "start_date": "2026-09-16",
                  "end_date": "2026-11-14", "days": 60})
    base.update(overrides)
    return dq.build_run_record(**base)


def test_run_record_totals_and_acceptance_rate():
    record = sample_record()
    assert record["totals"]["rows_extracted"] == 150
    assert record["totals"]["rows_quarantined"] == 3
    assert record["totals"]["acceptance_rate"] == pytest.approx(0.98)
    assert record["source"]["seed"] == 7
    assert record["ground_truth"][0]["recall"] == 1.0


def test_run_record_survives_json_round_trip():
    record = sample_record()
    assert json.loads(json.dumps(record)) == record


def test_a_failed_run_records_why():
    record = sample_record(status="FAILED", aborted_reason="boom")
    assert record["status"] == "FAILED" and record["aborted_reason"] == "boom"


def test_history_entry_keeps_only_what_a_trend_needs():
    entry = dq.history_entry(sample_record())
    assert entry["failed_by_rule"] == {"R02:a.csv": 3}, "rules that found nothing must not bloat the history"
    assert entry["ground_truth"] == {"negative_received_qty": {"injected": 3, "detected": 3}}
    assert "rules" not in entry and "datasets" not in entry
    assert len(json.dumps(entry)) < 1500


def test_write_run_record_appends_one_history_line_per_run(tmp_path):
    history = tmp_path / "docs" / "history.jsonl"
    runs = tmp_path / "runs"
    for n in range(3):
        dq.write_run_record(sample_record(run_id=f"0000000{n}-0000-0000-0000-000000000000"),
                            runs_dir=runs, history_path=history)
    assert len(history.read_text(encoding="utf-8").splitlines()) == 3
    assert len(list(runs.glob("*.json"))) == 3
    assert [e["run_id"][:8] for e in dq.read_history(history)] == ["00000000", "00000001", "00000002"]


def test_read_history_skips_corrupt_lines_instead_of_failing(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text('{"run_id": "ok"}\n\nnot json at all\n{"run_id": "ok2"}\n', encoding="utf-8")
    assert [e["run_id"] for e in dq.read_history(path)] == ["ok", "ok2"]


def test_read_history_of_a_missing_file_is_empty(tmp_path):
    assert dq.read_history(tmp_path / "nope.jsonl") == []


def test_load_manifest_tolerates_absence_and_corruption(tmp_path):
    assert dq.load_manifest(tmp_path) is None
    (tmp_path / "_manifest.json").write_text("{broken", encoding="utf-8")
    assert dq.load_manifest(tmp_path) is None
    (tmp_path / "_manifest.json").write_text('{"seed": 1}', encoding="utf-8")
    assert dq.load_manifest(tmp_path) == {"seed": 1}
