.PHONY: help install all gate gate-soft run run-s3 test lint fmt analytics dashboard \
        docs digest benchmark mutation prove localstack-up localstack-down clean check

PY := python3
export PYTHONPATH := src

ACK := "uom_resolution_coverage=Ticket DE-412. Vendor confirmed the 2026-04-01 unit-of-measure \
change and is back-filling the flag. Publishing with coverage stamped on the report."

help:
	@echo ""
	@echo "  make all              full run: pipeline -> analytics -> dashboard -> docs"
	@echo "  make check            everything CI runs: lint + tests + gate + publish"
	@echo ""
	@echo "  make gate             run WITHOUT acknowledging the blocking failure."
	@echo "                        Refuses to publish, quarantines the warehouse,"
	@echo "                        exits 1. Look at this first."
	@echo "  make run              acknowledge it in writing, and publish"
	@echo "  make run-s3           same, against LocalStack instead of the local lake"
	@echo ""
	@echo "  make test             pytest"
	@echo "  make mutation         mutation-test disclosure.py -- see docs/MUTATION.md"
	@echo "  make prove            break it on purpose and see what catches it"
	@echo "  make lint             ruff"
	@echo "  make analytics        run the ten queries in sql/analytics.sql"
	@echo "  make dashboard        rebuild dashboard.html from the last run"
	@echo "  make digest           show the weekly clinical-operations digest"
	@echo "  make docs             regenerate docs/DATA_DICTIONARY.md"
	@echo "  make benchmark        time the pipeline at 1x, 4x and 12x scale"
	@echo ""
	@echo "  make localstack-up    start LocalStack (Docker)"
	@echo "  make install          install dependencies"
	@echo "  make clean            remove generated data"
	@echo ""

install:
	$(PY) -m pip install -r requirements.txt

# Runs the gate first so a fresh clone sees the pipeline refuse to publish
# before it sees it publish.
all: gate-soft run analytics dashboard docs
	@echo ""
	@echo "  Done."
	@echo "    dashboard.html                      the report"
	@echo "    data/out/reports/weekly_digest.md   what a coordinator receives"
	@echo "    data/out/reports/quality_report.md  what the gate decided"
	@echo ""

# Exits non-zero on purpose. That is the whole point of calling it a gate:
# CI has to be able to fail a build when the data is not fit to publish.
gate:
	@$(PY) -m hourglass.pipeline --no-s3; code=$$?; \
	if [ $$code -ne 0 ]; then \
	  echo "  ^ Non-zero exit is correct. The gate refused to publish."; \
	  echo "    Read data/out/reports/quality_report.md, then run: make run"; \
	  echo ""; \
	fi; exit $$code

gate-soft:
	-@$(PY) -m hourglass.pipeline --no-s3

run:
	$(PY) -m hourglass.pipeline --no-s3 --no-regenerate --timing --acknowledge $(ACK)

run-s3:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-west-2 \
	$(PY) -m hourglass.pipeline --acknowledge $(ACK)

test:
	$(PY) -m pytest

# Not part of `check`: a full mutation run costs minutes, not seconds, and the
# survivors need reading rather than a pass/fail. See docs/MUTATION.md.
mutation:
	$(PY) scripts/mutation.py disclosure --tests tests/test_disclosure.py

lint:
	ruff check .

fmt:
	ruff check . --fix

check: lint test gate-soft
	@$(PY) -m hourglass.pipeline --no-s3 --no-regenerate --quiet --acknowledge $(ACK)
	@$(PY) scripts/run_analytics.py --rows 2 > /dev/null && echo "  analytics OK"
	@$(PY) scripts/build_data_dictionary.py > /dev/null && echo "  data dictionary OK"
	@echo "  all checks passed"

analytics:
	$(PY) scripts/run_analytics.py

dashboard:
	$(PY) scripts/build_dashboard.py

digest:
	@test -f data/out/reports/weekly_digest.md || { echo "No digest yet -- run 'make run' first."; exit 1; }
	@cat data/out/reports/weekly_digest.md

docs:
	$(PY) scripts/build_data_dictionary.py

benchmark:
	$(PY) scripts/benchmark.py --scales 1 4 12

localstack-up:
	docker compose up -d
	@echo "waiting for LocalStack..."
	@until curl -sf http://localhost:4566/_localstack/health >/dev/null; do sleep 2; done
	@echo "ready on http://localhost:4566"

localstack-down:
	docker compose down

clean:
	rm -rf data/raw data/lake data/out dashboard.html
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

prove:
	$(PY) scripts/prove.py
