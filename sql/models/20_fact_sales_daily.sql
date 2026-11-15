-- model: fact_sales_daily
-- kind: incremental
-- target: mart.fact_sales_daily
-- depends_on: dim_date, dim_store, dim_product
-- description: Sales at the grain of one store, one product, one day.
--
-- Incremental with a lookback window. Each run reprocesses everything from
-- (latest loaded sale date - :lookback_days) forward and UPSERTS it, instead of
-- scanning history or appending blindly. The overlap is the point: POS data
-- arrives late and gets corrected, and a strict "only newer than the watermark"
-- load would never see either. :full_refresh rebuilds from the beginning.
--
-- Facts are priced with the product version valid on the sale date (a
-- point-in-time join to the SCD2 dimension), so a later repricing does not
-- rewrite past margin.

CREATE TABLE IF NOT EXISTS mart.fact_sales_daily (
    date_key            INTEGER NOT NULL REFERENCES mart.dim_date (date_key),
    store_key           INTEGER NOT NULL REFERENCES mart.dim_store (store_key),
    product_key         BIGINT  NOT NULL REFERENCES mart.dim_product (product_key),
    units_sold          NUMERIC(10, 2) NOT NULL,
    gross_revenue       NUMERIC(12, 2) NOT NULL,
    cost_of_goods_sold  NUMERIC(12, 2) NOT NULL,
    gross_margin        NUMERIC(12, 2) GENERATED ALWAYS AS (gross_revenue - cost_of_goods_sold) STORED,
    PRIMARY KEY (date_key, store_key, product_key)
);
CREATE INDEX IF NOT EXISTS idx_fact_sales_store_product
    ON mart.fact_sales_daily (store_key, product_key, date_key);

INSERT INTO mart.fact_sales_daily (date_key, store_key, product_key, units_sold,
                                   gross_revenue, cost_of_goods_sold)
SELECT TO_CHAR(s.sale_date, 'YYYYMMDD')::INT,
       s.store_id,
       p.product_key,
       s.units_sold,
       s.gross_revenue,
       s.cost_of_goods_sold
  FROM daily_sales s
  JOIN mart.dim_product p
    ON p.product_id = s.product_id
   AND s.sale_date >= p.valid_from
   AND s.sale_date <  p.valid_to
 WHERE s.sale_date >= CASE
           WHEN :full_refresh THEN DATE '1900-01-01'
           ELSE COALESCE((SELECT MAX(d.full_date)
                            FROM mart.fact_sales_daily f
                            JOIN mart.dim_date d ON d.date_key = f.date_key),
                         DATE '1900-01-01') - CAST(:lookback_days AS INTEGER)
       END
ON CONFLICT (date_key, store_key, product_key) DO UPDATE
   SET units_sold         = EXCLUDED.units_sold,
       gross_revenue      = EXCLUDED.gross_revenue,
       cost_of_goods_sold = EXCLUDED.cost_of_goods_sold;
