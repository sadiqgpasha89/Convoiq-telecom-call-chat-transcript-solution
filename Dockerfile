# syntax=docker/dockerfile:1.7
# One image, many services: Cloud Run picks the app with SERVICE=<gateway|redaction|enrichment|orchestrator|action_broker|analytics|monitor|telecom_sim>.
# Models are TRAINED IN THE BUILD (stage "models") so they always match the installed scikit-learn - no pickles cross library versions.
ARG PYTHON_VERSION=3.13

# ---- 1. dependencies ------------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- 2. train models (deterministic, ~3 min on 4 vCPU; layer-cached while code/data are unchanged) ------------------------
FROM deps AS models
WORKDIR /app
ENV PYTHONPATH=/app
COPY convoiq ./convoiq
COPY data ./data
RUN python -m convoiq.training.train_all --split all --out /app/models

# ---- 2b. CI image: runtime code + models + tests/evals/scripts (never deployed) ------------------------------------------------------
FROM models AS ci
RUN pip install "pytest>=8"
COPY tests ./tests
COPY evals ./evals
COPY scripts ./scripts
COPY infra/terraform/generated ./infra/terraform/generated
COPY pyproject.toml requirements.txt ./

# ---- 3. runtime -------------------------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PATH="/opt/venv/bin:$PATH" \
    PORT=8080 SERVICE=gateway CONVOIQ_ENV=prod
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY --chown=app:app convoiq ./convoiq
COPY --chown=app:app data ./data
COPY --from=models --chown=app:app /app/models ./models
USER 10001
EXPOSE 8080
# exec form via sh so SERVICE/PORT are expanded and uvicorn is PID 1 (receives SIGTERM from Cloud Run)
CMD ["sh", "-c", "exec uvicorn convoiq.services.${SERVICE}:create --factory --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log --timeout-graceful-shutdown 25"]
