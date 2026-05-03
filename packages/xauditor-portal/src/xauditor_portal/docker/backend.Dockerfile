# syntax=docker/dockerfile:1.6
#
# xauditor-portal backend image.
#
# Build context: `src/xauditor_portal/` (the installed Python package
# directory). The portal CLI passes that location as the docker build
# context; this Dockerfile copies the package source tree into the
# image and installs runtime deps from a shipped requirements file.
#
# Dev and wheel installs both work because the Dockerfile + requirements
# travel with the Python package (not at the sdist root).
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime dependencies. The requirements file lives inside the
# package (docker/requirements.txt) and mirrors the pyproject dep list
# so this stage works without pyproject.toml in the build context.
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# Copy the package tree itself into /app/xauditor_portal. The frontend
# subtree is excluded by .dockerignore at the build-context root.
COPY . /app/xauditor_portal

ENV PYTHONPATH=/app

EXPOSE 8000

# Container readiness is probed from the host CLI via `docker exec`.
#
# Startup runs Alembic migrations first so a freshly-built image never
# serves API traffic against an out-of-date schema. The portal backend
# is deployed as a single instance by the managed runtime, so we do not
# need distributed locking here — if you horizontally scale the backend,
# serialize migrations externally (one-shot migrate job, leader election,
# or pg advisory lock in an entrypoint wrapper).
#
# Migration URL resolution: the managed runtime sets
# `XAUDITOR_PORTAL_DATABASE_URL` on the container env, which both the
# migrate command and the FastAPI lifespan consume. If you run this image
# manually, set that env var explicitly; otherwise `xauditor-portal
# migrate` will try to load the host xauditor config and fail (xauditor
# is not installed in this image).
CMD ["sh", "-c", "python -m xauditor_portal.cli migrate && exec uvicorn xauditor_portal.app:create_app --factory --host 0.0.0.0 --port 8000"]
