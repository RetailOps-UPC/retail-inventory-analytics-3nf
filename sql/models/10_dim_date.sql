-- model: dim_date
-- kind: table
-- target: mart.dim_date
-- depends_on:
-- description: Calendar dimension with the retail attributes the demand model needs.
--
-- Rebuilt in full on every run: it is pure arithmetic on dates, so an upsert
-- over a generated series is cheaper and simpler than tracking what changed.
-- Holidays are FIXED-DATE national holidays only. Movable ones (Holy Thursday
-- and Good Friday) depend on the Easter calendar and are deliberately not
-- modelled; a flag that is sometimes wrong is worse than a column that says so.

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.dim_date (
    date_key         INTEGER PRIMARY KEY,           -- 20261114
    full_date        DATE NOT NULL UNIQUE,
    year             SMALLINT NOT NULL,
    quarter          SMALLINT NOT NULL,
    month            SMALLINT NOT NULL,
    month_name       TEXT NOT NULL,
    week_iso         SMALLINT NOT NULL,
    day_of_month     SMALLINT NOT NULL,
    iso_weekday      SMALLINT NOT NULL CHECK (iso_weekday BETWEEN 1 AND 7),
    weekday_name     TEXT NOT NULL,
    is_weekend       BOOLEAN NOT NULL,              -- retail weekend: Friday to Sunday
    is_payday_window BOOLEAN NOT NULL,              -- wages land mid-month and at month end
    is_holiday       BOOLEAN NOT NULL,
    holiday_name     TEXT
);

WITH days AS (
    SELECT g.d::DATE AS full_date
      FROM generate_series(DATE '2025-01-01', CURRENT_DATE + 730, INTERVAL '1 day') AS g(d)
),
enriched AS (
    SELECT full_date,
           EXTRACT(ISODOW FROM full_date)::INT                                    AS iso_weekday,
           EXTRACT(DAY FROM full_date)::INT                                       AS dom,
           EXTRACT(DAY FROM (DATE_TRUNC('month', full_date)
                             + INTERVAL '1 month' - INTERVAL '1 day'))::INT       AS month_len,
           TO_CHAR(full_date, 'MM-DD')                                            AS md
      FROM days
)
INSERT INTO mart.dim_date (
    date_key, full_date, year, quarter, month, month_name, week_iso,
    day_of_month, iso_weekday, weekday_name, is_weekend, is_payday_window,
    is_holiday, holiday_name)
SELECT TO_CHAR(e.full_date, 'YYYYMMDD')::INT,
       e.full_date,
       EXTRACT(YEAR FROM e.full_date)::SMALLINT,
       EXTRACT(QUARTER FROM e.full_date)::SMALLINT,
       EXTRACT(MONTH FROM e.full_date)::SMALLINT,
       (ARRAY['January','February','March','April','May','June','July',
              'August','September','October','November','December'])
           [EXTRACT(MONTH FROM e.full_date)::INT],
       EXTRACT(WEEK FROM e.full_date)::SMALLINT,
       e.dom::SMALLINT,
       e.iso_weekday::SMALLINT,
       (ARRAY['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])
           [e.iso_weekday],
       e.iso_weekday >= 5,
       -- Same window the demand simulator treats as a payday lift.
       (e.dom IN (15, 16, 17) OR e.dom >= e.month_len - 1 OR e.dom <= 2),
       e.md IN ('01-01', '05-01', '06-29', '07-28', '07-29', '08-06', '08-30',
                '10-08', '11-01', '12-08', '12-09', '12-25'),
       CASE e.md
            WHEN '01-01' THEN 'Año Nuevo'
            WHEN '05-01' THEN 'Día del Trabajo'
            WHEN '06-29' THEN 'San Pedro y San Pablo'
            WHEN '07-28' THEN 'Fiestas Patrias'
            WHEN '07-29' THEN 'Fiestas Patrias'
            WHEN '08-06' THEN 'Batalla de Junín'
            WHEN '08-30' THEN 'Santa Rosa de Lima'
            WHEN '10-08' THEN 'Combate de Angamos'
            WHEN '11-01' THEN 'Todos los Santos'
            WHEN '12-08' THEN 'Inmaculada Concepción'
            WHEN '12-09' THEN 'Batalla de Ayacucho'
            WHEN '12-25' THEN 'Navidad'
       END
  FROM enriched e
ON CONFLICT (date_key) DO UPDATE
   SET is_holiday  = EXCLUDED.is_holiday,
       holiday_name = EXCLUDED.holiday_name,
       is_payday_window = EXCLUDED.is_payday_window;
