#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Exports a static JSON snapshot of the warehouse for the web app.

The demo app is a static site (Vercel) with no API behind it, by design: this
repository is the analytics layer, and an operational API would duplicate Kardo.
The contract between the two repositories is therefore a FILE, not a service.
This script reads the star schema (``mart.*``), the forecasts (``ml.*``), the
pipeline and quality records (``ops.*``) and the experiment report, and writes
one ``snapshot.json`` that the app imports.

Why a snapshot and not hard-coded data
--------------------------------------
The app used to ship a hand-written ``inventoryData.js``. Hand-copied numbers
drift: the app once reported 9 tables when the DDL has 10, and a 95.0% fill rate
that the data does not support. Every number in the snapshot is computed from
the database at export time, and the snapshot records WHEN and from WHICH commit,
so the app can show how fresh its figures are.

Usage
-----
    python src/export_frontend_data.py
    python src/export_frontend_data.py --out ../retail-inventory-web/src/data/snapshot.json

Dependencies: pandas, SQLAlchemy, psycopg2 (and a populated warehouse: run
``python src/pipeline.py`` first).
Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbutil  # noqa: E402
import dq  # noqa: E402
import forecasting as fc  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT.parent / "retail-inventory-web" / "src" / "data" / "snapshot.json"
FALLBACK_OUT = PROJECT_ROOT / "data" / "processed" / "snapshot.json"
EXPERIMENT_SUMMARY = PROJECT_ROOT / "docs" / "experiments" / "policy_summary.json"

HISTORY_DAYS = 28          # actuals shown behind each forecast
FEFO_ROWS = 40
SCHEMA_VERSION = 1


def clean(value: Any) -> Any:
    """Make a value JSON-safe: numpy scalars to Python, NaN/Inf to None, dates to ISO."""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()[:10]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 4)
    return value


def records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    return [{k: clean(v) for k, v in row.items()} for row in frame.to_dict("records")]


def query(engine: Any, sql: str, **params: Any) -> pd.DataFrame:
    from sqlalchemy import text
    with engine.connect() as connection:
        return pd.read_sql(text(sql), connection, params=params)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def section_meta(engine: Any) -> Dict[str, Any]:
    window = query(engine, "SELECT MIN(d.full_date) AS first_day, MAX(d.full_date) AS last_day, "
                           "COUNT(DISTINCT f.store_key || '-' || f.product_key) AS series "
                           "FROM mart.fact_sales_daily f JOIN mart.dim_date d USING (date_key)").iloc[0]
    tables = int(query(engine, "SELECT COUNT(*) AS n FROM information_schema.tables "
                               "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").iloc[0]["n"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": dq.git_sha(),
        "window": {"start": clean(window["first_day"]), "end": clean(window["last_day"]),
                   "days": (window["last_day"] - window["first_day"]).days + 1},
        "series": int(window["series"]),
        "tables_3nf": tables,
        "source": "synthetic",
    }


def section_headline(engine: Any, as_of: date) -> Dict[str, Any]:
    """Network totals, computed here once so the app never re-derives a KPI."""
    sales = query(engine, "SELECT COALESCE(SUM(revenue),0) AS revenue, COALESCE(SUM(gross_margin),0) AS margin, "
                          "COALESCE(SUM(waste_cost),0) AS waste, COALESCE(SUM(expiry_cost),0) AS expiry "
                          "FROM mart.v_store_day_kpi").iloc[0]
    fill = query(engine, "SELECT SUM(qty_accepted) AS acc, SUM(qty_dispatched) AS disp, "
                         "SUM(value_short) AS short_value FROM mart.fact_receipt_line").iloc[0]
    lots = query(engine, "SELECT COUNT(*) FILTER (WHERE expiration_date <= :d + 2) AS critical, "
                         "COUNT(*) AS within_five, "
                         "COALESCE(SUM(current_qty * COALESCE(gi.unit_purchase_cost, p.unit_cost)),0) AS exposure "
                         "FROM inventory_batches b JOIN products p USING (product_id) "
                         "LEFT JOIN goods_receipt_items gi ON gi.grn_item_id = b.grn_item_id "
                         "WHERE b.batch_status = 'ACTIVE' AND b.current_qty > 0 "
                         "AND b.expiration_date BETWEEN :d AND :d + 5", d=as_of).iloc[0]
    revenue = float(sales["revenue"])
    return {
        "as_of": as_of.isoformat(),
        "revenue": clean(revenue), "gross_margin": clean(float(sales["margin"])),
        "waste_cost": clean(float(sales["waste"])), "expiry_cost": clean(float(sales["expiry"])),
        "waste_intensity_pct": clean(100 * float(sales["waste"]) / revenue) if revenue else None,
        "expiry_share_pct": clean(100 * float(sales["expiry"]) / float(sales["waste"]))
        if float(sales["waste"]) else None,
        "fill_rate_pct": clean(100 * float(fill["acc"]) / float(fill["disp"])) if float(fill["disp"]) else None,
        "claimable_short_value": clean(float(fill["short_value"])),
        "lots_critical_48h": int(lots["critical"]), "lots_within_5d": int(lots["within_five"]),
        "exposure_5d": clean(float(lots["exposure"])),
    }


def section_products(engine: Any) -> List[Dict[str, Any]]:
    """Current catalog: what the filters and the forecast selector list."""
    return records(query(engine,
        "SELECT sku_code, product_name AS name, category_name AS category, shelf_life_days, "
        "retail_price, unit_cost FROM mart.dim_product WHERE is_current ORDER BY sku_code"))


def section_stores(engine: Any) -> List[Dict[str, Any]]:
    return records(query(engine,
        "SELECT s.store_key AS id, s.store_code AS code, s.store_name AS name, s.store_format AS format, "
        "s.district, k.revenue, k.waste_cost, "
        "ROUND(100 * k.waste_cost / NULLIF(k.revenue, 0), 3) AS waste_intensity_pct, "
        "ROUND(100 * k.expiry_cost / NULLIF(k.waste_cost, 0), 1) AS expiry_share_pct "
        "FROM mart.dim_store s JOIN (SELECT store_code, SUM(revenue) AS revenue, SUM(waste_cost) AS waste_cost, "
        "SUM(expiry_cost) AS expiry_cost FROM mart.v_store_day_kpi GROUP BY store_code) k "
        "ON k.store_code = s.store_code ORDER BY s.store_key"))


def section_fefo(engine: Any, as_of: date) -> List[Dict[str, Any]]:
    frame = query(engine,
        "SELECT s.store_id, s.store_code, s.store_name, p.sku_code, p.product_name, "
        "c.category_name, b.batch_code, b.current_qty AS qty, (b.expiration_date - :d) AS days_left, "
        "ROUND(b.current_qty * COALESCE(gi.unit_purchase_cost, p.unit_cost), 2) AS exposure, "
        "DENSE_RANK() OVER (PARTITION BY s.store_id, c.category_id "
        "                   ORDER BY b.expiration_date, b.batch_id) AS rank "
        "FROM inventory_batches b JOIN stores s USING (store_id) JOIN products p USING (product_id) "
        "JOIN product_categories c ON c.category_id = p.category_id "
        "LEFT JOIN goods_receipt_items gi ON gi.grn_item_id = b.grn_item_id "
        "WHERE b.batch_status = 'ACTIVE' AND b.current_qty > 0 "
        "AND b.expiration_date BETWEEN :d AND :d + 5 "
        "ORDER BY days_left, exposure DESC LIMIT :n", d=as_of, n=FEFO_ROWS)
    frame["action"] = frame["days_left"].map(fc.markdown_action)
    return records(frame)


def section_suppliers(engine: Any) -> List[Dict[str, Any]]:
    return records(query(engine,
        "SELECT supplier_code AS code, company_name AS name, receipts, qty_dispatched, qty_accepted, "
        "fill_rate_pct, short_line_pct, claimable_value, worst_fill_rank AS rank "
        "FROM mart.v_supplier_scorecard ORDER BY fill_rate_pct"))


def section_velocity(engine: Any) -> List[Dict[str, Any]]:
    """Days of inventory: stock on hand over the trailing 7-day average sales."""
    return records(query(engine,
        "WITH latest AS (SELECT MAX(date_key) AS dk FROM mart.fact_stock_position), "
        "velocity AS (SELECT f.store_key, f.product_key, AVG(f.units_sold) AS daily "
        "  FROM mart.fact_sales_daily f WHERE f.date_key > (SELECT dk FROM latest) - 100 "
        "  AND f.date_key IN (SELECT date_key FROM mart.dim_date WHERE full_date > "
        "      (SELECT MAX(d.full_date) FROM mart.fact_sales_daily x JOIN mart.dim_date d USING (date_key)) - 7) "
        "  GROUP BY f.store_key, f.product_key) "
        "SELECT s.store_code, p.sku_code, p.product_name, sp.on_hand_qty AS stock, ROUND(v.daily, 1) AS daily_velocity, "
        "ROUND(sp.on_hand_qty / NULLIF(v.daily, 0), 2) AS dsi_days "
        "FROM mart.fact_stock_position sp JOIN latest l ON l.dk = sp.date_key "
        "JOIN velocity v ON v.store_key = sp.store_key AND v.product_key = sp.product_key "
        "JOIN mart.dim_store s ON s.store_key = sp.store_key "
        "JOIN mart.dim_product p ON p.product_key = sp.product_key "
        "ORDER BY dsi_days ASC LIMIT 12"))


def section_weekday(engine: Any) -> List[Dict[str, Any]]:
    return records(query(engine,
        "SELECT store_code, sku_code, product_name, iso_weekday, avg_units, seasonality_index "
        "FROM mart.v_weekday_demand_profile ORDER BY store_code, sku_code, iso_weekday"))


def section_forecast(engine: Any) -> Dict[str, Any]:
    run = query(engine, "SELECT * FROM ml.forecast_runs ORDER BY generated_on DESC LIMIT 1")
    if run.empty:
        return {"available": False}
    run = run.iloc[0]
    start = run["generated_on"]

    fcst = query(engine,
        "SELECT d.store_id, d.product_id, s.store_code, p.sku_code, p.product_name, d.target_date, "
        "d.yhat, d.yhat_lo, d.yhat_hi, d.model FROM ml.demand_forecast d "
        "JOIN stores s USING (store_id) JOIN products p USING (product_id) "
        "WHERE d.generated_on = :g ORDER BY s.store_code, p.sku_code, d.target_date", g=start)
    hist = query(engine,
        "SELECT s.store_code, p.sku_code, dd.full_date AS day, f.units_sold "
        "FROM mart.fact_sales_daily f JOIN mart.dim_store s ON s.store_key = f.store_key "
        "JOIN mart.dim_product p ON p.product_key = f.product_key "
        "JOIN mart.dim_date dd ON dd.date_key = f.date_key "
        "WHERE dd.full_date >= :g - :h ORDER BY s.store_code, p.sku_code, dd.full_date",
        g=start, h=HISTORY_DAYS)

    series: List[Dict[str, Any]] = []
    for (store_code, sku), group in fcst.groupby(["store_code", "sku_code"], sort=True):
        past = hist[(hist["store_code"] == store_code) & (hist["sku_code"] == sku)]
        series.append({
            "store_code": store_code, "sku_code": sku, "product_name": group["product_name"].iloc[0],
            "model": group["model"].iloc[0],
            "history": [[clean(d), clean(float(u))] for d, u in zip(past["day"], past["units_sold"])],
            "forecast": [[clean(d), clean(float(y)), clean(float(lo)), clean(float(hi))]
                         for d, y, lo, hi in zip(group["target_date"], group["yhat"],
                                                 group["yhat_lo"], group["yhat_hi"])],
        })

    accuracy = records(query(engine, "SELECT * FROM ml.v_forecast_accuracy"))
    recs = records(query(engine,
        "SELECT s.store_code, p.sku_code, p.product_name, r.cover_days, r.expected_demand, "
        "r.safety_stock, r.order_up_to, r.on_hand_qty, r.recommended_order_qty "
        "FROM ml.replenishment_recommendation r JOIN stores s USING (store_id) "
        "JOIN products p USING (product_id) WHERE r.generated_on = :g AND r.recommended_order_qty > 0 "
        "ORDER BY r.recommended_order_qty DESC LIMIT 12", g=start))
    expiring = records(query(engine,
        "SELECT * FROM ml.v_markdown_worklist LIMIT 10"))
    return {
        "available": True,
        "generated_on": clean(start), "horizon_days": int(run["horizon_days"]),
        "history_days": int(run["history_days"]), "series_count": int(run["series_count"]),
        "holdout_wape": clean(float(run["holdout_wape"])),
        "baseline_wape": clean(float(run["baseline_wape"])),
        "interval_coverage": clean(float(run["interval_coverage"])),
        "strategy": (run["parameters"] or {}).get("strategy") if isinstance(run["parameters"], dict) else None,
        "accuracy_by_model": accuracy, "series": series, "recommendations": recs,
        "projected_expiry": expiring,
    }


def section_experiment() -> Dict[str, Any]:
    if not EXPERIMENT_SUMMARY.is_file():
        return {"available": False}
    return {"available": True, **json.loads(EXPERIMENT_SUMMARY.read_text(encoding="utf-8"))}


def section_quality(engine: Any) -> Dict[str, Any]:
    latest = query(engine, "SELECT * FROM ops.dq_runs WHERE NOT dry_run ORDER BY recorded_at DESC LIMIT 1")
    history = dq.read_history()
    trend = [{"recorded_at": h["recorded_at"], "dry_run": h["dry_run"],
              "seed": (h.get("source") or {}).get("seed"),
              "acceptance_rate": h["totals"]["acceptance_rate"],
              "rows": h["totals"]["rows_extracted"], "quarantined": h["totals"]["rows_quarantined"],
              "caught": sum(v["detected"] for v in h["ground_truth"].values()),
              "planted": sum(v["injected"] for v in h["ground_truth"].values())}
             for h in sorted(history, key=lambda x: x["recorded_at"])]
    if latest.empty:
        return {"available": bool(trend), "trend": trend}
    dq_run_id = int(latest.iloc[0]["dq_run_id"])
    rules = query(engine, "SELECT rule_id, dataset, dimension, severity, rows_evaluated, rows_failed "
                          "FROM ops.dq_rule_results WHERE dq_run_id = :i ORDER BY rule_id, dataset", i=dq_run_id)
    truth = query(engine, "SELECT anomaly_kind AS kind, rule_id, injected, detected, unexpected "
                          "FROM ops.dq_ground_truth WHERE dq_run_id = :i ORDER BY anomaly_kind", i=dq_run_id)
    dimensions = (rules.groupby("dimension")[["rows_evaluated", "rows_failed"]].sum().reset_index())
    dimensions["pass_rate"] = 1 - dimensions["rows_failed"] / dimensions["rows_evaluated"]
    order = {name: i for i, name in enumerate(dq.DIMENSIONS)}
    dimensions = dimensions.sort_values("dimension", key=lambda s: s.map(order))
    rules["title"] = rules["rule_id"].map(lambda r: dq.RULES[r].title if r in dq.RULES else "")
    row = latest.iloc[0]
    return {
        "available": True,
        "acceptance_rate": clean(float(row["acceptance_rate"])),
        "rows_extracted": int(row["rows_extracted"]), "rows_quarantined": int(row["rows_quarantined"]),
        "dimensions": records(dimensions), "rules": records(rules),
        "ground_truth": records(truth), "trend": trend,
        "rules_total": int(len(dq.RULES)),
    }


def section_pipeline(engine: Any) -> Dict[str, Any]:
    runs = query(engine, "SELECT run_id::TEXT AS run_id, started_at, run_status AS status, duration_s, "
                         "tasks_ok, tasks_failed, tasks_skipped FROM ops.v_run_history LIMIT 5")
    if runs.empty:
        return {"available": False}
    last = runs.iloc[0]["run_id"]
    tasks = query(engine, "SELECT task_name AS name, status, attempt, duration_ms, detail "
                          "FROM ops.task_runs WHERE run_id = CAST(:r AS UUID) ORDER BY task_run_id", r=last)
    models = query(engine, "SELECT model_name AS name, kind, status, rows_affected AS rows_upserted "
                           "FROM ops.model_runs ORDER BY model_run_id DESC LIMIT 10")
    return {"available": True, "runs": records(runs), "last_run_tasks": records(tasks),
            "models": records(models)}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_snapshot(engine: Any) -> Dict[str, Any]:
    meta = section_meta(engine)
    as_of = date.fromisoformat(meta["window"]["end"]) + timedelta(days=1)
    return {
        "meta": meta,
        "headline": section_headline(engine, as_of),
        "stores": section_stores(engine),
        "products": section_products(engine),
        "fefo": section_fefo(engine, as_of),
        "suppliers": section_suppliers(engine),
        "velocity": section_velocity(engine),
        "weekday_profile": section_weekday(engine),
        "forecast": section_forecast(engine),
        "experiment": section_experiment(),
        "quality": section_quality(engine),
        "pipeline": section_pipeline(engine),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the web app's data snapshot.")
    parser.add_argument("--dsn", default=None, help="SQLAlchemy DSN (or RETAIL_DB_DSN).")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output file. Default: the sibling web repo if present, else data/processed/.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    out = args.out or (DEFAULT_OUT if DEFAULT_OUT.parent.is_dir() else FALLBACK_OUT)

    engine = dbutil.get_engine(args.dsn)
    try:
        snapshot = build_snapshot(engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not build the snapshot: {type(exc).__name__}: "
              f"{str(exc).splitlines()[0]}\n       Run the pipeline first: python src/pipeline.py",
              file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
                   encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"Wrote {out} ({kb:,.0f} KB)")
    print(f"  window {snapshot['meta']['window']['start']} .. {snapshot['meta']['window']['end']}"
          f" | {snapshot['meta']['series']} series | commit {snapshot['meta']['git_sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
