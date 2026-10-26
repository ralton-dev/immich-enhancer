# syntax=docker/dockerfile:1

# immich-enhancer: CPU-only PyTorch with the model weights baked in, run as
# uid 1000. At runtime the image talks to Immich and nothing else: every weight
# the pipeline loads is under /opt/models, verified by sha256 at build time.

# Refresh both independent trust stores before downloading dependencies. The
# Python PSRT wheel hash makes the pip bootstrap safe even with an older base.
FROM python:3.15-slim AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

# -- 1. Python dependencies, exactly as locked ---------------------------------
FROM base AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
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
FROM base AS weights
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,source=scripts/fetch-weights.sh,target=/tmp/fetch-weights.sh \
    bash /tmp/fetch-weights.sh /opt/models

# -- 3. Runtime -----------------------------------------------------------------
# No additional runtime packages beyond the certificate-refreshed base.
# The self-check at the end proves that nothing the job imports is missing.
FROM base

COPY --from=deps /opt/venv /opt/venv
COPY --from=weights /opt/models /opt/models
WORKDIR /app
COPY immich_enhancer/ ./immich_enhancer/

# HOME=/tmp: the pod's root filesystem is read-only with an emptyDir at /tmp,
# and libraries that cache under $HOME must not fail on it.
# OMP_NUM_THREADS is deliberately left unset. The CronJob sets it, and a
# default here would hide a manifest that forgot to.
ENV PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# pip and its bundled tooling are needed only to build the locked environment;
# use the base interpreter because PATH now selects the pip-free app venv.
RUN /usr/local/bin/python -m pip uninstall --yes pip

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
