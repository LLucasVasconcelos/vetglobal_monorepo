# syntax=docker/dockerfile:1

# One image, run three ways: the API, the worker, and the migration step.
# They are separate services with separate lifecycles, but they are the same
# repository at the same commit — a second Dockerfile would be a second copy of
# the same install, free to drift from the first.
#
# Build targets:
#   --target runtime  (default) API + worker, production dependencies only
#   --target test     adds the dev group and the suite; `pytest -q`

ARG PYTHON_VERSION=3.12

# The uv image already carries the interpreter this project pins, so nothing is
# downloaded at build time and `.python-version` is honoured by construction.
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Outside /app on purpose: a bind mount over the source during development
    # would otherwise hide the virtualenv the image just built.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies before source. This layer is keyed on the lock file alone, so
# editing a route reinstalls nothing.
#
# No `--mount=type=cache` on the uv cache: it needs BuildKit, and a Dockerfile
# that only builds on machines with buildx installed is a Dockerfile that fails
# on somebody else's. The layer cache above already covers the common case.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY worker.py ./


# --- test -------------------------------------------------------------------
# The suite runs against a real PostgreSQL, so this stage is only half of it:
# `docker compose --profile test run --rm test` supplies the other half.
FROM base AS test

RUN uv sync --locked

# tests/test_config.py reads `.env.example` and fails when it drifts from
# Settings, so the file is part of the suite rather than documentation.
COPY .env.example ./
COPY tests ./tests

CMD ["pytest", "-q"]


# --- runtime ----------------------------------------------------------------
FROM base AS runtime

# Nothing here needs root, and a container that cannot write to its own code is
# one less thing an upload bug can reach.
#
# Not `--system`: a system account is expected below SYS_UID_MAX (999), and
# useradd warns about the mismatch on every build. This is a service account
# with a fixed high uid, which is what a bind mount would need to match.
RUN useradd --uid 10001 --home-dir /app --shell /usr/sbin/nologin --no-create-home app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# No HEALTHCHECK here: this same image runs the worker, which serves no HTTP and
# would fail an HTTP probe forever. The API's probe lives on its compose service,
# where it describes one service instead of every use of the image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
