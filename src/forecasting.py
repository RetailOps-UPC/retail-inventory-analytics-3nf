#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Demand forecasting, replenishment recommendations and expiry projection.

This module turns the sales history in the warehouse into three things a store
manager can act on:

1. **A demand forecast** per store and SKU for the next ``horizon`` days, with an
   80% prediction interval, produced by whichever of five models backtests best
   for THAT series.
2. **A replenishment recommendation**: how much to order so stock covers the days
   until the next delivery plus a safety margin, capped by what can be sold
   before the lot expires.
3. **A projected-expiry worklist**: a forward FEFO simulation that consumes the
   lots on hand against the forecast and reports which ones will still be on the
   shelf when they expire, i.e. what to mark down *now*, while it still has value.

The forecasting core is pure (numpy in, numpy out) and has no database
dependency, so it is unit-tested on synthetic series with known structure.

Models
------
``seasonal_naive``   this weekday, last week
``moving_average``   flat mean of the last 7 days
``weekday_mean``     mean of the same weekday over the last 4 weeks
``holt_winters``     additive level + damped trend + weekly seasonality, with its
                     smoothing parameters picked by grid search on in-sample error
``ensemble``         mean of ``weekday_mean`` and ``holt_winters``

Evaluation (what makes the numbers trustworthy)
-----------------------------------------------
* **Rolling-origin backtest.** Models are scored on several consecutive 7-day
  windows, each forecast using only the data before it. A single train/test
  split would make the ranking depend on one lucky or unlucky week.
* **Champion selection is scored on data it never saw.** The champion for each
  series is picked on the EARLY folds and reported on the LATE ones. Picking the
  best of five models and scoring it on the same data it was picked from would
  overstate its accuracy: with five candidates, one will look good by chance.
* **Interval calibration is checked out of sample.** Residual quantiles are
  estimated on the early folds and their empirical coverage is measured on the
  late folds. The nominal 80% is a claim; the measured coverage is the evidence.

Known limitation
----------------
The history is *sales*, not demand. On a day the shelf ran empty, units sold
understate what customers wanted, and the model learns that understatement.
Correcting it needs stock-out flags the warehouse does not store.

Usage
-----
    python src/forecasting.py                      # forecast from the warehouse
    python src/forecasting.py --horizon 14 --service-z 1.65
    python src/forecasting.py --no-db-write        # compute and write CSVs only

Dependencies: numpy, pandas; SQLAlchemy + psycopg2 only for the database path.
Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbutil  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "processed"

SEASON = 7
DEFAULT_HORIZON = 14
BACKTEST_HORIZON = 7
BACKTEST_FOLDS = 4
MIN_TRAIN = 28                 # first backtest origin needs four full weeks
MIN_HISTORY = 14               # below this, no model beyond a flat mean is justified
DEFAULT_SERVICE_Z = 1.65       # ~95% cycle service level
INTERVAL_QUANTILES = (0.10, 0.90)   # a nominal 80% interval

#: Simpler models win ties, so a complex model must EARN its place.
COMPLEXITY_ORDER = ("moving_average", "seasonal_naive", "weekday_mean",
                    "holt_winters", "ensemble")


# ===========================================================================
# 1. Forecasting models (pure functions: history in, h-step forecast out)
# ===========================================================================

def moving_average(y: np.ndarray, h: int, window: int = 7) -> np.ndarray:
    """Flat forecast: the mean of the last ``window`` observations."""
    return np.full(h, float(np.mean(y[-window:])))


def seasonal_naive(y: np.ndarray, h: int, m: int = SEASON) -> np.ndarray:
    """This weekday, last week. Needs at least one full season."""
    if len(y) < m:
        return moving_average(y, h)
    return y[len(y) - m + (np.arange(h) % m)].astype(float)


def weekday_mean(y: np.ndarray, h: int, weeks: int = 4, m: int = SEASON) -> np.ndarray:
    """Mean of the same weekday over up to the last ``weeks`` weeks."""
    n = len(y)
    if n < m:
        return moving_average(y, h)
    out = np.empty(h)
    for i in range(h):
        first_back = i // m + 1          # fewest weeks back that lands inside the history
        picks = [y[n + i - m * (first_back + k)]
                 for k in range(weeks) if n + i - m * (first_back + k) >= 0]
        out[i] = float(np.mean(picks)) if picks else float(np.mean(y))
    return out


_HW_ALPHAS = (0.05, 0.10, 0.20, 0.30)
_HW_BETAS = (0.0, 0.05)
_HW_GAMMAS = (0.05, 0.10, 0.20, 0.40)
_HW_PHI = 0.9                    # trend damping: keeps a short history from extrapolating wildly


def _hw_run(y: np.ndarray, alpha: float, beta: float, gamma: float,
            m: int = SEASON) -> Tuple[float, float, float, np.ndarray]:
    """One pass of damped additive Holt-Winters. Returns (sse, level, trend, season)."""
    n = len(y)
    level = float(np.mean(y[:m]))
    trend = float((np.mean(y[m:2 * m]) - np.mean(y[:m])) / m) if n >= 2 * m else 0.0
    season = (y[:m] - level).astype(float)

    sse = 0.0
    for t in range(m, n):
        s = season[t % m]
        forecast = level + _HW_PHI * trend + s
        sse += (y[t] - forecast) ** 2
        new_level = alpha * (y[t] - s) + (1 - alpha) * (level + _HW_PHI * trend)
        trend = beta * (new_level - level) + (1 - beta) * _HW_PHI * trend
        season[t % m] = gamma * (y[t] - new_level) + (1 - gamma) * s
        level = new_level
    return sse, level, trend, season


def holt_winters(y: np.ndarray, h: int, m: int = SEASON) -> np.ndarray:
    """Damped additive Holt-Winters, parameters chosen by in-sample one-step SSE.

    Falls back to ``weekday_mean`` with fewer than two full seasons, where the
    seasonal initialisation is not identifiable.
    """
    n = len(y)
    if n < 2 * m:
        return weekday_mean(y, h)

    best = None
    for alpha in _HW_ALPHAS:
        for beta in _HW_BETAS:
            for gamma in _HW_GAMMAS:
                sse, level, trend, season = _hw_run(y, alpha, beta, gamma, m)
                if best is None or sse < best[0]:
                    best = (sse, level, trend, season)
    _, level, trend, season = best

    out = np.empty(h)
    damp_sum = 0.0
    for i in range(h):
        damp_sum += _HW_PHI ** (i + 1)
        out[i] = level + damp_sum * trend + season[(n + i) % m]
    return np.clip(out, 0.0, None)


def ensemble(y: np.ndarray, h: int) -> np.ndarray:
    """Mean of the weekday mean and Holt-Winters: two different error profiles."""
    return (weekday_mean(y, h) + holt_winters(y, h)) / 2.0


MODELS: Dict[str, Callable[[np.ndarray, int], np.ndarray]] = {
    "moving_average": moving_average,
    "seasonal_naive": seasonal_naive,
    "weekday_mean": weekday_mean,
    "holt_winters": holt_winters,
    "ensemble": ensemble,
}


# ===========================================================================
# 2. Accuracy metrics
# ===========================================================================

def wape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Weighted absolute percentage error: sum|e| / sum(actual).

    Preferred to MAPE for retail, where daily demand includes zeros that make
    MAPE undefined, and where low-volume days would otherwise dominate.
    """
    total = float(np.sum(actual))
    return float(np.sum(np.abs(predicted - actual)) / total) if total > 0 else float("nan")


def bias(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Signed error as a share of demand. Positive means over-forecasting."""
    total = float(np.sum(actual))
    return float(np.sum(predicted - actual) / total) if total > 0 else float("nan")


def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(predicted - actual))) if len(actual) else float("nan")


# ===========================================================================
# 3. Rolling-origin backtest
# ===========================================================================

@dataclass
class Fold:
    """One backtest window: the actuals and every model's forecast of them."""

    origin: int
    actual: np.ndarray
    predicted: Dict[str, np.ndarray]


def rolling_origin_backtest(y: np.ndarray, horizon: int = BACKTEST_HORIZON,
                            n_folds: int = BACKTEST_FOLDS,
                            min_train: int = MIN_TRAIN) -> List[Fold]:
    """Forecast consecutive ``horizon``-day windows, each from data before it only.

    Origins are spaced one horizon apart and end at the last observation, so the
    most recent behaviour is always the last fold. Folds whose training set
    would be shorter than ``min_train`` are dropped rather than scored on too
    little history.
    """
    n = len(y)
    folds: List[Fold] = []
    for k in range(n_folds, 0, -1):
        origin = n - k * horizon
        if origin < min_train:
            continue
        train, actual = y[:origin], y[origin:origin + horizon]
        if len(actual) < horizon:
            continue
        folds.append(Fold(origin, actual.astype(float),
                          {name: fn(train, horizon) for name, fn in MODELS.items()}))
    return folds


def pooled(folds: Sequence[Fold], model: str) -> Tuple[np.ndarray, np.ndarray]:
    """Concatenate actuals and one model's forecasts across folds."""
    if not folds:
        return np.array([]), np.array([])
    return (np.concatenate([f.actual for f in folds]),
            np.concatenate([f.predicted[model] for f in folds]))


def choose_champion(folds: Sequence[Fold]) -> str:
    """Lowest pooled WAPE over ``folds``; ties go to the simpler model."""
    scores = {}
    for model in MODELS:
        actual, predicted = pooled(folds, model)
        score = wape(actual, predicted)
        scores[model] = score if not math.isnan(score) else float("inf")
    return min(scores, key=lambda m: (round(scores[m], 9), COMPLEXITY_ORDER.index(m)))


def split_folds(folds: Sequence[Fold]) -> Tuple[List[Fold], List[Fold]]:
    """Early folds (for selection and calibration) and late folds (for scoring)."""
    cut = max(1, len(folds) // 2)
    return list(folds[:cut]), list(folds[cut:])


# ===========================================================================
# 4. Per-series forecast
# ===========================================================================

#: "ensemble"   ship the fixed ensemble for every series (the default).
#: "per_series" ship, for each series, the model that backtested best on it.
STRATEGIES = ("ensemble", "per_series")
DEFAULT_STRATEGY = "ensemble"
#: Pooled residuals are only trusted for calibration with at least this many.
MIN_CALIBRATION_POINTS = 50


@dataclass
class SeriesForecast:
    store_id: int
    product_id: int
    champion: str                      # the model actually shipped for this series
    yhat: np.ndarray
    yhat_lo: np.ndarray
    yhat_hi: np.ndarray
    residual_sigma: float
    metrics: Dict[str, Dict[str, float]]
    n_folds: int
    # Out-of-sample evidence, filled when there are enough folds to hold out.
    holdout_actual: np.ndarray = field(default_factory=lambda: np.array([]))
    holdout_pred: np.ndarray = field(default_factory=lambda: np.array([]))
    holdout_baseline: np.ndarray = field(default_factory=lambda: np.array([]))
    holdout_selected: np.ndarray = field(default_factory=lambda: np.array([]))
    holdout_by_model: Dict[str, Tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    early_rel_resid: np.ndarray = field(default_factory=lambda: np.array([]))
    holdout_covered: int = 0
    holdout_points: int = 0


def _relative_residuals(actual: np.ndarray, predicted: np.ndarray) -> np.ndarray:
    """Error as a fraction of the forecast, floored at 1 unit to stay finite near zero."""
    return (actual - predicted) / np.maximum(predicted, 1.0)


def forecast_series(y: np.ndarray, horizon: int, *, store_id: int = 0,
                    product_id: int = 0,
                    strategy: str = DEFAULT_STRATEGY) -> SeriesForecast:
    """Backtest every model, choose what to ship, forecast ``horizon`` days ahead.

    Intervals set here come from this series' own residuals and are PROVISIONAL:
    with only a few dozen residuals they run too narrow. ``forecast_network``
    replaces them with intervals calibrated on residuals pooled across series.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")
    y = np.asarray(y, dtype=float)
    folds = rolling_origin_backtest(y)

    if not folds:
        # Too little history to backtest. Use the plain weekday mean when there
        # is a week to learn from, a flat mean otherwise, and say so: there is no
        # accuracy claim to make, so the metrics stay empty.
        name = "weekday_mean" if len(y) >= MIN_HISTORY else "moving_average"
        yhat = MODELS[name](y, horizon)
        sigma = max(1.0, 0.3 * float(np.mean(y))) if len(y) else 1.0
        z = 1.2816
        return SeriesForecast(store_id, product_id, name, yhat,
                              np.clip(yhat - z * sigma, 0, None), yhat + z * sigma,
                              sigma, {}, 0)

    shipped = "ensemble" if strategy == "ensemble" else choose_champion(folds)
    residuals = np.concatenate([f.actual - f.predicted[shipped] for f in folds])
    sigma = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else 1.0
    q_lo, q_hi = np.quantile(residuals, INTERVAL_QUANTILES)

    yhat = MODELS[shipped](y, horizon)
    result = SeriesForecast(
        store_id, product_id, shipped, yhat,
        np.minimum(np.clip(yhat + q_lo, 0.0, None), yhat),
        np.maximum(yhat + q_hi, yhat),
        max(sigma, 1e-9), {}, len(folds))

    for model in MODELS:
        actual, predicted = pooled(folds, model)
        result.metrics[model] = {"wape": wape(actual, predicted),
                                 "bias": bias(actual, predicted),
                                 "mae": mae(actual, predicted)}

    # Honest evaluation: choose and calibrate on the early folds, score on the
    # late ones. Needs at least two folds to have something to hold out.
    early, late = split_folds(folds)
    if early and late:
        selected = choose_champion(early)
        # What would have been shipped had we only had the early folds.
        shipped_then = "ensemble" if strategy == "ensemble" else selected
        actual, pred = pooled(late, shipped_then)
        result.holdout_actual, result.holdout_pred = actual, pred
        result.holdout_baseline = pooled(late, "seasonal_naive")[1]
        result.holdout_selected = pooled(late, selected)[1]
        result.holdout_by_model = {m: pooled(late, m) for m in MODELS}
        result.holdout_points = len(actual)
        result.early_rel_resid = np.concatenate([
            _relative_residuals(f.actual, f.predicted[shipped_then]) for f in early])
    return result


# ===========================================================================
# 5. Whole-network forecast
# ===========================================================================

def build_series(sales: pd.DataFrame) -> Dict[Tuple[int, int], np.ndarray]:
    """Dense daily series per (store, product) from sparse sales rows.

    The warehouse stores a row only on days something sold, so a missing day is
    a day of zero sales and is filled as such. Every series shares one calendar
    ending at the last day with any sales, so their forecasts line up.
    """
    if sales.empty:
        return {}
    frame = sales.copy()
    frame["sale_date"] = pd.to_datetime(frame["sale_date"])
    full = pd.date_range(frame["sale_date"].min(), frame["sale_date"].max(), freq="D")
    series = {}
    for (store_id, product_id), group in frame.groupby(["store_id", "product_id"]):
        daily = group.groupby("sale_date")["units_sold"].sum().reindex(full, fill_value=0.0)
        series[(int(store_id), int(product_id))] = daily.to_numpy(dtype=float)
    return series


def calibrate_intervals(series: Sequence[SeriesForecast]) -> Optional[Tuple[float, float]]:
    """Replace each series' interval with one calibrated on residuals pooled across series.

    A single series offers a few dozen residuals, too few to estimate a 10th and
    90th percentile: the resulting intervals are systematically too narrow (a
    nominal 80% interval measured at 66% coverage on this data). Pooling the
    RELATIVE residuals of every series, taken from the early folds only, gives
    hundreds of points and a stable estimate. The relative form (error divided
    by the forecast) lets a high-volume SKU and a low-volume one share a scale.

    Coverage is then measured on the late folds, which calibration never saw.
    Returns the (lower, upper) relative quantiles, or None if there was too
    little data to calibrate and the provisional intervals were kept.
    """
    pooled_resid = np.concatenate([s.early_rel_resid for s in series
                                   if s.early_rel_resid.size] or [np.array([])])
    if pooled_resid.size < MIN_CALIBRATION_POINTS:
        return None
    q_lo, q_hi = (float(q) for q in np.quantile(pooled_resid, INTERVAL_QUANTILES))

    for s in series:
        scale = np.maximum(s.yhat, 1.0)
        s.yhat_lo = np.minimum(np.clip(s.yhat + q_lo * scale, 0.0, None), s.yhat)
        s.yhat_hi = np.maximum(s.yhat + q_hi * scale, s.yhat)
        if s.holdout_points:
            hscale = np.maximum(s.holdout_pred, 1.0)
            low = s.holdout_pred + q_lo * hscale
            high = s.holdout_pred + q_hi * hscale
            s.holdout_covered = int(np.sum((s.holdout_actual >= low)
                                           & (s.holdout_actual <= high)))
    return q_lo, q_hi


@dataclass
class ForecastOutput:
    forecast_start: date
    horizon: int
    history_days: int
    series: List[SeriesForecast]
    strategy: str = DEFAULT_STRATEGY
    calibration: Optional[Tuple[float, float]] = None

    def forecast_frame(self) -> pd.DataFrame:
        rows = []
        for s in self.series:
            for i in range(self.horizon):
                rows.append({"generated_on": self.forecast_start, "store_id": s.store_id,
                             "product_id": s.product_id,
                             "target_date": self.forecast_start + timedelta(days=i),
                             "horizon_day": i + 1, "model": s.champion,
                             "yhat": round(float(s.yhat[i]), 2),
                             "yhat_lo": round(float(s.yhat_lo[i]), 2),
                             "yhat_hi": round(float(s.yhat_hi[i]), 2)})
        return pd.DataFrame(rows)

    def backtest_frame(self) -> pd.DataFrame:
        rows = []
        for s in self.series:
            for model, m in s.metrics.items():
                rows.append({"generated_on": self.forecast_start, "store_id": s.store_id,
                             "product_id": s.product_id, "model": model,
                             "folds": s.n_folds,
                             "wape": None if math.isnan(m["wape"]) else round(m["wape"], 4),
                             "bias": None if math.isnan(m["bias"]) else round(m["bias"], 4),
                             "mae": None if math.isnan(m["mae"]) else round(m["mae"], 3),
                             "is_champion": model == s.champion})
        return pd.DataFrame(rows)

    def summary(self) -> Dict[str, Any]:
        """Network accuracy on held-out folds, and what per-series selection would add."""
        scored = [s for s in self.series if s.holdout_points]
        if not scored:
            return {"holdout_wape": None, "baseline_wape": None, "selection_wape": None,
                    "coverage": None, "by_model": {}, "scored_series": 0,
                    "strategy": self.strategy}

        def cat(attr: str) -> np.ndarray:
            return np.concatenate([getattr(s, attr) for s in scored])

        actual = cat("holdout_actual")
        by_model = {m: wape(np.concatenate([s.holdout_by_model[m][0] for s in scored]),
                            np.concatenate([s.holdout_by_model[m][1] for s in scored]))
                    for m in MODELS}
        points = sum(s.holdout_points for s in scored)
        return {
            "strategy": self.strategy,
            "holdout_wape": wape(actual, cat("holdout_pred")),
            "baseline_wape": wape(actual, cat("holdout_baseline")),
            "selection_wape": wape(actual, cat("holdout_selected")),
            "coverage": (sum(s.holdout_covered for s in scored) / points
                         if self.calibration else None),
            "by_model": by_model, "scored_series": len(scored),
            "champion_counts": {m: sum(1 for s in self.series if s.champion == m)
                                for m in MODELS}}


def forecast_network(sales: pd.DataFrame, horizon: int = DEFAULT_HORIZON,
                     strategy: str = DEFAULT_STRATEGY) -> ForecastOutput:
    """Forecast every series. The forecast starts the day after the last sale."""
    series = build_series(sales)
    last = pd.to_datetime(sales["sale_date"]).max().date() if not sales.empty else date.today()
    history = next(iter(series.values())).shape[0] if series else 0
    results = [forecast_series(y, horizon, store_id=s, product_id=p, strategy=strategy)
               for (s, p), y in sorted(series.items())]
    calibration = calibrate_intervals(results)
    return ForecastOutput(last + timedelta(days=1), horizon, history, results,
                          strategy, calibration)


# ===========================================================================
# 6. Forward FEFO projection: which lots will expire unsold?
# ===========================================================================

@dataclass
class Lot:
    batch_id: int
    store_id: int
    product_id: int
    qty: float
    expiration_date: date
    unit_cost: float


@dataclass
class ProjectedLot:
    lot: Lot
    sold: float
    expired: float

    @property
    def expiry_value(self) -> float:
        return self.expired * self.lot.unit_cost


def project_fefo(lots: Sequence[Lot], daily_forecast: Sequence[float],
                 start: date) -> List[ProjectedLot]:
    """Consume ``lots`` against the forecast in strict FEFO order.

    Each day, demand is served from the lot that expires soonest. At the end of
    a lot's expiration day, whatever is left is written off. The result is what
    each lot will contribute in sales and in waste if demand follows the
    forecast and nothing is marked down.

    FEFO makes this independent of future deliveries: a lot received later
    expires later, so it is consumed after every lot already on the shelf and
    cannot change their outcome.

    A lot already past its expiry on ``start`` is written off in full at once.
    A lot expiring beyond the forecast horizon reports zero projected expiry,
    because the forecast cannot see that far: absence of a projected loss is
    not evidence there will be none.
    """
    remaining = {l.batch_id: float(l.qty) for l in lots}
    sold = {l.batch_id: 0.0 for l in lots}
    expired = {l.batch_id: 0.0 for l in lots}
    order = sorted(lots, key=lambda l: (l.expiration_date, l.batch_id))

    for lot in order:
        if lot.expiration_date < start:
            expired[lot.batch_id], remaining[lot.batch_id] = remaining[lot.batch_id], 0.0

    for offset, demand in enumerate(daily_forecast):
        today = start + timedelta(days=offset)
        need = float(demand)
        for lot in order:
            if need <= 0:
                break
            if lot.expiration_date < today or remaining[lot.batch_id] <= 0:
                continue
            take = min(remaining[lot.batch_id], need)
            remaining[lot.batch_id] -= take
            sold[lot.batch_id] += take
            need -= take
        for lot in order:
            if lot.expiration_date == today and remaining[lot.batch_id] > 0:
                expired[lot.batch_id] += remaining[lot.batch_id]
                remaining[lot.batch_id] = 0.0

    return [ProjectedLot(l, sold[l.batch_id], expired[l.batch_id]) for l in order]


def markdown_action(days_to_expiry: int) -> str:
    """How urgently a lot projected to expire needs a price action."""
    if days_to_expiry <= 2:
        return "MARKDOWN_NOW"
    if days_to_expiry <= 5:
        return "MARKDOWN_SOON"
    return "WATCH"


# ===========================================================================
# 7. Replenishment recommendation
# ===========================================================================

def review_period_days(shelf_life_days: Optional[int]) -> int:
    """Days between deliveries, driven by shelf life: the shorter, the more often.

    The same cadence the store simulation uses, so the recommendation is
    consistent with how this network is actually replenished.
    """
    if shelf_life_days is None:
        return 7
    if shelf_life_days <= 3:
        return 1
    if shelf_life_days <= 21:
        return 2
    if shelf_life_days <= 60:
        return 4
    return 7


@dataclass
class Recommendation:
    store_id: int
    product_id: int
    cover_days: int
    expected_demand: float
    safety_stock: float
    order_up_to: float
    on_hand_qty: float
    projected_expiry_qty: float
    effective_on_hand: float
    recommended_order_qty: float
    shelf_life_capped: bool


def recommend_order(forecast: SeriesForecast, shelf_life_days: Optional[int],
                    on_hand_qty: float, projected_expiry_qty: float,
                    service_z: float = DEFAULT_SERVICE_Z,
                    cover_days: Optional[int] = None) -> Recommendation:
    """Order-up-to level over the cover period, net of stock that will expire.

    cover = days until the next delivery (from the shelf life);
    expected = forecast demand over the cover, capped at the shelf life because
    nothing can be sold after the lot expires;
    safety = z * residual std * sqrt(cover);
    order = order-up-to level minus the stock that is genuinely sellable, i.e.
    on hand minus the part the FEFO projection says will expire first.
    """
    # By default the cover is the delivery cycle, which never exceeds the shelf
    # life. ``cover_days`` lets a caller plan a longer cycle (a skipped
    # delivery, a holiday), and that is when the shelf-life cap starts to bind.
    cover = cover_days if cover_days is not None else review_period_days(shelf_life_days)
    sellable_days = cover if shelf_life_days is None else min(cover, shelf_life_days)
    capped = shelf_life_days is not None and shelf_life_days < cover

    expected = float(np.sum(forecast.yhat[:sellable_days]))
    safety = service_z * forecast.residual_sigma * math.sqrt(sellable_days)
    up_to = expected + safety
    effective = max(0.0, on_hand_qty - projected_expiry_qty)
    order = max(0.0, math.ceil(up_to - effective))
    return Recommendation(forecast.store_id, forecast.product_id, cover,
                          round(expected, 2), round(safety, 2), round(up_to, 2),
                          round(on_hand_qty, 2), round(projected_expiry_qty, 2),
                          round(effective, 2), float(order), capped)


# ===========================================================================
# 8. Database I/O
# ===========================================================================

def load_sales_history(engine: Any) -> pd.DataFrame:
    """Sales from the star schema, with the business keys the forecast needs."""
    return pd.read_sql(
        "SELECT f.store_key AS store_id, p.product_id, d.full_date AS sale_date, "
        "       f.units_sold "
        "  FROM mart.fact_sales_daily f "
        "  JOIN mart.dim_product p ON p.product_key = f.product_key "
        "  JOIN mart.dim_date    d ON d.date_key    = f.date_key", engine)


def load_products(engine: Any) -> pd.DataFrame:
    return pd.read_sql("SELECT product_id, sku_code, shelf_life_days FROM products", engine)


def load_lots(engine: Any, as_of: date) -> List[Lot]:
    """ACTIVE, unexpired lots with their true unit cost (receipt cost, else catalog)."""
    frame = pd.read_sql(
        "SELECT b.batch_id, b.store_id, b.product_id, b.current_qty, "
        "       b.expiration_date, "
        "       COALESCE(gi.unit_purchase_cost, pr.unit_cost) AS unit_cost "
        "  FROM inventory_batches b "
        "  JOIN products pr ON pr.product_id = b.product_id "
        "  LEFT JOIN goods_receipt_items gi ON gi.grn_item_id = b.grn_item_id "
        " WHERE b.batch_status = 'ACTIVE' AND b.current_qty > 0 "
        "   AND b.expiration_date >= %(as_of)s", engine, params={"as_of": as_of})
    return [Lot(int(r.batch_id), int(r.store_id), int(r.product_id),
                float(r.current_qty), pd.Timestamp(r.expiration_date).date(),
                float(r.unit_cost)) for r in frame.itertuples(index=False)]


@dataclass
class PlanOutput:
    forecast: ForecastOutput
    recommendations: List[Recommendation]
    projections: List[ProjectedLot]
    start: date


def plan(sales: pd.DataFrame, products: pd.DataFrame, lots: Sequence[Lot],
         horizon: int = DEFAULT_HORIZON,
         service_z: float = DEFAULT_SERVICE_Z,
         strategy: str = DEFAULT_STRATEGY) -> PlanOutput:
    """Forecast, then project expiry and size the orders. No database access."""
    output = forecast_network(sales, horizon, strategy)
    shelf = {int(r.product_id): (None if pd.isna(r.shelf_life_days)
                                 else int(r.shelf_life_days))
             for r in products.itertuples(index=False)}
    by_series: Dict[Tuple[int, int], SeriesForecast] = {
        (s.store_id, s.product_id): s for s in output.series}

    lots_by_series: Dict[Tuple[int, int], List[Lot]] = {}
    for lot in lots:
        lots_by_series.setdefault((lot.store_id, lot.product_id), []).append(lot)

    projections: List[ProjectedLot] = []
    recommendations: List[Recommendation] = []
    for key, series in by_series.items():
        series_lots = lots_by_series.get(key, [])
        projected = project_fefo(series_lots, series.yhat, output.forecast_start)
        projections.extend(projected)
        on_hand = sum(l.qty for l in series_lots)
        expiring = sum(p.expired for p in projected)
        recommendations.append(recommend_order(series, shelf.get(key[1]), on_hand,
                                               expiring, service_z))
    return PlanOutput(output, recommendations, projections, output.forecast_start)


def _upsert(connection: Any, table: str, rows: List[Dict[str, Any]],
            conflict: Sequence[str]) -> None:
    """INSERT ... ON CONFLICT DO UPDATE for a list of dicts sharing the same keys."""
    from sqlalchemy import text
    if not rows:
        return
    columns = list(rows[0].keys())
    updates = [c for c in columns if c not in conflict]
    statement = (f"INSERT INTO {table} ({', '.join(columns)}) "
                 f"VALUES ({', '.join(':' + c for c in columns)}) "
                 f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET "
                 + ", ".join(f"{c} = EXCLUDED.{c}" for c in updates))
    for start in range(0, len(rows), 1000):
        connection.execute(text(statement), rows[start:start + 1000])


def _py(value: Any) -> Any:
    """numpy / pandas scalars to plain Python, NaN to None, for the driver."""
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if math.isnan(float(value)) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.date()
    return value


def persist(engine: Any, result: PlanOutput, *, run_id: Optional[str],
            parameters: Dict[str, Any]) -> None:
    """Write one forecast run. Re-running the same day replaces that day's rows."""
    from sqlalchemy import text

    start = result.start
    summary = result.forecast.summary()
    with engine.begin() as connection:
        # Replace the day's run wholesale so a re-run cannot leave stale rows
        # from a series that has since disappeared.
        for table in ("ml.demand_forecast", "ml.forecast_backtest",
                      "ml.replenishment_recommendation", "ml.projected_expiry"):
            connection.execute(text(f"DELETE FROM {table} WHERE generated_on = :d"),
                               {"d": start})
        connection.execute(text("DELETE FROM ml.forecast_runs WHERE generated_on = :d"),
                           {"d": start})

        connection.execute(text(
            "INSERT INTO ml.forecast_runs (run_id, generated_on, horizon_days, "
            "  history_days, series_count, holdout_wape, baseline_wape, "
            "  interval_coverage, parameters) "
            "VALUES (CAST(:run_id AS UUID), :d, :h, :hist, :n, :hw, :bw, :cov, "
            "        CAST(:params AS JSONB))"),
            {"run_id": run_id, "d": start, "h": result.forecast.horizon,
             "hist": result.forecast.history_days, "n": len(result.forecast.series),
             "hw": _py(summary["holdout_wape"]), "bw": _py(summary["baseline_wape"]),
             "cov": _py(summary["coverage"]),
             "params": json.dumps(parameters)})

        for frame, table, key in (
                (result.forecast.forecast_frame(), "ml.demand_forecast",
                 ("generated_on", "store_id", "product_id", "target_date")),
                (result.forecast.backtest_frame(), "ml.forecast_backtest",
                 ("generated_on", "store_id", "product_id", "model"))):
            rows = [{k: _py(v) for k, v in r.items()} for r in frame.to_dict("records")]
            _upsert(connection, table, rows, key)

        recs = [{"generated_on": start, "store_id": r.store_id, "product_id": r.product_id,
                 "cover_days": r.cover_days, "expected_demand": r.expected_demand,
                 "safety_stock": r.safety_stock, "order_up_to": r.order_up_to,
                 "on_hand_qty": r.on_hand_qty,
                 "projected_expiry_qty": r.projected_expiry_qty,
                 "effective_on_hand": r.effective_on_hand,
                 "recommended_order_qty": r.recommended_order_qty,
                 "shelf_life_capped": r.shelf_life_capped}
                for r in result.recommendations]
        _upsert(connection, "ml.replenishment_recommendation", recs,
                ("generated_on", "store_id", "product_id"))

        expiring = []
        for p in result.projections:
            if p.expired <= 1e-9:
                continue
            days = (p.lot.expiration_date - start).days
            expiring.append({"generated_on": start, "batch_id": p.lot.batch_id,
                             "store_id": p.lot.store_id, "product_id": p.lot.product_id,
                             "expiration_date": p.lot.expiration_date,
                             "days_to_expiry": days, "qty_now": round(p.lot.qty, 2),
                             "projected_sold": round(p.sold, 2),
                             "projected_expiry_qty": round(p.expired, 2),
                             "projected_expiry_value": round(p.expiry_value, 2),
                             "action": markdown_action(max(days, 0))})
        _upsert(connection, "ml.projected_expiry", expiring, ("generated_on", "batch_id"))


# ===========================================================================
# 9. CLI
# ===========================================================================

def write_csvs(result: PlanOutput, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, frame in (
            ("demand_forecast.csv", result.forecast.forecast_frame()),
            ("forecast_backtest.csv", result.forecast.backtest_frame()),
            ("replenishment_recommendation.csv", pd.DataFrame(
                [r.__dict__ for r in result.recommendations])),
            ("projected_expiry.csv", pd.DataFrame(
                [{"batch_id": p.lot.batch_id, "store_id": p.lot.store_id,
                  "product_id": p.lot.product_id,
                  "expiration_date": p.lot.expiration_date, "qty_now": p.lot.qty,
                  "projected_sold": round(p.sold, 2),
                  "projected_expiry_qty": round(p.expired, 2),
                  "projected_expiry_value": round(p.expiry_value, 2)}
                 for p in result.projections if p.expired > 1e-9]))):
        path = out_dir / name
        frame.to_csv(path, index=False, encoding="utf-8")
        written.append(path)
    return written


def print_summary(result: PlanOutput) -> None:
    summary = result.forecast.summary()
    fc = result.forecast
    print("=" * 74)
    print(" DEMAND FORECAST & REPLENISHMENT")
    print("=" * 74)
    print(f" Series forecast     : {len(fc.series)} (store x SKU), {fc.history_days} days of history")
    print(f" Forecast window     : {fc.forecast_start} -> "
          f"{fc.forecast_start + timedelta(days=fc.horizon - 1)} ({fc.horizon} days)")
    if summary["holdout_wape"] is None:
        print(" Accuracy            : not enough history to hold out folds")
    else:
        print(f" Shipped strategy    : {summary['strategy']}")
        print(" Accuracy on HELD-OUT folds (nothing below was chosen or calibrated on them)")
        print(f"   shipped WAPE      : {100 * summary['holdout_wape']:.1f}%")
        print(f"   seasonal-naive    : {100 * summary['baseline_wape']:.1f}%   (the bar to beat)")
        print(f"   per-series pick   : {100 * summary['selection_wape']:.1f}%   "
              f"(best model chosen separately per series)")
        if summary["coverage"] is not None:
            print(f"   80% interval cov. : {100 * summary['coverage']:.1f}%   (nominal 80%)")
        print(" Every fixed model on the same held-out folds:")
        for model, score in sorted(summary["by_model"].items(), key=lambda kv: kv[1]):
            print(f"   {model:<16} WAPE {100 * score:5.1f}%")
    expiring = [p for p in result.projections if p.expired > 1e-9]
    value = sum(p.expiry_value for p in expiring)
    orders = [r for r in result.recommendations if r.recommended_order_qty > 0]
    print("-" * 74)
    print(f" Lots projected to expire unsold : {len(expiring)} (S/ {value:,.2f} at cost)")
    print(f" SKUs needing an order           : {len(orders)} of {len(result.recommendations)}")
    print("=" * 74)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forecast demand and size replenishment.")
    parser.add_argument("--dsn", default=None, help="SQLAlchemy DSN (or RETAIL_DB_DSN).")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--service-z", type=float, default=DEFAULT_SERVICE_Z,
                        help=f"Safety-stock z (default {DEFAULT_SERVICE_Z}, ~95%% service).")
    parser.add_argument("--strategy", choices=STRATEGIES, default=DEFAULT_STRATEGY,
                        help="ensemble: one fixed model for every series (default). "
                             "per_series: the best-backtesting model for each series.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--no-db-write", action="store_true",
                        help="Compute and write CSVs only; do not write to the ml schema.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.horizon < 7:
        print("ERROR: --horizon must be at least 7", file=sys.stderr)
        return 2

    engine = dbutil.get_engine(args.dsn)
    try:
        sales = load_sales_history(engine)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not read mart.fact_sales_daily: {type(exc).__name__}: "
              f"{str(exc).splitlines()[0]}\n       Run the models first: "
              f"python src/model_runner.py", file=sys.stderr)
        return 1
    if sales.empty:
        print("ERROR: no sales history in the warehouse.", file=sys.stderr)
        return 1

    start = pd.to_datetime(sales["sale_date"]).max().date() + timedelta(days=1)
    result = plan(sales, load_products(engine), load_lots(engine, start),
                  horizon=args.horizon, service_z=args.service_z,
                  strategy=args.strategy)
    write_csvs(result, args.out)
    print_summary(result)

    if not args.no_db_write:
        dbutil.ensure_schemas(engine)
        persist(engine, result, run_id=os.environ.get("PIPELINE_RUN_ID"),
                parameters={"horizon": args.horizon, "service_z": args.service_z,
                            "strategy": args.strategy})
        print(" Persisted to ml.* (generated_on = %s)" % result.start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
