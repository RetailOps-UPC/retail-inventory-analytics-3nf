-- model: mart_views
-- kind: view
-- target: mart.v_store_day_kpi
-- depends_on: fact_sales_daily, fact_waste, fact_receipt_line, fact_stock_position
-- description: Business-facing views over the star schema. Power BI and ad-hoc
--              analysis connect here instead of joining facts and dimensions by hand.
--
-- Views, not tables: they are cheap definitions over facts that are already
-- incremental, so there is nothing to refresh and nothing to go stale.

CREATE SCHEMA IF NOT EXISTS mart;

-- ----------------------------------------------------------------------------
-- Store x day: revenue, margin and waste side by side.
-- Sales and waste are aggregated to the same grain BEFORE joining. Joining the
-- two facts row-to-row would repeat each waste row once per product sold and
-- silently inflate the loss: the classic fan-out trap.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_store_day_kpi AS
WITH sales AS (
    SELECT f.date_key, f.store_key,
           SUM(f.units_sold)         AS units_sold,
           SUM(f.gross_revenue)      AS revenue,
           SUM(f.gross_margin)       AS gross_margin
      FROM mart.fact_sales_daily f
     GROUP BY f.date_key, f.store_key
),
waste AS (
    SELECT w.date_key, w.store_key,
           SUM(w.total_loss_amount)                                  AS waste_cost,
           SUM(w.total_loss_amount) FILTER (WHERE w.is_expiry)       AS expiry_cost,
           COUNT(*) FILTER (WHERE NOT w.is_authorized
                              AND w.total_loss_amount > 50)          AS unauthorized_material_events
      FROM mart.fact_waste w
     GROUP BY w.date_key, w.store_key
)
SELECT d.full_date,
       d.iso_weekday,
       d.weekday_name,
       d.is_weekend,
       s.store_code,
       s.store_name,
       sa.units_sold,
       sa.revenue,
       sa.gross_margin,
       COALESCE(wa.waste_cost, 0)                                    AS waste_cost,
       COALESCE(wa.expiry_cost, 0)                                   AS expiry_cost,
       COALESCE(wa.unauthorized_material_events, 0)                  AS unauthorized_material_events,
       ROUND(100.0 * COALESCE(wa.waste_cost, 0) / NULLIF(sa.revenue, 0), 2)
                                                                     AS waste_intensity_pct,
       sa.gross_margin - COALESCE(wa.waste_cost, 0)                  AS margin_after_waste
  FROM sales sa
  JOIN mart.dim_date  d ON d.date_key  = sa.date_key
  JOIN mart.dim_store s ON s.store_key = sa.store_key
  LEFT JOIN waste wa ON wa.date_key = sa.date_key AND wa.store_key = sa.store_key;

-- ----------------------------------------------------------------------------
-- Supplier scorecard: fill rate and the money left on the table.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_supplier_scorecard AS
WITH agg AS (
    SELECT r.supplier_key,
           COUNT(DISTINCT r.grn_number)                               AS receipts,
           SUM(r.qty_dispatched)                                      AS qty_dispatched,
           SUM(r.qty_accepted)                                        AS qty_accepted,
           SUM(r.value_dispatched)                                    AS value_dispatched,
           SUM(r.value_short)                                         AS value_short,
           COUNT(*) FILTER (WHERE r.discrepancy_qty > 0)              AS short_lines,
           COUNT(*)                                                   AS lines
      FROM mart.fact_receipt_line r
     GROUP BY r.supplier_key
)
SELECT s.supplier_code,
       s.company_name,
       a.receipts,
       a.qty_dispatched,
       a.qty_accepted,
       ROUND(100.0 * a.qty_accepted / NULLIF(a.qty_dispatched, 0), 2)  AS fill_rate_pct,
       ROUND(100.0 * a.short_lines / NULLIF(a.lines, 0), 2)            AS short_line_pct,
       a.value_short                                                   AS claimable_value,
       RANK() OVER (ORDER BY a.qty_accepted / NULLIF(a.qty_dispatched, 0) ASC)
                                                                       AS worst_fill_rank
  FROM agg a
  JOIN mart.dim_supplier s ON s.supplier_key = a.supplier_key;

-- ----------------------------------------------------------------------------
-- Weekday demand profile: the seasonality the forecasting model learns,
-- expressed as an index (1.00 = that SKU's average day at that store).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_weekday_demand_profile AS
WITH by_weekday AS (
    SELECT f.store_key,
           f.product_key,
           d.iso_weekday,
           d.weekday_name,
           AVG(f.units_sold)  AS avg_units,
           COUNT(*)           AS observations
      FROM mart.fact_sales_daily f
      JOIN mart.dim_date d ON d.date_key = f.date_key
     GROUP BY f.store_key, f.product_key, d.iso_weekday, d.weekday_name
)
SELECT s.store_code,
       p.sku_code,
       p.product_name,
       b.iso_weekday,
       b.weekday_name,
       ROUND(b.avg_units, 2)                                           AS avg_units,
       b.observations,
       ROUND(b.avg_units / NULLIF(AVG(b.avg_units) OVER (
                 PARTITION BY b.store_key, b.product_key), 0), 3)      AS seasonality_index
  FROM by_weekday b
  JOIN mart.dim_store   s ON s.store_key   = b.store_key
  JOIN mart.dim_product p ON p.product_key = b.product_key;

-- ----------------------------------------------------------------------------
-- Latest expiry exposure: the newest snapshot, ranked by value at risk.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_fefo_exposure_latest AS
WITH latest AS (
    SELECT MAX(date_key) AS date_key FROM mart.fact_stock_position
)
SELECT d.full_date                                  AS snapshot_date,
       s.store_code,
       p.sku_code,
       p.product_name,
       p.category_name,
       f.on_hand_qty,
       f.nearest_expiry,
       f.days_to_nearest_expiry,
       f.qty_expiring_2d,
       f.qty_expiring_5d,
       f.value_expiring_2d,
       f.value_expiring_5d,
       RANK() OVER (ORDER BY f.value_expiring_5d DESC) AS risk_rank
  FROM mart.fact_stock_position f
  JOIN latest            l ON l.date_key    = f.date_key
  JOIN mart.dim_date     d ON d.date_key    = f.date_key
  JOIN mart.dim_store    s ON s.store_key   = f.store_key
  JOIN mart.dim_product  p ON p.product_key = f.product_key
 WHERE f.value_expiring_5d > 0;
