ARG WORKDIR="/opt/app"

FROM docker.rcplus.io/python:3.13-slim AS voice-build
ARG WORKDIR

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPYCACHEPREFIX="/tmp"

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:0.7.15 /uv /uvx /bin/

WORKDIR ${WORKDIR}
COPY . .

# Clean any Python cache that might have been copied
RUN find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true && \
    find . -name "*.pyc" -delete 2>/dev/null || true && \
    find . -name "*.pyo" -delete 2>/dev/null || true

# Copy shared packages from named build contexts
COPY --from=ringier-a2a-sdk . ../ringier-a2a-sdk

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM docker.rcplus.io/python:3.13-slim AS voice-runtime
ARG WORKDIR

WORKDIR ${WORKDIR}
COPY --from=voice-build ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=voice-build ${WORKDIR} .

# HOST and PORT are read from environment variables at runtime.
# HOST must be set to 0.0.0.0 in ECS (set via task definition environment).
# PORT must match the container port mapping (set via task definition environment).
CMD ["./.venv/bin/python", "-m", "main"]
