from __future__ import annotations

from typing import Callable

from xauditor.config import ReportDBConfig
from xauditor.errors import ReportDBError
from xauditor.integrations.docker import (
    CommandResult,
    ContainerLifecycleManager,
    ContainerLifecycleSpec,
    MANAGED_LABEL,
    PortMapping,
    ReadinessProbe,
    default_command_runner,
)


def postgres_spec(config: ReportDBConfig) -> ContainerLifecycleSpec:
    """Build a lifecycle spec for the managed PostgreSQL report database."""

    return ContainerLifecycleSpec(
        subject="PostgreSQL",
        image=config.image,
        container_name=config.container_name,
        volume_name=config.volume_name,
        volume_mount_path="/var/lib/postgresql/data",
        ports=(PortMapping(host=config.port, container=5432),),
        env=(
            ("POSTGRES_USER", config.username),
            ("POSTGRES_PASSWORD", config.password),
            ("POSTGRES_DB", config.database),
        ),
        label=MANAGED_LABEL,
        ready_timeout_seconds=config.ready_timeout_seconds,
        remote_configured=config.remote is not None,
        init_command_hint="xauditor reportdb init",
        ready_message="reportdb ready",
        network_name=config.network_name,
    )


def pg_isready_probe(
    config: ReportDBConfig,
    *,
    command_runner: Callable[..., CommandResult] | None = None,
) -> ReadinessProbe:
    """Default readiness probe that shells out to `pg_isready` inside the container.

    Use a real `asyncpg`-based probe in production code paths that already
    hold a database connection; this default exists so the `reportdb init`
    flow can verify the container has come up without adding a dependency on
    asyncpg in the main package.
    """

    runner = command_runner or default_command_runner

    def probe() -> None:
        result = runner(
            [
                "docker",
                "exec",
                config.container_name,
                "pg_isready",
                "-U",
                config.username,
                "-d",
                config.database,
            ]
        )
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "pg_isready reported the database is not yet accepting connections"
            )
            raise ReportDBError(detail)

    return probe


class PostgresContainerManager(ContainerLifecycleManager):
    """Managed PostgreSQL container lifecycle.

    Wraps `ContainerLifecycleManager` with the PostgreSQL-specific spec and a
    default `pg_isready`-based readiness probe. Users who want an asyncpg
    probe can inject one via the constructor.
    """

    def __init__(
        self,
        config: ReportDBConfig,
        *,
        command_runner: Callable[..., CommandResult] | None = None,
        readiness_probe: ReadinessProbe | None = None,
    ) -> None:
        self.config = config
        probe = readiness_probe or pg_isready_probe(
            config, command_runner=command_runner
        )
        super().__init__(
            postgres_spec(config),
            error_cls=ReportDBError,
            command_runner=command_runner,
            readiness_probe=probe,
        )


class InMemoryPostgresContainerManager:
    """Test double mirroring `PostgresContainerManager`'s public surface."""

    def __init__(self, config: ReportDBConfig) -> None:
        self.config = config
        self.initialized = False
        self.running = False
        self.readiness_probe: ReadinessProbe | None = None
        self.remote_configured = config.remote is not None

    def set_readiness_probe(self, probe: ReadinessProbe) -> None:
        self.readiness_probe = probe

    def _bypass_message(self) -> str:
        return (
            "PostgreSQL: remote endpoint is configured; "
            "no managed container to manage."
        )

    def init_runtime(self) -> str:
        if self.remote_configured:
            return self._bypass_message()
        self.initialized = True
        self.running = True
        return "reportdb ready"

    def start_runtime(self) -> str:
        if self.remote_configured:
            return self._bypass_message()
        if not self.initialized:
            raise ReportDBError(
                "Initialization is required; run `xauditor reportdb init` first."
            )
        self.running = True
        return "Managed PostgreSQL container started"

    def stop_runtime(self) -> str:
        if self.remote_configured:
            return self._bypass_message()
        if not self.initialized or not self.running:
            return "No managed container was running"
        self.running = False
        return "Managed PostgreSQL container stopped"

    def reset_runtime(self, *, confirmed: bool) -> list[str]:
        if self.remote_configured:
            return []
        if not confirmed:
            raise ReportDBError("Reset requires explicit confirmation with --yes.")
        deleted: list[str] = []
        if self.initialized:
            deleted.extend(
                [
                    self.config.container_name,
                    self.config.volume_name,
                    self.config.image,
                ]
            )
        self.initialized = False
        self.running = False
        return deleted

    def wait_ready(self, *, timeout_seconds: int = 30) -> None:
        if self.remote_configured:
            return
        if not self.running:
            raise ReportDBError("Managed PostgreSQL container is not running.")
