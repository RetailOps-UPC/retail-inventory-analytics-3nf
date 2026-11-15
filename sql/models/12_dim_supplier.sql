-- model: dim_supplier
-- kind: table
-- target: mart.dim_supplier
-- depends_on:
-- description: Supplier dimension (SCD type 1).

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.dim_supplier (
    supplier_key       INTEGER PRIMARY KEY,         -- = suppliers.supplier_id
    supplier_code      TEXT NOT NULL UNIQUE,
    tax_id_ruc         TEXT NOT NULL,
    company_name       TEXT NOT NULL,
    payment_terms_days INTEGER NOT NULL,
    lead_time_days     INTEGER NOT NULL,
    is_active          BOOLEAN NOT NULL
);

INSERT INTO mart.dim_supplier (supplier_key, supplier_code, tax_id_ruc, company_name,
                               payment_terms_days, lead_time_days, is_active)
SELECT s.supplier_id, s.supplier_code, s.tax_id_ruc, s.company_name,
       s.payment_terms_days, s.lead_time_days, s.is_active
  FROM suppliers s
ON CONFLICT (supplier_key) DO UPDATE
   SET supplier_code      = EXCLUDED.supplier_code,
       tax_id_ruc         = EXCLUDED.tax_id_ruc,
       company_name       = EXCLUDED.company_name,
       payment_terms_days = EXCLUDED.payment_terms_days,
       lead_time_days     = EXCLUDED.lead_time_days,
       is_active          = EXCLUDED.is_active;
