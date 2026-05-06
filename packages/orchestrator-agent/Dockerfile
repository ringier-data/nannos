ARG WORKDIR="/opt/app"

FROM zhaowde/rambler:5.4.0-rag2 AS rambler-source

FROM docker.rcplus.io/python:3.13-slim AS nannos-build
ARG WORKDIR
ARG OTEL_ENABLED

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPYCACHEPREFIX="/tmp"

# Install uv
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
COPY --from=object-storage . ../object-storage
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM docker.rcplus.io/python:3.13-slim AS nannos-runtime
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos

ARG WORKDIR

# Install postgresql-client (required by migrate.sh for psql)
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*

# Copy rambler binary for running migrations
COPY --from=rambler-source /rambler /usr/local/bin/rambler
RUN chmod +x /usr/local/bin/rambler

WORKDIR ${WORKDIR}
COPY --from=nannos-build ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=nannos-build ${WORKDIR}/../agent-common ../agent-common
COPY --from=nannos-build ${WORKDIR}/../object-storage ../object-storage
COPY --from=nannos-build ${WORKDIR} .

# Copy migration files
COPY sqlmigrations/ddl /migrations
COPY sqlmigrations/migrate.sh ./migrate.sh
RUN chmod +x ./migrate.sh

# Rambler environment defaults
ENV RAMBLER_DRIVER=postgresql
ENV RAMBLER_PROTOCOL=tcp
ENV RAMBLER_DIRECTORY=/migrations
ENV RAMBLER_TABLE=migrations
ENV RAMBLER_SSLMODE=require

# Download NLTK punkt tokenizer for semantic chunking
RUN ./.venv/bin/python -m nltk.downloader punkt

COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
