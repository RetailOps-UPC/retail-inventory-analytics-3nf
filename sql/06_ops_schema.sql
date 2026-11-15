-- ============================================================================
-- OPERATIONS SCHEMA: PIPELINE OBSERVABILITY
-- Author: Odar Javier Alcocer Vásquez (Information Systems Engineering - UPC)
-- Engine: PostgreSQL 15+
-- Purpose: A pipeline you cannot interrogate is a pipeline you cannot trust.
--          Every run, every task attempt, every model build and every quality
--          result is recorded here, so "did last night's load work, and how
--          clean was the data?" is a query, not an archaeology exercise.
--
-- Fully idempotent (IF NOT EXISTS everywhere): the orchestrator re-applies it
-- on each start, which is what lets it work against a database volume that was
-- initialised before this file existed.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS ops;

-- ----------------------------------------------------------------------------
-- 1. Pipeline runs and task attempts (written by src/pipeline.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
    run_id        UUID PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL
                  CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    trigger       TEXT NOT NULL DEFAULT 'manual',
    parameters    JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE TABLE IF NOT EXISTS ops.task_runs (
    task_run_id   BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES ops.pipeline_runs (run_id) ON DELETE CASCADE,
    task_name     TEXT NOT NULL,
    attempt       INTEGER NOT NULL CHECK (attempt >= 1),
    status        TEXT NOT NULL
                  CHECK (status IN ('SUCCESS', 'FAILED', 'SKIPPED')),
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ NOT NULL,
    duration_ms   INTEGER NOT NULL CHECK (duration_ms >= 0),
    detail        JSONB,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_run ON ops.task_runs (run_id, task_name);

-- ----------------------------------------------------------------------------
-- 2. Model builds and model tests (written by src/model_runner.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.model_runs (
    model_run_id  BIGSERIAL PRIMARY KEY,
    run_id        UUID,
    model_name    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    rows_affected BIGINT,
    started_at    TIMESTAMPTZ NOT NULL,
    duration_ms   INTEGER NOT NULL CHECK (duration_ms >= 0),
    full_refresh  BOOLEAN NOT NULL DEFAULT FALSE,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_runs_model ON ops.model_runs (model_name, started_at DESC);

CREATE TABLE IF NOT EXISTS ops.model_test_results (
    test_result_id BIGSERIAL PRIMARY KEY,
    run_id         UUID,
    test_name      TEXT NOT NULL,
    model_name     TEXT NOT NULL,
    severity       TEXT NOT NULL CHECK (severity IN ('error', 'warn')),
    status         TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    failing_rows   BIGINT NOT NULL CHECK (failing_rows >= 0),
    executed_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_model_tests_name ON ops.model_test_results (test_name, executed_at DESC);

-- ----------------------------------------------------------------------------
-- 3. Data-quality results (written by src/etl_pipeline.py via src/dq.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.dq_runs (
    dq_run_id        BIGSERIAL PRIMARY KEY,
    run_id           UUID,
    recorded_at      TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL,
    dry_run          BOOLEAN NOT NULL DEFAULT FALSE,
    git_sha          TEXT,
    source_seed      INTEGER,
    window_start     DATE,
    window_end       DATE,
    rows_extracted   BIGINT NOT NULL,
    rows_quarantined BIGINT NOT NULL,
    rows_loaded      BIGINT NOT NULL,
    acceptance_rate  NUMERIC(9, 6)
);
CREATE INDEX IF NOT EXISTS idx_dq_runs_time ON ops.dq_runs (recorded_at DESC);

CREATE TABLE IF NOT EXISTS ops.dq_rule_results (
    dq_run_id      BIGINT NOT NULL REFERENCES ops.dq_runs (dq_run_id) ON DELETE CASCADE,
    rule_id        TEXT NOT NULL,
    dataset        TEXT NOT NULL,
    dimension      TEXT NOT NULL,
    severity       TEXT NOT NULL,
    rows_evaluated BIGINT NOT NULL CHECK (rows_evaluated >= 0),
    rows_failed    BIGINT NOT NULL CHECK (rows_failed >= 0),
    PRIMARY KEY (dq_run_id, rule_id, dataset),
    CONSTRAINT chk_failed_le_evaluated CHECK (rows_failed <= rows_evaluated)
);

-- Ground truth only exists for synthetic data: the generator knows exactly what
-- it broke, so the gate's recall is measurable rather than asserted.
CREATE TABLE IF NOT EXISTS ops.dq_ground_truth (
    dq_run_id    BIGINT NOT NULL REFERENCES ops.dq_runs (dq_run_id) ON DELETE CASCADE,
    anomaly_kind TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    injected     INTEGER NOT NULL CHECK (injected >= 0),
    detected     INTEGER NOT NULL CHECK (detected >= 0),
    unexpected   INTEGER NOT NULL CHECK (unexpected >= 0),
    PRIMARY KEY (dq_run_id, anomaly_kind),
    CONSTRAINT chk_detected_le_injected CHECK (detected <= injected)
);

-- ----------------------------------------------------------------------------
-- 4. Convenience views
-- ----------------------------------------------------------------------------

-- Latest outcome of every task of every run, newest first.
CREATE OR REPLACE VIEW ops.v_run_history AS
SELECT p.run_id,
       p.started_at,
       p.finished_at,
       p.status                                    AS run_status,
       ROUND(EXTRACT(EPOCH FROM (p.finished_at - p.started_at))::NUMERIC, 1)
                                                   AS duration_s,
       COUNT(t.*) FILTER (WHERE t.status = 'SUCCESS') AS tasks_ok,
       COUNT(t.*) FILTER (WHERE t.status = 'FAILED')  AS tasks_failed,
       COUNT(t.*) FILTER (WHERE t.status = 'SKIPPED') AS tasks_skipped
  FROM ops.pipeline_runs p
  LEFT JOIN ops.task_runs t ON t.run_id = p.run_id
 GROUP BY p.run_id, p.started_at, p.finished_at, p.status
 ORDER BY p.started_at DESC;

-- Data-quality trend: acceptance rate and gate recall per run.
CREATE OR REPLACE VIEW ops.v_dq_trend AS
SELECT d.dq_run_id,
       d.recorded_at,
       d.source_seed,
       d.window_end,
       d.rows_extracted,
       d.rows_quarantined,
       d.acceptance_rate,
       COALESCE(SUM(g.detected), 0)                AS defects_detected,
       COALESCE(SUM(g.injected), 0)                AS defects_injected,
       CASE WHEN COALESCE(SUM(g.injected), 0) > 0
            THEN ROUND(SUM(g.detected)::NUMERIC / SUM(g.injected), 4) END
                                                   AS gate_recall
  FROM ops.dq_runs d
  LEFT JOIN ops.dq_ground_truth g ON g.dq_run_id = d.dq_run_id
 WHERE NOT d.dry_run
 GROUP BY d.dq_run_id
 ORDER BY d.recorded_at DESC;
