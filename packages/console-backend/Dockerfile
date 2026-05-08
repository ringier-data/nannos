ARG WORKDIR="/opt/app"

FROM zhaowde/rambler:5.4.0-rag2 AS rambler-source

# Build the backend
# NOTE: ringier-a2a-sdk must be copied into agent-console/ before building
FROM docker.rcplus.io/python:3.13-slim AS backend-builder
ARG WORKDIR

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
# Copy shared SDK from named build context
COPY --from=ringier-a2a-sdk . ../ringier-a2a-sdk
COPY --from=object-storage . ../object-storage
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
RUN uv pip install validators

# --- API runtime (no LibreOffice/poppler — lighter image for console-backend) ---
FROM docker.rcplus.io/python:3.13-slim AS api
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos
ARG WORKDIR
ARG API_PORT
ENV API_PORT=${API_PORT}

# Install postgresql-client (required by migrate.sh for psql)
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*

# Copy rambler binary for running migrations
COPY --from=rambler-source /rambler /usr/local/bin/rambler
RUN chmod +x /usr/local/bin/rambler

WORKDIR ${WORKDIR}
COPY --from=backend-builder ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=backend-builder ${WORKDIR}/../object-storage ../object-storage
COPY --from=backend-builder ${WORKDIR} .

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

COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Expose the port the backend server runs on
EXPOSE ${API_PORT}

ENTRYPOINT ["./entrypoint.sh"]


# --- Catalog worker runtime (poppler for PDF thumbnail rendering) ---
FROM docker.rcplus.io/python:3.13-slim AS catalog-worker
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos
ARG WORKDIR

# poppler-utils provides pdfinfo/pdftoppm for PDF thumbnail rendering via pdf2image.
# LibreOffice (soffice) is NOT installed here — all PPTX conversion is
# delegated to the soffice-worker pod running in its own cgroup.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils && rm -rf /var/lib/apt/lists/*

WORKDIR ${WORKDIR}
COPY --from=backend-builder ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=backend-builder ${WORKDIR}/../object-storage ../object-storage
COPY --from=backend-builder ${WORKDIR} .

ENTRYPOINT [".venv/bin/python", "catalog_worker.py"]
