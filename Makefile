# =============================================================================
# Retail Inventory & Waste Reduction Analytics -- task runner
# =============================================================================
# Quick start:
#     make up          # start PostgreSQL 15 (schema + seed + triggers applied)
#     make pipeline    # generate -> quality gate -> load -> star schema -> forecast
#     make test        # 300+ Python tests, then the SQL regression suite
#
# Everything, from nothing:  make all
#
# The stages can also be run one at a time; see `make help`.
#
# Windows without make: use the PowerShell equivalent, scripts/run_project.ps1
# =============================================================================

# --- Configuration (override on the command line: make run-etl DB_PORT=5555) --
COMPOSE        ?= docker compose
SERVICE        ?= postgres
DB_USER        ?= retail_admin
DB_PASSWORD    ?= retail_secret
DB_NAME        ?= retail_inventory_db
DB_HOST        ?= localhost
DB_PORT        ?= 5433
PYTHON         ?= python
SIM_DAYS       ?= 60
SIM_SEED       ?= 20261012
# Last simulated day. Empty means "yesterday", so the data (and every figure derived
# from it) moves each day. Pin it to reproduce the numbers quoted in the docs:
#     make generate SIM_END=2026-10-11
SIM_END        ?=
SIM_END_ARG    := $(if $(SIM_END),--end-date $(SIM_END),)

DSN            := postgresql+psycopg2://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)
PSQL           := $(COMPOSE) exec -T -e PGPASSWORD=$(DB_PASSWORD) $(SERVICE) \
                  psql -v ON_ERROR_STOP=1 -U $(DB_USER) -d $(DB_NAME)

# The connection the Python entry points read (src/dbutil.py).
export RETAIL_DB_DSN := $(DSN)
# The integration tests create and drop throwaway databases, so they need a
# role that can CREATE DATABASE on the maintenance database.
export RETAIL_TEST_ADMIN_DSN ?= postgresql+psycopg2://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/postgres

.DEFAULT_GOAL  := help
.PHONY: help all up down destroy init-db wait-db deps deps-dev generate validate \
        run-etl models forecast pipeline dq-report experiment run-queries \
        refresh-datamart test test-unit test-sql psql logs status clean-data

# -----------------------------------------------------------------------------
help: ## Show this help
	@echo "Retail Inventory & Waste Reduction Analytics -- available targets:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Database: $(DB_USER)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)"

all: init-db pipeline run-queries ## Everything: infra -> pipeline -> analytical queries

# --- Infrastructure ----------------------------------------------------------
up: ## Start PostgreSQL (applies DDL + seed + triggers + ops/ml schemas on first boot)
	$(COMPOSE) up -d $(SERVICE)

down: ## Stop containers, keep the data volume
	$(COMPOSE) down

destroy: ## Stop containers AND delete the data volume (irreversible)
	$(COMPOSE) down -v

wait-db: ## Block until the database reports healthy
	@echo "Waiting for PostgreSQL to accept connections..."
	@for i in $$(seq 1 40); do \
	  if $(COMPOSE) exec -T $(SERVICE) pg_isready -U $(DB_USER) -d $(DB_NAME) >/dev/null 2>&1; then \
	    echo "PostgreSQL is ready."; exit 0; \
	  fi; \
	  sleep 2; \
	done; \
	echo "ERROR: PostgreSQL did not become ready in time." >&2; exit 1

init-db: up wait-db ## Start the database and confirm the schema is in place
	@echo "--- Objects created ---"
	@$(PSQL) -c "\dt"
	@$(PSQL) -c "SELECT routine_name, routine_type FROM information_schema.routines WHERE routine_schema = 'public' ORDER BY routine_name;"
	@$(PSQL) -c "SELECT tgname AS trigger_name, relname AS table_name FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal;"

# --- Python environment ------------------------------------------------------
deps: ## Install the runtime dependencies
	$(PYTHON) -m pip install -r requirements.txt

deps-dev: ## Install runtime + test dependencies
	$(PYTHON) -m pip install -r requirements-dev.txt

# --- The pipeline, as one command or stage by stage --------------------------
pipeline: ## Run every stage in order, with retries and a run record
	$(PYTHON) src/pipeline.py --days $(SIM_DAYS) --seed $(SIM_SEED) $(SIM_END_ARG)

generate: ## Generate the synthetic CSV datasets into data/raw/
	$(PYTHON) src/data_generator.py --days $(SIM_DAYS) --seed $(SIM_SEED) $(SIM_END_ARG)

validate: ## Run the quality gate only -- no database needed
	$(PYTHON) src/etl_pipeline.py --dry-run

run-etl: ## Extract -> quality gate -> upsert the CSV datasets into PostgreSQL
	$(PYTHON) src/etl_pipeline.py --dsn "$(DSN)"

models: ## Build the star schema incrementally, then run the model tests
	$(PYTHON) src/model_runner.py

forecast: ## Forecast demand, project expiry, recommend orders
	$(PYTHON) src/forecasting.py

dq-report: ## Render docs/data_quality_report.md from the quality history
	$(PYTHON) src/dq_report.py

experiment: ## Re-run the replenishment-policy experiment (~15 s, stdlib only)
	$(PYTHON) src/policy_experiment.py

# --- Analytics ---------------------------------------------------------------
# SQL is fed through stdin rather than `psql -f /sql/...`: Git Bash on Windows
# rewrites a leading-slash argument into a Windows path and the file is never found.
run-queries: ## Execute the 4 advanced analytical queries
	@$(PSQL) < sql/03_analytical_queries.sql

refresh-datamart: ## Refresh the Power BI materialized view concurrently
	@$(PSQL) -c "SELECT fn_refresh_waste_datamart(TRUE);"

# --- Verification ------------------------------------------------------------
test: test-unit test-sql ## Everything: the Python suite, then the SQL regression suite

test-unit: ## Python tests (unit + integration; integration skips if PostgreSQL is down)
	$(PYTHON) -m pytest tests -q

test-sql: ## SQL regression suite: FEFO, anti-fraud trigger, datamart (19 assertions)
	@$(PSQL) < sql/05_tests_business_logic.sql

# --- Utilities ---------------------------------------------------------------
psql: ## Open an interactive psql shell
	$(COMPOSE) exec -e PGPASSWORD=$(DB_PASSWORD) $(SERVICE) \
	  psql -U $(DB_USER) -d $(DB_NAME)

logs: ## Tail the database logs
	$(COMPOSE) logs -f $(SERVICE)

status: ## Show container status and row counts
	@$(COMPOSE) ps
	@$(PSQL) -c "SELECT 'stores' AS table, COUNT(*) FROM stores \
	  UNION ALL SELECT 'products', COUNT(*) FROM products \
	  UNION ALL SELECT 'goods_receipt_headers', COUNT(*) FROM goods_receipt_headers \
	  UNION ALL SELECT 'goods_receipt_items', COUNT(*) FROM goods_receipt_items \
	  UNION ALL SELECT 'inventory_batches', COUNT(*) FROM inventory_batches \
	  UNION ALL SELECT 'waste_log', COUNT(*) FROM waste_log \
	  UNION ALL SELECT 'daily_sales', COUNT(*) FROM daily_sales;"

clean-data: ## Delete generated CSVs, quarantine, quality records and the ETL log
	rm -rf data/raw/*.csv data/raw/_manifest.json data/quarantine/*.csv data/quality_runs data/processed/* docs/etl_execution.log
	@echo "Generated artifacts removed."
