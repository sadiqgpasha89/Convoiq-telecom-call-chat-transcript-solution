PY ?= python
.PHONY: help install data train train-eval test eval eval-quick run-local demo infra-gen infra-check lint docker compose-up clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' Makefile | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:         ## install runtime + dev dependencies
	$(PY) -m pip install -r requirements-dev.txt

TAU2_DIR ?= ../tau2-bench
data:            ## rebuild the tau2 corpus from a clone of sierra-research/tau2-bench (TAU2_DIR=...); see docs/DATA_CARD.md
	$(PY) -m convoiq.data.tau2_corpus --tau2-dir $(TAU2_DIR)

train:           ## train PRODUCTION models on all data -> models/
	$(PY) -m convoiq.training.train_all --split all --out models

train-eval:      ## train models on the tau2 TRAIN split only -> models_eval/ (for honest held-out evaluation)
	$(PY) -m convoiq.training.train_all --split train --out models_eval

test:            ## unit + integration + e2e tests (needs `make train` first)
	$(PY) -m pytest tests/v2

eval: train-eval ## full evaluation with release gates (exit 1 on any failed gate)
	$(PY) -m convoiq.evals.runner --models models_eval --out reports

eval-quick:      ## fast suites only (no agent mesh / drift / performance)
	$(PY) -m convoiq.evals.runner --models models_eval --out reports --suites redaction,classification,security,compliance,rag,anomaly

run-local:       ## run every service in-process (no Docker, no GCP) and serve the gateway on :8080
	$(PY) -m convoiq.runtime.serve --port 8080

demo:            ## push synthetic conversations into a running stack and print insights
	$(PY) scripts/demo.py --base http://localhost:8080

infra-gen:       ## regenerate Terraform inputs (BigQuery schemas, Pub/Sub topology) from code
	$(PY) scripts/gen_infra.py

infra-check:     ## fail if generated infra files are stale
	$(PY) scripts/gen_infra.py --check

lint:            ## ruff
	ruff check convoiq tests scripts

docker:          ## build the runtime image (trains models inside)
	docker build --target runtime -t convoiq:local .

compose-up:      ## full stack on emulators
	docker compose up --build

clean:
	rm -rf .pytest_cache .ruff_cache reports/*.tmp
