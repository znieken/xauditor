"""Pure helpers shared by the coder runtime manager and the workflow.

No subprocess / docker calls live here; that lets the helpers be reused
under both the real :class:`CoderRuntimeManager` and the test double.
"""

from __future__ import annotations

import urllib.parse
from typing import Literal

from xauditor.config import CoderConfig


EndpointKind = Literal["unix", "loopback", "remote"]


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def endpoint_kind(coder_cfg: CoderConfig) -> EndpointKind:
    """Classify ``coder.endpoint`` so callers don't re-parse the URL.

    - ``unix://...``                  → ``"unix"``
    - ``http://localhost`` / ``127.0.0.1`` / ``[::1]`` → ``"loopback"``
    - everything else (including HTTPS, non-loopback HTTP, or empty)    → ``"remote"``

    The "remote" bucket is the conservative default: when the endpoint
    string is empty or unrecognised, callers SHALL refuse lifecycle
    operations (we don't want to silently manage a container we can't
    identify).
    """

    endpoint = (coder_cfg.endpoint or "").strip()
    if not endpoint:
        return "remote"
    if endpoint.startswith("unix://"):
        return "unix"
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(endpoint)
        except ValueError:
            return "remote"
        host = (parsed.hostname or "").strip().lower()
        if (
            endpoint.startswith("http://")
            and host in _LOOPBACK_HOSTS
        ):
            return "loopback"
    return "remote"


def endpoint_is_local(coder_cfg: CoderConfig) -> bool:
    """``True`` when the endpoint is a Unix socket OR loopback HTTP.

    Lifecycle commands (init / build / start / stop / reset) only act
    on local endpoints; remote ones are managed by the deployment layer.
    """

    return endpoint_kind(coder_cfg) in ("unix", "loopback")


def derive_socket_path(coder_cfg: CoderConfig, *, runtime_root: str | None = None) -> str:
    """Return the host filesystem path the lifecycle commands will bind-mount.

    Resolution order matches what ``_build_coder_config`` already
    encodes:

    1. Explicit ``coder.runtime_socket_path`` — used verbatim.
    2. ``coder.endpoint`` parsed when it is ``unix://...``.
    3. ``<runtime_root>/coder.sock`` when ``runtime_root`` is supplied.
    4. Empty string when nothing else matches (caller should refuse).
    """

    if coder_cfg.runtime_socket_path:
        return coder_cfg.runtime_socket_path
    endpoint = (coder_cfg.endpoint or "").strip()
    if endpoint.startswith("unix://"):
        return endpoint[len("unix://"):]
    if runtime_root:
        return runtime_root.rstrip("/") + "/coder.sock"
    return ""


def _resolve_loopback_port(coder_cfg: CoderConfig) -> int | None:
    """Pull the TCP port out of an ``http://loopback:PORT`` endpoint."""

    endpoint = (coder_cfg.endpoint or "").strip()
    if not endpoint.startswith("http://"):
        return None
    try:
        parsed = urllib.parse.urlparse(endpoint)
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        return None
    if parsed.port is None:
        return None
    return int(parsed.port)


def build_run_argv(
    coder_cfg: CoderConfig,
    *,
    repo_root: str,
    runtime_root: str | None = None,
) -> list[str]:
    """Build the full ``docker run`` argv for ``coder init`` / ``coder start``.

    Encodes the container parameters described in the
    ``coder-runtime-management`` capability:

    - ``--rm`` is OMITTED — we want the container to persist across audits
      so ``coder stop`` / ``coder start`` can recycle it.
    - ``-d`` (detached) — the lifecycle command returns immediately
      after starting; the readiness probe lives on top.
    - Bind-mount the repo at ``/workspace`` read-only.
    - Bind-mount the unix socket dir when applicable.
    - Publish the loopback TCP port when applicable.
    - Inject ``XAUDITOR_CODER_SERVICE_*`` env vars from the config.
    - Drop every Linux capability and run read-only with a tmpfs at /tmp.
    """

    # Multi-project bind-mount: ``coder.workspace_root`` is the new
    # canonical source. The deprecation shim populates
    # ``effective_workspace_root`` from the legacy ``repo_mount_path``
    # so this picks up either shape transparently. When both are
    # unset, fall back to the audit's repo_root so the legacy
    # single-repo init path still works.
    repo_mount_source = (
        coder_cfg.effective_workspace_root
        or coder_cfg.repo_mount_path
        or repo_root
    )
    home_volume = f"{coder_cfg.container_name}-home"
    argv: list[str] = [
        "docker", "run",
        "--detach",
        "--name", coder_cfg.container_name,
        "--read-only",
        "--tmpfs", "/tmp",
        # Claude Code 2.x writes session state, agent caches, and
        # settings under ``~/.claude/`` (and a few siblings) on
        # startup. Under ``--read-only`` rootfs those writes silently
        # block — claude hangs waiting for a directory it cannot
        # create. We give it a writable HOME via a docker NAMED
        # VOLUME (NOT tmpfs): tmpfs sits in host RAM and would let a
        # long-running worker accumulate claude session state until
        # the host runs out of memory. A named volume sits on disk,
        # is owned and cleaned up by docker's normal volume lifecycle,
        # and persists across ``coder stop`` / ``coder start``
        # (cheap state warmup); it is wiped explicitly by
        # ``coder reset`` (see ``CoderRuntimeManager.reset_runtime``).
        # Volume name is keyed off the container name so multiple
        # coder containers (rare but possible) don't share state.
        "--volume", f"{home_volume}:/home/coder",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--volume", f"{repo_mount_source}:/workspace:ro",
    ]
    kind = endpoint_kind(coder_cfg)
    if kind == "unix":
        socket_path = derive_socket_path(coder_cfg, runtime_root=runtime_root)
        if not socket_path:
            raise ValueError(
                "coder.endpoint is unix:// but no socket path could be resolved; "
                "set coder.runtime_socket_path explicitly."
            )
        # We bind-mount the socket's parent dir so the service can both
        # read and write the file, while the host xauditor sees the same
        # path to connect through.
        from os.path import dirname
        socket_dir = dirname(socket_path) or "/"
        argv.extend([
            "--volume", f"{socket_dir}:{socket_dir}",
            "--env", f"XAUDITOR_CODER_SERVICE_BIND=unix:{socket_path}",
        ])
    elif kind == "loopback":
        port = _resolve_loopback_port(coder_cfg) or 8090
        argv.extend([
            "--publish", f"127.0.0.1:{port}:{port}",
            "--env", f"XAUDITOR_CODER_SERVICE_BIND=0.0.0.0:{port}",
        ])
    else:
        raise ValueError(
            "build_run_argv called for a non-local endpoint; the lifecycle "
            "commands SHALL refuse remote endpoints upstream of this helper."
        )

    if coder_cfg.enable_auth:
        argv.extend([
            "--env", "XAUDITOR_CODER_SERVICE_ENABLE_AUTH=true",
            "--env", f"XAUDITOR_CODER_SERVICE_TOKEN={coder_cfg.endpoint_token}",
        ])
    else:
        argv.extend(["--env", "XAUDITOR_CODER_SERVICE_ENABLE_AUTH=false"])

    # Bake claude code's three required env vars into the container at
    # creation time. Claude Code 2.x drives model selection / endpoint
    # / auth purely through env vars (no CLI flags exist for the base
    # URL; ``--model`` exists but env is more uniform). The container
    # is treated as a "fixed-target" worker — one container is bound
    # to one (model, endpoint, key) triple for its lifetime.
    #
    # Trade-off accepted: rotating the API key requires
    # ``xauditor coder reset --yes && xauditor coder init`` rather than
    # a yaml edit, AND the key is visible via ``docker inspect
    # --format '{{.Config.Env}}'``. The simplicity gain (no
    # per-request plumbing of model/url/key through HTTP body and
    # subprocess env) outweighs the rotation friction for our scale,
    # since the model_url and model_name HAVE to be in container env
    # anyway (claude has no flag for the URL), so adding the key is
    # consistent rather than additive in surface area.
    if coder_cfg.model_api_key:
        argv.extend(["--env", f"ANTHROPIC_API_KEY={coder_cfg.model_api_key}"])
    if coder_cfg.model_url:
        argv.extend(["--env", f"ANTHROPIC_BASE_URL={coder_cfg.model_url}"])
    if coder_cfg.model_name:
        argv.extend(["--env", f"ANTHROPIC_MODEL={coder_cfg.model_name}"])

    argv.append(coder_cfg.container_image)
    return argv


__all__ = [
    "EndpointKind",
    "build_run_argv",
    "derive_socket_path",
    "endpoint_is_local",
    "endpoint_kind",
]
