ARG WORKDIR="/opt/app"

# Build the backend
# NOTE: ringier-a2a-sdk must be copied into playground-backend/ before building
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
RUN mv ringier-a2a-sdk ../ringier-a2a-sdk
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
RUN uv pip install validators

# Final runtime image
FROM docker.rcplus.io/python:3.13-slim AS runtime
ARG WORKDIR
ARG API_PORT
ENV API_PORT=${API_PORT}

WORKDIR ${WORKDIR}
COPY --from=backend-builder ${WORKDIR}/../ringier-a2a-sdk ../ringier-a2a-sdk
COPY --from=backend-builder ${WORKDIR} .
# Expose the port the backend server runs on
EXPOSE ${API_PORT}
# The command to run the server.
# Note: We must use asgi_app (not app) to include the Socket.IO ASGI wrapper
WORKDIR ${WORKDIR}/src
CMD ["/bin/sh", "-c", "../.venv/bin/uvicorn app:asgi_app --host 0.0.0.0 --port ${API_PORT} --log-config ../log_conf.yml --no-access-log --no-use-colors"]
