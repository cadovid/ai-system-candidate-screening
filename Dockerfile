# syntax=docker/dockerfile:1

# Digest resolved for the linux/amd64 Python 3.14-slim image used by CI.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder

ENV PDM_CHECK_UPDATE=false \
    PDM_IGNORE_SAVED_PYTHON=1 \
    PDM_USE_VENV=1 \
    PDM_VENV_IN_PROJECT=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Keep the dependency resolver in the build stage.  The frozen lockfile makes
# image builds fail when pyproject.toml and pdm.lock drift apart.
RUN pip install --no-cache-dir "pdm==2.28.0"
COPY pyproject.toml pdm.lock ./
RUN pdm install --prod --frozen-lockfile --no-editable --no-self


FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app \
    && mkdir -p /app/var \
    && chown -R app:app /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app src ./src
COPY --chown=app:app data ./data
COPY --chown=app:app static ./static
COPY --chown=app:app templates ./templates
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/entrypoint.sh

USER app

EXPOSE 8001

# urllib.request is part of the Python standard library, so the healthcheck
# does not require curl or another extra runtime dependency.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8001\")}/healthz', timeout=3).read()"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
