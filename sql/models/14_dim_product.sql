-- model: dim_product
-- kind: scd2
-- target: mart.dim_product
-- depends_on:
-- description: Product dimension, SCD type 2: every change to a tracked attribute
--              opens a new version, so history is queryable as it was.
--
-- Why type 2 here and not type 1: a SKU's cost and price change over time, and
-- a fact must be priced with the version that was valid WHEN IT HAPPENED. With
-- type 1, repricing a SKU silently rewrites the margin of every past sale in
-- every report. Facts join on (product_id, date BETWEEN valid_from AND valid_to).
--
-- Mechanics, in three statements inside one transaction:
--   1. Same-day correction: a change detected on the day the current version
--      opened overwrites it in place (a typo fixed twice in a day is one
--      version, not two zero-length ones).
--   2. Close every version whose tracked attributes no longer match the source.
--   3. Open a new version for changed products and for brand-new products.
-- A product seen for the FIRST time gets valid_from = 1900-01-01 so facts that
-- predate its first load still resolve; a later version opens on :as_of.
--
-- Idempotent: change detection compares a hash of the tracked attributes, so a
-- re-run with unchanged source opens no versions.

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.dim_product (
    product_key       BIGSERIAL PRIMARY KEY,
    product_id        INTEGER NOT NULL,             -- business key (products.product_id)
    sku_code          TEXT NOT NULL,
    barcode_ean13     TEXT,
    product_name      TEXT NOT NULL,
    category_code     TEXT NOT NULL,
    category_name     TEXT NOT NULL,
    storage_temp_type TEXT NOT NULL,
    unit_of_measure   TEXT NOT NULL,
    unit_cost         NUMERIC(10, 4) NOT NULL,
    retail_price      NUMERIC(10, 2) NOT NULL,
    shelf_life_days   INTEGER,
    is_perishable     BOOLEAN NOT NULL,
    min_safety_stock  INTEGER NOT NULL,
    reorder_point     INTEGER NOT NULL,
    row_hash          TEXT NOT NULL,                -- hash of the tracked attributes
    valid_from        DATE NOT NULL,
    valid_to          DATE NOT NULL DEFAULT DATE '9999-12-31',   -- exclusive
    is_current        BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_dim_product_validity CHECK (valid_from < valid_to)
);

-- At most one current version per product, and no two versions may open on the
-- same day. These are the invariants the SCD logic relies on, enforced by the
-- database rather than trusted.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_product_current
    ON mart.dim_product (product_id) WHERE is_current;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_product_version
    ON mart.dim_product (product_id, valid_from);
CREATE INDEX IF NOT EXISTS idx_dim_product_lookup
    ON mart.dim_product (product_id, valid_from, valid_to);

CREATE OR REPLACE TEMP VIEW stg_product AS
SELECT p.product_id, p.sku_code, p.barcode_ean13, p.product_name,
       c.category_code, c.category_name, c.storage_temp_type,
       p.unit_of_measure, p.unit_cost, p.retail_price, p.shelf_life_days,
       p.is_perishable, p.min_safety_stock, p.reorder_point,
       MD5(CONCAT_WS('|', p.sku_code, p.product_name, c.category_code,
                     p.unit_of_measure, p.unit_cost::TEXT, p.retail_price::TEXT,
                     COALESCE(p.shelf_life_days::TEXT, ''), p.is_perishable::TEXT,
                     p.min_safety_stock::TEXT, p.reorder_point::TEXT)) AS row_hash
  FROM products p
  JOIN product_categories c ON c.category_id = p.category_id;

-- 1. Same-day correction: overwrite the version that opened today.
UPDATE mart.dim_product d
   SET sku_code = s.sku_code, barcode_ean13 = s.barcode_ean13,
       product_name = s.product_name, category_code = s.category_code,
       category_name = s.category_name, storage_temp_type = s.storage_temp_type,
       unit_of_measure = s.unit_of_measure, unit_cost = s.unit_cost,
       retail_price = s.retail_price, shelf_life_days = s.shelf_life_days,
       is_perishable = s.is_perishable, min_safety_stock = s.min_safety_stock,
       reorder_point = s.reorder_point, row_hash = s.row_hash
  FROM stg_product s
 WHERE d.product_id = s.product_id
   AND d.is_current
   AND d.valid_from = CAST(:as_of AS DATE)
   AND d.row_hash <> s.row_hash;

-- 2. Close versions superseded by a change.
UPDATE mart.dim_product d
   SET valid_to = CAST(:as_of AS DATE), is_current = FALSE
  FROM stg_product s
 WHERE d.product_id = s.product_id
   AND d.is_current
   AND d.row_hash <> s.row_hash;

-- 3. Open a version for changed and for new products. This is the last
--    statement, so its row count is what the runner reports.
INSERT INTO mart.dim_product (
    product_id, sku_code, barcode_ean13, product_name, category_code,
    category_name, storage_temp_type, unit_of_measure, unit_cost, retail_price,
    shelf_life_days, is_perishable, min_safety_stock, reorder_point, row_hash,
    valid_from, valid_to, is_current)
SELECT s.product_id, s.sku_code, s.barcode_ean13, s.product_name, s.category_code,
       s.category_name, s.storage_temp_type, s.unit_of_measure, s.unit_cost,
       s.retail_price, s.shelf_life_days, s.is_perishable, s.min_safety_stock,
       s.reorder_point, s.row_hash,
       CASE WHEN EXISTS (SELECT 1 FROM mart.dim_product x
                          WHERE x.product_id = s.product_id)
            THEN CAST(:as_of AS DATE)
            ELSE DATE '1900-01-01'
       END,
       DATE '9999-12-31', TRUE
  FROM stg_product s
 WHERE NOT EXISTS (SELECT 1 FROM mart.dim_product d
                    WHERE d.product_id = s.product_id AND d.is_current);
