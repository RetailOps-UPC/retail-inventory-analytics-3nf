-- model: dim_store
-- kind: table
-- target: mart.dim_store
-- depends_on:
-- description: Store dimension (SCD type 1: attribute changes overwrite).

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.dim_store (
    store_key      INTEGER PRIMARY KEY,             -- = stores.store_id
    store_code     TEXT NOT NULL UNIQUE,
    store_name     TEXT NOT NULL,
    store_format   TEXT NOT NULL,
    district       TEXT NOT NULL,
    city           TEXT NOT NULL,
    square_meters  NUMERIC(6, 2),
    is_active      BOOLEAN NOT NULL
);

INSERT INTO mart.dim_store (store_key, store_code, store_name, store_format,
                            district, city, square_meters, is_active)
SELECT s.store_id, s.store_code, s.store_name, s.store_format, s.district,
       s.city, s.square_meters, s.is_active
  FROM stores s
ON CONFLICT (store_key) DO UPDATE
   SET store_code    = EXCLUDED.store_code,
       store_name    = EXCLUDED.store_name,
       store_format  = EXCLUDED.store_format,
       district      = EXCLUDED.district,
       city          = EXCLUDED.city,
       square_meters = EXCLUDED.square_meters,
       is_active     = EXCLUDED.is_active;
