# `deploy/portal/`

Dockerfiles for the portal images consumed by the top-level
`deploy/compose.yaml`. Operators normally don't invoke them directly —
`docker compose -f deploy/compose.yaml up -d --build` does the wiring,
including:

- reading the wheel filename from `deploy/.env` (`PORTAL_WHEEL=...`)
- using `deploy/` as the build context so `COPY packages/${PORTAL_WHEEL}`
  resolves against the host wheel drop directory
- tagging the result as `xauditor-portal-backend:local` /
  `xauditor-portal-frontend:local` (overridable with
  `PORTAL_BACKEND_IMAGE` / `PORTAL_FRONTEND_IMAGE` to skip the local
  build and pull a registry tag instead)

## Files

- `backend.Dockerfile` — installs `xauditor-portal` from the wheel, runs
  Alembic migrations on container start, serves FastAPI under uvicorn.
- `frontend.Dockerfile` — extracts the frontend tree from the same
  wheel (shipped as package data), runs `next build`, ships the
  Next.js standalone bundle on `node:20-slim`.

## Building one image directly

If you really want to invoke `docker build` by hand for a single
service, do it from the parent `deploy/` directory so the build context
matches what compose.yaml uses:

```bash
cd deploy
docker build -t xauditor-portal-backend:local \
  --build-arg PORTAL_WHEEL=xauditor_portal-0.3.0-py3-none-any.whl \
  -f portal/backend.Dockerfile \
  .
```

## See also

- `deploy/README.md` — full operator bring-up walkthrough
- `deploy/compose.yaml` — the stack definition
- `deploy/coder-service/README.md` — the optional coder verification sidecar
