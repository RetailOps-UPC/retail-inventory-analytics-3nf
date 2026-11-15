# -*- coding: utf-8 -*-
"""
Tests for the forecasting core, on synthetic series with KNOWN structure.

A forecaster tested only on real-looking data can be wrong in ways nobody can
see. Here the right answer is known in advance: a perfectly weekly series must
be forecast almost perfectly, a flat series must not grow a phantom seasonality,
and the accuracy metrics must match hand-computed values.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import forecasting as fc

WEEK = np.array([10.0, 12.0, 11.0, 13.0, 30.0, 40.0, 20.0])     # Mon..Sun pattern


def weekly_series(weeks=9, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    y = np.tile(WEEK, weeks)
    return np.clip(y + rng.normal(0, noise, y.size), 0, None) if noise else y


# ---------------------------------------------------------------------------
# Metrics: hand-computed
# ---------------------------------------------------------------------------

def test_wape_bias_and_mae_match_hand_computed_values():
    actual = np.array([10.0, 20.0, 30.0])
    pred = np.array([12.0, 18.0, 30.0])
    assert fc.wape(actual, pred) == pytest.approx(4 / 60)       # (2+2+0)/60
    assert fc.bias(actual, pred) == pytest.approx(0.0)          # +2 and -2 cancel
    assert fc.mae(actual, pred) == pytest.approx(4 / 3)


def test_bias_is_positive_when_over_forecasting():
    assert fc.bias(np.array([10.0, 10.0]), np.array([15.0, 15.0])) == pytest.approx(0.5)


def test_wape_is_undefined_not_zero_when_there_was_no_demand():
    assert np.isnan(fc.wape(np.zeros(5), np.ones(5)))


# ---------------------------------------------------------------------------
# Individual models
# ---------------------------------------------------------------------------

def test_seasonal_naive_repeats_last_week():
    y = weekly_series(4)
    np.testing.assert_allclose(fc.seasonal_naive(y, 7), WEEK)
    np.testing.assert_allclose(fc.seasonal_naive(y, 10)[7:], WEEK[:3])


def test_weekday_mean_is_exact_on_a_perfectly_periodic_series():
    y = weekly_series(6)
    np.testing.assert_allclose(fc.weekday_mean(y, 14), np.tile(WEEK, 2))


def test_weekday_mean_averages_the_requested_number_of_weeks():
    y = np.concatenate([np.full(7, 100.0), np.full(7, 10.0), np.full(7, 20.0),
                        np.full(7, 30.0), np.full(7, 40.0)])
    # Last 4 weeks of every weekday: (10 + 20 + 30 + 40) / 4 = 25. The 100 week is excluded.
    np.testing.assert_allclose(fc.weekday_mean(y, 7), np.full(7, 25.0))


def test_moving_average_is_flat():
    out = fc.moving_average(np.arange(1.0, 15.0), 5)
    assert np.all(out == out[0]) and out[0] == pytest.approx(np.mean(np.arange(8.0, 15.0)))


def test_holt_winters_recovers_a_clean_weekly_pattern():
    y = weekly_series(8)
    forecast = fc.holt_winters(y, 7)
    assert fc.wape(WEEK, forecast) < 0.05


def test_holt_winters_never_forecasts_negative_demand():
    y = np.maximum(0, weekly_series(8) - 25)         # mostly zeros with weekend spikes
    assert np.all(fc.holt_winters(y, 14) >= 0)


def test_holt_winters_falls_back_gracefully_on_a_short_history():
    out = fc.holt_winters(np.arange(10.0), 7)         # under two seasons
    assert out.shape == (7,) and np.all(np.isfinite(out))


def test_models_do_not_invent_seasonality_for_a_flat_series():
    y = np.full(63, 20.0)
    for name, model in fc.MODELS.items():
        np.testing.assert_allclose(model(y, 7), np.full(7, 20.0), atol=1e-6, err_msg=name)


def test_every_model_returns_the_requested_horizon():
    y = weekly_series(6)
    for name, model in fc.MODELS.items():
        assert model(y, 11).shape == (11,), name


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def test_backtest_folds_use_only_data_before_each_window():
    """No look-ahead: a forecast for fold k must be unaffected by the future."""
    y = weekly_series(9, noise=2.0)
    folds = fc.rolling_origin_backtest(y)
    assert len(folds) == fc.BACKTEST_FOLDS

    first = folds[0]
    poisoned = y.copy()
    poisoned[first.origin:] = 9999.0                  # destroy everything after the origin
    replay = fc.rolling_origin_backtest(poisoned)[0]
    for model in fc.MODELS:
        np.testing.assert_allclose(first.predicted[model], replay.predicted[model],
                                   err_msg=f"{model} peeked at the future")


def test_backtest_windows_are_consecutive_and_end_at_the_last_observation():
    y = weekly_series(9, noise=1.0)
    folds = fc.rolling_origin_backtest(y)
    origins = [f.origin for f in folds]
    assert origins == sorted(origins)
    assert all(b - a == fc.BACKTEST_HORIZON for a, b in zip(origins, origins[1:]))
    assert origins[-1] + fc.BACKTEST_HORIZON == len(y)


def test_backtest_drops_folds_with_too_little_training_data():
    folds = fc.rolling_origin_backtest(weekly_series(5))           # 35 days
    assert all(f.origin >= fc.MIN_TRAIN for f in folds)
    assert len(folds) < fc.BACKTEST_FOLDS


def test_a_series_too_short_to_backtest_still_gets_a_forecast_and_no_accuracy_claim():
    result = fc.forecast_series(np.arange(1.0, 20.0), 7)
    assert result.n_folds == 0
    assert result.metrics == {}
    assert result.yhat.shape == (7,)


def test_champion_selection_prefers_seasonal_models_on_seasonal_data():
    folds = fc.rolling_origin_backtest(weekly_series(9, noise=1.0))
    assert fc.choose_champion(folds) not in ("moving_average",)


def test_champion_selection_ties_go_to_the_simpler_model():
    y = np.full(63, 20.0)                  # every model is perfect: pure tie
    assert fc.choose_champion(fc.rolling_origin_backtest(y)) == "moving_average"


# ---------------------------------------------------------------------------
# Whole-series behaviour
# ---------------------------------------------------------------------------

def test_the_shipped_model_beats_seasonal_naive_on_noisy_seasonal_data():
    wins, trials = 0, 12
    for seed in range(trials):
        y = weekly_series(9, noise=4.0, seed=seed)
        folds = fc.rolling_origin_backtest(y)
        _, late = fc.split_folds(folds)
        actual = np.concatenate([f.actual for f in late])
        naive = np.concatenate([f.predicted["seasonal_naive"] for f in late])
        ensemble = np.concatenate([f.predicted["ensemble"] for f in late])
        wins += fc.wape(actual, ensemble) < fc.wape(actual, naive)
    # Averaging noisy weeks must beat copying one noisy week, almost always.
    assert wins >= trials - 2


def test_forecast_intervals_always_bracket_the_point_forecast():
    result = fc.forecast_series(weekly_series(9, noise=3.0, seed=1), 14)
    assert np.all(result.yhat_lo <= result.yhat)
    assert np.all(result.yhat <= result.yhat_hi)
    assert np.all(result.yhat_lo >= 0)


def test_strategy_must_be_a_known_value():
    with pytest.raises(ValueError):
        fc.forecast_series(weekly_series(6), 7, strategy="magic")


def test_the_default_strategy_ships_the_ensemble():
    assert fc.forecast_series(weekly_series(9, noise=2.0), 7).champion == "ensemble"


def test_per_series_strategy_ships_the_backtest_winner():
    y = weekly_series(9, noise=2.0)
    shipped = fc.forecast_series(y, 7, strategy="per_series").champion
    assert shipped == fc.choose_champion(fc.rolling_origin_backtest(y))


# ---------------------------------------------------------------------------
# Network forecast and interval calibration
# ---------------------------------------------------------------------------

def make_sales(n_series=8, weeks=9, noise=3.0):
    start = date(2026, 1, 5)                    # a Monday
    rows = []
    for s in range(n_series):
        y = weekly_series(weeks, noise=noise, seed=s) * (1 + 0.2 * s)
        for i, units in enumerate(y):
            rows.append({"store_id": 1 + s % 2, "product_id": 1 + s,
                         "sale_date": start + timedelta(days=i), "units_sold": units})
    return pd.DataFrame(rows)


def test_missing_days_are_treated_as_zero_sales():
    sales = pd.DataFrame({"store_id": [1, 1], "product_id": [1, 1],
                          "sale_date": [date(2026, 1, 1), date(2026, 1, 4)],
                          "units_sold": [5.0, 7.0]})
    series = fc.build_series(sales)[(1, 1)]
    np.testing.assert_allclose(series, [5.0, 0.0, 0.0, 7.0])


def test_the_forecast_starts_the_day_after_the_last_sale():
    sales = make_sales()
    output = fc.forecast_network(sales, horizon=7)
    assert output.forecast_start == pd.to_datetime(sales["sale_date"]).max().date() + timedelta(days=1)
    frame = output.forecast_frame()
    assert frame["target_date"].min() == output.forecast_start
    assert frame.groupby(["store_id", "product_id"]).size().eq(7).all()


def test_pooled_calibration_brings_interval_coverage_close_to_nominal():
    """The reason calibration is pooled: per-series intervals run too narrow."""
    output = fc.forecast_network(make_sales(n_series=14, noise=4.0), horizon=7)
    assert output.calibration is not None
    coverage = output.summary()["coverage"]
    assert 0.68 <= coverage <= 0.92, f"80% interval covered {coverage:.0%}"


def test_summary_reports_held_out_accuracy_against_the_baseline():
    summary = fc.forecast_network(make_sales(n_series=6), horizon=7).summary()
    assert summary["scored_series"] == 6
    assert summary["holdout_wape"] < summary["baseline_wape"]
    assert set(summary["by_model"]) == set(fc.MODELS)


def test_backtest_frame_flags_exactly_one_champion_per_series():
    output = fc.forecast_network(make_sales(n_series=4), horizon=7)
    frame = output.backtest_frame()
    assert frame.groupby(["store_id", "product_id"])["is_champion"].sum().eq(1).all()


# ---------------------------------------------------------------------------
# Forward FEFO projection
# ---------------------------------------------------------------------------

D0 = date(2026, 6, 1)


def lot(batch_id, qty, days_to_expiry, cost=2.0):
    return fc.Lot(batch_id, 1, 1, qty, D0 + timedelta(days=days_to_expiry), cost)


def by_id(projected):
    return {p.lot.batch_id: p for p in projected}


def test_the_lot_that_expires_first_is_consumed_first():
    out = by_id(fc.project_fefo([lot(1, 100, 9), lot(2, 100, 1)], [6, 6, 6], D0))
    assert out[2].sold == pytest.approx(12.0)         # the near lot is drained for two days
    assert out[1].sold == pytest.approx(6.0)          # the far lot only after it is gone
    assert out[2].expired == pytest.approx(88.0)      # and the near lot's remainder expires at the end of day 2


def test_a_lot_that_will_sell_out_before_expiry_projects_no_waste():
    out = by_id(fc.project_fefo([lot(1, 10, 5)], [4, 4, 4], D0))
    assert out[1].sold == pytest.approx(10.0)
    assert out[1].expired == 0.0


def test_a_lot_with_more_stock_than_demand_projects_the_leftover_as_waste():
    out = by_id(fc.project_fefo([lot(1, 20, 1)], [6, 6, 6, 6], D0))
    assert out[1].sold == pytest.approx(12.0)         # sold on day 0 and day 1 only
    assert out[1].expired == pytest.approx(8.0)       # the rest expires at the end of day 1


def test_the_loss_is_valued_at_cost():
    projected = fc.project_fefo([lot(1, 20, 1, cost=3.5)], [6, 6], D0)[0]
    assert projected.expiry_value == pytest.approx(8.0 * 3.5)


def test_a_lot_already_past_expiry_is_written_off_in_full_at_once():
    out = by_id(fc.project_fefo([lot(1, 15, -1)], [10, 10], D0))
    assert out[1].expired == pytest.approx(15.0)
    assert out[1].sold == 0.0


def test_a_lot_expiring_beyond_the_horizon_reports_no_projected_loss():
    out = by_id(fc.project_fefo([lot(1, 500, 60)], [5, 5, 5], D0))
    assert out[1].expired == 0.0, "the forecast cannot see that far, so it must not claim a loss"


def test_projection_conserves_stock():
    lots = [lot(1, 30, 2), lot(2, 50, 4), lot(3, 40, 8)]
    forecast = [7, 9, 12, 6, 8, 10, 5, 9]
    out = fc.project_fefo(lots, forecast, D0)
    for p in out:
        assert p.sold + p.expired <= p.lot.qty + 1e-9
    assert sum(p.sold for p in out) <= sum(forecast) + 1e-9


def test_future_deliveries_cannot_change_an_existing_lots_fate():
    """FEFO consumes older lots first, so a later lot never competes with them."""
    base = by_id(fc.project_fefo([lot(1, 30, 3)], [5] * 6, D0))
    with_new = by_id(fc.project_fefo([lot(1, 30, 3), lot(9, 500, 20)], [5] * 6, D0))
    assert base[1].sold == with_new[1].sold
    assert base[1].expired == with_new[1].expired


@pytest.mark.parametrize("days,expected", [(0, "MARKDOWN_NOW"), (2, "MARKDOWN_NOW"),
                                           (3, "MARKDOWN_SOON"), (5, "MARKDOWN_SOON"),
                                           (6, "WATCH"), (12, "WATCH")])
def test_markdown_urgency_follows_days_to_expiry(days, expected):
    assert fc.markdown_action(days) == expected


# ---------------------------------------------------------------------------
# Replenishment
# ---------------------------------------------------------------------------

def make_forecast(daily=20.0, sigma=4.0, horizon=14):
    return fc.SeriesForecast(1, 1, "ensemble", np.full(horizon, daily),
                             np.full(horizon, daily * 0.7), np.full(horizon, daily * 1.3),
                             sigma, {}, 4)


@pytest.mark.parametrize("shelf,cover", [(2, 1), (3, 1), (14, 2), (21, 2), (45, 4),
                                         (60, 4), (180, 7), (None, 7)])
def test_review_period_shrinks_as_shelf_life_shrinks(shelf, cover):
    assert fc.review_period_days(shelf) == cover


def test_order_covers_expected_demand_plus_safety_stock_minus_on_hand():
    rec = fc.recommend_order(make_forecast(20, 4), shelf_life_days=14,
                             on_hand_qty=10, projected_expiry_qty=0, service_z=1.645)
    # cover = 2 days: expected 40, safety = 1.645 * 4 * sqrt(2) = 9.3, up-to = 49.3, minus 10 on hand
    assert rec.cover_days == 2
    assert rec.expected_demand == pytest.approx(40.0)
    assert rec.safety_stock == pytest.approx(1.645 * 4 * np.sqrt(2), abs=0.01)
    assert rec.recommended_order_qty == pytest.approx(np.ceil(49.31 - 10), abs=1.0)


def test_nothing_is_ordered_when_stock_already_covers_the_need():
    rec = fc.recommend_order(make_forecast(10, 1), 14, on_hand_qty=500, projected_expiry_qty=0)
    assert rec.recommended_order_qty == 0.0


def test_stock_that_will_expire_does_not_count_as_cover():
    kept = fc.recommend_order(make_forecast(20, 4), 14, on_hand_qty=60, projected_expiry_qty=0)
    doomed = fc.recommend_order(make_forecast(20, 4), 14, on_hand_qty=60, projected_expiry_qty=45)
    assert doomed.effective_on_hand == pytest.approx(15.0)
    assert doomed.recommended_order_qty > kept.recommended_order_qty


def test_a_higher_service_level_orders_more():
    low = fc.recommend_order(make_forecast(), 14, 0, 0, service_z=1.0)
    high = fc.recommend_order(make_forecast(), 14, 0, 0, service_z=2.5)
    assert high.recommended_order_qty > low.recommended_order_qty


def test_the_order_is_never_negative():
    rec = fc.recommend_order(make_forecast(20, 4), 14, on_hand_qty=0, projected_expiry_qty=0)
    assert rec.recommended_order_qty >= 0
    assert rec.shelf_life_capped is False      # a 2-day cycle fits inside a 14-day life


def test_the_shelf_life_cap_stops_demand_after_expiry_from_inflating_the_order():
    """A 4-day cover on a 2-day-life product: day 3 and 4 demand is unsellable."""
    capped = fc.recommend_order(make_forecast(20, 4), shelf_life_days=2, on_hand_qty=0,
                                projected_expiry_qty=0, cover_days=4)
    uncapped = fc.recommend_order(make_forecast(20, 4), shelf_life_days=14, on_hand_qty=0,
                                  projected_expiry_qty=0, cover_days=4)
    assert capped.shelf_life_capped is True
    assert capped.expected_demand == pytest.approx(40.0)       # 2 sellable days, not 4
    assert uncapped.expected_demand == pytest.approx(80.0)
    assert capped.recommended_order_qty < uncapped.recommended_order_qty
