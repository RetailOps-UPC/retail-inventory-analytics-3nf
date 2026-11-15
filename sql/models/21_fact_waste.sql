-- model: fact_waste
-- kind: incremental
-- target: mart.fact_waste
-- depends_on: dim_date, dim_store, dim_product, dim_waste_reason
-- description: One row per write-off event, with the reason and who authorised it.
--
-- waste_id and batch_code are degenerate dimensions: identifiers that belong to
-- the event itself and have no attributes worth a dimension table of their own.
-- The date is taken in Lima time explicitly; a bare cast would use whatever
-- timezone the session happens to have and could move an evening write-off to
-- the next day.

CREATE TABLE IF NOT EXISTS mart.fact_waste (
    waste_id            INTEGER PRIMARY KEY,
    date_key            INTEGER NOT NULL REFERENCES mart.dim_date (date_key),
    store_key           INTEGER NOT NULL REFERENCES mart.dim_store (store_key),
    product_key         BIGINT  NOT NULL REFERENCES mart.dim_product (product_key),
    reason_key          INTEGER NOT NULL REFERENCES mart.dim_waste_reason (reason_key),
    batch_code          TEXT,
    qty_discarded       NUMERIC(10, 2) NOT NULL,
    unit_cost_at_loss   NUMERIC(10, 4) NOT NULL,
    total_loss_amount   NUMERIC(12, 2) NOT NULL,
    is_expiry           BOOLEAN NOT NULL,
    is_authorized       BOOLEAN NOT NULL,
    authorized_by       TEXT,
    logged_by_user      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_waste_store_date
    ON mart.fact_waste (store_key, date_key);

INSERT INTO mart.fact_waste (waste_id, date_key, store_key, product_key, reason_key,
                             batch_code, qty_discarded, unit_cost_at_loss,
                             total_loss_amount, is_expiry, is_authorized,
                             authorized_by, logged_by_user)
SELECT w.waste_id,
       TO_CHAR((w.logged_timestamp AT TIME ZONE 'America/Lima')::DATE, 'YYYYMMDD')::INT,
       w.store_id,
       p.product_key,
       w.waste_reason_id,
       b.batch_code,
       w.qty_discarded,
       w.unit_cost_at_loss,
       w.total_loss_amount,
       (r.loss_classification = 'EXPIRATION'),
       (w.authorized_by IS NOT NULL AND BTRIM(w.authorized_by) <> ''),
       w.authorized_by,
       w.logged_by_user
  FROM waste_log w
  JOIN waste_reasons r ON r.waste_reason_id = w.waste_reason_id
  LEFT JOIN inventory_batches b ON b.batch_id = w.batch_id
  JOIN mart.dim_product p
    ON p.product_id = w.product_id
   AND (w.logged_timestamp AT TIME ZONE 'America/Lima')::DATE >= p.valid_from
   AND (w.logged_timestamp AT TIME ZONE 'America/Lima')::DATE <  p.valid_to
 WHERE (w.logged_timestamp AT TIME ZONE 'America/Lima')::DATE >= CASE
           WHEN :full_refresh THEN DATE '1900-01-01'
           ELSE COALESCE((SELECT MAX(d.full_date)
                            FROM mart.fact_waste f
                            JOIN mart.dim_date d ON d.date_key = f.date_key),
                         DATE '1900-01-01') - CAST(:lookback_days AS INTEGER)
       END
ON CONFLICT (waste_id) DO UPDATE
   SET date_key          = EXCLUDED.date_key,
       store_key         = EXCLUDED.store_key,
       product_key       = EXCLUDED.product_key,
       reason_key        = EXCLUDED.reason_key,
       batch_code        = EXCLUDED.batch_code,
       qty_discarded     = EXCLUDED.qty_discarded,
       unit_cost_at_loss = EXCLUDED.unit_cost_at_loss,
       total_loss_amount = EXCLUDED.total_loss_amount,
       is_expiry         = EXCLUDED.is_expiry,
       is_authorized     = EXCLUDED.is_authorized,
       authorized_by     = EXCLUDED.authorized_by,
       logged_by_user    = EXCLUDED.logged_by_user;
