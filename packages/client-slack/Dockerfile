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

WORKDIR /app

ADD package.json package-lock.json ./

RUN echo $NPMRC_ENCODED | base64 -d > ~/.npmrc \
    && npm ci --no-color --omit=dev --ignore-scripts --audit=false \
    && rm ~/.npmrc

COPY --from=builder /app/dist/src /app/dist/src

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/node"]

CMD ["dist/src/index.js"]
