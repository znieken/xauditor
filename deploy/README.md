# Self-hosted xauditor stack

This directory is a turnkey `docker compose` deployment of the xauditor
stack — graph store (Neo4j), report database (Postgres), portal backend
and frontend, an internal PyPI index, and a static HTTP server hosting the
analyst install script.

The stack targets a remote VM whose only installed tooling is `docker` and
`docker compose`. Operators bring the stack up, drop xauditor wheels into
`deploy/packages/`, and share two URLs with their team: the portal UI and
the analyst install one-liner. Analyst laptops install with a single
`curl | bash`, then write the remote Neo4j / Postgres endpoints into their
`~/.xauditor/xauditor.yml` to run audits against the shared stack.

## Directory layout

```
deploy/
├── README.md                          this file
├── compose.yaml                       full self-hosted stack (graphdb + reportdb + portal + pypi + install-server)
├── .env.example                       copy → .env, fill in passwords + PORTAL_WHEEL
├── install/
│   └── install.sh                     analyst bootstrap script — `install-server` serves it
├── packages/                          drop xauditor wheels here (compose builds from + pypi serves from)
├── portal/                            portal images (consumed by compose.yaml)
│   ├── backend.Dockerfile
│   └── frontend.Dockerfile
└── coder-service/                     coder verification microservice (independent of compose.yaml)
    ├── Dockerfile
    ├── docker-compose.coder.yml
    └── README.md                      sidecar walkthrough; opt-in, see top-level README
```

The `compose.yaml` stack and the `coder-service/` sidecar are deliberately
separate: most operators deploy the portal stack remotely while running the
coder service locally on the audit-controller host (or not at all, if they
prefer the host-installed Claude Code under `coder.transport: subprocess`).
See `coder-service/README.md` for the coder sidecar's own bring-up flow.

---

## Operator bring-up

The VM only needs `docker` + `docker compose` + the `deploy/` directory
(copied via git, scp, or tarball — your choice) + one or more xauditor /
xauditor-portal wheels dropped into `deploy/packages/`. No Python, no
`xauditor` CLI, no source checkout required.

1. **Build wheels on your dev machine (or CI).**
   ```bash
   cd /path/to/xauditor
   uv build                                    # produces dist/xauditor-*.whl
   uv build --project packages/xauditor-portal # produces packages/xauditor-portal/dist/xauditor_portal-*.whl
   ```

2. **Ship the deploy tree and the wheels to the VM.**
   ```bash
   # Option A: git
   git clone https://github.com/<you>/xauditor.git /srv/xauditor
   # Option B: tarball
   tar -czf deploy.tgz deploy/ && scp deploy.tgz <vm>:/srv/ && ssh <vm> 'cd /srv && tar -xzf deploy.tgz'

   # Then drop the wheels in
   scp dist/xauditor-*.whl dist/xauditor-*.tar.gz \
       packages/xauditor-portal/dist/xauditor_portal-*.whl \
       packages/xauditor-portal/dist/xauditor_portal-*.tar.gz \
       <vm>:/srv/xauditor/deploy/packages/
   ```
   The same `deploy/packages/` directory serves two purposes: the PyPI
   container indexes it for analyst `pip install`, AND the portal-backend
   / portal-frontend containers build *from* a wheel inside it.

3. **Configure `deploy/.env`.**
   ```bash
   ssh <vm>
   cd /srv/xauditor/deploy
   cp .env.example .env
   chmod 600 .env
   $EDITOR .env
   ```
   Required fields:
   - `NEO4J_PASSWORD` — set to a strong password.
   - `REPORTDB_PASSWORD` — set to a strong password.
   - `PORTAL_WHEEL` — exact filename of the xauditor-portal wheel under
     `deploy/packages/` (e.g. `xauditor_portal-0.2.3-py3-none-any.whl`).
     Update this + rebuild every time you bump the portal version.

4. **Bring the stack up.**
   ```bash
   docker compose up -d --build
   ```
   Six containers come up in a health-gated sequence. The portal-backend
   and portal-frontend images are built from `PORTAL_WHEEL` during the
   `--build` step. Check `docker compose ps` until all six say `healthy`:
   - `xauditor-neo4j`
   - `xauditor-reportdb`
   - `xauditor-portal-backend` (runs Alembic migrations automatically on start)
   - `xauditor-portal-frontend`
   - `xauditor-pypi`
   - `xauditor-install-server`

5. **Share URLs with the team.**
   - Portal UI: `http://<vm-host>:${PORTAL_HOST_PORT:-8080}/`
   - Analyst install one-liner:
     ```
     curl http://<vm-host>:8082/install.sh | bash -s -- http://<vm-host>:8081/simple/
     ```
   - Neo4j bolt endpoint: `bolt://<vm-host>:7687`
   - Postgres endpoint: `postgresql://xauditor:<REPORTDB_PASSWORD>@<vm-host>:5432/xauditor_reportdb`

### Upgrading the portal

When you publish a new xauditor-portal version:

```bash
# 1. Ship the new wheel to deploy/packages/
scp packages/xauditor-portal/dist/xauditor_portal-0.3.0-py3-none-any.whl <vm>:/srv/xauditor/deploy/packages/

# 2. On the VM: update PORTAL_WHEEL in .env and rebuild
ssh <vm>
cd /srv/xauditor/deploy
$EDITOR .env                     # PORTAL_WHEEL=xauditor_portal-0.3.0-py3-none-any.whl
docker compose up -d --build portal-backend portal-frontend
```

The backend's startup CMD runs Alembic migrations before uvicorn so no
out-of-band migration step is needed.

---

## Analyst install

On a laptop with Python 3.12+ available:

```bash
curl http://<vm-host>:8082/install.sh | bash -s -- http://<vm-host>:8081/simple/
```

This creates a virtualenv at `~/.xauditor-venv/`, installs `xauditor` from
the stack's PyPI index, and prints the path to the installed binary. Add
it to your `PATH`:

```bash
export PATH="$HOME/.xauditor-venv/bin:$PATH"
xauditor --help
```

Optional: also install `xauditor-portal` into the venv with
`XAUDITOR_INSTALL_PORTAL=1` before the `bash -s --`:

```bash
curl http://<vm-host>:8082/install.sh \
  | XAUDITOR_INSTALL_PORTAL=1 bash -s -- http://<vm-host>:8081/simple/
```

### Analyst laptop config

Edit `~/.xauditor/xauditor.yml` so `xauditor audit` writes to the shared
Neo4j and Postgres:

```yaml
graph:
  db:
    remote:
      url: bolt://<vm-host>:7687

reportdb:
  remote:
    url: postgresql://xauditor:<REPORTDB_PASSWORD>@<vm-host>:5432/xauditor_reportdb
```

Passwords come from the operator out-of-band (1Password, Keychain, signed
DM, whatever).

---

## Security checklist

**Before the first `docker compose up -d`:**

- [ ] Set `NEO4J_PASSWORD` and `REPORTDB_PASSWORD` in `deploy/.env` (the
      compose file fails fast if they are unset).
- [ ] `chmod 600 deploy/.env` so other users on the host can't read the
      passwords.
- [ ] Decide on network exposure:
      - **Behind a VPN / private network** (recommended): keep
        `*_BIND_ADDR=0.0.0.0` as the defaults; the VPN is the trust
        boundary.
      - **Public internet**: put a reverse proxy (Caddy / Nginx / Traefik)
        in front of ports 8080 (portal), 8081 (pypi), and 8082 (install)
        terminating TLS, and add basic auth or OAuth in front of pypi
        (which serves anonymous read by default). Consider binding the
        bolt + Postgres ports to `127.0.0.1` and tunneling them via SSH
        for analysts.

**Every time you rotate a password:**

1. Edit `deploy/.env`.
2. `docker compose up -d --force-recreate neo4j reportdb` (or the full
   stack). Rotating Neo4j's password requires a short outage; Postgres
   picks up the new password next time the container restarts.

---

## Backup and restore

```bash
# Neo4j
docker run --rm \
  -v xauditor-neo4j-data:/data \
  -v "$PWD":/backup \
  alpine tar -czf /backup/neo4j-$(date +%Y%m%d).tar.gz -C /data .

# Postgres (snapshot the volume; or use pg_dump inside the container)
docker exec xauditor-reportdb pg_dump -U xauditor xauditor_reportdb \
  | gzip > reportdb-$(date +%Y%m%d).sql.gz
```

`deploy/packages/` and `deploy/install/` are plain host directories —
back them up with `rsync` or `tar` directly.

---

## One-orchestrator-per-host

This compose stack and the CLI-managed flow (`xauditor portal start`)
share container names (`xauditor-portal-backend`, `xauditor-reportdb`,
…) and named volumes (`xauditor-neo4j-data`, `xauditor-reportdb-data`).
That is intentional — it lets on-box runbooks (`docker exec xauditor-*`)
work under either mode and lets you switch data between the two without
migration.

But it means **you pick one orchestrator per host**. If you have been
running `xauditor portal start` on the same VM and switch to compose, run
`xauditor portal stop` first. Trying to run both simultaneously will
cause container-name collisions.

---

## Troubleshooting

**`docker compose up` fails with `NEO4J_PASSWORD is required in deploy/.env`.**
That is the fail-fast behavior. Set the password in `deploy/.env` and
retry.

**`docker compose ps` shows `portal-backend` as `unhealthy`.**
Run `docker compose logs portal-backend`. The most common cause is a
reportdb that is slow to come up on cold start — the backend has a 60s
`start_period` to accommodate migrations, but on very slow hosts you may
need to increase it. Check that `reportdb` itself is `healthy` first.

**`pip install` can't find a package that I just dropped into
`deploy/packages/`.**
- Check the mount is wired: `docker exec xauditor-pypi ls /data/packages`
  should show your wheel.
- Confirm the wheel filename follows PEP 491 (e.g. `xauditor-0.1.2-py3-none-any.whl`).
- `curl http://<vm>:8081/simple/xauditor/` should list every version of
  `xauditor` the index knows about.

**`portal-backend` dies with `password authentication failed for user "xauditor"`.**
Postgres initializes `POSTGRES_USER` / `POSTGRES_PASSWORD` **only when the
data directory is empty**. If the `xauditor-reportdb-data` volume carries
data from a previous run (for example a CLI-managed `xauditor portal start`
or an earlier compose bring-up with a different password), Postgres
silently ignores the new `REPORTDB_PASSWORD` from `.env` and keeps the old
credentials, and the backend then fails to connect.

Fix either direction:

- **Keep old data, use old password**: set `REPORTDB_PASSWORD` in `.env`
  to the value that was in use when the volume was first initialized, then
  `docker compose up -d`. Rotate it in-place afterwards with
  `docker exec xauditor-reportdb psql -U xauditor -d xauditor_reportdb
  -c "ALTER USER xauditor WITH PASSWORD '<new>';"` and update `.env`.
- **Start fresh (destroys data)**: `docker compose down -v` drops the
  named volumes; the next `up -d` reinitializes Postgres with the new
  `REPORTDB_PASSWORD`.

**Analyst install script exits with "python3 not found".**
The install script needs Python 3.12+ on the analyst's laptop. Install it
with `brew install python@3.12`, `apt install python3.12`, or `uv python
install 3.12` as appropriate.

**`wget http://<vm>:8080/` returns `Redirection (307) without location` but
the portal loads fine in a browser.**
That's expected. Next.js's server-component `redirect()` on the landing
route returns a 307 whose navigation target lives in the RSC flight
payload instead of an HTTP `Location` header. Browsers handle it; plain
HTTP clients don't. Use a browser to access the UI, and use
`wget http://<vm>:8080/login` (which always returns 200) if you need a
scripted reachability probe. The compose healthcheck probes `/login` for
the same reason.
