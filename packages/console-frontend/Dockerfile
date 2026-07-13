# syntax=docker/dockerfile:1.4
FROM node:22-alpine AS build

WORKDIR /repo

# npm-workspace install: dependency resolution is owned by the ROOT manifest +
# lockfile (console-frontend consumes @nannos/embed-sdk as a workspace member).
# The root and embed-sdk arrive as named build contexts (see justfile):
#   --build-context workspace-root=.  --build-context embed-sdk=packages/embed-sdk
COPY --from=workspace-root package.json package-lock.json ./
COPY --from=embed-sdk package.json packages/embed-sdk/
COPY package.json packages/console-frontend/
# npm ci skips platform-specific optional deps of nested packages
# (npm/cli#4828), which breaks rollup/tailwind/lightningcss native bindings on
# alpine. Install them explicitly: arch from BuildKit, versions pinned to the
# JS packages the lockfile resolved (native ABI versions match 1:1).
ARG TARGETARCH
RUN npm ci && \
    A=$([ "$TARGETARCH" = "amd64" ] && echo x64 || echo "$TARGETARCH") && \
    npm i --no-save \
      "@rollup/rollup-linux-${A}-musl@$(node -p "require('rollup/package.json').version")" \
      "@tailwindcss/oxide-linux-${A}-musl@$(node -p "require('@tailwindcss/oxide/package.json').version")" \
      "lightningcss-linux-${A}-musl@$(node -p "require('lightningcss/package.json').version")"

# Build the embed SDK first — console imports its dist via package exports.
COPY --from=embed-sdk . packages/embed-sdk/
RUN npm run build --workspace=@nannos/embed-sdk

# Build the console app.
COPY . packages/console-frontend/
RUN npm run build --workspace=console-frontend

FROM nginxinc/nginx-unprivileged:1.27-alpine
LABEL org.opencontainers.image.source=https://github.com/ringier-data/nannos

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/packages/console-frontend/dist /usr/share/nginx/html

EXPOSE 8081
