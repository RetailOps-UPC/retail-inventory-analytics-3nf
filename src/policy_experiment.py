#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Replenishment-policy experiment: does forecasting actually reduce waste?

The business case claims that better replenishment cuts expiry waste. This
script turns that claim from an assumption into a measurement, by running the
store simulation under three ordering policies and comparing them on seeds the
policies were never tuned on.

Policies (see ``data_generator.POLICIES``)
------------------------------------------
heuristic  The legacy rule: a static order-up-to level from the catalog base
           demand, blind to the weekday pattern, with one extra day of cover.
level      A statistical base-stock with the correct cover and safety stock but
           NO seasonality: demand is the flat mean of the last 14 days.
forecast   The same base-stock, with demand from a same-weekday forecast.

``forecast`` versus ``level`` is the clean test of the value of *forecasting*.
``level`` versus ``heuristic`` is the value of merely sizing orders correctly.
Reporting only forecast-vs-heuristic would credit the forecast with a gain that
a flat average also delivers.

Design
------
* The service level ``z`` of each learned policy is a decision variable. It is
  CHOSEN on training seeds (best mean net profit) and then EVALUATED on a
  disjoint set of test seeds. Tuning and scoring on the same runs would make
  the result an artefact of the tuning.
* Metric: net profit = gross margin - waste cost. Lost sales are captured
  because they never become margin. Holding cost and customer goodwill are not
  modelled, which if anything favours lean policies; see Limitations.
* Comparisons are paired by seed and summarised with a bootstrap 95% CI on the
  mean difference (10,000 resamples), so no distributional assumption is made.

Usage
-----
    python src/policy_experiment.py
    python src/policy_experiment.py --train-seeds 20 --test-seeds 100
    python src/policy_experiment.py --end-date 2026-10-11 --workers 4

Writes:
    data/processed/policy_experiment_runs.csv   one row per simulation
    docs/experiments/policy_comparison.md       the report
    docs/experiments/policy_frontier.svg        waste vs lost-sales frontier

Dependencies: Python 3.9+ standard library only.
Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_generator as gen  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_CSV = PROJECT_ROOT / "data" / "processed" / "policy_experiment_runs.csv"
DEFAULT_REPORT = PROJECT_ROOT / "docs" / "experiments" / "policy_comparison.md"
DEFAULT_FRONTIER = PROJECT_ROOT / "docs" / "experiments" / "policy_frontier.svg"
DEFAULT_SUMMARY = PROJECT_ROOT / "docs" / "experiments" / "policy_summary.json"

#: Fixed window so the report is reproducible whatever day it is run.
DEFAULT_END_DATE = "2026-10-11"
DEFAULT_DAYS = 60
Z_GRID = (1.28, 1.65, 2.05, 2.50, 3.00, 3.50)
TRAIN_SEED_BASE = 1000
TEST_SEED_BASE = 2000
BOOTSTRAP_RESAMPLES = 10_000

METRICS = ("net_profit", "gross_margin", "waste_cost", "expiry_waste",
           "lost_revenue", "fill_rate")


# ---------------------------------------------------------------------------
# One simulation -> one row of metrics
# ---------------------------------------------------------------------------

def simulate(job: Tuple[str, float, int, str, int]) -> Dict[str, float]:
    """Run one simulation and reduce it to the metrics we compare.

    Top-level and fed a plain tuple so it can cross a process boundary on
    Windows, where worker processes are spawned rather than forked.
    """
    policy, z, seed, end_iso, days = job
    end = date.fromisoformat(end_iso)
    start = end - timedelta(days=days - 1)

    # Anomaly injection is off: it is irrelevant to replenishment and would
    # only add noise to the comparison.
    sim = gen.RetailSimulator(start, end, seed, inject_anomalies=False,
                              policy=policy, service_z=z)
    result = sim.run()

    revenue = sum(s.gross_revenue for s in result.sales)
    cogs = sum(s.cost_of_goods_sold for s in result.sales)
    units_sold = sum(s.units_sold for s in result.sales)
    waste = sum(w.qty_discarded * w.unit_cost_at_loss for w in result.waste)
    expiry = sum(w.qty_discarded * w.unit_cost_at_loss
                 for w in result.waste if w.reason_code == "WR-EXP")
    demanded = units_sold + result.stockout_units

    return {
        "policy": policy, "z": z, "seed": seed,
        "gross_margin": revenue - cogs,
        "waste_cost": waste,
        "expiry_waste": expiry,
        "net_profit": revenue - cogs - waste,
        "lost_revenue": result.lost_revenue,
        "fill_rate": units_sold / demanded if demanded else 1.0,
    }


def run_jobs(jobs: Sequence[Tuple], workers: int) -> List[Dict[str, float]]:
    """Run jobs in parallel, preserving order so the output is deterministic."""
    if workers <= 1:
        return [simulate(job) for job in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(simulate, jobs, chunksize=4))


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def bootstrap_ci(diffs: Sequence[float], *, resamples: int = BOOTSTRAP_RESAMPLES,
                 level: float = 0.95, seed: int = 20261012) -> Tuple[float, float]:
    """Percentile bootstrap CI for the mean of paired differences."""
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(
        sum(diffs[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(resamples))
    lower = means[int(((1 - level) / 2) * resamples)]
    upper = means[int((1 - (1 - level) / 2) * resamples) - 1]
    return lower, upper


def paired_diff(rows_a: Sequence[Dict], rows_b: Sequence[Dict],
                metric: str) -> Dict[str, float]:
    """Mean of (a - b) paired by seed, with a bootstrap CI."""
    by_seed_b = {r["seed"]: r for r in rows_b}
    diffs = [r[metric] - by_seed_b[r["seed"]][metric]
             for r in rows_a if r["seed"] in by_seed_b]
    lower, upper = bootstrap_ci(diffs)
    return {"mean": mean(diffs), "lo": lower, "hi": upper,
            "n": len(diffs),
            "significant": lower > 0 or upper < 0}


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------

def tune_service_level(train_rows: List[Dict], policy: str) -> Tuple[float, Dict[float, Dict]]:
    """Pick the z with the best mean net profit on the training seeds."""
    by_z: Dict[float, Dict[str, float]] = {}
    for z in Z_GRID:
        rows = [r for r in train_rows if r["policy"] == policy and r["z"] == z]
        by_z[z] = {m: mean(r[m] for r in rows) for m in METRICS}
    best = max(by_z, key=lambda z: by_z[z]["net_profit"])
    return best, by_z


def run_experiment(end_iso: str, days: int, n_train: int, n_test: int,
                   workers: int) -> Dict:
    train_seeds = [TRAIN_SEED_BASE + i for i in range(n_train)]
    test_seeds = [TEST_SEED_BASE + i for i in range(n_test)]

    # --- 1. Tuning grid on the TRAIN seeds ---------------------------------
    train_jobs = [(p, z, s, end_iso, days)
                  for p in ("level", "forecast") for z in Z_GRID
                  for s in train_seeds]
    train_jobs += [("heuristic", 0.0, s, end_iso, days) for s in train_seeds]
    train_rows = run_jobs(train_jobs, workers)

    best_z = {}
    frontier = {}
    for policy in ("level", "forecast"):
        best_z[policy], frontier[policy] = tune_service_level(train_rows, policy)

    # --- 2. Evaluation on the held-out TEST seeds --------------------------
    test_jobs = ([("heuristic", 0.0, s, end_iso, days) for s in test_seeds]
                 + [("level", best_z["level"], s, end_iso, days) for s in test_seeds]
                 + [("forecast", best_z["forecast"], s, end_iso, days)
                    for s in test_seeds])
    test_rows = run_jobs(test_jobs, workers)

    return {"train_rows": train_rows, "test_rows": test_rows,
            "best_z": best_z, "frontier": frontier,
            "train_seeds": train_seeds, "test_seeds": test_seeds,
            "end": end_iso, "days": days}


def write_runs_csv(path: Path, experiment: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["split", "policy", "z", "seed", *METRICS]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for split, rows in (("train", experiment["train_rows"]),
                            ("test", experiment["test_rows"])):
            for row in rows:
                writer.writerow({"split": split, **{k: (round(v, 4)
                                 if isinstance(v, float) else v)
                                 for k, v in row.items() if k in fields}})


# ---------------------------------------------------------------------------
# Frontier chart (hand-written SVG: no plotting dependency)
# ---------------------------------------------------------------------------

def frontier_svg(experiment: Dict) -> str:
    """Waste cost vs lost revenue, one curve per learned policy.

    Down and to the left is better on both axes. The heuristic is a single
    point because it has no service-level knob.
    """
    train = experiment["train_rows"]
    heur = [r for r in train if r["policy"] == "heuristic"]
    points: Dict[str, List[Tuple[float, float, float]]] = {}
    for policy in ("level", "forecast"):
        points[policy] = [
            (experiment["frontier"][policy][z]["lost_revenue"],
             experiment["frontier"][policy][z]["waste_cost"], z)
            for z in Z_GRID]
    heur_pt = (mean(r["lost_revenue"] for r in heur),
               mean(r["waste_cost"] for r in heur))

    xs = [p[0] for pts in points.values() for p in pts] + [heur_pt[0]]
    ys = [p[1] for pts in points.values() for p in pts] + [heur_pt[1]]
    width, height = 720, 440
    left, right, top, bottom = 78, 30, 40, 64
    x_max = max(xs) * 1.08
    y_max = max(ys) * 1.08

    def px(x: float) -> float:
        return left + (x / x_max) * (width - left - right)

    def py(y: float) -> float:
        return height - bottom - (y / y_max) * (height - top - bottom)

    colors = {"level": "#8a5a00", "forecast": "#116a45", "heuristic": "#b3261e"}
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="t d" font-family="system-ui, sans-serif">',
        '<title id="t">Replenishment policy frontier: waste vs lost sales</title>',
        '<desc id="d">Mean waste cost against mean lost revenue over the training '
        'seeds. Lower and further left is better. Curves are the level and '
        'forecast policies across service levels; the heuristic is one point.</desc>',
        f'<rect width="{width}" height="{height}" fill="#fcfbf8"/>',
    ]
    # grid + axes
    for i in range(5):
        gx = x_max * i / 4
        gy = y_max * i / 4
        out.append(f'<line x1="{px(gx):.1f}" y1="{top}" x2="{px(gx):.1f}" '
                   f'y2="{height - bottom}" stroke="#e7e2d8"/>')
        out.append(f'<line x1="{left}" y1="{py(gy):.1f}" x2="{width - right}" '
                   f'y2="{py(gy):.1f}" stroke="#e7e2d8"/>')
        out.append(f'<text x="{px(gx):.1f}" y="{height - bottom + 18}" '
                   f'text-anchor="middle" font-size="11" fill="#5b564c">'
                   f'{gx / 1000:.0f}k</text>')
        out.append(f'<text x="{left - 8}" y="{py(gy) + 4:.1f}" text-anchor="end" '
                   f'font-size="11" fill="#5b564c">{gy / 1000:.1f}k</text>')
    out.append(f'<text x="{(left + width - right) / 2:.0f}" y="{height - 14}" '
               f'text-anchor="middle" font-size="12" fill="#2b2822">'
               f'Lost revenue per 60 days, S/ (service gap)</text>')
    out.append(f'<text transform="translate(18 {(top + height - bottom) / 2:.0f}) '
               f'rotate(-90)" text-anchor="middle" font-size="12" fill="#2b2822">'
               f'Waste cost per 60 days, S/</text>')

    for policy, pts in points.items():
        path = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(y):.1f}"
                        for i, (x, y, _z) in enumerate(pts))
        out.append(f'<path d="{path}" fill="none" stroke="{colors[policy]}" '
                   f'stroke-width="2"/>')
        for x, y, z in pts:
            chosen = abs(z - experiment["best_z"][policy]) < 1e-9
            radius = 6 if chosen else 3.5
            out.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="{radius}" '
                       f'fill="{colors[policy]}" '
                       f'stroke="#fcfbf8" stroke-width="1.5"/>')
            out.append(f'<text x="{px(x) + 8:.1f}" y="{py(y) - 7:.1f}" '
                       f'font-size="10" fill="{colors[policy]}">z={z:g}</text>')

    hx, hy = px(heur_pt[0]), py(heur_pt[1])
    out.append(f'<rect x="{hx - 6:.1f}" y="{hy - 6:.1f}" width="12" height="12" '
               f'fill="{colors["heuristic"]}" stroke="#fcfbf8" stroke-width="1.5"/>')
    out.append(f'<text x="{hx + 10:.1f}" y="{hy + 4:.1f}" font-size="11" '
               f'fill="{colors["heuristic"]}">heuristic</text>')

    # legend
    for i, (label, key) in enumerate((("forecast (seasonal)", "forecast"),
                                      ("level (flat mean)", "level"))):
        ly = top + 6 + i * 18
        out.append(f'<line x1="{width - 190}" y1="{ly}" x2="{width - 164}" '
                   f'y2="{ly}" stroke="{colors[key]}" stroke-width="2"/>')
        out.append(f'<text x="{width - 158}" y="{ly + 4}" font-size="11" '
                   f'fill="#2b2822">{label}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def fmt_money(value: float, signed: bool = False) -> str:
    return f"{value:+,.0f}" if signed else f"{value:,.0f}"


def verdict(diff: Dict[str, float], *, higher_is_better: bool) -> str:
    """Plain-language reading of a paired difference."""
    if not diff["significant"]:
        return "no detectable difference (the 95% interval spans zero)"
    better = (diff["mean"] > 0) == higher_is_better
    return "better" if better else "worse"


def build_report(experiment: Dict) -> str:
    test = experiment["test_rows"]
    by_policy = {p: [r for r in test if r["policy"] == p]
                 for p in ("heuristic", "level", "forecast")}
    best_z = experiment["best_z"]
    n_test = len(experiment["test_seeds"])
    n_train = len(experiment["train_seeds"])

    summary = {p: {m: mean(r[m] for r in rows) for m in METRICS}
               for p, rows in by_policy.items()}
    spread = {p: statistics.stdev([r["net_profit"] for r in rows])
              for p, rows in by_policy.items()}

    f_vs_h = {m: paired_diff(by_policy["forecast"], by_policy["heuristic"], m)
              for m in METRICS}
    l_vs_h = {m: paired_diff(by_policy["level"], by_policy["heuristic"], m)
              for m in METRICS}
    f_vs_l = {m: paired_diff(by_policy["forecast"], by_policy["level"], m)
              for m in METRICS}

    def pct_change(new: float, old: float) -> str:
        return f"{100 * (new - old) / old:+.1f}%" if old else "n/a"

    lines: List[str] = []
    add = lines.append

    add("# Replenishment policy experiment")
    add("")
    add("> Generated by `src/policy_experiment.py`. Do not edit by hand: every number "
        "below is recomputed on each run.")
    add("")
    add("## Question")
    add("")
    add("The business case assumes that better replenishment cuts waste. Does a "
        "forecast-driven ordering policy actually do that in this simulated network, "
        "and how much of any gain is due to *forecasting* as opposed to merely "
        "sizing orders sensibly?")
    add("")
    add("## Design")
    add("")
    add("| Item | Value |")
    add("|---|---|")
    add(f"| Network | 4 stores, 15 SKUs, 6 suppliers, {experiment['days']} days "
        f"ending {experiment['end']} |")
    add(f"| Training seeds | {n_train} (`{experiment['train_seeds'][0]}`.."
        f"`{experiment['train_seeds'][-1]}`), used ONLY to choose the service level |")
    add(f"| Test seeds | {n_test} (`{experiment['test_seeds'][0]}`.."
        f"`{experiment['test_seeds'][-1]}`), disjoint from training |")
    add(f"| Service-level grid | z in {', '.join(f'{z:g}' for z in Z_GRID)} |")
    add(f"| Chosen on training | level z={best_z['level']:g}, "
        f"forecast z={best_z['forecast']:g} |")
    add("| Metric | net profit = gross margin - waste cost (lost sales never become "
        "margin, so stockouts are priced in) |")
    add(f"| Inference | paired by seed, percentile bootstrap, "
        f"{BOOTSTRAP_RESAMPLES:,} resamples, 95% interval |")
    add("")
    add("The three policies:")
    add("")
    add("- **heuristic** - the legacy rule: a static order-up-to level from the "
        "catalog base demand, blind to the weekday pattern, with one extra day of "
        "cover.")
    add("- **level** - base-stock with the correct cover and a statistical safety "
        "stock, but demand is the flat mean of the last 14 days. No seasonality.")
    add("- **forecast** - the same base-stock, with demand from a same-weekday forecast.")
    add("")
    add("`forecast` vs `level` isolates the value of forecasting. `level` vs "
        "`heuristic` isolates the value of sizing orders properly.")
    add("")
    add(f"## Results on {n_test} held-out seeds")
    add("")
    add("Mean per 60-day run, S/ (units for fill rate).")
    add("")
    add("| Policy | Net profit | Gross margin | Waste cost | of which expiry "
        "| Lost revenue | Fill rate |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for p in ("heuristic", "level", "forecast"):
        s = summary[p]
        label = p if p == "heuristic" else f"{p} (z={best_z[p]:g})"
        add(f"| {label} | {fmt_money(s['net_profit'])} | {fmt_money(s['gross_margin'])} "
            f"| {fmt_money(s['waste_cost'])} | {fmt_money(s['expiry_waste'])} "
            f"| {fmt_money(s['lost_revenue'])} | {100 * s['fill_rate']:.1f}% |")
    add("")
    add("### Paired differences")
    add("")
    add("Mean difference, with 95% bootstrap interval. For net profit and fill rate "
        "a positive number is better; for waste and lost revenue a negative number "
        "is better.")
    add("")
    add("| Comparison | Net profit | Waste cost | Lost revenue | Reading (net profit) |")
    add("|---|---:|---:|---:|---|")
    for label, d in (("forecast vs heuristic", f_vs_h),
                     ("level vs heuristic", l_vs_h),
                     ("**forecast vs level**", f_vs_l)):
        def cell(metric: str) -> str:
            x = d[metric]
            return f"{fmt_money(x['mean'], True)} [{fmt_money(x['lo'], True)}, " \
                   f"{fmt_money(x['hi'], True)}]"
        add(f"| {label} | {cell('net_profit')} | {cell('waste_cost')} "
            f"| {cell('lost_revenue')} | "
            f"{verdict(d['net_profit'], higher_is_better=True)} |")
    add("")
    add("### Reading the numbers")
    add("")
    add(f"- Against the legacy heuristic, the forecast policy changes waste by "
        f"**{pct_change(summary['forecast']['waste_cost'], summary['heuristic']['waste_cost'])}** "
        f"and expiry waste by "
        f"**{pct_change(summary['forecast']['expiry_waste'], summary['heuristic']['expiry_waste'])}**, "
        f"and lost revenue by "
        f"**{pct_change(summary['forecast']['lost_revenue'], summary['heuristic']['lost_revenue'])}**.")
    add(f"- Net profit per run: forecast vs heuristic is "
        f"**{fmt_money(f_vs_h['net_profit']['mean'], True)}** "
        f"({pct_change(summary['forecast']['net_profit'], summary['heuristic']['net_profit'])}), "
        f"{verdict(f_vs_h['net_profit'], higher_is_better=True)}.")
    add(f"- **The decomposition that matters.** The net difference "
        f"({fmt_money(f_vs_h['net_profit']['mean'], True)}) splits into the value of "
        f"forecasting itself, forecast vs level "
        f"(**{fmt_money(f_vs_l['net_profit']['mean'], True)}**, "
        f"{verdict(f_vs_l['net_profit'], higher_is_better=True)}), plus what a "
        f"leaner order-up-to level does on its own without seasonality, level vs "
        f"heuristic (**{fmt_money(l_vs_h['net_profit']['mean'], True)}**, "
        f"{verdict(l_vs_h['net_profit'], higher_is_better=True)})."
        + (" The second term is significantly negative: a flat average is not "
           "merely unhelpful, tighter orders without a seasonal signal cost more "
           "in stockouts than they save in waste."
           if l_vs_h['net_profit']['significant']
           and l_vs_h['net_profit']['mean'] < 0 else ""))
    add(f"- Run-to-run noise: the standard deviation of net profit across seeds is "
        f"about S/ {spread['heuristic']:,.0f} (heuristic), "
        f"S/ {spread['level']:,.0f} (level) and S/ {spread['forecast']:,.0f} "
        f"(forecast). A single-seed comparison would be unreliable, which is why "
        f"this uses {n_test} seeds.")
    add("")
    add("## Waste vs service frontier (training seeds)")
    add("")
    add("![Waste vs lost sales frontier](policy_frontier.svg)")
    add("")
    add("Each learned policy trades waste against stockouts through its service "
        "level `z`; the larger dots mark the value chosen on training seeds. The "
        "heuristic has no such knob and sits at one point. A policy whose curve "
        "lies below and left of another dominates it: less waste *and* fewer "
        "stockouts at a matched service level.")
    add("")
    add("| Policy | z | Net profit | Waste cost | Lost revenue |")
    add("|---|---:|---:|---:|---:|")
    heur_train = [r for r in experiment["train_rows"] if r["policy"] == "heuristic"]
    add(f"| heuristic | - | {fmt_money(mean(r['net_profit'] for r in heur_train))} "
        f"| {fmt_money(mean(r['waste_cost'] for r in heur_train))} "
        f"| {fmt_money(mean(r['lost_revenue'] for r in heur_train))} |")
    for policy in ("level", "forecast"):
        for z in Z_GRID:
            s = experiment["frontier"][policy][z]
            mark = " **(chosen)**" if abs(z - best_z[policy]) < 1e-9 else ""
            add(f"| {policy} | {z:g}{mark} | {fmt_money(s['net_profit'])} "
                f"| {fmt_money(s['waste_cost'])} | {fmt_money(s['lost_revenue'])} |")
    add("")
    add("## Specification notes (what went wrong first)")
    add("")
    add("These are recorded because the first version of the experiment was wrong "
        "in two instructive ways, and hiding that would overstate how clean the "
        "result is.")
    add("")
    add("1. **Cover.** The first forecast policy copied the heuristic's cover of "
        "`interval + 1` days. For two-day-shelf-life hot food delivered daily, that "
        "holds two days of stock against a two-day life. Corrected to the textbook "
        "periodic-review base-stock: stock must last until the *next* delivery, "
        "i.e. `interval` days.")
    add("2. **Censored demand.** Sales understate demand on days the shelf ran "
        "empty. Learning from them naively made the policy under-order and spiral "
        "into stockouts. The tempting fix, *dropping* those days, was tried and "
        "made things worse: it removes exactly the high-demand days, biasing the "
        "estimate low by selection on the outcome. The fix kept is to floor each "
        "censored day at the mean of the uncensored days.")
    add("")
    add("Neither change was tuned against the test seeds; both follow from the "
        "inventory theory and were applied before the held-out evaluation.")
    add("")
    add("## Limitations")
    add("")
    add("- **The demand is synthetic and strongly weekly by construction.** The "
        "simulator multiplies a weekday shape and a weekend uplift into demand, so "
        "a same-weekday forecast is *built to find* that signal. The result "
        "measures the value of capturing seasonality when it exists, not the "
        "value of forecasting in an arbitrary real store.")
    add("- **The heuristic knows the true base rate.** It reads `base_daily_demand` "
        "from the catalog, while the learned policies must infer demand from "
        "censored sales. That favours the heuristic.")
    add("- **Net profit omits holding cost and customer goodwill.** A stockout "
        "costs more than the lost margin in real retail (lost trips, lost loyalty), "
        "which would favour policies that keep service high; the frontier table "
        "lets a reader re-weight that trade-off.")
    add("- **One shared service level per policy.** A per-category `z` (higher for "
        "ambient, lower for 2-day food) would likely do better and is not tried.")
    add("- **Sampling error is quantified; model error is not.** The intervals "
        "reflect seed-to-seed variation inside this simulator only.")
    add("")
    add("## Reproduce")
    add("")
    add("```bash")
    add(f"python src/policy_experiment.py --end-date {experiment['end']} "
        f"--train-seeds {n_train} --test-seeds {n_test}")
    add("```")
    add("")
    return "\n".join(lines)


def build_summary(experiment: Dict) -> Dict:
    """Machine-readable results, for the web app. The same numbers as the report."""
    test = experiment["test_rows"]
    by_policy = {p: [r for r in test if r["policy"] == p]
                 for p in ("heuristic", "level", "forecast")}
    means = {p: {m: mean(r[m] for r in rows) for m in METRICS}
             for p, rows in by_policy.items()}

    def diff(a: str, b: str) -> Dict[str, Dict]:
        return {m: paired_diff(by_policy[a], by_policy[b], m)
                for m in ("net_profit", "waste_cost", "expiry_waste", "lost_revenue")}

    train = experiment["train_rows"]
    heur = [r for r in train if r["policy"] == "heuristic"]
    frontier = {"heuristic": [{
        "z": None, "waste_cost": mean(r["waste_cost"] for r in heur),
        "lost_revenue": mean(r["lost_revenue"] for r in heur),
        "net_profit": mean(r["net_profit"] for r in heur)}]}
    for policy in ("level", "forecast"):
        frontier[policy] = [{
            "z": z, "chosen": abs(z - experiment["best_z"][policy]) < 1e-9,
            "waste_cost": experiment["frontier"][policy][z]["waste_cost"],
            "lost_revenue": experiment["frontier"][policy][z]["lost_revenue"],
            "net_profit": experiment["frontier"][policy][z]["net_profit"]} for z in Z_GRID]

    net = diff("forecast", "heuristic")["net_profit"]
    # One run is 60 days of a 4-store network; scale to a 400-store chain per year.
    scale = 365 / experiment["days"] / 4 * 400
    return {
        "end_date": experiment["end"], "days": experiment["days"],
        "train_seeds": len(experiment["train_seeds"]), "test_seeds": len(experiment["test_seeds"]),
        "best_z": experiment["best_z"], "means": means,
        "diffs": {"forecast_vs_heuristic": diff("forecast", "heuristic"),
                  "level_vs_heuristic": diff("level", "heuristic"),
                  "forecast_vs_level": diff("forecast", "level")},
        "frontier": frontier,
        "annualized_400_stores": {"net_gain": net["mean"] * scale, "ci_low": net["lo"] * scale,
                                  "ci_high": net["hi"] * scale},
        "share_of_gross_saving_kept": net["mean"] / -diff("forecast", "heuristic")["waste_cost"]["mean"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare replenishment policies on held-out simulation seeds.")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE,
                        help=f"Last simulated day (default {DEFAULT_END_DATE}, fixed "
                             f"so the report is reproducible).")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--train-seeds", type=int, default=20)
    parser.add_argument("--test-seeds", type=int, default=100)
    parser.add_argument("--workers", type=int,
                        default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--runs-csv", type=Path, default=DEFAULT_RUNS_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--frontier-svg", type=Path, default=DEFAULT_FRONTIER)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        date.fromisoformat(args.end_date)
    except ValueError:
        print("ERROR: --end-date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if args.train_seeds < 2 or args.test_seeds < 2:
        print("ERROR: need at least 2 seeds per split", file=sys.stderr)
        return 2

    started = datetime.now()
    total = (args.train_seeds * (len(Z_GRID) * 2 + 1) + args.test_seeds * 3)
    print(f"Running {total} simulations on {args.workers} worker(s)...")

    experiment = run_experiment(args.end_date, args.days, args.train_seeds,
                                args.test_seeds, args.workers)

    write_runs_csv(args.runs_csv, experiment)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_report(experiment), encoding="utf-8")
    args.frontier_svg.write_text(frontier_svg(experiment), encoding="utf-8")
    args.summary_json.write_text(
        json.dumps(build_summary(experiment), indent=2) + "\n", encoding="utf-8")

    elapsed = (datetime.now() - started).total_seconds()
    print(f"Chosen service levels (train): {experiment['best_z']}")
    print(f"Wrote {args.runs_csv.relative_to(PROJECT_ROOT)}")
    print(f"Wrote {args.report.relative_to(PROJECT_ROOT)}")
    print(f"Wrote {args.frontier_svg.relative_to(PROJECT_ROOT)}")
    print(f"Wrote {args.summary_json.relative_to(PROJECT_ROOT)}")
    print(f"Done in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
