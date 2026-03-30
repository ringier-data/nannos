FROM zhaowde/rambler:5.4.0-rag2 AS rambler-source

FROM node:24-alpine AS builder

ARG NPMRC_ENCODED

WORKDIR /app

ADD package.json package-lock.json tsconfig.json ./
COPY . .

RUN echo $NPMRC_ENCODED | base64 -d > ~/.npmrc \
    && npm ci --no-color --audit=false \
    && npm --no-color run build


FROM node:24-alpine
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos

ARG NPMRC_ENCODED

ENV NODE_ENV=production

# Install dependencies for migrations (gcompat for glibc-linked rambler binary)
RUN apk add --no-cache gcompat netcat-openbsd postgresql-client bash

# Copy rambler binary for running migrations
COPY --from=rambler-source /rambler /usr/local/bin/rambler
RUN chmod +x /usr/local/bin/rambler

WORKDIR /app

ADD package.json package-lock.json ./

RUN echo $NPMRC_ENCODED | base64 -d > ~/.npmrc \
    && npm ci --no-color --omit=dev --ignore-scripts --audit=false \
    && rm ~/.npmrc

COPY --from=builder /app/dist/src /app/dist/src

# Copy migration files
COPY sqlmigrations/ddl /sqlmigrations
COPY sqlmigrations/migrate.sh ./migrate.sh
RUN chmod +x ./migrate.sh

# Rambler environment defaults
ENV RAMBLER_DRIVER=postgresql
ENV RAMBLER_PROTOCOL=tcp
ENV RAMBLER_DIRECTORY=/sqlmigrations
ENV RAMBLER_TABLE=migrations
ENV RAMBLER_SSLMODE=require

COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["./entrypoint.sh"]
