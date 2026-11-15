-- ============================================================================
-- MODEL TESTS
-- ============================================================================
-- Every test is a query that returns the rows that VIOLATE an expectation. A
-- test passes when it returns zero rows, so a failure always arrives with the
-- offending keys attached instead of a bare "assertion failed".
--
-- Header format (one per test, parsed by src/model_runner.py):
--     -- test: <name> | model=<model> | severity=<error|warn>
--
-- error  fails the run (non-zero exit): the warehouse is wrong.
-- warn   is recorded and reported but does not fail the run.
--
-- Most of these are RECONCILIATIONS: the same total computed from the 3NF
-- source and from the star schema must agree to the cent. That is what proves
-- the incremental loads, the SCD2 point-in-time joins and the key mappings did
-- not silently drop or duplicate rows, which no amount of "it ran without
-- errors" can show.
-- ============================================================================

-- test: dim_product_one_current_version | model=dim_product | severity=error
-- Every product has exactly one current version: none missing, none doubled.
SELECT product_id FROM mart.dim_product WHERE is_current GROUP BY product_id HAVING COUNT(*) > 1
UNION ALL
SELECT p.product_id FROM products p
 WHERE NOT EXISTS (SELECT 1 FROM mart.dim_product d
                    WHERE d.product_id = p.product_id AND d.is_current);

-- test: dim_product_versions_contiguous | model=dim_product | severity=error
-- Version periods tile the timeline: each starts exactly where the previous
-- ended. A gap would drop facts from every join; an overlap would duplicate them.
SELECT product_id, valid_from
  FROM (SELECT product_id, valid_from,
               LAG(valid_to) OVER (PARTITION BY product_id ORDER BY valid_from) AS prev_to
          FROM mart.dim_product) t
 WHERE prev_to IS NOT NULL AND prev_to <> valid_from;

-- test: dim_date_contiguous | model=dim_date | severity=error
-- No missing calendar days.
SELECT 1 FROM (SELECT COUNT(*) AS n, MAX(full_date) - MIN(full_date) + 1 AS span
                 FROM mart.dim_date) t
 WHERE n <> span;

-- test: fact_sales_row_count_matches_source | model=fact_sales_daily | severity=error
SELECT 1 FROM (SELECT (SELECT COUNT(*) FROM daily_sales)            AS src,
                      (SELECT COUNT(*) FROM mart.fact_sales_daily)  AS tgt) t
 WHERE src IS DISTINCT FROM tgt;

-- test: fact_sales_reconciles_units | model=fact_sales_daily | severity=error
SELECT 1 FROM (SELECT (SELECT COALESCE(SUM(units_sold), 0) FROM daily_sales)            AS src,
                      (SELECT COALESCE(SUM(units_sold), 0) FROM mart.fact_sales_daily)  AS tgt) t
 WHERE src IS DISTINCT FROM tgt;

-- test: fact_sales_reconciles_revenue | model=fact_sales_daily | severity=error
SELECT 1 FROM (SELECT (SELECT COALESCE(SUM(gross_revenue), 0) FROM daily_sales)            AS src,
                      (SELECT COALESCE(SUM(gross_revenue), 0) FROM mart.fact_sales_daily)  AS tgt) t
 WHERE src IS DISTINCT FROM tgt;

-- test: fact_sales_priced_with_valid_version | model=fact_sales_daily | severity=error
-- The point-in-time join: no sale may point at a product version that was not
-- valid on the day of the sale.
SELECT f.date_key, f.store_key, f.product_key
  FROM mart.fact_sales_daily f
  JOIN mart.dim_product p ON p.product_key = f.product_key
  JOIN mart.dim_date    d ON d.date_key    = f.date_key
 WHERE d.full_date < p.valid_from OR d.full_date >= p.valid_to;

-- test: fact_waste_reconciles_loss | model=fact_waste | severity=error
SELECT 1 FROM (SELECT (SELECT COALESCE(SUM(total_loss_amount), 0) FROM waste_log)       AS src,
                      (SELECT COALESCE(SUM(total_loss_amount), 0) FROM mart.fact_waste)  AS tgt) t
 WHERE src IS DISTINCT FROM tgt;

-- test: fact_waste_expiry_flag_matches_reason | model=fact_waste | severity=error
-- The denormalised is_expiry flag must agree with the reason dimension.
SELECT w.waste_id
  FROM mart.fact_waste w
  JOIN mart.dim_waste_reason r ON r.reason_key = w.reason_key
 WHERE w.is_expiry <> r.is_fefo_addressable;

-- test: fact_receipt_reconciles_dispatched | model=fact_receipt_line | severity=error
SELECT 1 FROM (SELECT (SELECT COALESCE(SUM(qty_dispatched), 0) FROM goods_receipt_items)         AS src,
                      (SELECT COALESCE(SUM(qty_dispatched), 0) FROM mart.fact_receipt_line)       AS tgt) t
 WHERE src IS DISTINCT FROM tgt;

-- test: fact_receipt_discrepancy_is_dispatched_minus_accepted | model=fact_receipt_line | severity=error
SELECT grn_item_id FROM mart.fact_receipt_line
 WHERE discrepancy_qty <> qty_dispatched - qty_accepted;

-- test: fact_stock_position_is_coherent | model=fact_stock_position | severity=error
-- Expiring quantities nest (2-day within 5-day within on-hand); nothing negative.
SELECT date_key, store_key, product_key
  FROM mart.fact_stock_position
 WHERE on_hand_qty <= 0
    OR stock_value_at_cost < 0
    OR qty_expiring_5d > on_hand_qty
    OR qty_expiring_2d > qty_expiring_5d
    OR value_expiring_2d > value_expiring_5d;

-- test: fact_stock_position_is_recent | model=fact_stock_position | severity=warn
-- The snapshot history only grows when the pipeline runs; flag a stale one.
SELECT 1 FROM (SELECT MAX(d.full_date) AS latest
                 FROM mart.fact_stock_position f
                 JOIN mart.dim_date d ON d.date_key = f.date_key) t
 WHERE latest IS NULL OR latest < CURRENT_DATE - 2;
