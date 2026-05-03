from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from xauditor.config import Neo4jConfig
from xauditor.errors import GraphdbError, PreflightError, XAuditorError


ReadinessProbe = Callable[[], None]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def default_command_runner(args: list[str], *, timeout: int | None = None) -> CommandResult:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


@dataclass(frozen=True)
class PortMapping:
    host: int
    container: int


@dataclass(frozen=True)
class ContainerLifecycleSpec:
    """Declarative parameters for a managed Docker container.

    `subject` is the user-facing label (e.g. "Neo4j", "PostgreSQL") used in
    human-readable status and error messages.

    When `remote_configured` is true, every lifecycle operation short-circuits
    with a "remote configured; no managed container to manage" result instead
    of invoking docker.
    """

    subject: str
    image: str
    container_name: str
    volume_name: str
    volume_mount_path: str
    ports: tuple[PortMapping, ...]
    env: tuple[tuple[str, str], ...] = ()
    label: str = "com.xauditor.managed=true"
    ready_timeout_seconds: int = 60
    remote_configured: bool = False
    init_command_hint: str = "init"
    extra_create_args: tuple[str, ...] = ()
    ready_message: str = ""  # empty → defaults to f"{subject} ready"
    # Docker network the container joins at creation time (passed via
    # ``--network`` to ``docker create``). When set, ``init_runtime``
    # also ensures the network exists before container creation, and
    # re-attaches an already-existing container that drifted off this
    # network (the upgrade path from xauditor 0.5.1 where db
    # containers lived on the default ``bridge``). ``None`` means
    # "no managed network" — the container goes on docker's default
    # bridge as before this change.
    network_name: str | None = None


MANAGED_LABEL = "com.xauditor.managed=true"


class ContainerLifecycleManager:
    """Reusable managed-container lifecycle primitives.

    Not tied to any specific service. Consumers provide a `ContainerLifecycleSpec`
    plus an error class used when docker operations fail.
    """

    def __init__(
        self,
        spec: ContainerLifecycleSpec,
        *,
        error_cls: type[XAuditorError] = GraphdbError,
        command_runner: Callable[..., CommandResult] | None = None,
        readiness_probe: ReadinessProbe | None = None,
    ) -> None:
        self.spec = spec
        self.error_cls = error_cls
        self.command_runner = command_runner or default_command_runner
        self.readiness_probe = readiness_probe

    def set_readiness_probe(self, probe: ReadinessProbe) -> None:
        self.readiness_probe = probe

    # Public lifecycle methods ------------------------------------------------

    def init_runtime(self) -> str:
        if self.spec.remote_configured:
            return self._remote_bypass_message()
        self._ensure_docker_available()
        # Ensure the shared docker network exists BEFORE ``docker
        # create`` runs — the create command appends ``--network
        # <name>`` (see ``_create_command``) and would fail if the
        # network is missing. Idempotent: ``docker network create``
        # is only invoked when ``docker network inspect`` reports the
        # network missing.
        self._ensure_network_present()
        if not self._image_present():
            self._run(
                ["docker", "pull", self.spec.image],
                error=f"Failed to pull the configured {self.spec.subject} image.",
            )
        if not self._volume_exists():
            self._run(
                ["docker", "volume", "create", self.spec.volume_name],
                error=f"Failed to create the managed {self.spec.subject} volume.",
            )
        if not self._container_exists():
            self._run(
                self._create_command(),
                error=f"Failed to create the managed {self.spec.subject} container.",
            )
        else:
            # Container already exists from a prior xauditor version
            # (e.g. 0.5.1, when db containers lived on the default
            # bridge network). Verify it is connected to the
            # configured ``spec.network_name`` and reconnect if not —
            # this is the upgrade path that retires the silent
            # post-attach helper that used to hide failures.
            self._ensure_container_attached_to_network()
        self._run(
            ["docker", "start", self.spec.container_name],
            error=f"Failed to start the managed {self.spec.subject} container.",
        )
        self.wait_ready(timeout_seconds=self.spec.ready_timeout_seconds)
        return self.spec.ready_message or f"{self.spec.subject} ready"

    def start_runtime(self) -> str:
        if self.spec.remote_configured:
            return self._remote_bypass_message()
        self._ensure_docker_available()
        if not self._container_exists():
            raise self.error_cls(
                f"Initialization is required; run `{self.spec.init_command_hint}` first."
            )
        if not self._container_running():
            self._run(
                ["docker", "start", self.spec.container_name],
                error=f"Failed to start the managed {self.spec.subject} container.",
            )
        self.wait_ready(timeout_seconds=self.spec.ready_timeout_seconds)
        return f"Managed {self.spec.subject} container started"

    def stop_runtime(self) -> str:
        if self.spec.remote_configured:
            return self._remote_bypass_message()
        self._ensure_docker_available()
        if not self._container_exists() or not self._container_running():
            return "No managed container was running"
        self._run(
            ["docker", "stop", self.spec.container_name],
            error=f"Failed to stop the managed {self.spec.subject} container.",
        )
        return f"Managed {self.spec.subject} container stopped"

    def reset_runtime(self, *, confirmed: bool) -> list[str]:
        if self.spec.remote_configured:
            # Remote databases have no managed resources to remove; return an
            # empty deletion list. Callers surface the "remote configured"
            # message via `remote_bypass_message()` when it matters.
            return []
        self._ensure_docker_available()
        if not confirmed:
            raise self.error_cls("Reset requires explicit confirmation with --yes.")
        deleted: list[str] = []
        if self._container_exists():
            if self._container_running():
                self._run(
                    ["docker", "stop", self.spec.container_name],
                    error=f"Failed to stop the managed {self.spec.subject} container.",
                )
            self._run(
                ["docker", "rm", self.spec.container_name],
                error=f"Failed to remove the managed {self.spec.subject} container.",
            )
            deleted.append(self.spec.container_name)
        if self._volume_exists():
            self._run(
                ["docker", "volume", "rm", self.spec.volume_name],
                error=f"Failed to remove the managed {self.spec.subject} volume.",
            )
            deleted.append(self.spec.volume_name)
        if self._image_present():
            self._run(
                ["docker", "image", "rm", self.spec.image],
                error=f"Failed to remove the configured {self.spec.subject} image.",
            )
            deleted.append(self.spec.image)
        return deleted

    def wait_ready(self, *, timeout_seconds: int = 30) -> None:
        if self.spec.remote_configured:
            return
        if self.readiness_probe is None:
            raise self.error_cls(
                f"Readiness probe is not configured for the managed {self.spec.subject} container."
            )
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while True:
            try:
                self.readiness_probe()
                return
            except Exception as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        if last_error is None:
            raise self.error_cls(
                f"Timed out while waiting for the managed {self.spec.subject} container to become ready."
            )
        raise self.error_cls(
            f"Timed out while waiting for the managed {self.spec.subject} container to become ready. "
            f"Last readiness check failed: {last_error}"
        )

    def remote_bypass_message(self) -> str:
        return self._remote_bypass_message()

    # Internal helpers --------------------------------------------------------

    def _remote_bypass_message(self) -> str:
        return (
            f"{self.spec.subject}: remote endpoint is configured; "
            "no managed container to manage."
        )

    def _create_command(self) -> list[str]:
        cmd: list[str] = [
            "docker",
            "create",
            "--name",
            self.spec.container_name,
            "--label",
            self.spec.label,
        ]
        if self.spec.network_name is not None:
            cmd.extend(["--network", self.spec.network_name])
        for key, value in self.spec.env:
            cmd.extend(["-e", f"{key}={value}"])
        for port in self.spec.ports:
            cmd.extend(["-p", f"{port.host}:{port.container}"])
        cmd.extend(["-v", f"{self.spec.volume_name}:{self.spec.volume_mount_path}"])
        cmd.extend(self.spec.extra_create_args)
        cmd.append(self.spec.image)
        return cmd

    def _ensure_network_present(self) -> None:
        """Idempotently create the docker network the spec binds to.

        No-op when ``spec.network_name`` is ``None`` (the legacy path
        that pre-dates this change). Used by ``init_runtime`` so the
        ``--network`` arg in ``_create_command`` always references an
        existing network.
        """

        if self.spec.network_name is None:
            return
        inspect = self.command_runner(
            ["docker", "network", "inspect", self.spec.network_name]
        )
        if inspect.returncode == 0:
            return
        self._run(
            [
                "docker",
                "network",
                "create",
                "--label",
                MANAGED_LABEL,
                self.spec.network_name,
            ],
            error=(
                f"Failed to create managed docker network "
                f"`{self.spec.network_name}` for {self.spec.subject}."
            ),
        )

    def _ensure_container_attached_to_network(self) -> None:
        """Re-attach an already-existing container to the configured network.

        Handles the upgrade path from xauditor 0.5.1 where the db
        containers were created on the default ``bridge`` only and a
        post-create helper inside the portal runtime tried to attach
        them to ``xauditor-portal-net`` later. That helper is gone in
        0.5.2; this method is the explicit replacement and surfaces
        attach failures hard (no silent swallow). No-op when
        ``spec.network_name`` is ``None``.
        """

        if self.spec.network_name is None:
            return
        result = self.command_runner(
            [
                "docker",
                "inspect",
                "-f",
                "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                self.spec.container_name,
            ]
        )
        if result.returncode != 0:
            # Container might be in a transitional state; bail without
            # claiming success or failure — the next ``docker start`` /
            # readiness probe will surface a real problem.
            return
        attached = result.stdout.strip().split()
        if self.spec.network_name in attached:
            return
        self._run(
            [
                "docker",
                "network",
                "connect",
                self.spec.network_name,
                self.spec.container_name,
            ],
            error=(
                f"Failed to attach existing managed {self.spec.subject} "
                f"container `{self.spec.container_name}` to network "
                f"`{self.spec.network_name}`."
            ),
        )

    def _image_present(self) -> bool:
        return (
            self.command_runner(["docker", "image", "inspect", self.spec.image]).returncode
            == 0
        )

    def _volume_exists(self) -> bool:
        return (
            self.command_runner(
                ["docker", "volume", "inspect", self.spec.volume_name]
            ).returncode
            == 0
        )

    def _container_exists(self) -> bool:
        return (
            self.command_runner(
                ["docker", "container", "inspect", self.spec.container_name]
            ).returncode
            == 0
        )

    def _container_running(self) -> bool:
        result = self.command_runner(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.spec.container_name]
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _ensure_docker_available(self) -> None:
        if self.command_runner(["docker", "version"]).returncode != 0:
            raise PreflightError(
                f"Docker is required for {self.spec.subject.lower()} lifecycle commands."
            )

    def _run(
        self, args: list[str], *, error: str, timeout: int | None = None
    ) -> CommandResult:
        try:
            result = self.command_runner(args, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timeout_value = exc.timeout if exc.timeout is not None else timeout
            raise self.error_cls(
                f"{error} command timed out after {timeout_value} seconds"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown docker error"
            raise self.error_cls(f"{error} {detail}")
        return result


def _neo4j_spec(config: Neo4jConfig) -> ContainerLifecycleSpec:
    return ContainerLifecycleSpec(
        subject="Neo4j",
        image=config.image,
        container_name=config.container_name,
        volume_name=config.volume_name,
        volume_mount_path="/data",
        ports=(
            PortMapping(host=config.bolt_port, container=7687),
            PortMapping(host=config.http_port, container=7474),
        ),
        env=(("NEO4J_AUTH", f"{config.username}/{config.password}"),),
        label=MANAGED_LABEL,
        ready_timeout_seconds=config.ready_timeout_seconds,
        remote_configured=config.remote is not None,
        init_command_hint="xauditor graphdb init",
        ready_message="graphdb ready",
        network_name=config.network_name,
    )


class DockerManager:
    """Backwards-compatible Neo4j docker manager.

    Thin wrapper over `ContainerLifecycleManager` that keeps the existing
    `DockerManager(config, command_runner=..., readiness_probe=...)` API so
    graphdb callers and tests continue working unchanged.
    """

    managed_label = MANAGED_LABEL

    def __init__(
        self,
        config: Neo4jConfig,
        *,
        command_runner: Callable[..., CommandResult] | None = None,
        readiness_probe: ReadinessProbe | None = None,
    ) -> None:
        self.config = config
        self._inner = ContainerLifecycleManager(
            _neo4j_spec(config),
            error_cls=GraphdbError,
            command_runner=command_runner,
            readiness_probe=readiness_probe,
        )

    @property
    def command_runner(self) -> Callable[..., CommandResult]:
        return self._inner.command_runner

    @command_runner.setter
    def command_runner(self, value: Callable[..., CommandResult]) -> None:
        self._inner.command_runner = value

    @property
    def readiness_probe(self) -> ReadinessProbe | None:
        return self._inner.readiness_probe

    @readiness_probe.setter
    def readiness_probe(self, value: ReadinessProbe | None) -> None:
        self._inner.readiness_probe = value

    def set_readiness_probe(self, probe: ReadinessProbe) -> None:
        self._inner.set_readiness_probe(probe)

    def init_runtime(self) -> str:
        return self._inner.init_runtime()

    def start_runtime(self) -> str:
        return self._inner.start_runtime()

    def stop_runtime(self) -> str:
        return self._inner.stop_runtime()

    def reset_runtime(self, *, confirmed: bool) -> list[str]:
        return self._inner.reset_runtime(confirmed=confirmed)

    def wait_ready(self, *, timeout_seconds: int = 30) -> None:
        self._inner.wait_ready(timeout_seconds=timeout_seconds)


class _InMemoryContainerMixin:
    """Shared in-memory container lifecycle state for test doubles."""

    subject: str = "container"
    init_required_command: str = "init"

    def __init__(self) -> None:
        self.initialized = False
        self.running = False
        self.readiness_probe: ReadinessProbe | None = None
        self.remote_configured = False

    def set_readiness_probe(self, probe: ReadinessProbe) -> None:
        self.readiness_probe = probe

    def _bypass_message(self) -> str:
        return (
            f"{self.subject}: remote endpoint is configured; "
            "no managed container to manage."
        )


class InMemoryDockerManager(_InMemoryContainerMixin):
    subject = "Neo4j"

    def __init__(self, config: Neo4jConfig) -> None:
        super().__init__()
        self.config = config
        self.remote_configured = config.remote is not None

    def init_runtime(self) -> str:
        if self.remote_configured:
            return self._bypass_message()
        self.initialized = True
        self.running = True
        return "graphdb ready"

    def start_runtime(self) -> str:
        if self.remote_configured:
            return self._bypass_message()
        if not self.initialized:
            raise GraphdbError("Initialization is required; run `xauditor graphdb init` first.")
        self.running = True
        return "Managed Neo4j container started"

    def stop_runtime(self) -> str:
        if self.remote_configured:
            return self._bypass_message()
        if not self.initialized or not self.running:
            return "No managed container was running"
        self.running = False
        return "Managed Neo4j container stopped"

    def reset_runtime(self, *, confirmed: bool) -> list[str]:
        if self.remote_configured:
            return []
        if not confirmed:
            raise GraphdbError("Reset requires explicit confirmation with --yes.")
        deleted = []
        if self.initialized:
            deleted.extend([self.config.container_name, self.config.volume_name, self.config.image])
        self.initialized = False
        self.running = False
        return deleted

    def wait_ready(self, *, timeout_seconds: int = 30) -> None:
        if self.remote_configured:
            return
        if not self.running:
            raise GraphdbError("Managed Neo4j container is not running.")
