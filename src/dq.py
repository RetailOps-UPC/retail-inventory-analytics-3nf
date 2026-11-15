# -*- coding: utf-8 -*-
"""
Data-quality scorecard: rule catalog, ground-truth scoring and run records.

The ETL quality gate (``etl_pipeline.py``) decides what to quarantine. This
module turns what it saw into something measurable and versionable:

* **A rule catalog** that classifies every rule by data-quality dimension
  (completeness, uniqueness, validity, consistency, integrity, timeliness), so
  results roll up into a scorecard rather than a flat list of findings.
* **Evaluated-vs-failed counts for EVERY rule, including the ones that found
  nothing.** The original gate only recorded rules that fired, which makes a
  pass rate impossible to compute: "0 findings" cannot distinguish a clean
  dataset from a rule that never ran.
* **Ground-truth scoring.** The synthetic generator writes a manifest listing
  the natural key of every defect it injected. Scoring the gate against that
  list yields real recall and precision per defect type, instead of the usual
  unfalsifiable claim that the checks "work".
* **A run record** (JSON) and a one-line history entry that are small enough to
  commit, so data quality has a trend line under version control.

Pure standard library; no pandas or SQLAlchemy import at module level, so the
scoring logic is unit-testable without either.

Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "docs" / "data_quality" / "history.jsonl"
DEFAULT_RUNS_DIR = PROJECT_ROOT / "data" / "quality_runs"
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Rule catalog
# ---------------------------------------------------------------------------

#: Data-quality dimensions used to roll rules up into a scorecard.
DIMENSIONS = ("completeness", "uniqueness", "validity", "consistency",
              "integrity", "timeliness")


@dataclass(frozen=True)
class RuleSpec:
    dimension: str
    title: str


#: Every rule the gate can evaluate. The single source of truth: the ETL, the
#: report generator and the tests all read it, so a rule cannot exist in one
#: place and be undocumented in another.
RULES: Dict[str, RuleSpec] = {
    "G01": RuleSpec("completeness", "Mandatory column is null or failed type coercion"),
    "G02": RuleSpec("uniqueness", "Natural key is unique"),
    "R01": RuleSpec("validity", "Dispatched quantity is strictly positive"),
    "R02": RuleSpec("validity", "Received quantity is not negative"),
    "R03": RuleSpec("consistency", "Received quantity does not exceed dispatched"),
    "R04": RuleSpec("consistency", "Dispatch guide matches the physical count"),
    "R05": RuleSpec("validity", "Intake temperature within the plausible band"),
    "R06": RuleSpec("consistency", "Every shortage against the guide has a documented reason"),
    "B01": RuleSpec("validity", "Expiration date is not before manufacturing date"),
    "B02": RuleSpec("validity", "Initial quantity is strictly positive"),
    "B03": RuleSpec("validity", "Current quantity is not negative"),
    "B04": RuleSpec("consistency", "Current quantity does not exceed initial quantity"),
    "B05": RuleSpec("consistency", "Observed shelf life matches the SKU master"),
    "B06": RuleSpec("timeliness", "No ACTIVE batch holds stock past its expiration"),
    "W01": RuleSpec("validity", "Discarded quantity is strictly positive"),
    "W02": RuleSpec("validity", "Unit cost at loss is strictly positive"),
    "W03": RuleSpec("integrity", "Material waste carries an authorizer"),
    "W04": RuleSpec("integrity", "Material waste is not self-authorized"),
    "S01": RuleSpec("validity", "Sales figures are not negative"),
    "S02": RuleSpec("validity", "Revenue is not below cost of goods sold"),
    "S03": RuleSpec("consistency", "Units and revenue agree on being zero or not"),
    "S04": RuleSpec("timeliness", "Sale date is not in the future"),
    "F01": RuleSpec("integrity", "Foreign natural key resolves to a parent row"),
}


def dimension_of(rule_id: str) -> str:
    spec = RULES.get(rule_id)
    return spec.dimension if spec else "validity"


# ---------------------------------------------------------------------------
# Rule results
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    """One rule evaluated against one dataset, whether or not it fired."""

    rule_id: str
    dataset: str
    severity: str
    evaluated: int
    failed: int

    @property
    def dimension(self) -> str:
        return dimension_of(self.rule_id)

    @property
    def pass_rate(self) -> Optional[float]:
        """Share of evaluated rows that passed; None when nothing was evaluated."""
        if self.evaluated <= 0:
            return None
        return 1.0 - (self.failed / self.evaluated)


_SEVERITY_RANK = {"FATAL": 3, "ERROR": 2, "WARNING": 1, "INFO": 0}


def merge_results(results: Iterable[RuleResult]) -> List[RuleResult]:
    """Collapse results that share a (rule, dataset) key into one.

    Some rules run several times against one dataset: the foreign-key rule F01
    checks each FK column separately, so ``goods_receipts.csv`` yields three F01
    results. The audit tables key on (rule, dataset), and a plain INSERT of
    three rows with that key would either fail or, with ON CONFLICT DO NOTHING,
    silently drop two of them. Merging sums the row-checks (a pass rate across
    ALL the FK checks of the dataset) and keeps the most severe severity.
    """
    merged: Dict[Tuple[str, str], RuleResult] = {}
    for r in results:
        key = (r.rule_id, r.dataset)
        if key not in merged:
            merged[key] = RuleResult(r.rule_id, r.dataset, r.severity, r.evaluated, r.failed)
            continue
        current = merged[key]
        current.evaluated += r.evaluated
        current.failed += r.failed
        if _SEVERITY_RANK.get(r.severity, 0) > _SEVERITY_RANK.get(current.severity, 0):
            current.severity = r.severity
    return list(merged.values())


def dimension_scores(results: Iterable[RuleResult]) -> Dict[str, Dict[str, Any]]:
    """Roll rule results up by dimension.

    The unit is a *row-check*: one row evaluated by one rule. A dimension with
    three rules over the same 1,000 rows has 3,000 row-checks. The score is the
    share of row-checks that passed.
    """
    totals: Dict[str, List[int]] = {}
    for result in results:
        bucket = totals.setdefault(result.dimension, [0, 0])
        bucket[0] += result.evaluated
        bucket[1] += result.failed
    scores: Dict[str, Dict[str, Any]] = {}
    for dimension in DIMENSIONS:
        if dimension not in totals:
            continue
        evaluated, failed = totals[dimension]
        scores[dimension] = {
            "evaluated": evaluated,
            "failed": failed,
            "pass_rate": (1.0 - failed / evaluated) if evaluated else None,
        }
    return scores


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GroundTruthRule:
    """How one injected defect type is supposed to be caught."""

    rule_id: str
    dataset: str
    key_columns: Tuple[str, ...]


#: The generator's defect types and the rule that must catch each one.
GROUND_TRUTH_RULES: Dict[str, GroundTruthRule] = {
    "expiry_before_manufacturing": GroundTruthRule(
        "B01", "batches.csv", ("batch_code",)),
    "negative_received_qty": GroundTruthRule(
        "R02", "goods_receipts.csv", ("grn_number", "sku_code")),
    "guide_vs_count_discrepancy": GroundTruthRule(
        "R06", "goods_receipts.csv", ("grn_number", "sku_code")),
    "unauthorized_high_waste": GroundTruthRule(
        "W03", "waste_log.csv", ("waste_code",)),
}


@dataclass
class GroundTruthResult:
    kind: str
    rule_id: str
    injected: int
    detected: int
    missed: List[str] = field(default_factory=list)
    unexpected: int = 0

    @property
    def recall(self) -> Optional[float]:
        return self.detected / self.injected if self.injected else None

    @property
    def precision(self) -> Optional[float]:
        flagged = self.detected + self.unexpected
        return self.detected / flagged if flagged else None


def score_ground_truth(manifest: Optional[Dict[str, Any]],
                       failed_keys: Dict[Tuple[str, str], Set[Tuple[str, ...]]]
                       ) -> List[GroundTruthResult]:
    """Score the gate against the generator's list of injected defects.

    ``failed_keys`` maps ``(rule_id, dataset)`` to the natural keys of the rows
    that rule rejected. A defect counts as detected only if the *specific row*
    was flagged by the rule responsible for it, not merely if the right number
    of rows was flagged somewhere.

    ``unexpected`` counts rows the rule flagged that the generator did NOT
    inject. These are not necessarily false positives (the simulation can
    produce a genuine instance of the same condition), which is why the figure
    is reported as a count to inspect rather than folded into a verdict.
    """
    if not manifest or not manifest.get("anomalies"):
        return []

    by_kind: Dict[str, Set[Tuple[str, ...]]] = {}
    for anomaly in manifest["anomalies"]:
        spec = GROUND_TRUTH_RULES.get(anomaly.get("kind", ""))
        if spec is None:
            continue
        key = tuple(str(anomaly.get(column, "")) for column in spec.key_columns)
        by_kind.setdefault(anomaly["kind"], set()).add(key)

    results: List[GroundTruthResult] = []
    for kind, spec in GROUND_TRUTH_RULES.items():
        injected_keys = by_kind.get(kind, set())
        if not injected_keys:
            continue
        flagged = failed_keys.get((spec.rule_id, spec.dataset), set())
        detected = injected_keys & flagged
        results.append(GroundTruthResult(
            kind=kind, rule_id=spec.rule_id,
            injected=len(injected_keys), detected=len(detected),
            missed=sorted("|".join(k) for k in injected_keys - flagged),
            unexpected=len(flagged - injected_keys)))
    return results


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------

def git_sha() -> Optional[str]:
    """Short commit hash of the code that ran, or None outside a repository.

    Suffixed ``+dirty`` when tracked files have uncommitted changes. A bare hash
    would claim that exactly that commit produced the result, which is false the
    moment someone runs the pipeline from a working tree they have edited.
    """
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=PROJECT_ROOT, capture_output=True, text=True,
                              timeout=5, check=False)
        sha = done.stdout.strip()
        if not sha:
            return None
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                cwd=PROJECT_ROOT, capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{sha}+dirty" if status.stdout.strip() else sha


def load_manifest(raw_dir: Path) -> Optional[Dict[str, Any]]:
    """Read ``_manifest.json`` written by the generator, if present and valid."""
    path = raw_dir / "_manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_run_record(*, run_id: Optional[str], started_at: datetime,
                     finished_at: datetime, status: str, dry_run: bool,
                     extracted: Dict[str, int], quarantined: Dict[str, int],
                     loaded: Dict[str, int], results: Sequence[RuleResult],
                     ground_truth: Sequence[GroundTruthResult],
                     manifest: Optional[Dict[str, Any]],
                     aborted_reason: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the JSON-serialisable record of one quality run."""
    results = merge_results(results)
    total_extracted = sum(extracted.values())
    total_quarantined = sum(quarantined.values())
    source = None
    if manifest:
        source = {key: manifest.get(key) for key in
                  ("seed", "policy", "start_date", "end_date", "days")}

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or str(uuid.uuid4()),
        "recorded_at": finished_at.isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 2),
        "status": status,
        "dry_run": dry_run,
        "aborted_reason": aborted_reason,
        "git_sha": git_sha(),
        "source": source,
        "totals": {
            "rows_extracted": total_extracted,
            "rows_quarantined": total_quarantined,
            "rows_loaded": sum(loaded.values()),
            "acceptance_rate": (round(1.0 - total_quarantined / total_extracted, 6)
                                if total_extracted else None),
        },
        "datasets": {name: {"extracted": extracted[name],
                            "quarantined": quarantined.get(name, 0)}
                     for name in sorted(extracted)},
        "rules": [{"rule_id": r.rule_id, "dataset": r.dataset,
                   "severity": r.severity, "dimension": r.dimension,
                   "evaluated": r.evaluated, "failed": r.failed}
                  for r in results],
        "dimension_scores": dimension_scores(results),
        "ground_truth": [{"kind": g.kind, "rule_id": g.rule_id,
                          "injected": g.injected, "detected": g.detected,
                          "missed": g.missed, "unexpected": g.unexpected,
                          "recall": g.recall, "precision": g.precision}
                         for g in ground_truth],
    }


def history_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    """Condense a run record to the single line kept in version control.

    Full records are bulky and change on every run; the history keeps only what
    a trend needs: totals, per-rule failure counts, dimension pass rates and
    ground-truth recall.
    """
    return {
        "run_id": record["run_id"],
        "recorded_at": record["recorded_at"],
        "status": record["status"],
        "dry_run": record["dry_run"],
        "git_sha": record.get("git_sha"),
        "source": record.get("source"),
        "totals": record["totals"],
        "failed_by_rule": {f"{r['rule_id']}:{r['dataset']}": r["failed"]
                           for r in record["rules"] if r["failed"]},
        "dimension_pass_rate": {d: (round(v["pass_rate"], 6)
                                    if v["pass_rate"] is not None else None)
                                for d, v in record["dimension_scores"].items()},
        "ground_truth": {g["kind"]: {"injected": g["injected"],
                                     "detected": g["detected"]}
                         for g in record["ground_truth"]},
    }


def write_run_record(record: Dict[str, Any], *,
                     runs_dir: Path = DEFAULT_RUNS_DIR,
                     history_path: Path = DEFAULT_HISTORY_PATH) -> Tuple[Path, Path]:
    """Write the full record (untracked) and append the history line (tracked)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = record["recorded_at"].replace(":", "").replace("-", "")
    record_path = runs_dir / f"{stamp}_{record['run_id'][:8]}.json"
    record_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(history_entry(record), ensure_ascii=False,
                                sort_keys=True) + "\n")
    return record_path, history_path


def read_history(history_path: Path = DEFAULT_HISTORY_PATH) -> List[Dict[str, Any]]:
    """Parse the history file, skipping blank or corrupt lines rather than failing."""
    if not history_path.is_file():
        return []
    entries: List[Dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------

def persist_to_db(engine: Any, record: Dict[str, Any]) -> int:
    """Insert the run into ``ops.dq_*``. Returns the new ``dq_run_id``.

    Imports SQLAlchemy lazily so the rest of this module stays dependency-free.
    The caller is expected to treat failure as non-fatal: losing the audit row
    must never undo a load that already committed.
    """
    from sqlalchemy import text

    source = record.get("source") or {}
    with engine.begin() as connection:
        dq_run_id = connection.execute(text(
            "INSERT INTO ops.dq_runs "
            "  (run_id, recorded_at, status, dry_run, git_sha, source_seed, "
            "   window_start, window_end, rows_extracted, rows_quarantined, "
            "   rows_loaded, acceptance_rate) "
            "VALUES (CAST(:run_id AS UUID), CAST(:recorded_at AS TIMESTAMPTZ), "
            "        :status, :dry_run, :git_sha, :seed, "
            "        CAST(:start AS DATE), CAST(:end AS DATE), :extracted, "
            "        :quarantined, :loaded, :acceptance) "
            "RETURNING dq_run_id"),
            {"run_id": record["run_id"], "recorded_at": record["recorded_at"],
             "status": record["status"], "dry_run": record["dry_run"],
             "git_sha": record.get("git_sha"), "seed": source.get("seed"),
             "start": source.get("start_date"), "end": source.get("end_date"),
             "extracted": record["totals"]["rows_extracted"],
             "quarantined": record["totals"]["rows_quarantined"],
             "loaded": record["totals"]["rows_loaded"],
             "acceptance": record["totals"]["acceptance_rate"]}).scalar_one()

        if record["rules"]:
            connection.execute(text(
                "INSERT INTO ops.dq_rule_results "
                "  (dq_run_id, rule_id, dataset, dimension, severity, "
                "   rows_evaluated, rows_failed) "
                "VALUES (:dq_run_id, :rule_id, :dataset, :dimension, :severity, "
                "        :evaluated, :failed) "
                "ON CONFLICT (dq_run_id, rule_id, dataset) DO NOTHING"),
                [{"dq_run_id": dq_run_id, **{k: r[k] for k in (
                    "rule_id", "dataset", "dimension", "severity",
                    "evaluated", "failed")}} for r in record["rules"]])

        if record["ground_truth"]:
            connection.execute(text(
                "INSERT INTO ops.dq_ground_truth "
                "  (dq_run_id, anomaly_kind, rule_id, injected, detected, unexpected) "
                "VALUES (:dq_run_id, :kind, :rule_id, :injected, :detected, "
                "        :unexpected) "
                "ON CONFLICT (dq_run_id, anomaly_kind) DO NOTHING"),
                [{"dq_run_id": dq_run_id, **{k: g[k] for k in (
                    "kind", "rule_id", "injected", "detected", "unexpected")}}
                 for g in record["ground_truth"]])
    return int(dq_run_id)
