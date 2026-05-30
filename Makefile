# kix-platform developer Makefile
# Run `make help` for the list of targets.

PYTHON ?= python3
PIP    ?= pip
PYTEST ?= pytest
IMAGE  ?= kix-platform
TAG    ?= dev-$(shell git rev-parse --short HEAD 2>/dev/null || echo local)

.PHONY: help install lint test test-fast security e2e load-baseline build \
        deploy-staging bible-check clean fmt

help:
	@echo "kix-platform make targets:"
	@echo "  install         pip install + pre-commit install"
	@echo "  lint            ruff + black + mypy"
	@echo "  fmt             auto-format (ruff + black)"
	@echo "  test            full pytest with coverage"
	@echo "  test-fast       only @pytest.mark.fast tests"
	@echo "  security        bandit + safety + pip-audit"
	@echo "  e2e             e2e smoke (superapp + ads)"
	@echo "  load-baseline   load test baseline"
	@echo "  build           docker build $(IMAGE):$(TAG)"
	@echo "  deploy-staging  manual staging deploy (gated)"
	@echo "  bible-check     run bible drift check"
	@echo "  clean           remove caches, coverage, build artifacts"

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install ruff black mypy bandit safety pip-audit pre-commit pytest pytest-cov pytest-xdist
	pre-commit install
	pre-commit install --hook-type pre-push

lint:
	ruff check app/ scripts/ tests/
	black --check app/ scripts/ tests/
	mypy app/ --ignore-missing-imports || true

fmt:
	ruff check --fix app/ scripts/ tests/
	black app/ scripts/ tests/

test:
	$(PYTEST) -n auto --cov=app --cov-report=term --cov-report=html

test-fast:
	$(PYTEST) -m "fast" -x -q

security:
	bandit -r app/ -ll
	safety check -r requirements.txt --full-report || true
	pip-audit -r requirements.txt || true

e2e:
	$(PYTHON) scripts/e2e_superapp.py
	$(PYTHON) scripts/e2e_ads_platform.py
	$(PYTHON) scripts/smoke_voucher_reserve_claim.py

load-baseline:
	@if [ -d load_tests ]; then \
		echo "Running load baseline (placeholder — wire to locust/k6)"; \
		ls load_tests/; \
	else \
		echo "No load_tests/ directory"; exit 1; \
	fi

build:
	docker build -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

deploy-staging:
	@echo "Manual staging deploy — requires kubectl + cluster context"
	@echo "TODO: configure KUBECONFIG and ECR/GHCR auth before running"
	@read -p "Confirm deploy $(IMAGE):$(TAG) to staging? [y/N] " ok; \
	  [ "$$ok" = "y" ] || (echo "aborted"; exit 1)
	kubectl set image deployment/kix-api kix-api=$(IMAGE):$(TAG) -n staging
	kubectl rollout status deployment/kix-api -n staging --timeout=5m

bible-check:
	@if [ -f scripts/bible_check.py ]; then \
		$(PYTHON) scripts/bible_check.py; \
	else \
		echo "scripts/bible_check.py not present yet (D2 deliverable)"; \
	fi

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
