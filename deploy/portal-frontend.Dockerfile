# syntax=docker/dockerfile:1.6
#
# Build the xauditor-portal frontend container directly from a pre-built
# xauditor-portal Python wheel. The wheel's package-data includes the full
# Next.js source tree under `xauditor_portal/frontend/`, so we unzip the
# wheel in stage 1, run the standard Next.js build in stage 2, and ship
# the standalone bundle in stage 3.
#
# Keeps the frontend image self-contained: no git clone, no repo checkout
# — only the wheel drop in `deploy/packages/`.

FROM alpine:3.20 AS extractor
RUN apk add --no-cache unzip
ARG PORTAL_WHEEL
RUN test -n "${PORTAL_WHEEL}" \
    || (echo "error: PORTAL_WHEEL build-arg is required — set PORTAL_WHEEL=<exact-wheel-filename> in deploy/.env" >&2 && exit 1)
COPY packages/${PORTAL_WHEEL} /tmp/portal.whl
RUN mkdir -p /extract \
    && unzip -q /tmp/portal.whl -d /extract \
    && test -f /extract/xauditor_portal/frontend/package.json \
    || (echo "error: wheel does not contain xauditor_portal/frontend/; check the wheel's package-data include list" >&2 && exit 2)


FROM node:20-slim AS builder

WORKDIR /app

COPY --from=extractor /extract/xauditor_portal/frontend/ ./

# Production image doesn't run tests; drop test files before `next build`
# type-checks them. Without this, Next.js tries to compile
# `vitest.config.ts` + `tests/**/*.test.tsx` and trips on a vitest/vite
# type-version mismatch that is unrelated to the runtime.
RUN rm -rf tests vitest.config.ts vitest.setup.ts 2>/dev/null || true

ENV NEXT_TELEMETRY_DISABLED=1
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi

RUN npm run build


FROM node:20-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# Docker auto-sets HOSTNAME to the container id; Next.js's standalone
# server.js reads HOSTNAME as the bind address and would otherwise listen
# only on the container-hostname interface — which breaks any in-container
# probe (`localhost:3000`). Forcing HOSTNAME=0.0.0.0 binds all interfaces
# so the healthcheck can reach the server AND external port-forwarding
# keeps working.

WORKDIR /app

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
