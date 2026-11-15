-- model: dim_waste_reason
-- kind: table
-- target: mart.dim_waste_reason
-- depends_on:
-- description: Waste reason dimension (SCD type 1) with the FEFO-addressable flag.

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.dim_waste_reason (
    reason_key          INTEGER PRIMARY KEY,        -- = waste_reasons.waste_reason_id
    reason_code         TEXT NOT NULL UNIQUE,
    reason_description  TEXT NOT NULL,
    loss_classification TEXT NOT NULL,
    -- Only expiration is something a rotation rule can prevent. Keeping the
    -- flag in the dimension means no report has to re-derive that judgement.
    is_fefo_addressable BOOLEAN NOT NULL
);

INSERT INTO mart.dim_waste_reason (reason_key, reason_code, reason_description,
                                   loss_classification, is_fefo_addressable)
SELECT r.waste_reason_id, r.reason_code, r.reason_description,
       r.loss_classification, (r.loss_classification = 'EXPIRATION')
  FROM waste_reasons r
ON CONFLICT (reason_key) DO UPDATE
   SET reason_code         = EXCLUDED.reason_code,
       reason_description  = EXCLUDED.reason_description,
       loss_classification = EXCLUDED.loss_classification,
       is_fefo_addressable = EXCLUDED.is_fefo_addressable;
