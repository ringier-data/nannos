ARG WORKDIR="/opt/app"

FROM docker.rcplus.io/python:3.13-slim AS alloy-build
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
RUN mv ringier-a2a-sdk ../ringier-a2a-sdk
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM docker.rcplus.io/python:3.13-slim AS alloy-runtime
ARG WORKDIR

WORKDIR ${WORKDIR}
COPY --from=alloy-build ${WORKDIR} .
CMD ["./.venv/bin/python", "main.py"]
