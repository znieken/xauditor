# syntax=docker/dockerfile:1.6
#
# xauditor-coder-service container image (in-package Dockerfile).
#
# Build context: ``src/xauditor_coder_service/`` — the installed Python
# package directory. ``xauditor coder build`` passes that location as
# the docker build context; this Dockerfile copies the package source
# tree into the image and installs runtime deps from a shipped
# requirements file. Mirrors the ``xauditor-portal`` pattern.
#
# Both editable and wheel installs work because the Dockerfile +
# requirements travel with the Python package (not at the repo root).
FROM node:22-bookworm-slim AS node-stage

# Install Claude Code globally so it lives at /usr/local/bin/claude in
# the final image. Separate stage so the runtime image inherits both
# python and node tooling cleanly.
#
# After install, ASSERT that the CLI surface we depend on still exists.
# claude-code 1.x → 2.x renamed flags (e.g. --thinking-effort →
# --effort) and any future minor can do the same. Failing the build
# now beats failing every audit run silently as Inconclusive results.
# The four checks cover everything our worker depends on:
#   -p / --print       : non-interactive print mode
#   --effort           : thinking-effort level
#   ANTHROPIC_API_KEY  : auth (env)
#   ANTHROPIC_BASE_URL : base URL (env, no flag exists)
#   ANTHROPIC_MODEL    : model selection (env)
# The env vars are checked by grepping the binary's strings — they're
# the only way claude exposes them, since `claude --help` doesn't
# document env vars.
RUN npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && claude --help 2>&1 | grep -qE -- '-p, --print' \
        || (echo 'CLAUDE-FLAG-CHECK: -p/--print flag missing — claude code may have changed its non-interactive flag' >&2 && exit 1) \
    && claude --help 2>&1 | grep -qE -- '--effort <level>' \
        || (echo 'CLAUDE-FLAG-CHECK: --effort flag missing — was it renamed?' >&2 && exit 1) \
    && grep -aq 'ANTHROPIC_API_KEY' /usr/local/bin/claude \
        || (echo 'CLAUDE-ENV-CHECK: ANTHROPIC_API_KEY string missing from binary' >&2 && exit 1) \
    && grep -aq 'ANTHROPIC_BASE_URL' /usr/local/bin/claude \
        || (echo 'CLAUDE-ENV-CHECK: ANTHROPIC_BASE_URL string missing from binary' >&2 && exit 1) \
    && grep -aq 'ANTHROPIC_MODEL' /usr/local/bin/claude \
        || (echo 'CLAUDE-ENV-CHECK: ANTHROPIC_MODEL string missing from binary' >&2 && exit 1)


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

WORKDIR /app

# Install runtime dependencies. The requirements file lives inside the
# package (docker/requirements.txt) and mirrors the pyproject dep list
# so this stage works without pyproject.toml in the build context.
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# Copy the package tree itself into /app/xauditor_coder_service. The
# docker/ subtree (including this Dockerfile) is dropped via
# .dockerignore at the build-context root.
COPY . /app/xauditor_coder_service

ENV PYTHONPATH=/app

# Drop privileges. The repo bind-mount is read-only and the image is
# read-only at runtime, so the only writable surface inside the
# container is /tmp (mounted as a tmpfs by the operator's runtime).
RUN useradd --create-home --uid 1000 --shell /bin/bash coder \
    && mkdir -p /workspace \
    && chown coder:coder /workspace

USER coder
WORKDIR /workspace

# Default bind: 0.0.0.0:8090 inside the container. The host runtime
# (`xauditor coder init`) decides whether to publish a TCP port or
# bind-mount a Unix socket via XAUDITOR_CODER_SERVICE_BIND.
ENV XAUDITOR_CODER_SERVICE_BIND="0.0.0.0:8090"

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --silent --fail http://127.0.0.1:8090/health || exit 1

ENTRYPOINT ["python", "-m", "xauditor_coder_service"]
