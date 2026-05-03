# syntax=docker/dockerfile:1.6
#
# Build the xauditor-portal backend container directly from a pre-built
# xauditor-portal Python wheel (same wheel the `pypi` service hosts for
# analyst `pip install`). This removes the git-clone requirement from the
# deploy path — the VM needs only docker + docker compose + a wheel drop.
#
# The wheel lives at `deploy/packages/${PORTAL_WHEEL}` where
# `PORTAL_WHEEL` is set in `deploy/.env` to the exact filename (e.g.
# `xauditor_portal-0.2.3-py3-none-any.whl`).

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG PORTAL_WHEEL
RUN test -n "${PORTAL_WHEEL}" \
    || (echo "error: PORTAL_WHEEL build-arg is required — set PORTAL_WHEEL=<exact-wheel-filename> in deploy/.env" >&2 && exit 1)

# Copy the wheel (only) into a scratch dir and install it. Transitive deps
# resolve from the public PyPI by default; pass `PIP_INDEX_URL` /
# `PIP_EXTRA_INDEX_URL` as build-args if operating inside a private network.
#
# We keep the stock pip that ships with python:3.12-slim rather than
# `pip install --upgrade pip` — a fresh pip major (e.g. 26.x) can land with
# metadata-compat breaks, and bundling the upgrade into the same RUN as the
# wheel install makes failures ambiguous under `docker compose build`'s
# interleaved output. Separate RUN layers also surface the real error.
# Keep the original wheel filename — pip rejects anything that doesn't match
# the PEP 427 `<name>-<version>-<python>-<abi>-<platform>.whl` pattern.
COPY packages/${PORTAL_WHEEL} /tmp/${PORTAL_WHEEL}
RUN pip install /tmp/${PORTAL_WHEEL}
RUN rm /tmp/${PORTAL_WHEEL}

EXPOSE 8000

# Same CMD as the package's in-tree backend.Dockerfile: migrate then uvicorn.
CMD ["sh", "-c", "python -m xauditor_portal.cli migrate && exec uvicorn xauditor_portal.app:create_app --factory --host 0.0.0.0 --port 8000"]
