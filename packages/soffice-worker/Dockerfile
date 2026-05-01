ARG WORKDIR="/opt/app"

# Build stage: install Python deps with uv
FROM docker.rcplus.io/python:3.13-slim AS builder
ARG WORKDIR

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.7.15 /uv /uvx /bin/

WORKDIR ${WORKDIR}
# Copy all package files — uv sync --frozen needs both pyproject.toml and uv.lock
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project


# Runtime stage: slim base + LibreOffice
FROM docker.rcplus.io/python:3.13-slim AS runtime
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos
ARG WORKDIR

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# libreoffice-impress-nogui: soffice --headless PPTX→ODP/PDF without X11 deps
# libjemalloc2: reduces RSS fragmentation from repeated soffice child process
#   allocations; soffice allocates and frees ~700 MB of image data per conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress-nogui \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR ${WORKDIR}
COPY --from=builder ${WORKDIR} .
COPY main.py .

# Preload jemalloc for the worker process (arch-agnostic glob)
ENTRYPOINT ["/bin/sh", "-c", \
    "export LD_PRELOAD=$(ls /usr/lib/*/libjemalloc.so.2 2>/dev/null | head -1) && \
     exec .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
