# syntax=docker/dockerfile:1

# immich-enhancer: CPU-only PyTorch with the model weights baked in, run as
# uid 1000. At runtime the image talks to Immich and nothing else: every weight
# the pipeline loads is under /opt/models, verified by sha256 at build time.

# -- 1. Python dependencies, exactly as locked ---------------------------------
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --locked --no-dev --no-install-project

# -- 2. Model weights, pinned by sha256 -----------------------------------------
FROM python:3.12-slim AS weights
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,source=scripts/fetch-weights.sh,target=/tmp/fetch-weights.sh \
    bash /tmp/fetch-weights.sh /opt/models

# -- 3. Runtime -----------------------------------------------------------------
# No system packages beyond the slim base: nothing the job imports links a
# library it lacks. The self-check at the end proves that on every build.
FROM python:3.12-slim

COPY --from=deps /opt/venv /opt/venv
COPY --from=weights /opt/models /opt/models
WORKDIR /app
COPY immich_enhancer/ ./immich_enhancer/

# HOME=/tmp: the pod's root filesystem is read-only with an emptyDir at /tmp,
# and libraries that cache under $HOME must not fail on it.
# PIP_PYTHON: the venv has no pip of its own, so this points the base image's
# pip at it. Without it, `pip list` here lists the system site-packages and
# says nothing about what the app runs.
# OMP_NUM_THREADS is deliberately left unset. The CronJob sets it, and a
# default here would hide a manifest that forgot to.
ENV PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    PIP_PYTHON=/opt/venv/bin/python \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 1000:1000

# Fail here rather than in the first run: as uid 1000, with no network, import
# the job and run both upscalers from /opt/models through its pipeline.
# PYTHONPATH for this step only: Python puts a script's own directory on
# sys.path, not the working directory where the package is.
RUN --network=none \
    --mount=type=tmpfs,target=/tmp \
    --mount=type=bind,source=scripts/selfcheck.py,target=/opt/selfcheck.py \
    PYTHONPATH=/app python /opt/selfcheck.py

# CMD rather than ENTRYPOINT, so `docker run <image> <command>` still works for
# inspection. The CronJob sets neither, so this is what it runs.
CMD ["python", "-m", "immich_enhancer.run"]
