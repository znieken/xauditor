"""Container-lifecycle manager for the xauditor coder microservice.

Mirrors :class:`xauditor.integrations.portal.runtime.PortalRuntimeManager`
in shape but scaled down to a single container with no shared docker
network. Drives ``xauditor coder {init,build,start,stop,reset,status}``
plus the auto-start branch in ``xauditor.audit.preflight``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import httpx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only when httpx absent
    httpx = None  # type: ignore[assignment]

from xauditor.config import CoderConfig
from xauditor.errors import PreflightError, XAuditorError
from xauditor.integrations.coder.helpers import (
    build_run_argv,
    derive_socket_path,
    endpoint_is_local,
    endpoint_kind,
)
from xauditor.integrations.coder.package import (
    CoderPackageInfo,
    CoderPackageMissing,
    resolve_coder_package,
)
from xauditor.integrations.coder.projects_walk import walk_projects
from xauditor.integrations.docker import (
    CommandResult,
    MANAGED_LABEL,
    default_command_runner,
)


HttpProbe = Callable[[CoderConfig], dict]
"""Probe ``GET /health`` against the coder service.

Implementations return the parsed JSON body (or raise on error). The
real implementation lives in :mod:`xauditor.audit.preflight` to avoid a
circular import; the lifecycle manager passes it as a dependency.
"""


@dataclass(frozen=True)
class CoderRuntimeStatus:
    """Returned by ``xauditor coder status`` (mirrors `PortalStatus` shape)."""

    transport: str
    endpoint_kind: str  # "unix" | "loopback" | "remote"
    endpoint: str
    container_name: str
    image: str
    image_present: bool
    container_exists: bool
    container_running: bool
    health_status: int | None        # HTTP status, or None when unreachable
    claude_cli_version: str
    in_flight: int | None
    error: str | None
    # Multi-project workspace fields (since multi-project-coder-service).
    # ``workspace_root`` is the resolved host directory bind-mounted at
    # ``/workspace`` (post-shim). ``projects`` is the list of audit-able
    # subdirectories — sourced from the service's ``GET /projects`` when
    # reachable, or a host-side ``os.scandir`` fallback annotated by
    # ``projects_source``.
    workspace_root: str = ""
    projects: tuple[str, ...] = ()
    projects_source: str = ""  # "service" | "host_scan" | ""


_READY_TIMEOUT_SECONDS_FALLBACK = 60


class CoderRuntimeManager:
    """Lifecycle verbs over the single ``xauditor-coder-service`` container.

    Subprocess shell-out goes through ``CommandRunner`` (same shape used
    by the portal manager) so an in-memory test double can stand in.
    """

    managed_label = MANAGED_LABEL

    def __init__(
        self,
        config: CoderConfig,
        *,
        repo_root: Path,
        runtime_root: Path | None = None,
        command_runner: Callable[..., CommandResult] | None = None,
        http_probe: HttpProbe | None = None,
        package_resolver: Callable[[Path], CoderPackageInfo] | None = None,
        ready_timeout_seconds: int = _READY_TIMEOUT_SECONDS_FALLBACK,
    ) -> None:
        self.config = config
        self.repo_root = Path(repo_root)
        self.runtime_root = Path(runtime_root) if runtime_root else None
        self.command_runner = command_runner or default_command_runner
        self.http_probe = http_probe
        self.package_resolver = package_resolver or resolve_coder_package
        self.ready_timeout_seconds = ready_timeout_seconds

    # ---- public verbs ----------------------------------------------------

    def build(self, *, push: bool = False) -> str:
        """Build the configured image. Push when ``push=True`` AND tag has a registry."""

        self._ensure_local()
        self._ensure_docker_available()
        info = self._resolve_package()
        self._docker_build(info)
        if push:
            self._docker_push()
        return self.config.container_image

    def init_runtime(self) -> str:
        """Build (if missing) → create (if missing) → start → wait for /health.

        When an existing container's ``/workspace`` bind-mount source
        does NOT match the configured ``effective_workspace_root``,
        refuse with an actionable error. Bind-mounts are baked at
        ``docker run`` time and cannot be changed by ``docker start``,
        so a stale container under a new config is a foot-gun
        (claude would silently audit the wrong tree). Operators must
        explicitly tear down via ``coder reset --yes`` before re-init.
        """

        self._ensure_local()
        self._ensure_docker_available()
        if not self._image_present():
            info = self._resolve_package()
            self._docker_build(info)
        if not self._container_exists():
            self._docker_create_and_start()
        else:
            self._verify_existing_workspace_mount()
            if not self._container_running():
                self._docker_start()
        self._wait_until_healthy()
        return self._ready_message()

    def _verify_existing_workspace_mount(self) -> None:
        """Refuse if existing container's /workspace bind-mount source
        differs from the configured effective_workspace_root.

        Returns silently when:
        - The container has no /workspace mount we can read (treat as
          "fine", don't refuse on missing data).
        - The configured workspace is empty (legacy single-repo mode).
        """

        configured = (
            self.config.effective_workspace_root
            or self.config.repo_mount_path
            or ""
        )
        if not configured:
            return
        running_source = self._inspect_workspace_mount_source()
        if not running_source:
            return
        if Path(running_source).resolve() == Path(configured).resolve():
            return
        raise XAuditorError(
            f"Coder container `{self.config.container_name}` was "
            f"created with /workspace bind-mounted from "
            f"`{running_source}`, but config now has "
            f"coder.workspace_root=`{configured}`. Bind-mounts cannot "
            "be changed without re-creating the container. Run "
            "`xauditor coder reset --yes` then `xauditor coder init` "
            "to apply the new workspace_root."
        )

    def inspect_workspace_mount_source(self) -> str:
        """Public alias of ``_inspect_workspace_mount_source`` so the
        audit-time preflight in ``xauditor.audit.preflight`` can call it
        without reaching into private API. Returns the host source of
        the running container's ``/workspace`` mount, or ``""`` when
        unreadable.
        """

        return self._inspect_workspace_mount_source()

    def _inspect_workspace_mount_source(self) -> str:
        """Return the host source of the container's /workspace mount, or "".

        Best-effort: parses ``docker inspect --format`` output. Returns
        "" on parse failure or absence so the caller can decide.
        """

        result = self._run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{range .Mounts}}{{if eq .Destination \"/workspace\"}}{{.Source}}{{end}}{{end}}",
                self.config.container_name,
            ],
            error="",
            check=False,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    def start_runtime(self) -> str:
        self._ensure_local()
        self._ensure_docker_available()
        if not self._container_exists():
            raise XAuditorError(
                f"Coder container `{self.config.container_name}` does not exist. "
                "Run `xauditor coder init` to build and start it."
            )
        if not self._container_running():
            self._docker_start()
        self._wait_until_healthy()
        return self._ready_message()

    def stop_runtime(self) -> str:
        self._ensure_local()
        self._ensure_docker_available()
        if not self._container_exists():
            return f"Coder container `{self.config.container_name}` does not exist."
        if not self._container_running():
            return f"Coder container `{self.config.container_name}` is already stopped."
        self._run(
            ["docker", "stop", self.config.container_name],
            error=f"Failed to stop coder container `{self.config.container_name}`.",
        )
        return f"Stopped coder container `{self.config.container_name}`."

    def reset_runtime(self, *, confirmed: bool) -> list[str]:
        self._ensure_local()
        if not confirmed:
            raise XAuditorError(
                "Coder reset requires explicit confirmation with --yes."
            )
        self._ensure_docker_available()
        deleted: list[str] = []
        if self._container_exists():
            if self._container_running():
                self._run(
                    ["docker", "stop", self.config.container_name],
                    error=f"Failed to stop coder container `{self.config.container_name}`.",
                )
            self._run(
                ["docker", "rm", self.config.container_name],
                error=f"Failed to remove coder container `{self.config.container_name}`.",
            )
            deleted.append(self.config.container_name)
        if self._image_present():
            self._run(
                ["docker", "image", "rm", self.config.container_image],
                error=f"Failed to remove coder image `{self.config.container_image}`.",
            )
            deleted.append(self.config.container_image)
        # Remove the named volume that backs ``/home/coder`` (claude's
        # session state). The volume name mirrors what
        # ``build_run_argv`` mounts. ``docker volume rm`` on a
        # non-existent volume is non-zero, so we tolerate it via
        # ``check=False``; same when the volume is still in use by a
        # leftover container we couldn't remove (rare).
        home_volume = f"{self.config.container_name}-home"
        rm_result = self._run(
            ["docker", "volume", "rm", home_volume],
            error=f"Failed to remove coder home volume `{home_volume}`.",
            check=False,
        )
        if rm_result.returncode == 0:
            deleted.append(home_volume)
        socket_path = derive_socket_path(
            self.config,
            runtime_root=str(self.runtime_root) if self.runtime_root else None,
        )
        if socket_path:
            sock = Path(socket_path)
            if sock.exists():
                try:
                    sock.unlink()
                    deleted.append(socket_path)
                except OSError:
                    pass
        return deleted

    def runtime_status(self) -> CoderRuntimeStatus:
        # status is the only verb that works for both local and remote
        kind = endpoint_kind(self.config)
        workspace_root = (
            self.config.effective_workspace_root
            or self.config.repo_mount_path
            or ""
        )
        if not endpoint_is_local(self.config):
            health_status, version, in_flight, error = self._probe_health_safely()
            projects, source = self._discover_projects(
                container_running=False,
                workspace_root=workspace_root,
                remote=True,
            )
            return CoderRuntimeStatus(
                transport=self.config.transport,
                endpoint_kind=kind,
                endpoint=self.config.endpoint,
                container_name="(remote)",
                image="(remote)",
                image_present=False,
                container_exists=False,
                container_running=False,
                health_status=health_status,
                claude_cli_version=version,
                in_flight=in_flight,
                error=error,
                workspace_root=workspace_root,
                projects=projects,
                projects_source=source,
            )
        self._ensure_docker_available()
        image_present = self._image_present()
        container_exists = self._container_exists()
        container_running = container_exists and self._container_running()
        health_status, version, in_flight, error = (
            self._probe_health_safely() if container_running else (None, "", None, None)
        )
        projects, source = self._discover_projects(
            container_running=container_running,
            workspace_root=workspace_root,
            remote=False,
        )
        return CoderRuntimeStatus(
            transport=self.config.transport,
            endpoint_kind=kind,
            endpoint=self.config.endpoint,
            container_name=self.config.container_name,
            image=self.config.container_image,
            image_present=image_present,
            container_exists=container_exists,
            container_running=container_running,
            health_status=health_status,
            claude_cli_version=version,
            in_flight=in_flight,
            error=error,
            workspace_root=workspace_root,
            projects=projects,
            projects_source=source,
        )

    def _discover_projects(
        self,
        *,
        container_running: bool,
        workspace_root: str,
        remote: bool,
    ) -> tuple[tuple[str, ...], str]:
        """Source the project list for ``coder status``.

        Prefers ``GET /projects`` on the service when reachable
        (truthful for what verifications will see). Falls back to a
        host-side ``os.scandir`` of ``workspace_root`` when the
        container is unreachable AND we're on a local deployment;
        annotated with ``"host_scan"`` so operators see the source.
        """

        if container_running or remote:
            try:
                projects = self._probe_projects()
            except Exception:  # noqa: BLE001 - probe is best-effort
                projects = None
            if projects is not None:
                return tuple(projects), "service"
        if remote:
            return (), ""
        if not workspace_root:
            return (), ""
        if not Path(workspace_root).is_dir():
            return (), ""
        names, _ = walk_projects(workspace_root)
        return tuple(names), "host_scan"

    def _probe_projects(self) -> list[str] | None:
        """Issue ``GET /projects`` against the configured endpoint."""

        if httpx is None:  # pragma: no cover
            return None
        endpoint = (self.config.endpoint or "").strip()
        if not endpoint:
            return None
        timeout = httpx.Timeout(
            connect=float(self.config.preflight_timeout_seconds),
            read=float(self.config.preflight_timeout_seconds),
            write=float(self.config.preflight_timeout_seconds),
            pool=float(self.config.preflight_timeout_seconds),
        )
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.config.enable_auth and self.config.endpoint_token:
            headers["Authorization"] = f"Bearer {self.config.endpoint_token}"
        if endpoint.startswith("unix://"):
            socket_path = endpoint[len("unix://"):]
            client_kwargs: dict[str, Any] = {
                "base_url": "http://localhost",
                "transport": httpx.HTTPTransport(uds=socket_path),
            }
        else:
            client_kwargs = {"base_url": endpoint.rstrip("/")}
        try:
            with httpx.Client(headers=headers, timeout=timeout, **client_kwargs) as client:
                resp = client.get("/projects")
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        projects = body.get("projects") if isinstance(body, dict) else None
        if not isinstance(projects, list):
            return None
        return [str(p) for p in projects if isinstance(p, str)]

    # ---- preflight integration -------------------------------------------

    def inspect_for_preflight(self) -> str:
        """Used by ``check_coder_runtime`` when transport=http + endpoint is local.

        Returns one of: ``"running"``, ``"started"`` (we just started it),
        ``"missing"``. The preflight wraps the result accordingly.
        """

        self._ensure_local()
        self._ensure_docker_available()
        if not self._container_exists():
            return "missing"
        if self._container_running():
            return "running"
        self._docker_start()
        try:
            self._wait_until_healthy()
        except XAuditorError as exc:
            # Capture last log lines for the operator before raising.
            tail = self._docker_logs_tail(20)
            raise PreflightError(
                f"Coder container `{self.config.container_name}` was started "
                f"but did not become healthy within {self.ready_timeout_seconds}s. "
                f"Last logs:\n{tail}\n"
                "Try `xauditor coder reset --yes && xauditor coder init`."
            ) from exc
        return "started"

    # ---- internal --------------------------------------------------------

    def _ensure_local(self) -> None:
        if not endpoint_is_local(self.config):
            raise XAuditorError(
                "coder.endpoint resolves to a remote target "
                f"({self.config.endpoint!r}); container lifecycle is "
                "managed by your deployment, not xauditor. Use "
                "`xauditor audit run` to verify the deployment responds."
            )

    def _ensure_docker_available(self) -> None:
        try:
            self._run(["docker", "version"], error="Docker is not available.")
        except XAuditorError:
            raise

    def _resolve_package(self) -> CoderPackageInfo:
        try:
            return self.package_resolver(self.repo_root)
        except CoderPackageMissing:
            raise

    def _docker_build(self, info: CoderPackageInfo) -> None:
        """Run ``docker build`` against the installed package directory.

        ``info.context`` is the resolved ``xauditor_coder_service/``
        package root (mirrors how :class:`PortalRuntimeManager` builds
        the portal backend). ``COPY .`` inside the Dockerfile picks up
        the package source tree directly; no wheel staging is involved.
        """

        self._run(
            [
                "docker", "build",
                "--tag", self.config.container_image,
                "--file", str(info.dockerfile),
                str(info.context),
            ],
            error=f"Failed to build coder image `{self.config.container_image}`.",
        )

    def _docker_push(self) -> None:
        if "/" not in self.config.container_image:
            raise XAuditorError(
                f"`{self.config.container_image}` has no registry prefix; "
                "set `coder.container_image` to e.g. "
                "registry.example.com/xauditor-coder-service:0.1.0 to push."
            )
        self._run(
            ["docker", "push", self.config.container_image],
            error=f"Failed to push coder image `{self.config.container_image}`.",
        )

    def _docker_create_and_start(self) -> None:
        argv = build_run_argv(
            self.config,
            repo_root=str(self.repo_root),
            runtime_root=str(self.runtime_root) if self.runtime_root else None,
        )
        self._run(
            argv,
            error=f"Failed to create coder container `{self.config.container_name}`.",
        )

    def _docker_start(self) -> None:
        self._run(
            ["docker", "start", self.config.container_name],
            error=f"Failed to start coder container `{self.config.container_name}`.",
        )

    def _container_exists(self) -> bool:
        result = self._run(
            ["docker", "container", "inspect", self.config.container_name],
            error="",
            check=False,
        )
        return result.returncode == 0

    def _container_running(self) -> bool:
        result = self._run(
            [
                "docker", "container", "inspect",
                "--format", "{{.State.Running}}",
                self.config.container_name,
            ],
            error="",
            check=False,
        )
        if result.returncode != 0:
            return False
        return result.stdout.strip().lower() == "true"

    def _image_present(self) -> bool:
        result = self._run(
            ["docker", "image", "inspect", self.config.container_image],
            error="",
            check=False,
        )
        return result.returncode == 0

    def _docker_logs_tail(self, lines: int) -> str:
        result = self._run(
            ["docker", "logs", "--tail", str(lines), self.config.container_name],
            error="",
            check=False,
        )
        if result.returncode != 0:
            return "(unable to retrieve logs)"
        # docker logs writes container stderr on stderr and stdout on stdout
        merged = (result.stdout or "") + (result.stderr or "")
        return merged.strip() or "(no logs)"

    def _wait_until_healthy(self) -> None:
        if self.http_probe is None:
            # Caller didn't wire a probe — treat as best-effort, just wait
            # for the container to be running.
            time.sleep(0.2)
            return
        deadline = time.monotonic() + float(self.ready_timeout_seconds)
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                self.http_probe(self.config)
                return
            except (PreflightError, XAuditorError, OSError) as exc:
                last_error = str(exc)
                time.sleep(0.25)
            except Exception as exc:  # noqa: BLE001 - defensive catch
                # The default probe (`_default_coder_health_probe`) wraps
                # httpx errors as PreflightError, but third-party probe
                # implementations might raise transport errors that aren't
                # OSError subclasses (httpx.HTTPError, custom transports).
                # During container startup these are transient by
                # definition — treat them as "not ready yet" and retry
                # rather than killing the run. Persistent failures still
                # surface via the deadline-exhausted XAuditorError below.
                last_error = f"{exc.__class__.__name__}: {exc}"
                time.sleep(0.25)
        raise XAuditorError(
            f"Coder container `{self.config.container_name}` did not respond "
            f"to /health within {self.ready_timeout_seconds}s "
            f"({last_error or 'unknown error'})."
        )

    def _probe_health_safely(self) -> tuple[int | None, str, int | None, str | None]:
        if self.http_probe is None:
            return None, "", None, None
        try:
            payload = self.http_probe(self.config)
        except Exception as exc:  # noqa: BLE001 - status SHALL never raise
            return None, "", None, f"{exc.__class__.__name__}: {exc}"
        if not isinstance(payload, dict):
            return 200, "", None, None
        version = str(payload.get("claude_cli_version", "") or "")
        in_flight_raw = payload.get("in_flight")
        try:
            in_flight = int(in_flight_raw) if in_flight_raw is not None else None
        except (TypeError, ValueError):
            in_flight = None
        return 200, version, in_flight, None

    def _ready_message(self) -> str:
        return (
            f"Coder runtime ready: {self.config.endpoint or self.config.container_name} "
            f"(image={self.config.container_image})"
        )

    def _run(
        self,
        argv: list[str],
        *,
        error: str,
        check: bool = True,
        timeout: int | None = None,
    ) -> CommandResult:
        result = self.command_runner(argv, timeout=timeout)
        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or f"exit code {result.returncode}"
            raise XAuditorError(f"{error} {detail}")
        return result


# --------------------------------------------------------------------------
# Test double
# --------------------------------------------------------------------------


class InMemoryCoderRuntimeManager:
    """Drop-in replacement for tests that don't shell out to docker.

    Tracks state in instance attributes so test bodies can drive the
    lifecycle without spawning real containers.
    """

    def __init__(self, config: CoderConfig) -> None:
        self.config = config
        self.image_built = False
        self.container_state: str = "absent"  # "absent" | "stopped" | "running"
        self.deleted_resources: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def build(self, *, push: bool = False) -> str:
        self.calls.append(("build", str(push)))
        if not endpoint_is_local(self.config):
            raise XAuditorError("remote endpoint")
        self.image_built = True
        return self.config.container_image

    def init_runtime(self) -> str:
        self.calls.append(("init",))
        if not endpoint_is_local(self.config):
            raise XAuditorError("remote endpoint")
        self.image_built = True
        if self.container_state == "absent":
            self.container_state = "running"
        elif self.container_state == "stopped":
            self.container_state = "running"
        return f"Coder runtime ready: {self.config.endpoint}"

    def start_runtime(self) -> str:
        self.calls.append(("start",))
        if not endpoint_is_local(self.config):
            raise XAuditorError("remote endpoint")
        if self.container_state == "absent":
            raise XAuditorError(
                f"Coder container `{self.config.container_name}` does not exist. "
                "Run `xauditor coder init` to build and start it."
            )
        self.container_state = "running"
        return f"Coder runtime ready: {self.config.endpoint}"

    def stop_runtime(self) -> str:
        self.calls.append(("stop",))
        if self.container_state == "absent":
            return "absent"
        if self.container_state == "running":
            self.container_state = "stopped"
            return "stopped"
        return "already stopped"

    def reset_runtime(self, *, confirmed: bool) -> list[str]:
        self.calls.append(("reset", str(confirmed)))
        if not confirmed:
            raise XAuditorError("--yes required")
        deleted: list[str] = []
        if self.container_state != "absent":
            deleted.append(self.config.container_name)
            self.container_state = "absent"
        if self.image_built:
            deleted.append(self.config.container_image)
            self.image_built = False
        self.deleted_resources = deleted
        return deleted

    def runtime_status(self) -> CoderRuntimeStatus:
        kind = endpoint_kind(self.config)
        running = self.container_state == "running"
        return CoderRuntimeStatus(
            transport=self.config.transport,
            endpoint_kind=kind,
            endpoint=self.config.endpoint,
            container_name=self.config.container_name,
            image=self.config.container_image,
            image_present=self.image_built,
            container_exists=self.container_state != "absent",
            container_running=running,
            health_status=200 if running else None,
            claude_cli_version="(stub)" if running else "",
            in_flight=0 if running else None,
            error=None,
        )

    def inspect_for_preflight(self) -> str:
        self.calls.append(("inspect_for_preflight",))
        if self.container_state == "absent":
            return "missing"
        if self.container_state == "running":
            return "running"
        self.container_state = "running"
        return "started"

    def inspect_workspace_mount_source(self) -> str:
        # In-memory variant is used by tests; defaults to "" so the
        # mount-mismatch check is a no-op unless the test explicitly
        # sets ``workspace_mount_source``.
        return getattr(self, "workspace_mount_source", "") or ""


__all__ = [
    "CoderRuntimeManager",
    "CoderRuntimeStatus",
    "HttpProbe",
    "InMemoryCoderRuntimeManager",
]
