# `xauditor-coder-service` deployment

This directory ships the container image and a docker-compose file for
the xauditor coder verification microservice.

## When to use the HTTP transport

Use the HTTP microservice when you want any of:

- **Warm worker pool**: re-use the Claude Code CLI tool-init across
  verifications. The first finding pays the multi-second startup, the
  rest don't.
- **Remote / k8s deployment**: run the verification fleet on dedicated
  (potentially GPU-rich) hosts and have xauditor talk to them over the
  network.
- **Resource isolation**: cap the verification worker pool's CPU /
  memory at the container boundary, independent of xauditor's own
  process.

If none of those apply, leave `coder.transport: subprocess` (the default)
and skip this directory entirely.

## Local sidecar (one-machine setup)

### Lifecycle-managed (recommended)

The `xauditor coder` verbs build and run the container directly from
the installed `xauditor-coder-service` Python package — no wheel files
to stage, no docker-compose plumbing.

```bash
pip install xauditor xauditor-coder-service
export ANTHROPIC_API_KEY=sk-ant-...
xauditor coder init             # build + start, zero cp
xauditor audit run ...
```

The build resolves the Dockerfile through `importlib.metadata` against
the installed `xauditor-coder-service` package — same pattern as
`xauditor portal build` resolves `xauditor-portal`. The Dockerfile
itself lives at
`<site-packages>/xauditor_coder_service/docker/Dockerfile`; the build
context is the installed package directory and `COPY .` inside the
Dockerfile picks up the package source tree.

### Multi-project workspace

Since the `multi-project-coder-service` change (xauditor 0.11.0+), the
container's `/workspace` bind-mount is a parent directory whose direct
child subdirectories ARE the audit-able projects. Adding a new project
is `mkdir + git clone` on the host with no container restart; xauditor
derives the project name from `basename(realpath(audit.repo_root))` by
default, and every `POST /verifications` carries a `project` field that
the service validates against an allowlist + realpath check before
admitting the worker.

Set `CODER_WORKSPACE_ROOT` in your environment before bringing up the
compose file (or rely on the `${PWD}` fallback for the legacy
single-repo flow):

```bash
mkdir -p ~/coder-workspace
git clone git@github.com:foo/secmind.git    ~/coder-workspace/secmind
git clone git@github.com:foo/othertool.git  ~/coder-workspace/othertool

export CODER_WORKSPACE_ROOT=~/coder-workspace
docker compose -f deploy/coder-service/docker-compose.coder.yml up -d
```

Match the xauditor side:

```yaml
coder:
  enabled: true
  transport: http
  endpoint: http://127.0.0.1:8090
  workspace_root: /home/me/coder-workspace        # same path on host
  # project_name: secmind                          # optional override
```

`xauditor coder status` lists the discovered projects from the live
service (or falls back to a host-side scan with a
`(host scan; container unreachable)` annotation when the service is
down). New projects appear immediately — no `coder` lifecycle command,
no container restart. Concurrent verifications across different
projects are isolated via per-project `HOME=/home/coder/<project>/`,
backed by the named `xauditor-coder-home` docker volume so caches
survive `coder stop` / `start`.

### Compose / manual `docker build` path

Only needed when the lifecycle commands are not available (e.g. cluster
deployment scripts that already speak compose). Drop a wheel into
`deploy/packages/` and pass its filename via build-arg:

```bash
mkdir -p deploy/packages
cp /path/to/xauditor_coder_service-*.whl deploy/packages/
echo 'CODER_WHEEL=xauditor_coder_service-0.2.0-py3-none-any.whl' > deploy/.env

docker build \
  --build-arg CODER_WHEEL=xauditor_coder_service-0.2.0-py3-none-any.whl \
  -t xauditor-coder-service:local \
  -f deploy/coder-service/Dockerfile \
  deploy/

docker compose -f deploy/coder-service/docker-compose.coder.yml up -d
```

The outer `deploy/coder-service/Dockerfile` mirrors
`deploy/portal/backend.Dockerfile`'s pattern: `pip install` the wheel
into a clean Python base image. The lifecycle path uses the in-package
Dockerfile instead.

2. Bring up the sidecar:

   ```bash
   docker compose -f deploy/coder-service/docker-compose.coder.yml up -d
   ```

3. Configure xauditor to use it:

   ```yaml
   # xauditor.yml
   coder:
     enabled: true
     transport: http
     endpoint: http://127.0.0.1:8090
   ```

4. Run an audit. The pre-flight check probes `GET /health`; the
   verification stage POSTs each finding and long-polls for the verdict.

## Authentication

Authentication is **disabled by default**. To turn it on:

1. Generate a token (any opaque random string is fine):

   ```bash
   export XAUDITOR_CODER_TOKEN="$(openssl rand -hex 32)"
   ```

2. Restart the sidecar with the matching env vars:

   ```bash
   XAUDITOR_CODER_SERVICE_ENABLE_AUTH=true \
   XAUDITOR_CODER_SERVICE_TOKEN="$XAUDITOR_CODER_TOKEN" \
     docker compose -f deploy/coder-service/docker-compose.coder.yml up -d --force-recreate
   ```

3. Mirror on the xauditor side:

   ```yaml
   coder:
     enable_auth: true
     endpoint_token: "<paste $XAUDITOR_CODER_TOKEN>"
   ```

   Or via env var: `export XAUDITOR_CODER_ENDPOINT_TOKEN="$XAUDITOR_CODER_TOKEN"`.

xauditor redacts the token from `repr` and registers it with the
runtime logger redactor, so it never appears in stderr or `logging.file`.

## Remote / k8s deployment (sketch)

The image works the same way under any orchestrator. A minimal k8s
deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: xauditor-coder-service
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: coder
          image: registry.example.com/xauditor-coder-service:0.1.0
          env:
            - name: XAUDITOR_CODER_SERVICE_BIND
              value: "0.0.0.0:8090"
            - name: XAUDITOR_CODER_SERVICE_ENABLE_AUTH
              value: "true"
            - name: XAUDITOR_CODER_SERVICE_TOKEN
              valueFrom:
                secretKeyRef:
                  name: xauditor-coder-token
                  key: token
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: anthropic-api-key
                  key: key
          ports:
            - containerPort: 8090
          # Mount the repo as a PVC, or rebuild the image with the repo
          # baked in for read-only audit fleets. The container expects
          # /workspace to exist and contain the code under audit.
          volumeMounts:
            - name: repo
              mountPath: /workspace
              readOnly: true
      volumes:
        - name: repo
          persistentVolumeClaim:
            claimName: audit-repo-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: xauditor-coder-service
spec:
  selector:
    app: xauditor-coder-service
  ports:
    - port: 443
      targetPort: 8090
```

Behind a TLS-terminating ingress, your xauditor config becomes:

```yaml
coder:
  transport: http
  endpoint: https://coder-pool.internal/
  enable_auth: true
  endpoint_token: "${CODER_TOKEN}"
```

xauditor's startup INFO log will print:

```
Coder transport: http via https://coder-pool.internal/ (auth: enabled)
```

so SIEM / log-scraping rules can flag deployments that drift from the
expected posture.

## Operational verbs

For day-to-day operations the recommended path is the dedicated
`xauditor coder` lifecycle commands shipped in the next change
(`add-coder-runtime-management`). Until that lands, manage the sidecar
with raw `docker compose` as above.
