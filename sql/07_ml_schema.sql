-- ============================================================================
-- ML SCHEMA: DEMAND FORECASTS, REPLENISHMENT AND EXPIRY PROJECTIONS
-- Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
-- Engine: PostgreSQL 15+
-- Purpose: Persist the outputs of src/forecasting.py so they are queryable,
--          comparable across runs and joinable to the star schema, instead of
--          living in a CSV nobody can join to anything.
--
-- Every table is keyed by generated_on, so a forecast is never overwritten by
-- the next one: you can ask "what did we predict last Tuesday, and how did it
-- turn out?". Re-running on the same day replaces that day's forecast.
--
-- Fully idempotent (IF NOT EXISTS): applied on pipeline start.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS ml;

-- One row per forecasting run: the headline accuracy numbers.
CREATE TABLE IF NOT EXISTS ml.forecast_runs (
    forecast_run_id   BIGSERIAL PRIMARY KEY,
    run_id            UUID,
    generated_on      DATE NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    horizon_days      INTEGER NOT NULL,
    history_days      INTEGER NOT NULL,
    series_count      INTEGER NOT NULL,
    -- Champion WAPE on folds the model selection never saw. The honest number:
    -- scoring the champion on the folds it was picked from would flatter it.
    holdout_wape      NUMERIC(8, 4),
    baseline_wape     NUMERIC(8, 4),      -- seasonal naive, same folds
    interval_coverage NUMERIC(6, 4),      -- nominal 0.80
    parameters        JSONB NOT NULL DEFAULT '{}'::JSONB,
    CONSTRAINT uq_forecast_runs_day UNIQUE (generated_on)
);

CREATE TABLE IF NOT EXISTS ml.demand_forecast (
    generated_on  DATE NOT NULL,
    store_id      INTEGER NOT NULL REFERENCES stores (store_id),
    product_id    INTEGER NOT NULL REFERENCES products (product_id),
    target_date   DATE NOT NULL,
    horizon_day   INTEGER NOT NULL CHECK (horizon_day >= 1),
    model         TEXT NOT NULL,
    yhat          NUMERIC(10, 2) NOT NULL CHECK (yhat >= 0),
    yhat_lo       NUMERIC(10, 2) NOT NULL CHECK (yhat_lo >= 0),
    yhat_hi       NUMERIC(10, 2) NOT NULL,
    PRIMARY KEY (generated_on, store_id, product_id, target_date),
    CONSTRAINT chk_forecast_band CHECK (yhat_lo <= yhat AND yhat <= yhat_hi)
);
CREATE INDEX IF NOT EXISTS idx_demand_forecast_target
    ON ml.demand_forecast (target_date, store_id, product_id);

-- Backtest accuracy for every model on every series.
CREATE TABLE IF NOT EXISTS ml.forecast_backtest (
    generated_on  DATE NOT NULL,
    store_id      INTEGER NOT NULL REFERENCES stores (store_id),
    product_id    INTEGER NOT NULL REFERENCES products (product_id),
    model         TEXT NOT NULL,
    folds         INTEGER NOT NULL,
    wape          NUMERIC(8, 4),
    bias          NUMERIC(8, 4),           -- positive = over-forecast
    mae           NUMERIC(10, 3),
    is_champion   BOOLEAN NOT NULL,
    PRIMARY KEY (generated_on, store_id, product_id, model)
);

CREATE TABLE IF NOT EXISTS ml.replenishment_recommendation (
    generated_on          DATE NOT NULL,
    store_id              INTEGER NOT NULL REFERENCES stores (store_id),
    product_id            INTEGER NOT NULL REFERENCES products (product_id),
    cover_days            INTEGER NOT NULL,
    expected_demand       NUMERIC(10, 2) NOT NULL,
    safety_stock          NUMERIC(10, 2) NOT NULL,
    order_up_to           NUMERIC(10, 2) NOT NULL,
    on_hand_qty           NUMERIC(10, 2) NOT NULL,
    projected_expiry_qty  NUMERIC(10, 2) NOT NULL,
    effective_on_hand     NUMERIC(10, 2) NOT NULL,
    recommended_order_qty NUMERIC(10, 2) NOT NULL CHECK (recommended_order_qty >= 0),
    shelf_life_capped     BOOLEAN NOT NULL,
    PRIMARY KEY (generated_on, store_id, product_id)
);

-- Lots the forecast says will expire unsold: the markdown worklist.
CREATE TABLE IF NOT EXISTS ml.projected_expiry (
    generated_on           DATE NOT NULL,
    batch_id               INTEGER NOT NULL REFERENCES inventory_batches (batch_id),
    store_id               INTEGER NOT NULL REFERENCES stores (store_id),
    product_id             INTEGER NOT NULL REFERENCES products (product_id),
    expiration_date        DATE NOT NULL,
    days_to_expiry         INTEGER NOT NULL,
    qty_now                NUMERIC(10, 2) NOT NULL,
    projected_sold         NUMERIC(10, 2) NOT NULL,
    projected_expiry_qty   NUMERIC(10, 2) NOT NULL CHECK (projected_expiry_qty > 0),
    projected_expiry_value NUMERIC(12, 2) NOT NULL,
    action                 TEXT NOT NULL CHECK (action IN ('MARKDOWN_NOW', 'MARKDOWN_SOON', 'WATCH')),
    PRIMARY KEY (generated_on, batch_id)
);

-- ----------------------------------------------------------------------------
-- Views
-- ----------------------------------------------------------------------------

-- Accuracy per model for the latest run, ordered best first.
CREATE OR REPLACE VIEW ml.v_forecast_accuracy AS
SELECT b.model,
       COUNT(*)                                          AS series,
       COUNT(*) FILTER (WHERE b.is_champion)             AS series_won,
       ROUND(AVG(b.wape), 4)                             AS mean_wape,
       ROUND(AVG(b.bias), 4)                             AS mean_bias
  FROM ml.forecast_backtest b
 WHERE b.generated_on = (SELECT MAX(generated_on) FROM ml.forecast_runs)
 GROUP BY b.model
 ORDER BY mean_wape;

-- The markdown worklist for the latest run, biggest loss first.
CREATE OR REPLACE VIEW ml.v_markdown_worklist AS
SELECT e.generated_on,
       s.store_code,
       p.sku_code,
       p.product_name,
       e.expiration_date,
       e.days_to_expiry,
       e.qty_now,
       e.projected_expiry_qty,
       e.projected_expiry_value,
       e.action
  FROM ml.projected_expiry e
  JOIN stores   s ON s.store_id   = e.store_id
  JOIN products p ON p.product_id = e.product_id
 WHERE e.generated_on = (SELECT MAX(generated_on) FROM ml.forecast_runs)
 ORDER BY e.projected_expiry_value DESC;
