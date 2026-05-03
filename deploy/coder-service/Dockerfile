# syntax=docker/dockerfile:1.6
#
# Build the xauditor-coder-service container directly from a pre-built
# xauditor-coder-service Python wheel — same pattern as
# `deploy/portal/backend.Dockerfile`. This removes the source-checkout
# requirement from the deploy path: a VM only needs docker + docker
# compose + a wheel drop.
#
# The wheel lives at `deploy/packages/${CODER_WHEEL}` where
# `CODER_WHEEL` is set in `deploy/.env` to the exact filename (e.g.
# `xauditor_coder_service-0.2.0-py3-none-any.whl`).
#
# For the lifecycle-managed flow (`xauditor coder build`), use the
# in-package Dockerfile at
# ``<site-packages>/xauditor_coder_service/docker/Dockerfile`` instead —
# the lifecycle commands resolve it automatically.

FROM node:22-bookworm-slim AS node-stage

RUN npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Bring node + the globally-installed claude binary across.
COPY --from=node-stage /usr/local/bin/node          /usr/local/bin/node
COPY --from=node-stage /usr/local/bin/claude        /usr/local/bin/claude
COPY --from=node-stage /usr/local/lib/node_modules  /usr/local/lib/node_modules
RUN ln -sf /usr/local/bin/node /usr/local/bin/nodejs

# Standard tooling Claude Code's tools rely on (git, curl, ca-certs).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install xauditor-coder-service from the dropped wheel. Transitive deps
# resolve from PyPI by default; pass PIP_INDEX_URL / PIP_EXTRA_INDEX_URL
# as build-args if running inside a private network. Keep the wheel
# filename verbatim — pip rejects anything that doesn't match the PEP 427
# `<name>-<version>-<python>-<abi>-<platform>.whl` pattern.
ARG CODER_WHEEL
RUN test -n "${CODER_WHEEL}" \
    || (echo "error: CODER_WHEEL build-arg is required — set CODER_WHEEL=<exact-wheel-filename> in deploy/.env" >&2 && exit 1)
COPY packages/${CODER_WHEEL} /tmp/${CODER_WHEEL}
RUN pip install /tmp/${CODER_WHEEL}
RUN rm /tmp/${CODER_WHEEL}

RUN useradd --create-home --uid 1000 --shell /bin/bash coder \
    && mkdir -p /workspace \
    && chown coder:coder /workspace

USER coder
WORKDIR /workspace

ENV XAUDITOR_CODER_SERVICE_BIND="0.0.0.0:8090"

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --silent --fail http://127.0.0.1:8090/health || exit 1

ENTRYPOINT ["python", "-m", "xauditor_coder_service"]
