ARG WORKDIR="/opt/app"

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
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM docker.rcplus.io/python:3.13-slim AS nannos-runtime
ARG WORKDIR

WORKDIR ${WORKDIR}
COPY --from=nannos-build ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=nannos-build ${WORKDIR}/../agent-common ../agent-common
COPY --from=nannos-build ${WORKDIR} .

# Download NLTK punkt tokenizer for semantic chunking
RUN ./.venv/bin/python -m nltk.downloader punkt

CMD ["./.venv/bin/python", "main.py"]
