-- model: fact_receipt_line
-- kind: incremental
-- target: mart.fact_receipt_line
-- depends_on: dim_date, dim_store, dim_supplier, dim_product
-- description: One row per goods-receipt line: what the supplier dispatched against
--              what the store physically counted.
--
-- value_short prices the gap at purchase cost. It is the amount a store could
-- claim back with a credit note, which is why it lives on the fact instead of
-- being recomputed in every supplier report.

CREATE TABLE IF NOT EXISTS mart.fact_receipt_line (
    grn_item_id         INTEGER PRIMARY KEY,
    date_key            INTEGER NOT NULL REFERENCES mart.dim_date (date_key),
    store_key           INTEGER NOT NULL REFERENCES mart.dim_store (store_key),
    supplier_key        INTEGER NOT NULL REFERENCES mart.dim_supplier (supplier_key),
    product_key         BIGINT  NOT NULL REFERENCES mart.dim_product (product_key),
    grn_number          TEXT NOT NULL,
    grn_status          TEXT NOT NULL,
    qty_dispatched      NUMERIC(10, 2) NOT NULL,
    qty_accepted        NUMERIC(10, 2) NOT NULL,
    discrepancy_qty     NUMERIC(10, 2) NOT NULL,
    unit_purchase_cost  NUMERIC(10, 4) NOT NULL,
    value_dispatched    NUMERIC(14, 2) NOT NULL,
    value_short         NUMERIC(14, 2) NOT NULL,
    temperature_celsius NUMERIC(4, 1)
);
CREATE INDEX IF NOT EXISTS idx_fact_receipt_supplier
    ON mart.fact_receipt_line (supplier_key, date_key);

INSERT INTO mart.fact_receipt_line (grn_item_id, date_key, store_key, supplier_key,
                                    product_key, grn_number, grn_status,
                                    qty_dispatched, qty_accepted, discrepancy_qty,
                                    unit_purchase_cost, value_dispatched,
                                    value_short, temperature_celsius)
SELECT i.grn_item_id,
       TO_CHAR((h.received_timestamp AT TIME ZONE 'America/Lima')::DATE, 'YYYYMMDD')::INT,
       h.store_id,
       h.supplier_id,
       p.product_key,
       h.grn_number,
       h.status,
       i.qty_dispatched,
       i.qty_accepted,
       i.discrepancy_qty,
       i.unit_purchase_cost,
       ROUND(i.qty_dispatched * i.unit_purchase_cost, 2),
       ROUND(GREATEST(i.discrepancy_qty, 0) * i.unit_purchase_cost, 2),
       i.temperature_celsius
  FROM goods_receipt_items i
  JOIN goods_receipt_headers h ON h.grn_id = i.grn_id
  JOIN mart.dim_product p
    ON p.product_id = i.product_id
   AND (h.received_timestamp AT TIME ZONE 'America/Lima')::DATE >= p.valid_from
   AND (h.received_timestamp AT TIME ZONE 'America/Lima')::DATE <  p.valid_to
 WHERE (h.received_timestamp AT TIME ZONE 'America/Lima')::DATE >= CASE
           WHEN :full_refresh THEN DATE '1900-01-01'
           ELSE COALESCE((SELECT MAX(d.full_date)
                            FROM mart.fact_receipt_line f
                            JOIN mart.dim_date d ON d.date_key = f.date_key),
                         DATE '1900-01-01') - CAST(:lookback_days AS INTEGER)
       END
ON CONFLICT (grn_item_id) DO UPDATE
   SET date_key            = EXCLUDED.date_key,
       store_key           = EXCLUDED.store_key,
       supplier_key        = EXCLUDED.supplier_key,
       product_key         = EXCLUDED.product_key,
       grn_status          = EXCLUDED.grn_status,
       qty_dispatched      = EXCLUDED.qty_dispatched,
       qty_accepted        = EXCLUDED.qty_accepted,
       discrepancy_qty     = EXCLUDED.discrepancy_qty,
       unit_purchase_cost  = EXCLUDED.unit_purchase_cost,
       value_dispatched    = EXCLUDED.value_dispatched,
       value_short         = EXCLUDED.value_short,
       temperature_celsius = EXCLUDED.temperature_celsius;
