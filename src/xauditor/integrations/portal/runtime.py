from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from xauditor.config import Neo4jConfig, PortalConfig, ReportDBConfig
from xauditor.errors import PortalError, PreflightError
from xauditor.integrations.docker import (
    CommandResult,
    MANAGED_LABEL,
    default_command_runner,
)
from xauditor.integrations.portal.network import NetworkManager
from xauditor.integrations.portal.package import (
    PortalPackageInfo,
    PortalPackageMissing,
    resolve_portal_package,
)


HttpProbe = Callable[[str], int]


def default_http_probe(url: str) -> int:
    """Return an HTTP status code for `url`. Raises on network error.

    Kept out of the hot path of other tests by using a minimal stdlib client.
    """

    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - internal probe
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


@dataclass(frozen=True)
class PortalServiceStatus:
    container_name: str
    image: str
    image_present: bool
    container_exists: bool
    container_running: bool


@dataclass(frozen=True)
class PortalStatus:
    network_name: str
    network_exists: bool
    backend: PortalServiceStatus
    frontend: PortalServiceStatus
    host_url: str | None


def _backend_reportdb_url(config: ReportDBConfig) -> str:
    """Return the Postgres URL the portal backend should use.

    Remote-configured reportdbs use the yml URL directly. Local managed
    reportdbs resolve via the container's hostname on the shared docker
    network (the backend runs on `xauditor-portal-net`; connecting the
    reportdb container to that network makes this hostname resolvable).
    """

    if config.remote is not None:
        return config.remote.url
    return (
        f"postgresql+asyncpg://{config.username}:{config.password}"
        f"@{config.container_name}:5432/{config.database}"
    )


class PortalRuntimeManager:
    """Orchestrate the two-container portal runtime (backend + frontend + network).

    Intentionally separate from `ContainerLifecycleManager`: the portal needs
    cross-container ordering, network creation, image auto-build from the
    `xauditor-portal` package, and frontend-only host-port publication, which
    do not fit the single-container lifecycle pattern.
    """

    managed_label = MANAGED_LABEL

    def __init__(
        self,
        config: PortalConfig,
        *,
        reportdb_config: ReportDBConfig | None = None,
        graphdb_config: Neo4jConfig | None = None,
        command_runner: Callable[..., CommandResult] | None = None,
        http_probe: HttpProbe | None = None,
        package_resolver: Callable[[], PortalPackageInfo] | None = None,
    ) -> None:
        self.config = config
        self.reportdb_config = reportdb_config
        self.graphdb_config = graphdb_config
        self.command_runner = command_runner or default_command_runner
        self.http_probe = http_probe or default_http_probe
        self.package_resolver = package_resolver or resolve_portal_package
        self.network = NetworkManager(
            config.network_name,
            error_cls=PortalError,
            command_runner=self.command_runner,
        )

    # Public verbs ------------------------------------------------------------

    def init_runtime(self) -> str:
        """First-time setup: build images (if missing) → create containers → start → healthcheck.

        Mirrors :meth:`xauditor.integrations.coder.runtime.CoderRuntimeManager.init_runtime`'s
        shape so the portal lifecycle uses the same vocabulary as the
        coder, graphdb, and reportdb runtimes. Re-runs are idempotent —
        existing images / containers are reused.
        """

        self._ensure_docker_available()
        self.network.ensure()
        self._ensure_image_present(
            image=self.config.backend.image,
            auto_build_service="backend",
            subject="portal backend",
        )
        self._ensure_backend_container_running()
        self._probe_backend_health()
        self._ensure_image_present(
            image=self.config.frontend.image,
            auto_build_service="frontend",
            subject="portal frontend",
        )
        self._ensure_frontend_container_running()
        host_url = self._host_url()
        self._probe_frontend_health(host_url)
        return f"Portal running at {host_url}"

    def start(self) -> str:
        """Start existing portal containers. Refuses if either is missing.

        Strict counterpart to :meth:`init_runtime` (which creates from
        scratch). Mirrors ``CoderRuntimeManager.start_runtime``'s
        contract: if the operator hasn't run ``portal init`` yet, this
        verb says so explicitly rather than silently reaching for the
        build-and-create path.
        """

        self._ensure_docker_available()
        self._refuse_if_containers_missing()
        # Network might have been removed (e.g. by ``portal reset``) but
        # the containers themselves were preserved. ``ensure`` is
        # idempotent so re-creating a network here is safe.
        self.network.ensure()
        # No more post-attach: graphdb / reportdb containers join
        # ``xauditor-portal-net`` at their own ``init_runtime`` time
        # via ``--network``. See ``move-network-to-bootstrap``.
        self._ensure_backend_container_running()
        self._probe_backend_health()
        self._ensure_frontend_container_running()
        host_url = self._host_url()
        self._probe_frontend_health(host_url)
        return f"Portal running at {host_url}"

    def _refuse_if_containers_missing(self) -> None:
        missing: list[str] = []
        for name in (
            self.config.backend.container_name,
            self.config.frontend.container_name,
        ):
            if not self._container_exists(name):
                missing.append(name)
        if missing:
            raise PortalError(
                f"Portal container(s) missing: {', '.join(missing)}. "
                "Run `xauditor portal init` to build and create them first."
            )

    def stop(self) -> str:
        self._ensure_docker_available()
        stopped: list[str] = []
        for name in (
            self.config.frontend.container_name,
            self.config.backend.container_name,
        ):
            if self._container_exists(name) and self._container_running(name):
                self._run(
                    ["docker", "stop", name],
                    error=f"Failed to stop managed portal container `{name}`.",
                )
                stopped.append(name)
        if not stopped:
            return "No managed portal containers were running"
        return f"Stopped managed portal containers: {', '.join(stopped)}"

    def reset(self, *, confirmed: bool) -> list[str]:
        if not confirmed:
            raise PortalError("Reset requires explicit confirmation with --yes.")
        self._ensure_docker_available()
        deleted: list[str] = []
        for name in (
            self.config.frontend.container_name,
            self.config.backend.container_name,
        ):
            if self._container_exists(name):
                if self._container_running(name):
                    self._run(
                        ["docker", "stop", name],
                        error=f"Failed to stop managed portal container `{name}`.",
                    )
                self._run(
                    ["docker", "rm", name],
                    error=f"Failed to remove managed portal container `{name}`.",
                )
                deleted.append(name)
        for image in (self.config.frontend.image, self.config.backend.image):
            if self._image_present(image):
                self._run(
                    ["docker", "image", "rm", image],
                    error=f"Failed to remove managed portal image `{image}`.",
                )
                deleted.append(image)
        # 0.5.2+: ``xauditor-portal-net`` is shared infrastructure
        # (graphdb / reportdb also join it at create time). ``portal
        # reset`` no longer removes the network — operators who want
        # a fully clean slate run ``graphdb reset`` /
        # ``reportdb reset`` first, then ``docker network rm
        # xauditor-portal-net`` if desired.
        return deleted

    def status(self) -> PortalStatus:
        self._ensure_docker_available()
        backend = self._service_status(self.config.backend.container_name, self.config.backend.image)
        frontend = self._service_status(self.config.frontend.container_name, self.config.frontend.image)
        host_url = self._host_url() if frontend.container_running else None
        return PortalStatus(
            network_name=self.config.network_name,
            network_exists=self.network.exists(),
            backend=backend,
            frontend=frontend,
            host_url=host_url,
        )

    # Internal ---------------------------------------------------------------

    def _ensure_image_present(
        self, *, image: str, auto_build_service: str, subject: str
    ) -> None:
        if self._image_present(image):
            return
        try:
            package = self.package_resolver()
        except PortalPackageMissing as exc:
            raise PortalError(str(exc)) from exc
        dockerfile = (
            package.backend_dockerfile
            if auto_build_service == "backend"
            else package.frontend_dockerfile
        )
        context = (
            package.backend_context
            if auto_build_service == "backend"
            else package.frontend_context
        )
        self._docker_build(
            tag=image,
            context=str(context),
            dockerfile=str(dockerfile),
            subject=subject,
        )

    def _docker_build(
        self, *, tag: str, context: str, dockerfile: str, subject: str
    ) -> None:
        self._run(
            [
                "docker",
                "build",
                "--tag",
                tag,
                "--label",
                MANAGED_LABEL,
                "--file",
                dockerfile,
                context,
            ],
            error=f"Failed to build {subject} image.",
        )

    def _ensure_backend_container_running(self) -> None:
        name = self.config.backend.container_name
        # 0.5.2+: graphdb / reportdb containers join the shared portal
        # network at their own ``init_runtime`` (via ``--network``), so
        # the backend can resolve them by hostname without any
        # post-create attach helper.
        if self._container_exists(name):
            if not self._container_running(name):
                self._run(
                    ["docker", "start", name],
                    error=f"Failed to start managed portal backend container `{name}`.",
                )
            return
        create_cmd: list[str] = [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            MANAGED_LABEL,
            "--network",
            self.config.network_name,
            "--network-alias",
            name,
        ]
        for env_key, env_value in self._backend_env().items():
            create_cmd.extend(["-e", f"{env_key}={env_value}"])
        if self.config.expose_backend_on_localhost:
            create_cmd.extend(["-p", "127.0.0.1:8000:8000"])
        create_cmd.append(self.config.backend.image)
        self._run(create_cmd, error="Failed to create managed portal backend container.")
        self._run(
            ["docker", "start", name],
            error=f"Failed to start managed portal backend container `{name}`.",
        )

    def _ensure_frontend_container_running(self) -> None:
        name = self.config.frontend.container_name
        if self._container_exists(name):
            if not self._container_running(name):
                self._run(
                    ["docker", "start", name],
                    error=f"Failed to start managed portal frontend container `{name}`.",
                )
            return
        backend_url = f"http://{self.config.backend.container_name}:8000"
        create_cmd = [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            MANAGED_LABEL,
            "--network",
            self.config.network_name,
            "-p",
            f"{self.config.host_port}:3000",
            "-e",
            f"BACKEND_INTERNAL_URL={backend_url}",
            self.config.frontend.image,
        ]
        self._run(create_cmd, error="Failed to create managed portal frontend container.")
        self._run(
            ["docker", "start", name],
            error=f"Failed to start managed portal frontend container `{name}`.",
        )

    def _probe_backend_health(self) -> None:
        name = self.config.backend.container_name
        deadline = time.monotonic() + self.config.ready_timeout_seconds
        last_error: str = ""
        while True:
            result = self.command_runner(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    (
                        "import sys, urllib.request\n"
                        "try:\n"
                        "    r = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)\n"
                        "    sys.exit(0 if r.status == 200 else 1)\n"
                        "except Exception:\n"
                        "    sys.exit(1)\n"
                    ),
                ]
            )
            if result.returncode == 0:
                return
            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or "health check failed"
            )
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        raise PortalError(
            "Timed out waiting for the managed portal backend to become ready. "
            f"Last error: {last_error}"
        )

    def _probe_frontend_health(self, host_url: str) -> None:
        deadline = time.monotonic() + self.config.ready_timeout_seconds
        last_error: str = ""
        while True:
            try:
                status_code = self.http_probe(f"{host_url}/api/health")
            except Exception as exc:  # noqa: BLE001 - summarize upstream failure
                last_error = str(exc)
            else:
                if status_code == 200:
                    return
                last_error = f"frontend proxied /api/health returned HTTP {status_code}"
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        raise PortalError(
            "Timed out waiting for the managed portal frontend to become ready. "
            f"Last error: {last_error}"
        )

    def _host_url(self) -> str:
        return f"http://127.0.0.1:{self.config.host_port}"

    def _service_status(self, container_name: str, image: str) -> PortalServiceStatus:
        return PortalServiceStatus(
            container_name=container_name,
            image=image,
            image_present=self._image_present(image),
            container_exists=self._container_exists(container_name),
            container_running=self._container_running(container_name),
        )

    def _image_present(self, image: str) -> bool:
        return self.command_runner(["docker", "image", "inspect", image]).returncode == 0

    def _container_exists(self, name: str) -> bool:
        return (
            self.command_runner(["docker", "container", "inspect", name]).returncode == 0
        )

    def _container_running(self, name: str) -> bool:
        result = self.command_runner(
            ["docker", "inspect", "-f", "{{.State.Running}}", name]
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _ensure_docker_available(self) -> None:
        if self.command_runner(["docker", "version"]).returncode != 0:
            raise PreflightError("Docker is required for portal lifecycle commands.")

    def _backend_env(self) -> dict[str, str]:
        """Environment variables passed to the backend container on create.

        Key addition: `XAUDITOR_PORTAL_DATABASE_URL` points at the managed
        `xauditor-reportdb` container by its hostname on the shared portal
        network, so the backend doesn't try to dial `127.0.0.1:5432`
        (its own loopback) for Postgres.
        """

        env: dict[str, str] = {}
        if self.reportdb_config is not None:
            env["XAUDITOR_PORTAL_DATABASE_URL"] = _backend_reportdb_url(
                self.reportdb_config
            )
        return env

    def _run(
        self, args: list[str], *, error: str, timeout: int | None = None
    ) -> CommandResult:
        try:
            result = self.command_runner(args, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timeout_value = exc.timeout if exc.timeout is not None else timeout
            raise PortalError(
                f"{error} command timed out after {timeout_value} seconds"
            ) from exc
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or "unknown docker error"
            )
            raise PortalError(f"{error} {detail}")
        return result


class InMemoryPortalRuntimeManager:
    """Test double mirroring `PortalRuntimeManager`'s public surface."""

    def __init__(
        self,
        config: PortalConfig,
        *,
        portal_package_installed: bool = True,
    ) -> None:
        self.config = config
        self.portal_package_installed = portal_package_installed
        self.network_exists = False
        self.backend_image_built = False
        self.frontend_image_built = False
        self.backend_container_exists = False
        self.frontend_container_exists = False
        self.backend_running = False
        self.frontend_running = False

    def init_runtime(self) -> str:
        if not self.backend_image_built or not self.frontend_image_built:
            if not self.portal_package_installed:
                raise PortalError(
                    "The `xauditor-portal` package is required for this command. "
                    "Install it with `pip install xauditor-portal`."
                )
            self.backend_image_built = True
            self.frontend_image_built = True
        self.network_exists = True
        self.backend_container_exists = True
        self.frontend_container_exists = True
        self.backend_running = True
        self.frontend_running = True
        return f"Portal running at http://127.0.0.1:{self.config.host_port}"

    def start(self) -> str:
        missing: list[str] = []
        if not self.backend_container_exists:
            missing.append(self.config.backend.container_name)
        if not self.frontend_container_exists:
            missing.append(self.config.frontend.container_name)
        if missing:
            raise PortalError(
                f"Portal container(s) missing: {', '.join(missing)}. "
                "Run `xauditor portal init` to build and create them first."
            )
        self.network_exists = True
        self.backend_running = True
        self.frontend_running = True
        return f"Portal running at http://127.0.0.1:{self.config.host_port}"

    def stop(self) -> str:
        stopped: list[str] = []
        if self.frontend_running:
            stopped.append(self.config.frontend.container_name)
        if self.backend_running:
            stopped.append(self.config.backend.container_name)
        self.frontend_running = False
        self.backend_running = False
        if not stopped:
            return "No managed portal containers were running"
        return f"Stopped managed portal containers: {', '.join(stopped)}"

    def reset(self, *, confirmed: bool) -> list[str]:
        if not confirmed:
            raise PortalError("Reset requires explicit confirmation with --yes.")
        deleted: list[str] = []
        if self.frontend_container_exists:
            deleted.append(self.config.frontend.container_name)
        if self.backend_container_exists:
            deleted.append(self.config.backend.container_name)
        if self.frontend_image_built:
            deleted.append(self.config.frontend.image)
        if self.backend_image_built:
            deleted.append(self.config.backend.image)
        if self.network_exists:
            deleted.append(self.config.network_name)
        self.frontend_running = False
        self.backend_running = False
        self.frontend_container_exists = False
        self.backend_container_exists = False
        self.frontend_image_built = False
        self.backend_image_built = False
        self.network_exists = False
        return deleted

    def status(self) -> PortalStatus:
        backend = PortalServiceStatus(
            container_name=self.config.backend.container_name,
            image=self.config.backend.image,
            image_present=self.backend_image_built,
            container_exists=self.backend_container_exists,
            container_running=self.backend_running,
        )
        frontend = PortalServiceStatus(
            container_name=self.config.frontend.container_name,
            image=self.config.frontend.image,
            image_present=self.frontend_image_built,
            container_exists=self.frontend_container_exists,
            container_running=self.frontend_running,
        )
        host_url = (
            f"http://127.0.0.1:{self.config.host_port}"
            if self.frontend_running
            else None
        )
        return PortalStatus(
            network_name=self.config.network_name,
            network_exists=self.network_exists,
            backend=backend,
            frontend=frontend,
            host_url=host_url,
        )
