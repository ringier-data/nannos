ARG WORKDIR="/opt/app"

FROM docker.rcplus.io/python:3.13-slim AS nannos-build
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
COPY --from=agent-common . ../agent-common

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM docker.rcplus.io/python:3.13-slim AS nannos-runtime
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos
ARG WORKDIR
ARG API_PORT
ENV API_PORT=${API_PORT}

WORKDIR ${WORKDIR}
COPY --from=nannos-build ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=nannos-build ${WORKDIR}/../agent-common ../agent-common
COPY --from=nannos-build ${WORKDIR} .

CMD ["/bin/sh", "-c", "./.venv/bin/python -m main --port ${API_PORT}"]
