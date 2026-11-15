-- model: fact_stock_position
-- kind: snapshot
-- target: mart.fact_stock_position
-- depends_on: dim_date, dim_store, dim_product
-- description: Periodic snapshot of sellable stock and expiry exposure, one row
--              per store, product and snapshot date.
--
-- WHY THIS IS A SNAPSHOT AND NOT DERIVED: inventory_batches only holds the
-- CURRENT quantity of each lot. Yesterday's stock cannot be reconstructed from
-- it, so the history has to be captured as it happens, one snapshot per run.
-- That is also why this model is never truncated on :full_refresh: truncating
-- it would destroy history that cannot be rebuilt.
--
-- Re-running the same day overwrites that day's snapshot (idempotent); running
-- on a new day adds a new one. The table therefore grows by one slice per run,
-- and is what makes "how did value at risk move over time" answerable.

CREATE TABLE IF NOT EXISTS mart.fact_stock_position (
    date_key              INTEGER NOT NULL REFERENCES mart.dim_date (date_key),
    store_key             INTEGER NOT NULL REFERENCES mart.dim_store (store_key),
    product_key           BIGINT  NOT NULL REFERENCES mart.dim_product (product_key),
    active_batches        INTEGER NOT NULL,
    on_hand_qty           NUMERIC(12, 2) NOT NULL,
    stock_value_at_cost   NUMERIC(14, 2) NOT NULL,
    nearest_expiry        DATE NOT NULL,
    days_to_nearest_expiry INTEGER NOT NULL,
    qty_expiring_2d       NUMERIC(12, 2) NOT NULL,
    qty_expiring_5d       NUMERIC(12, 2) NOT NULL,
    value_expiring_2d     NUMERIC(14, 2) NOT NULL,
    value_expiring_5d     NUMERIC(14, 2) NOT NULL,
    PRIMARY KEY (date_key, store_key, product_key)
);

WITH lots AS (
    SELECT b.store_id,
           b.product_id,
           b.current_qty,
           b.expiration_date,
           COALESCE(gi.unit_purchase_cost, pr.unit_cost) AS unit_cost
      FROM inventory_batches b
      JOIN products pr ON pr.product_id = b.product_id
      LEFT JOIN goods_receipt_items gi ON gi.grn_item_id = b.grn_item_id
     WHERE b.batch_status = 'ACTIVE'
       AND b.current_qty > 0
       AND b.expiration_date >= CAST(:snapshot_date AS DATE)
)
INSERT INTO mart.fact_stock_position (
    date_key, store_key, product_key, active_batches, on_hand_qty,
    stock_value_at_cost, nearest_expiry, days_to_nearest_expiry,
    qty_expiring_2d, qty_expiring_5d, value_expiring_2d, value_expiring_5d)
SELECT TO_CHAR(CAST(:snapshot_date AS DATE), 'YYYYMMDD')::INT,
       l.store_id,
       p.product_key,
       COUNT(*)::INT,
       SUM(l.current_qty),
       ROUND(SUM(l.current_qty * l.unit_cost), 2),
       MIN(l.expiration_date),
       (MIN(l.expiration_date) - CAST(:snapshot_date AS DATE))::INT,
       COALESCE(SUM(l.current_qty) FILTER (
           WHERE l.expiration_date <= CAST(:snapshot_date AS DATE) + 2), 0),
       COALESCE(SUM(l.current_qty) FILTER (
           WHERE l.expiration_date <= CAST(:snapshot_date AS DATE) + 5), 0),
       COALESCE(ROUND(SUM(l.current_qty * l.unit_cost) FILTER (
           WHERE l.expiration_date <= CAST(:snapshot_date AS DATE) + 2), 2), 0),
       COALESCE(ROUND(SUM(l.current_qty * l.unit_cost) FILTER (
           WHERE l.expiration_date <= CAST(:snapshot_date AS DATE) + 5), 2), 0)
  FROM lots l
  JOIN mart.dim_product p ON p.product_id = l.product_id AND p.is_current
 GROUP BY l.store_id, p.product_key
ON CONFLICT (date_key, store_key, product_key) DO UPDATE
   SET active_batches         = EXCLUDED.active_batches,
       on_hand_qty            = EXCLUDED.on_hand_qty,
       stock_value_at_cost    = EXCLUDED.stock_value_at_cost,
       nearest_expiry         = EXCLUDED.nearest_expiry,
       days_to_nearest_expiry = EXCLUDED.days_to_nearest_expiry,
       qty_expiring_2d        = EXCLUDED.qty_expiring_2d,
       qty_expiring_5d        = EXCLUDED.qty_expiring_5d,
       value_expiring_2d      = EXCLUDED.value_expiring_2d,
       value_expiring_5d      = EXCLUDED.value_expiring_5d;
