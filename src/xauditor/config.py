from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from xauditor.errors import ConfigError
from xauditor.runtime import RuntimeLayout


VALID_LOG_LEVELS = ("error", "warning", "info", "debug")
CODER_THINKING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Canonical thinking-effort levels accepted by ``llm.providers.<name>
# .thinking_effort``. Currently identical to ``CODER_THINKING_EFFORTS``
# because the Anthropic SDK's ``effort`` Literal accepts the exact same
# set; aliased separately so a future Anthropic dial change doesn't
# silently propagate to the coder CLI semantics (or vice versa).
LLM_THINKING_EFFORTS = CODER_THINKING_EFFORTS
_CODER_CLI_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_\-./=]+$")


@dataclass(frozen=True)
class RemoteConnectionConfig:
    """Remote-endpoint connection settings shared by graph and report databases.

    When attached to a database config, xauditor connects to the remote URL and
    skips container lifecycle management for that database.
    """

    url: str
    ssl_ca: str | None = None
    pool_size: int | None = None


@dataclass(frozen=True)
class Neo4jConfig:
    image: str = "neo4j:5-community"
    container_name: str = "xauditor-neo4j"
    volume_name: str = "xauditor-neo4j-data"
    username: str = "neo4j"
    password: str = "xauditor-password"
    bolt_port: int = 7687
    http_port: int = 7474
    database: str = "neo4j"
    ready_timeout_seconds: int = 60
    # Docker network the managed neo4j container joins at creation time.
    # Default matches the portal runtime's network so portal backend can
    # reach neo4j by hostname without a post-create
    # ``docker network connect`` step. Operators with a custom topology
    # override via ``graphdb.network_name`` in xauditor.yml.
    network_name: str = "xauditor-portal-net"
    remote: RemoteConnectionConfig | None = None


@dataclass(frozen=True)
class ReportDBConfig:
    """Configuration for the managed or remote PostgreSQL report database."""

    image: str = "postgres:16-alpine"
    container_name: str = "xauditor-reportdb"
    volume_name: str = "xauditor-reportdb-data"
    username: str = "xauditor"
    password: str = "xauditor-password"
    port: int = 5432
    database: str = "xauditor_reportdb"
    ready_timeout_seconds: int = 60
    # Docker network the managed postgres container joins at creation time.
    # Default matches the portal runtime's network so portal backend can
    # reach postgres by hostname without a post-create
    # ``docker network connect`` step.
    network_name: str = "xauditor-portal-net"
    remote: RemoteConnectionConfig | None = None


@dataclass(frozen=True)
class PortalServiceConfig:
    """One service (backend or frontend) of the managed portal."""

    container_name: str
    image: str


@dataclass(frozen=True)
class PortalConfig:
    """Configuration for the managed two-container portal runtime."""

    network_name: str = "xauditor-portal-net"
    backend: PortalServiceConfig = field(
        default_factory=lambda: PortalServiceConfig(
            container_name="xauditor-portal-backend",
            image="xauditor-portal-backend:local",
        )
    )
    frontend: PortalServiceConfig = field(
        default_factory=lambda: PortalServiceConfig(
            container_name="xauditor-portal-frontend",
            image="xauditor-portal-frontend:local",
        )
    )
    host_port: int = 8080
    expose_backend_on_localhost: bool = False
    ready_timeout_seconds: int = 60


@dataclass(frozen=True)
class CoderConfig:
    """Configuration for the repo-global coder verification stage.

    The coder stage spawns the Claude Code CLI as a subprocess once per
    finding to verify the upstream pipeline's verdict against the rest of
    the repository. The transport is independent from the LangChain LLM
    providers because Claude Code manages its own model routing; see the
    ``llm-provider-configuration`` capability for the full rationale.
    """

    enabled: bool = False
    transport: str = "subprocess"
    cli_command: tuple[str, ...] = ("claude",)
    concurrency: int = 2
    thinking_effort: str | None = None
    model_url: str | None = None
    model_name: str | None = None
    model_api_key: str = ""
    request_timeout_seconds: int = 1800
    working_directory: str | None = None
    # HTTP-transport keys (only meaningful when transport == "http")
    endpoint: str = ""
    enable_auth: bool = False
    endpoint_token: str = ""
    poll_interval_seconds: float = 1.0
    preflight_timeout_seconds: int = 5
    # Container-runtime keys (only meaningful when transport == "http"
    # AND the endpoint resolves to a local target). The lifecycle
    # commands (`xauditor coder {init,build,start,stop,reset,status}`)
    # consume these; they are ignored under subprocess transport or
    # remote endpoints.
    container_image: str = "xauditor-coder-service:local"
    container_name: str = "xauditor-coder-service"
    runtime_socket_path: str = ""
    repo_mount_path: str = ""
    # Multi-project workspace fields. ``workspace_root`` is the host
    # directory bind-mounted at ``/workspace`` whose direct child
    # subdirectories ARE the audit-able projects. ``project_name``
    # overrides the default ``basename(realpath(audit.repo_root))``
    # inference. The ``effective_*`` fields hold the post-shim values
    # downstream callers should consume; when only the legacy
    # ``repo_mount_path`` is set, the parser populates the effective
    # fields from it and emits a one-time deprecation WARN.
    workspace_root: str = ""
    project_name: str = ""
    effective_workspace_root: str = ""
    effective_project_name: str = ""

    def __repr__(self) -> str:
        # Custom __repr__ keeps ``model_api_key`` and ``endpoint_token`` out
        # of debug logs and crash dumps. Both secrets are also registered
        # with ``RuntimeLogger`` so any inadvertent log line is redacted.
        redacted_key = "***" if self.model_api_key else ""
        redacted_token = "***" if self.endpoint_token else ""
        parts = [
            f"enabled={self.enabled!r}",
            f"transport={self.transport!r}",
            f"cli_command={self.cli_command!r}",
            f"concurrency={self.concurrency!r}",
            f"thinking_effort={self.thinking_effort!r}",
            f"model_url={self.model_url!r}",
            f"model_name={self.model_name!r}",
            f"model_api_key={redacted_key!r}",
            f"request_timeout_seconds={self.request_timeout_seconds!r}",
            f"working_directory={self.working_directory!r}",
            f"endpoint={self.endpoint!r}",
            f"enable_auth={self.enable_auth!r}",
            f"endpoint_token={redacted_token!r}",
            f"poll_interval_seconds={self.poll_interval_seconds!r}",
            f"preflight_timeout_seconds={self.preflight_timeout_seconds!r}",
            f"container_image={self.container_image!r}",
            f"container_name={self.container_name!r}",
            f"runtime_socket_path={self.runtime_socket_path!r}",
            f"repo_mount_path={self.repo_mount_path!r}",
            f"workspace_root={self.workspace_root!r}",
            f"project_name={self.project_name!r}",
            f"effective_workspace_root={self.effective_workspace_root!r}",
            f"effective_project_name={self.effective_project_name!r}",
        ]
        return "CoderConfig(" + ", ".join(parts) + ")"


@dataclass(frozen=True)
class RepositoryConfig:
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    root_dir: Path

    @property
    def layout(self) -> RuntimeLayout:
        return RuntimeLayout.from_root(self.root_dir)


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "info"
    file: str | None = None


@dataclass(frozen=True)
class GraphBuildConfig:
    enable_llm_enrichment: bool = True
    max_file_bytes: int = 10_000_000
    paths_max_depth: int = 40
    paths_max_count: int = 50_000
    # Chunk size for ``canonical_finalize`` streaming writes into Neo4j.
    # Each per-record-kind UNWIND-MERGE batch sends this many records
    # per Bolt round trip. The default of 5,000 is a sweet spot for
    # Neo4j 5.x batch ingest (Neo4j docs recommend 1k–10k). Override
    # via ``XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE`` env var, clamped
    # to ``[100, 50000]``. See ``consolidate-on-neo4j-source`` design D6.
    neo4j_chunk_size: int = 5_000


@dataclass(frozen=True)
class GraphConfig:
    build: GraphBuildConfig = field(default_factory=GraphBuildConfig)


@dataclass(frozen=True)
class AuditConfig:
    """Runtime knobs for the audit workflow.

    ``worker_count`` is the single concurrency knob (since 0.10.0,
    ``consolidate-on-worker-count``). Host-wide in-flight path count
    equals ``worker_count`` regardless of pool kind:

    - ``1`` (default) → ``InlineExecutor``: paths run synchronously
      on master's thread, one at a time. No subprocess, no thread
      pool, no queue. Best fit for unit tests and dev runs.
    - ``>= 2`` → ``LocalSubprocessPool``: spawns N subprocess
      workers via ``multiprocessing.spawn``; each loops over a
      shared queue and processes one path at a time. Spawn cost
      paid once per pool lifetime. Best fit for production runs
      where worker crash isolation matters.

    ``worker_count`` is bounded ``[1, 16]``. Out-of-range values
    raise ``ConfigError`` at parse time naming the dot-path.

    Pre-0.10.0 ``audit.path_concurrency`` is removed; configs that
    still set it raise ``ConfigError`` with a translation message.
    The ``XAUDITOR_AUDIT_PATH_CONCURRENCY`` env var is no longer
    recognised — it emits a one-time WARN at parse time and is
    otherwise ignored, so stale shell rcs don't block startup.

    ``shutdown_timeout_seconds`` bounds the wall-clock budget for the
    graceful shutdown that begins on the first SIGINT. After this many
    seconds elapse the audit escalates to ``shutdown(wait=False)`` on
    every executor and SIGTERM/SIGKILL on every tracked subprocess.
    Bounded ``[1, 600]``.

    ``coder_shutdown_timeout_seconds`` is the corresponding cap for the
    coder dispatcher's drain inside the run-level shutdown — the coder
    layer always finishes (or is forced) before the run-level deadline
    expires. Bounded ``[1, 600]``. A startup warning fires when it is
    ``>= shutdown_timeout_seconds`` because in that configuration the
    coder layer cannot finish gracefully under the run-level cap.
    """

    worker_count: int = 1
    shutdown_timeout_seconds: int = 30
    coder_shutdown_timeout_seconds: int = 20


PROVIDER_KIND_OPENAI = "openai"
PROVIDER_KIND_ANTHROPIC = "anthropic"
PROVIDER_KINDS: tuple[str, ...] = (PROVIDER_KIND_OPENAI, PROVIDER_KIND_ANTHROPIC)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    thinking_enabled: bool = False
    # Discrete extended-thinking dial recognised by the Anthropic SDK
    # since late 2025 — preferred over the legacy ``thinking_enabled``
    # +budget tuple, which Anthropic is deprecating. One of
    # ``LLM_THINKING_EFFORTS``: ``"low" | "medium" | "high" | "xhigh"
    # | "max"``. ``None`` means "do not pass an effort dial". When set
    # on a ``kind: anthropic`` provider it routes to ChatAnthropic's
    # ``effort=`` kwarg; on OpenAI-compat providers the field is
    # ignored (Anthropic-only).
    thinking_effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None
    # Per-call request timeout (seconds) for the underlying LangChain
    # client. ``None`` means "do not pass a timeout kwarg" — the SDK's
    # own default (~600s on both OpenAI and Anthropic Python SDKs)
    # applies. Operators on ``thinking_effort: max`` or vLLM deep-
    # reasoning workloads should set a higher value here (e.g. 1800)
    # because the SDK default occasionally trips on legitimate slow
    # responses. Routes to ``ChatOpenAI(request_timeout=...)`` for
    # ``kind: openai`` providers and
    # ``ChatAnthropic(default_request_timeout=...)`` for
    # ``kind: anthropic``.
    request_timeout_seconds: float | None = None
    # Wire protocol the provider speaks. ``"openai"`` (default) uses
    # ``langchain_openai.ChatOpenAI`` against the configured
    # ``base_url`` — every existing yaml in the wild parses with this
    # default. ``"anthropic"`` opts into the native Anthropic SDK
    # (``langchain_anthropic.ChatAnthropic``) so operators can target
    # ``api.anthropic.com`` (or a Claude-API-compatible internal
    # endpoint) without going through Anthropic's OpenAI-compat shim.
    kind: str = PROVIDER_KIND_OPENAI

    def __repr__(self) -> str:
        redacted_key = "***" if self.api_key else ""
        parts = [
            f"base_url={self.base_url!r}",
            f"api_key={redacted_key!r}",
            f"model_name={self.model_name!r}",
            f"kind={self.kind!r}",
            f"thinking_enabled={self.thinking_enabled!r}",
        ]
        if self.thinking_effort is not None:
            parts.append(f"thinking_effort={self.thinking_effort!r}")
        if self.request_timeout_seconds is not None:
            parts.append(
                f"request_timeout_seconds={self.request_timeout_seconds!r}"
            )
        for name in ("temperature", "top_p", "top_k", "repetition_penalty"):
            value = getattr(self, name)
            if value is not None:
                parts.append(f"{name}={value!r}")
        return "LLMConfig(" + ", ".join(parts) + ")"

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.base_url:
            missing.append("base_url")
        if not self.api_key:
            missing.append("api_key")
        if not self.model_name:
            missing.append("model_name")
        return missing

    def sampling_dict(self) -> dict[str, float | int | None]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
        }


@dataclass(frozen=True)
class AgentLLMOverride:
    provider: str = ""
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None
    # Overlays the resolved provider's ``request_timeout_seconds``
    # for this agent only. ``None`` falls back to the provider's
    # value (which may itself be ``None``, meaning SDK default).
    request_timeout_seconds: float | None = None

    def sampling_overlay(self, base: LLMConfig) -> dict[str, float | int | None]:
        """Return the effective sampling dict: provider values overlaid with override fields."""
        resolved = base.sampling_dict()
        for field_name in ("temperature", "top_p", "top_k", "repetition_penalty"):
            value = getattr(self, field_name)
            if value is not None:
                resolved[field_name] = value
        return resolved

    def has_sampling(self) -> bool:
        return any(
            getattr(self, field_name) is not None
            for field_name in ("temperature", "top_p", "top_k", "repetition_penalty")
        )


@dataclass(frozen=True)
class AnalyzerTeamConfig:
    subagent_count: int = 1
    provider_list: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatorTeamConfig:
    subagent_count: int = 1
    provider_list: tuple[str, ...] = ()
    debate_rounds: int = 5


@dataclass(frozen=True)
class ExploiterTeamConfig:
    subagent_count: int = 1
    provider_list: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeamingConfig:
    enabled: bool = False
    analyzer: AnalyzerTeamConfig = field(default_factory=AnalyzerTeamConfig)
    validator: ValidatorTeamConfig = field(default_factory=ValidatorTeamConfig)
    exploiter: ExploiterTeamConfig = field(default_factory=ExploiterTeamConfig)


@dataclass(frozen=True)
class LLMSettings:
    default_provider: str = ""
    providers: dict[str, LLMConfig] = field(default_factory=dict)
    agent_overrides: dict[str, AgentLLMOverride] = field(default_factory=dict)
    graph_workflow_version: str = "graph-v2"
    enrichment_workflow_version: str = "enrich-v2"
    audit_workflow_version: str = "audit-v1"

    @property
    def agent_providers(self) -> dict[str, str]:
        return {
            agent: override.provider
            for agent, override in self.agent_overrides.items()
            if override.provider
        }

    @property
    def base_url(self) -> str:
        return self.provider_for().base_url

    @property
    def api_key(self) -> str:
        return self.provider_for().api_key

    @property
    def model_name(self) -> str:
        return self.provider_for().model_name

    @property
    def thinking_enabled(self) -> bool:
        return self.provider_for().thinking_enabled

    def provider_for(self, agent_name: str | None = None) -> LLMConfig:
        provider_name = self.selected_provider(agent_name)
        if not provider_name:
            return LLMConfig()
        return self.providers.get(provider_name, LLMConfig())

    def selected_provider(self, agent_name: str | None = None) -> str:
        if agent_name is not None:
            override = self.agent_overrides.get(agent_name)
            if override is not None and override.provider:
                return override.provider
        return self.default_provider

    def sampling_for(self, agent_name: str | None = None) -> dict[str, float | int | None]:
        provider = self.provider_for(agent_name)
        override = self.agent_overrides.get(agent_name) if agent_name is not None else None
        if override is None:
            return provider.sampling_dict()
        return override.sampling_overlay(provider)

    def request_timeout_for(self, agent_name: str | None = None) -> float | None:
        """Resolve the per-call LLM request timeout for the given agent.

        Overlay rule (mirrors sampling): agent override → provider
        field → ``None``. ``None`` means "do not pass a timeout kwarg
        to the underlying SDK" — the SDK's own default (~600s) applies.
        """

        provider = self.provider_for(agent_name)
        override = (
            self.agent_overrides.get(agent_name) if agent_name is not None else None
        )
        if override is not None and override.request_timeout_seconds is not None:
            return override.request_timeout_seconds
        return provider.request_timeout_seconds

    def api_keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    provider.api_key
                    for provider in self.providers.values()
                    if provider.api_key
                }
            )
        )

    def validate(self, *, require_llm: bool) -> None:
        has_llm_config = bool(self.default_provider or self.providers or self.agent_overrides)
        if not has_llm_config:
            if require_llm:
                raise ConfigError("Missing required LLM setting: llm.default_provider")
            return

        if not self.default_provider:
            raise ConfigError("Missing required LLM setting: llm.default_provider")

        for provider_name, provider in sorted(self.providers.items()):
            missing = provider.missing_fields()
            if missing:
                rendered = ", ".join(f"llm.providers.{provider_name}.{field_name}" for field_name in missing)
                raise ConfigError(f"Missing required LLM settings: {rendered}")
            _validate_sampling_ranges(provider, dot_prefix=f"llm.providers.{provider_name}")

        available = ", ".join(sorted(self.providers)) or "none"
        if self.default_provider not in self.providers:
            raise ConfigError(
                "Unknown default provider "
                f"`{self.default_provider}`. Available configured providers: {available}"
            )

        for agent_name, override in sorted(self.agent_overrides.items()):
            if override.provider and override.provider not in self.providers:
                raise ConfigError(
                    f"Unknown provider `{override.provider}` for agents.{agent_name}.llm.provider. "
                    f"Available configured providers: {available}"
                )
            _validate_sampling_ranges(override, dot_prefix=f"agents.{agent_name}.llm")


@dataclass(frozen=True)
class XAuditorConfig:
    repo_root: Path
    graphdb: Neo4jConfig
    repository: RepositoryConfig
    runtime: RuntimeConfig
    logging: LoggingConfig
    llm: LLMSettings
    graph: GraphConfig = field(default_factory=GraphConfig)
    teaming: TeamingConfig = field(default_factory=TeamingConfig)
    reportdb: ReportDBConfig = field(default_factory=ReportDBConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    coder: CoderConfig = field(default_factory=CoderConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)


DEFAULT_CONFIG_FILENAMES = ("xauditor.yml", ".xauditor.yml")
ENV_KEY_MAP = {
    "XAUDITOR_GRAPHDB_IMAGE": "graph.db.image",
    "XAUDITOR_GRAPHDB_PASSWORD": "graph.db.password",
    "XAUDITOR_GRAPHDB_NETWORK_NAME": "graph.db.network_name",
    "XAUDITOR_GRAPHDB_REMOTE_URL": "graph.db.remote.url",
    "XAUDITOR_RUNTIME_ROOT_DIR": "runtime.root_dir",
    "XAUDITOR_LOGGING_LEVEL": "logging.level",
    "XAUDITOR_LOGGING_FILE": "logging.file",
    "XAUDITOR_LLM_DEFAULT_PROVIDER": "llm.default_provider",
    "XAUDITOR_LLM_BASE_URL": "llm.base_url",
    "XAUDITOR_LLM_API_KEY": "llm.api_key",
    "XAUDITOR_LLM_MODEL_NAME": "llm.model_name",
    "XAUDITOR_LLM_THINKING_ENABLED": "llm.thinking_enabled",
    "XAUDITOR_LLM_REQUEST_TIMEOUT_SECONDS": "llm.request_timeout_seconds",
    "XAUDITOR_AUDIT_WORKER_COUNT": "audit.worker_count",
    "XAUDITOR_AUDIT_SHUTDOWN_TIMEOUT_SECONDS": "audit.shutdown_timeout_seconds",
    "XAUDITOR_AUDIT_CODER_SHUTDOWN_TIMEOUT_SECONDS": "audit.coder.shutdown_timeout_seconds",
    "XAUDITOR_REPOSITORY_EXCLUDES": "repository.excludes",
    "XAUDITOR_AGENTS_GRAPH_BUILDER_LLM_PROVIDER": "agents.graph_builder.llm.provider",
    "XAUDITOR_AGENTS_AUDITOR_LLM_PROVIDER": "agents.auditor.llm.provider",
    "XAUDITOR_AGENTS_EXPLOITATION_LLM_PROVIDER": "agents.exploitation.llm.provider",
    "XAUDITOR_AGENTS_VALIDATOR_LLM_PROVIDER": "agents.validator.llm.provider",
    "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "graph.build.enable_llm_enrichment",
    "XAUDITOR_TEAMING_ENABLED": "teaming.enabled",
    "XAUDITOR_TEAMING_ANALYZER_SUBAGENT_COUNT": "teaming.analyzer.subagent_count",
    "XAUDITOR_TEAMING_ANALYZER_PROVIDER_LIST": "teaming.analyzer.provider_list",
    "XAUDITOR_TEAMING_VALIDATOR_SUBAGENT_COUNT": "teaming.validator.subagent_count",
    "XAUDITOR_TEAMING_VALIDATOR_PROVIDER_LIST": "teaming.validator.provider_list",
    "XAUDITOR_TEAMING_VALIDATOR_DEBATE_ROUNDS": "teaming.validator.debate_rounds",
    "XAUDITOR_TEAMING_EXPLOITER_SUBAGENT_COUNT": "teaming.exploiter.subagent_count",
    "XAUDITOR_TEAMING_EXPLOITER_PROVIDER_LIST": "teaming.exploiter.provider_list",
    "XAUDITOR_REPORTDB_IMAGE": "reportdb.image",
    "XAUDITOR_REPORTDB_PASSWORD": "reportdb.password",
    "XAUDITOR_REPORTDB_PORT": "reportdb.port",
    "XAUDITOR_REPORTDB_DATABASE": "reportdb.database",
    "XAUDITOR_REPORTDB_NETWORK_NAME": "reportdb.network_name",
    "XAUDITOR_REPORTDB_REMOTE_URL": "reportdb.remote.url",
    "XAUDITOR_CODER_ENABLED": "coder.enabled",
    "XAUDITOR_CODER_CLI_COMMAND": "coder.cli_command",
    "XAUDITOR_CODER_CONCURRENCY": "coder.concurrency",
    "XAUDITOR_CODER_THINKING_EFFORT": "coder.thinking_effort",
    "XAUDITOR_CODER_MODEL_URL": "coder.model_url",
    "XAUDITOR_CODER_MODEL_NAME": "coder.model_name",
    "XAUDITOR_CODER_MODEL_API_KEY": "coder.model_api_key",
    "XAUDITOR_CODER_REQUEST_TIMEOUT_SECONDS": "coder.request_timeout_seconds",
    "XAUDITOR_CODER_WORKING_DIRECTORY": "coder.working_directory",
    "XAUDITOR_CODER_TRANSPORT": "coder.transport",
    "XAUDITOR_CODER_ENDPOINT": "coder.endpoint",
    "XAUDITOR_CODER_ENABLE_AUTH": "coder.enable_auth",
    "XAUDITOR_CODER_ENDPOINT_TOKEN": "coder.endpoint_token",
    "XAUDITOR_CODER_POLL_INTERVAL_SECONDS": "coder.poll_interval_seconds",
    "XAUDITOR_CODER_PREFLIGHT_TIMEOUT_SECONDS": "coder.preflight_timeout_seconds",
    "XAUDITOR_CODER_CONTAINER_IMAGE": "coder.container_image",
    "XAUDITOR_CODER_CONTAINER_NAME": "coder.container_name",
    "XAUDITOR_CODER_RUNTIME_SOCKET_PATH": "coder.runtime_socket_path",
    "XAUDITOR_CODER_REPO_MOUNT_PATH": "coder.repo_mount_path",
    "XAUDITOR_CODER_WORKSPACE_ROOT": "coder.workspace_root",
    "XAUDITOR_CODER_PROJECT_NAME": "coder.project_name",
}


def load_config(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    require_llm: bool = False,
) -> XAuditorConfig:
    env = env or {}
    data: dict[str, Any] = {
        "repository": {},
        "runtime": {"root_dir": ".xauditor"},
        "logging": {"level": "info"},
        "llm": {},
        "agents": {},
        "graph": {"build": {}, "db": {}},
        "teaming": {},
        "reportdb": {},
        "portal": {},
        "coder": {},
    }
    for file_path in _resolve_config_paths(repo_root, config_path, env):
        loaded = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        _merge_mapping(data, loaded)
    env_data = _env_to_mapping(env)
    _merge_mapping(data, env_data)
    if cli_overrides:
        for key, value in cli_overrides.items():
            _assign_dot_path(data, key, value)

    runtime_root = Path(str(data["runtime"].get("root_dir", ".xauditor")))
    if not runtime_root.is_absolute():
        runtime_root = repo_root / runtime_root

    repository_excludes = data["repository"].get("excludes", ())
    if isinstance(repository_excludes, str):
        repository_excludes = tuple(filter(None, (part.strip() for part in repository_excludes.split(","))))
    else:
        repository_excludes = tuple(repository_excludes or ())

    logging_data = data.get("logging") or {}
    logging_level = str(logging_data.get("level", "info")).strip().lower()
    if logging_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            "Invalid logging.level "
            f"`{logging_level}`. Supported values: {', '.join(VALID_LOG_LEVELS)}"
        )
    logging_file = logging_data.get("file")
    logging_file = str(logging_file).strip() if logging_file else None

    llm_settings = _build_llm_settings(data)
    llm_settings.validate(require_llm=require_llm)

    graph_config = _build_graph_config(data)
    teaming_config = _build_teaming_config(data, llm_settings)
    graphdb_config = _build_neo4j_config(data)
    reportdb_config = _build_reportdb_config(data)
    portal_config = _build_portal_config(data)
    coder_config = _build_coder_config(data, runtime_root=runtime_root)
    audit_config = _build_audit_config(data)

    config = XAuditorConfig(
        repo_root=repo_root,
        graphdb=graphdb_config,
        repository=RepositoryConfig(excludes=repository_excludes),
        runtime=RuntimeConfig(root_dir=runtime_root),
        logging=LoggingConfig(level=logging_level, file=logging_file),
        llm=llm_settings,
        graph=graph_config,
        teaming=teaming_config,
        reportdb=reportdb_config,
        portal=portal_config,
        coder=coder_config,
        audit=audit_config,
    )
    return config


_PATH_CONCURRENCY_REMOVAL_MESSAGE = (
    "`audit.path_concurrency` is no longer a valid setting (removed in "
    "0.10.0, ``consolidate-on-worker-count``).\n\n"
    "xauditor 0.10+ uses a single concurrency knob — ``audit.worker_count``.\n\n"
    "Translation:\n"
    "  pre-0.10.0                       →  0.10.0+\n"
    "  worker_count: 1                     worker_count: 1\n"
    "  path_concurrency: 4                 (master runs paths sequentially)\n"
    "\n"
    "  worker_count: 1                     worker_count: 4\n"
    "  path_concurrency: 4                 (4 subprocess workers, ~80 MB each)\n"
    "\n"
    "Note that worker_count: N >= 2 in 0.10.0+ spawns N subprocess workers\n"
    "(each ~80 MB Bolt client). For pure I/O-bound LLM workloads on a single\n"
    "process, use worker_count: 1; the audit will run paths sequentially."
)


def _build_audit_config(data: Mapping[str, Any]) -> AuditConfig:
    audit_raw = data.get("audit") or {}
    if not isinstance(audit_raw, Mapping):
        raise ConfigError("Invalid audit configuration; expected a mapping.")
    if "path_concurrency" in audit_raw:
        raise ConfigError(_PATH_CONCURRENCY_REMOVAL_MESSAGE)
    defaults = AuditConfig()
    worker_count = _normalize_positive_int(
        audit_raw.get("worker_count", defaults.worker_count),
        field_name="audit.worker_count",
        minimum=1,
    )
    if worker_count > 16:
        raise ConfigError(
            f"Invalid audit.worker_count value `{worker_count}`; "
            "expected a value in [1, 16]."
        )
    shutdown_timeout = _normalize_bounded_int(
        audit_raw.get("shutdown_timeout_seconds", defaults.shutdown_timeout_seconds),
        field_name="audit.shutdown_timeout_seconds",
        minimum=1,
        maximum=600,
    )
    audit_coder_raw = audit_raw.get("coder") or {}
    if not isinstance(audit_coder_raw, Mapping):
        raise ConfigError("Invalid audit.coder configuration; expected a mapping.")
    coder_shutdown_timeout = _normalize_bounded_int(
        audit_coder_raw.get(
            "shutdown_timeout_seconds", defaults.coder_shutdown_timeout_seconds
        ),
        field_name="audit.coder.shutdown_timeout_seconds",
        minimum=1,
        maximum=600,
    )
    if coder_shutdown_timeout >= shutdown_timeout:
        _emit_one_shot_warn(
            "audit.coder.shutdown_timeout_seconds_vs_run_level",
            (
                f"audit.coder.shutdown_timeout_seconds={coder_shutdown_timeout} "
                f"is >= audit.shutdown_timeout_seconds={shutdown_timeout}; "
                "the coder layer cannot finish gracefully under the run-level "
                "cap. Reduce audit.coder.shutdown_timeout_seconds below "
                "audit.shutdown_timeout_seconds to silence this warning."
            ),
        )
    return AuditConfig(
        worker_count=worker_count,
        shutdown_timeout_seconds=shutdown_timeout,
        coder_shutdown_timeout_seconds=coder_shutdown_timeout,
    )


def _normalize_bounded_int(
    value: Any, *, field_name: str, minimum: int, maximum: int
) -> int:
    parsed = _normalize_positive_int(value, field_name=field_name, minimum=minimum)
    if parsed > maximum:
        raise ConfigError(
            f"Invalid {field_name} value `{parsed}`; "
            f"expected a value in [{minimum}, {maximum}]."
        )
    return parsed


_ONE_SHOT_WARNED: set[str] = set()


def _emit_one_shot_warn(key: str, message: str) -> None:
    """Emit a one-shot WARN per *key* across the process lifetime.

    Same shape as :func:`_emit_coder_workspace_warn` but reusable for any
    config-validation warning that should not spam the operator across
    repeated audits.
    """

    if key in _ONE_SHOT_WARNED:
        return
    _ONE_SHOT_WARNED.add(key)
    import sys as _sys

    _sys.stderr.write(f"WARN: {message}\n")


def _build_graph_config(data: Mapping[str, Any]) -> GraphConfig:
    graph_data = data.get("graph") or {}
    if not isinstance(graph_data, Mapping):
        raise ConfigError("Invalid graph configuration; expected a mapping.")
    build_data = graph_data.get("build") or {}
    if not isinstance(build_data, Mapping):
        raise ConfigError("Invalid graph.build configuration; expected a mapping.")
    enable_llm_enrichment = _normalize_bool(
        build_data.get("enable_llm_enrichment", True),
        field_name="graph.build.enable_llm_enrichment",
    )
    defaults = GraphBuildConfig()
    max_file_bytes = _normalize_positive_int(
        build_data.get("max_file_bytes", defaults.max_file_bytes),
        field_name="graph.build.max_file_bytes",
        minimum=1,
    )
    paths_max_depth = _normalize_positive_int(
        build_data.get("paths_max_depth", defaults.paths_max_depth),
        field_name="graph.build.paths_max_depth",
        minimum=1,
    )
    paths_max_count = _normalize_positive_int(
        build_data.get("paths_max_count", defaults.paths_max_count),
        field_name="graph.build.paths_max_count",
        minimum=1,
    )
    neo4j_chunk_size = _resolve_neo4j_chunk_size(
        build_data.get("neo4j_chunk_size"),
        default=defaults.neo4j_chunk_size,
    )
    return GraphConfig(
        build=GraphBuildConfig(
            enable_llm_enrichment=enable_llm_enrichment,
            max_file_bytes=max_file_bytes,
            paths_max_depth=paths_max_depth,
            paths_max_count=paths_max_count,
            neo4j_chunk_size=neo4j_chunk_size,
        )
    )


_NEO4J_CHUNK_SIZE_ENV = "XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE"
_NEO4J_CHUNK_SIZE_MIN = 100
_NEO4J_CHUNK_SIZE_MAX = 50_000


def _resolve_neo4j_chunk_size(yaml_value: Any, *, default: int) -> int:
    """Resolve the Neo4j-write chunk size: env var > yaml > default.

    The env var ``XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE`` overrides any
    yaml setting. Values out of range ``[100, 50000]`` raise
    ``ConfigError`` naming both the source and the bounds.
    """

    env_raw = os.environ.get(_NEO4J_CHUNK_SIZE_ENV)
    if env_raw is not None and env_raw.strip():
        try:
            env_value = int(env_raw)
        except ValueError as exc:
            raise ConfigError(
                f"Invalid {_NEO4J_CHUNK_SIZE_ENV} value `{env_raw}`; "
                f"expected an integer in [{_NEO4J_CHUNK_SIZE_MIN}, "
                f"{_NEO4J_CHUNK_SIZE_MAX}]."
            ) from exc
        if not (_NEO4J_CHUNK_SIZE_MIN <= env_value <= _NEO4J_CHUNK_SIZE_MAX):
            raise ConfigError(
                f"{_NEO4J_CHUNK_SIZE_ENV}={env_value} is out of range; "
                f"expected an integer in [{_NEO4J_CHUNK_SIZE_MIN}, "
                f"{_NEO4J_CHUNK_SIZE_MAX}]."
            )
        return env_value
    if yaml_value is None:
        return default
    yaml_int = _normalize_positive_int(
        yaml_value,
        field_name="graph.build.neo4j_chunk_size",
        minimum=_NEO4J_CHUNK_SIZE_MIN,
    )
    if yaml_int > _NEO4J_CHUNK_SIZE_MAX:
        raise ConfigError(
            f"graph.build.neo4j_chunk_size={yaml_int} is out of range; "
            f"expected an integer in [{_NEO4J_CHUNK_SIZE_MIN}, "
            f"{_NEO4J_CHUNK_SIZE_MAX}]."
        )
    return yaml_int


_ALLOWED_REMOTE_SCHEMES = (
    "postgresql://",
    "postgresql+asyncpg://",
    "postgres://",
    "bolt://",
    "bolt+s://",
    "bolt+ssc://",
    "neo4j://",
    "neo4j+s://",
    "neo4j+ssc://",
)


def _validate_remote_url(url: str, *, dot_path: str) -> None:
    lowered = url.lower()
    if not any(lowered.startswith(scheme) for scheme in _ALLOWED_REMOTE_SCHEMES):
        supported = ", ".join(_ALLOWED_REMOTE_SCHEMES)
        raise ConfigError(
            f"Invalid {dot_path} value `{url}`; expected one of: {supported}."
        )


def _build_remote_config(
    remote_data: Any, *, dot_prefix: str
) -> RemoteConnectionConfig | None:
    if remote_data is None:
        return None
    if not isinstance(remote_data, Mapping):
        raise ConfigError(f"Invalid {dot_prefix} configuration; expected a mapping.")
    url_raw = remote_data.get("url")
    if url_raw is None or not str(url_raw).strip():
        raise ConfigError(f"Missing required setting: {dot_prefix}.url")
    url_str = str(url_raw).strip()
    _validate_remote_url(url_str, dot_path=f"{dot_prefix}.url")
    ssl_ca_raw = remote_data.get("ssl_ca")
    ssl_ca = str(ssl_ca_raw).strip() if ssl_ca_raw not in (None, "") else None
    pool_size_raw = remote_data.get("pool_size")
    pool_size = (
        _normalize_positive_int(
            pool_size_raw, field_name=f"{dot_prefix}.pool_size", minimum=1
        )
        if pool_size_raw is not None
        else None
    )
    return RemoteConnectionConfig(url=url_str, ssl_ca=ssl_ca, pool_size=pool_size)


def _build_neo4j_config(data: Mapping[str, Any]) -> Neo4jConfig:
    graph_data = data.get("graph") or {}
    if not isinstance(graph_data, Mapping):
        raise ConfigError("Invalid graph configuration; expected a mapping.")
    db_raw = graph_data.get("db") or {}
    if not isinstance(db_raw, Mapping):
        raise ConfigError("Invalid graph.db configuration; expected a mapping.")
    db_data: dict[str, Any] = dict(db_raw)
    remote_raw = db_data.pop("remote", None)
    remote = _build_remote_config(remote_raw, dot_prefix="graph.db.remote")
    for port_field in ("bolt_port", "http_port"):
        if port_field in db_data:
            db_data[port_field] = _normalize_positive_int(
                db_data[port_field], field_name=f"graph.db.{port_field}", minimum=1
            )
    if "ready_timeout_seconds" in db_data:
        db_data["ready_timeout_seconds"] = _normalize_positive_int(
            db_data["ready_timeout_seconds"],
            field_name="graph.db.ready_timeout_seconds",
            minimum=1,
        )
    if "network_name" in db_data:
        network_name = str(db_data["network_name"]).strip()
        if not network_name:
            raise ConfigError(
                "Invalid graph.db.network_name; expected a non-empty string."
            )
        db_data["network_name"] = network_name
    try:
        return Neo4jConfig(**db_data, remote=remote)
    except TypeError as exc:
        raise ConfigError(f"Invalid graph.db configuration: {exc}") from exc


def _build_portal_config(data: Mapping[str, Any]) -> PortalConfig:
    portal_raw = data.get("portal") or {}
    if not isinstance(portal_raw, Mapping):
        raise ConfigError("Invalid portal configuration; expected a mapping.")
    defaults = PortalConfig()
    network_name = str(portal_raw.get("network_name", defaults.network_name)).strip()
    if not network_name:
        raise ConfigError("Invalid portal.network_name; expected a non-empty string.")
    host_port_raw = portal_raw.get("host_port", defaults.host_port)
    host_port = _normalize_positive_int(
        host_port_raw, field_name="portal.host_port", minimum=1
    )
    ready_timeout_raw = portal_raw.get(
        "ready_timeout_seconds", defaults.ready_timeout_seconds
    )
    ready_timeout_seconds = _normalize_positive_int(
        ready_timeout_raw, field_name="portal.ready_timeout_seconds", minimum=1
    )
    expose_backend = _normalize_bool(
        portal_raw.get("expose_backend_on_localhost", False),
        field_name="portal.expose_backend_on_localhost",
    )
    backend = _build_portal_service_config(
        portal_raw.get("backend"),
        defaults.backend,
        dot_prefix="portal.backend",
    )
    frontend = _build_portal_service_config(
        portal_raw.get("frontend"),
        defaults.frontend,
        dot_prefix="portal.frontend",
    )
    return PortalConfig(
        network_name=network_name,
        backend=backend,
        frontend=frontend,
        host_port=host_port,
        expose_backend_on_localhost=expose_backend,
        ready_timeout_seconds=ready_timeout_seconds,
    )


def _build_portal_service_config(
    service_data: Any,
    defaults: PortalServiceConfig,
    *,
    dot_prefix: str,
) -> PortalServiceConfig:
    if service_data is None:
        return defaults
    if not isinstance(service_data, Mapping):
        raise ConfigError(f"Invalid {dot_prefix} configuration; expected a mapping.")
    container_name = str(
        service_data.get("container_name", defaults.container_name)
    ).strip()
    image = str(service_data.get("image", defaults.image)).strip()
    if not container_name:
        raise ConfigError(f"Invalid {dot_prefix}.container_name; expected a non-empty string.")
    if not image:
        raise ConfigError(f"Invalid {dot_prefix}.image; expected a non-empty string.")
    return PortalServiceConfig(container_name=container_name, image=image)


def _build_reportdb_config(data: Mapping[str, Any]) -> ReportDBConfig:
    reportdb_raw = data.get("reportdb") or {}
    if not isinstance(reportdb_raw, Mapping):
        raise ConfigError("Invalid reportdb configuration; expected a mapping.")
    reportdb_data: dict[str, Any] = dict(reportdb_raw)
    remote_raw = reportdb_data.pop("remote", None)
    remote = _build_remote_config(remote_raw, dot_prefix="reportdb.remote")
    if "port" in reportdb_data:
        reportdb_data["port"] = _normalize_positive_int(
            reportdb_data["port"], field_name="reportdb.port", minimum=1
        )
    if "ready_timeout_seconds" in reportdb_data:
        reportdb_data["ready_timeout_seconds"] = _normalize_positive_int(
            reportdb_data["ready_timeout_seconds"],
            field_name="reportdb.ready_timeout_seconds",
            minimum=1,
        )
    if "network_name" in reportdb_data:
        network_name = str(reportdb_data["network_name"]).strip()
        if not network_name:
            raise ConfigError(
                "Invalid reportdb.network_name; expected a non-empty string."
            )
        reportdb_data["network_name"] = network_name
    try:
        return ReportDBConfig(**reportdb_data, remote=remote)
    except TypeError as exc:
        raise ConfigError(f"Invalid reportdb configuration: {exc}") from exc


def _normalize_provider_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(
            part for part in (segment.strip() for segment in value.split(",")) if part
        )
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for entry in value:
            name = str(entry).strip()
            if not name:
                raise ConfigError(f"Invalid {field_name} entry: empty provider name.")
            items.append(name)
        return tuple(items)
    raise ConfigError(
        f"Invalid {field_name} value; expected a list of provider names or a comma-separated string."
    )


def _normalize_positive_int(value: Any, *, field_name: str, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"Invalid {field_name} value `{value}`; expected an integer >= {minimum}.")
    if isinstance(value, int):
        number = value
    else:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Invalid {field_name} value `{value}`; expected an integer >= {minimum}."
            ) from exc
    if number < minimum:
        raise ConfigError(f"Invalid {field_name} value `{number}`; must be >= {minimum}.")
    return number


def _build_teaming_config(data: Mapping[str, Any], llm_settings: LLMSettings) -> TeamingConfig:
    teaming_data = data.get("teaming") or {}
    if not isinstance(teaming_data, Mapping):
        raise ConfigError("Invalid teaming configuration; expected a mapping.")

    enabled = _normalize_bool(teaming_data.get("enabled", False), field_name="teaming.enabled")

    analyzer_raw = teaming_data.get("analyzer") or {}
    validator_raw = teaming_data.get("validator") or {}
    exploiter_raw = teaming_data.get("exploiter") or {}
    for name, block in (("analyzer", analyzer_raw), ("validator", validator_raw), ("exploiter", exploiter_raw)):
        if not isinstance(block, Mapping):
            raise ConfigError(f"Invalid teaming.{name} configuration; expected a mapping.")

    if not enabled:
        return TeamingConfig(
            enabled=False,
            analyzer=AnalyzerTeamConfig(
                subagent_count=_normalize_positive_int(
                    analyzer_raw.get("subagent_count", 1),
                    field_name="teaming.analyzer.subagent_count",
                ),
                provider_list=_normalize_provider_list(
                    analyzer_raw.get("provider_list", ()),
                    field_name="teaming.analyzer.provider_list",
                ),
            ),
            validator=ValidatorTeamConfig(
                subagent_count=_normalize_positive_int(
                    validator_raw.get("subagent_count", 1),
                    field_name="teaming.validator.subagent_count",
                ),
                provider_list=_normalize_provider_list(
                    validator_raw.get("provider_list", ()),
                    field_name="teaming.validator.provider_list",
                ),
                debate_rounds=_normalize_positive_int(
                    validator_raw.get("debate_rounds", 5),
                    field_name="teaming.validator.debate_rounds",
                ),
            ),
            exploiter=ExploiterTeamConfig(
                subagent_count=_normalize_positive_int(
                    exploiter_raw.get("subagent_count", 1),
                    field_name="teaming.exploiter.subagent_count",
                ),
                provider_list=_normalize_provider_list(
                    exploiter_raw.get("provider_list", ()),
                    field_name="teaming.exploiter.provider_list",
                ),
            ),
        )

    required_keys = (
        ("teaming.analyzer.subagent_count", analyzer_raw.get("subagent_count")),
        ("teaming.analyzer.provider_list", analyzer_raw.get("provider_list")),
        ("teaming.validator.subagent_count", validator_raw.get("subagent_count")),
        ("teaming.validator.provider_list", validator_raw.get("provider_list")),
        ("teaming.exploiter.subagent_count", exploiter_raw.get("subagent_count")),
        ("teaming.exploiter.provider_list", exploiter_raw.get("provider_list")),
    )
    for key, value in required_keys:
        if value is None or (isinstance(value, (list, tuple, str)) and not value):
            raise ConfigError(f"Missing required teaming setting: {key}")

    analyzer = AnalyzerTeamConfig(
        subagent_count=_normalize_positive_int(
            analyzer_raw.get("subagent_count"),
            field_name="teaming.analyzer.subagent_count",
        ),
        provider_list=_normalize_provider_list(
            analyzer_raw.get("provider_list"),
            field_name="teaming.analyzer.provider_list",
        ),
    )
    validator = ValidatorTeamConfig(
        subagent_count=_normalize_positive_int(
            validator_raw.get("subagent_count"),
            field_name="teaming.validator.subagent_count",
        ),
        provider_list=_normalize_provider_list(
            validator_raw.get("provider_list"),
            field_name="teaming.validator.provider_list",
        ),
        debate_rounds=_normalize_positive_int(
            validator_raw.get("debate_rounds", 5),
            field_name="teaming.validator.debate_rounds",
        ),
    )
    exploiter = ExploiterTeamConfig(
        subagent_count=_normalize_positive_int(
            exploiter_raw.get("subagent_count"),
            field_name="teaming.exploiter.subagent_count",
        ),
        provider_list=_normalize_provider_list(
            exploiter_raw.get("provider_list"),
            field_name="teaming.exploiter.provider_list",
        ),
    )

    available = ", ".join(sorted(llm_settings.providers)) or "none"
    for team_name, providers in (
        ("analyzer", analyzer.provider_list),
        ("validator", validator.provider_list),
        ("exploiter", exploiter.provider_list),
    ):
        if not providers:
            raise ConfigError(f"Missing required teaming setting: teaming.{team_name}.provider_list")
        for provider_name in providers:
            if provider_name not in llm_settings.providers:
                raise ConfigError(
                    f"Unknown provider `{provider_name}` in teaming.{team_name}.provider_list. "
                    f"Available configured providers: {available}"
                )

    return TeamingConfig(enabled=True, analyzer=analyzer, validator=validator, exploiter=exploiter)


def _resolve_config_paths(
    repo_root: Path,
    config_path: Path | None,
    env: Mapping[str, str],
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    home_dir = _resolve_home_dir(env)
    if home_dir is not None:
        user_config_path = home_dir / ".xauditor" / "xauditor.yml"
        if user_config_path.exists():
            candidates.append(user_config_path)

    project_path = _resolve_project_config_path(repo_root, config_path)
    if project_path is not None and project_path.exists() and project_path not in candidates:
        candidates.append(project_path)

    return tuple(candidates)


def _resolve_project_config_path(repo_root: Path, config_path: Path | None) -> Path | None:
    if config_path is not None:
        return config_path if config_path.is_absolute() else repo_root / config_path
    for candidate in DEFAULT_CONFIG_FILENAMES:
        path = repo_root / candidate
        if path.exists():
            return path
    return None


def _resolve_home_dir(env: Mapping[str, str]) -> Path | None:
    home = env.get("HOME")
    if home:
        return Path(home).expanduser()
    return None


_DEPRECATED_ENV_KEYS = {
    "XAUDITOR_AUDIT_PATH_CONCURRENCY": (
        "XAUDITOR_AUDIT_PATH_CONCURRENCY is no longer recognized in "
        "xauditor 0.10+. The audit will proceed using audit.worker_count "
        "only. Drop this env var from your shell rc / CI config to "
        "silence this warning."
    ),
}


_CODER_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_CODER_WORKSPACE_WARNED: set[str] = set()


def _emit_coder_workspace_warn(key: str, message: str) -> None:
    """Emit a one-shot WARN per *key* across the process lifetime.

    Used for the ``coder.repo_mount_path`` deprecation shim — running
    the same audit binary repeatedly should not spam the operator's
    terminal with the same WARN line.
    """

    if key in _CODER_WORKSPACE_WARNED:
        return
    _CODER_WORKSPACE_WARNED.add(key)
    import sys as _sys

    _sys.stderr.write(f"WARN: {message}\n")


def _resolve_coder_workspace(
    *, workspace_root: str, project_name: str, repo_mount_path: str
) -> tuple[str, str]:
    """Compute (effective_workspace_root, effective_project_name).

    Implements the deprecation shim: when only ``repo_mount_path`` is
    set, fall back to ``parent(repo_mount_path)`` /
    ``basename(repo_mount_path)``; when both ``workspace_root`` and
    ``repo_mount_path`` are set, prefer ``workspace_root`` and emit a
    WARN naming the ignored field. ``project_name`` is independent and
    optional — when empty under ``workspace_root`` mode, audit-startup
    derives it from ``basename(realpath(audit.repo_root))``.
    """

    if workspace_root:
        if repo_mount_path:
            _emit_coder_workspace_warn(
                "repo_mount_path_ignored",
                "coder.repo_mount_path is ignored because coder.workspace_root "
                "is set. Drop coder.repo_mount_path from xauditor.yml to "
                "silence this warning.",
            )
        return workspace_root, project_name

    if repo_mount_path:
        _emit_coder_workspace_warn(
            "repo_mount_path_deprecated",
            "coder.repo_mount_path is deprecated. Set coder.workspace_root="
            f"{Path(repo_mount_path).parent} and (optionally) "
            f"coder.project_name={Path(repo_mount_path).name} in "
            "xauditor.yml. Removed in the version after next.",
        )
        derived_root = str(Path(repo_mount_path).parent)
        derived_project = Path(repo_mount_path).name
        return derived_root, project_name or derived_project

    return "", project_name


def _emit_deprecated_env_warnings(env: Mapping[str, str]) -> None:
    """Emit one WARN line per deprecated env var present in *env*.

    See ``consolidate-on-worker-count`` design D3: deprecated env vars
    are WARN-then-ignored (not hard-failed) so stale shell rcs don't
    block startup. Goes to stderr because no logger is wired up at
    config-parse time.
    """

    import sys as _sys

    for env_key, message in _DEPRECATED_ENV_KEYS.items():
        if env_key in env and (env[env_key] or "").strip():
            _sys.stderr.write(f"WARN: {message}\n")


def _env_to_mapping(env: Mapping[str, str]) -> dict[str, Any]:
    _emit_deprecated_env_warnings(env)
    result: dict[str, Any] = {}
    for env_key, dot_path in ENV_KEY_MAP.items():
        if env_key not in env:
            continue
        value: Any = env[env_key]
        if isinstance(value, str) and not value.strip():
            continue
        if dot_path == "repository.excludes":
            value = tuple(filter(None, (part.strip() for part in value.split(","))))
        elif dot_path == "llm.thinking_enabled":
            value = _normalize_bool(value, field_name="llm.thinking_enabled")
        elif dot_path == "graph.build.enable_llm_enrichment":
            value = _normalize_bool(value, field_name="graph.build.enable_llm_enrichment")
        elif dot_path == "teaming.enabled":
            value = _normalize_bool(value, field_name="teaming.enabled")
        elif dot_path == "coder.enabled":
            value = _normalize_bool(value, field_name="coder.enabled")
        elif dot_path == "coder.enable_auth":
            value = _normalize_bool(value, field_name="coder.enable_auth")
        elif dot_path == "coder.cli_command":
            value = _parse_coder_cli_command_env(value)
        elif dot_path in (
            "coder.concurrency",
            "coder.request_timeout_seconds",
            "coder.preflight_timeout_seconds",
            "audit.shutdown_timeout_seconds",
            "audit.coder.shutdown_timeout_seconds",
        ):
            value = _normalize_positive_int(value, field_name=dot_path)
        elif dot_path == "coder.poll_interval_seconds":
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"Invalid coder.poll_interval_seconds value `{value}`; expected a float."
                ) from exc
        elif dot_path.endswith(".subagent_count") or dot_path.endswith(".debate_rounds"):
            value = _normalize_positive_int(value, field_name=dot_path)
        elif dot_path.endswith(".provider_list"):
            value = tuple(filter(None, (part.strip() for part in value.split(","))))
        _assign_dot_path(result, dot_path, value)
    for dot_path, value in _env_sampling_overrides(env).items():
        _assign_dot_path(result, dot_path, value)
    return result


_SAMPLING_ENV_SUFFIXES: dict[str, str] = {
    "TEMPERATURE": "temperature",
    "TOP_P": "top_p",
    "TOP_K": "top_k",
    "REPETITION_PENALTY": "repetition_penalty",
}


def _env_sampling_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Scan env for XAUDITOR_LLM_PROVIDERS_<P>_<FIELD> and XAUDITOR_AGENTS_<A>_LLM_<FIELD>."""
    overrides: dict[str, Any] = {}
    provider_prefix = "XAUDITOR_LLM_PROVIDERS_"
    agent_prefix = "XAUDITOR_AGENTS_"
    for env_key, raw_value in env.items():
        if raw_value is None:
            continue
        if isinstance(raw_value, str) and not raw_value.strip():
            continue
        matched: str | None = None
        field_slug: str | None = None
        agent_field: str | None = None
        dot_path: str | None = None

        if env_key.startswith(provider_prefix):
            remainder = env_key[len(provider_prefix):]
            for suffix, field_slug_candidate in _SAMPLING_ENV_SUFFIXES.items():
                marker = f"_{suffix}"
                if remainder.endswith(marker):
                    provider_segment = remainder[: -len(marker)]
                    if not provider_segment:
                        continue
                    matched = provider_segment.lower()
                    field_slug = field_slug_candidate
                    dot_path = f"llm.providers.{matched}.{field_slug}"
                    break
        elif env_key.startswith(agent_prefix):
            remainder = env_key[len(agent_prefix):]
            for suffix, field_slug_candidate in _SAMPLING_ENV_SUFFIXES.items():
                marker = f"_LLM_{suffix}"
                if remainder.endswith(marker):
                    agent_segment = remainder[: -len(marker)]
                    if not agent_segment:
                        continue
                    agent_field = agent_segment.lower()
                    field_slug = field_slug_candidate
                    dot_path = f"agents.{agent_field}.llm.{field_slug}"
                    break

        if dot_path is None or field_slug is None:
            continue
        overrides[dot_path] = _normalize_sampling_value(
            raw_value,
            dot_path=dot_path,
            field_name=field_slug,
        )
    return overrides


def _assign_dot_path(target: dict[str, Any], dot_path: str, value: Any) -> None:
    cursor = target
    parts = dot_path.split(".")
    for key in parts[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[parts[-1]] = value


def _merge_mapping(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_mapping(target[key], value)
        else:
            target[key] = value


def _build_llm_settings(data: Mapping[str, Any]) -> LLMSettings:
    llm_data = data.get("llm") or {}
    agents_data = data.get("agents") or {}
    if not isinstance(llm_data, Mapping):
        raise ConfigError("Invalid llm configuration; expected a mapping.")
    if not isinstance(agents_data, Mapping):
        raise ConfigError("Invalid agents configuration; expected a mapping.")

    default_provider = str(llm_data.get("default_provider", "")).strip()
    providers_raw = llm_data.get("providers") or {}
    if providers_raw and not isinstance(providers_raw, Mapping):
        raise ConfigError("Invalid llm.providers configuration; expected a mapping of provider names.")
    provider_mapping = {str(name): value for name, value in providers_raw.items()} if isinstance(providers_raw, Mapping) else {}

    legacy_fields = {}
    for field_name in (
        "base_url",
        "api_key",
        "model_name",
        "thinking_enabled",
        "request_timeout_seconds",
    ):
        if field_name in llm_data:
            legacy_fields[field_name] = llm_data[field_name]
    if legacy_fields:
        if not default_provider:
            default_provider = "default"
        merged_provider = dict(provider_mapping.get(default_provider) or {})
        merged_provider.update(legacy_fields)
        provider_mapping[default_provider] = merged_provider

    providers: dict[str, LLMConfig] = {}
    for provider_name, provider_data in sorted(provider_mapping.items()):
        if not isinstance(provider_data, Mapping):
            raise ConfigError(f"Invalid llm.providers.{provider_name} configuration; expected a mapping.")
        sampling_kwargs = {
            field_name: _normalize_sampling_value(
                provider_data.get(field_name),
                dot_path=f"llm.providers.{provider_name}.{field_name}",
                field_name=field_name,
            )
            for field_name in _SAMPLING_FIELDS
        }
        kind_raw = provider_data.get("kind", PROVIDER_KIND_OPENAI)
        kind = str(kind_raw).strip().lower() or PROVIDER_KIND_OPENAI
        if kind not in PROVIDER_KINDS:
            raise ConfigError(
                f"llm.providers.{provider_name}.kind={kind_raw!r} is not "
                f"one of {list(PROVIDER_KINDS)!r}"
            )
        thinking_effort_raw = provider_data.get("thinking_effort")
        if thinking_effort_raw is None or (
            isinstance(thinking_effort_raw, str) and not thinking_effort_raw.strip()
        ):
            thinking_effort: str | None = None
        else:
            thinking_effort_normalised = str(thinking_effort_raw).strip().lower()
            if thinking_effort_normalised not in LLM_THINKING_EFFORTS:
                raise ConfigError(
                    f"llm.providers.{provider_name}.thinking_effort="
                    f"{thinking_effort_raw!r} is not one of "
                    f"{list(LLM_THINKING_EFFORTS)!r}"
                )
            thinking_effort = thinking_effort_normalised
        request_timeout_seconds = _normalize_positive_float_optional(
            provider_data.get("request_timeout_seconds"),
            dot_path=f"llm.providers.{provider_name}.request_timeout_seconds",
        )
        providers[provider_name] = LLMConfig(
            base_url=str(provider_data.get("base_url", "")).strip(),
            api_key=str(provider_data.get("api_key", "")).strip(),
            model_name=str(provider_data.get("model_name", "")).strip(),
            thinking_enabled=_normalize_bool(
                provider_data.get("thinking_enabled", False),
                field_name=f"llm.providers.{provider_name}.thinking_enabled",
            ),
            thinking_effort=thinking_effort,
            request_timeout_seconds=request_timeout_seconds,
            kind=kind,
            **sampling_kwargs,
        )

    agent_overrides: dict[str, AgentLLMOverride] = {}
    for agent_name in ("graph_builder", "auditor", "exploitation", "validator"):
        agent_config = agents_data.get(agent_name) or {}
        if not isinstance(agent_config, Mapping):
            raise ConfigError(f"Invalid agents.{agent_name} configuration; expected a mapping.")
        llm_config = agent_config.get("llm") or {}
        if not isinstance(llm_config, Mapping):
            raise ConfigError(f"Invalid agents.{agent_name}.llm configuration; expected a mapping.")
        provider_name = str(llm_config.get("provider", "")).strip()
        sampling_kwargs = {
            field_name: _normalize_sampling_value(
                llm_config.get(field_name),
                dot_path=f"agents.{agent_name}.llm.{field_name}",
                field_name=field_name,
            )
            for field_name in _SAMPLING_FIELDS
        }
        agent_request_timeout = _normalize_positive_float_optional(
            llm_config.get("request_timeout_seconds"),
            dot_path=f"agents.{agent_name}.llm.request_timeout_seconds",
        )
        has_sampling = any(value is not None for value in sampling_kwargs.values())
        if provider_name or has_sampling or agent_request_timeout is not None:
            agent_overrides[agent_name] = AgentLLMOverride(
                provider=provider_name,
                request_timeout_seconds=agent_request_timeout,
                **sampling_kwargs,
            )

    return LLMSettings(
        default_provider=default_provider,
        providers=providers,
        agent_overrides=agent_overrides,
        graph_workflow_version=str(llm_data.get("graph_workflow_version", "graph-v2")).strip(),
        enrichment_workflow_version=str(llm_data.get("enrichment_workflow_version", "enrich-v2")).strip(),
        audit_workflow_version=str(llm_data.get("audit_workflow_version", "audit-v1")).strip(),
    )


def _normalize_sampling_value(
    value: Any,
    *,
    dot_path: str,
    field_name: str,
) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(
            f"Invalid {dot_path} value `{value}`; expected a numeric value."
        )
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        value = stripped
    if field_name == "top_k":
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ConfigError(
                    f"Invalid {dot_path} value `{value}`; top_k must be an integer."
                )
            return int(value)
        try:
            if "." in str(value):
                number = float(value)
                if not number.is_integer():
                    raise ConfigError(
                        f"Invalid {dot_path} value `{value}`; top_k must be an integer."
                    )
                return int(number)
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Invalid {dot_path} value `{value}`; expected an integer."
            ) from exc
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Invalid {dot_path} value `{value}`; expected a numeric value."
        ) from exc


_SAMPLING_FIELDS: tuple[str, ...] = ("temperature", "top_p", "top_k", "repetition_penalty")


def _normalize_positive_float_optional(value: Any, *, dot_path: str) -> float | None:
    """Coerce a yaml value to ``float | None`` and reject non-positive numbers.

    Returns ``None`` when the field is omitted or explicitly null/empty
    string. Raises :class:`ConfigError` on ``0``, negative numbers,
    ``nan``, ``inf``, booleans, or anything that does not parse as a
    finite positive float. Used by the LLM ``request_timeout_seconds``
    field — see ``add-llm-request-timeout-config``.
    """

    import math

    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(
            f"Invalid {dot_path} value `{value}`; expected a positive numeric value."
        )
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        value = stripped
    try:
        number = float(value) if not isinstance(value, (int, float)) else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Invalid {dot_path} value `{value}`; expected a positive numeric value."
        ) from exc
    if math.isnan(number) or math.isinf(number) or number <= 0:
        raise ConfigError(
            f"Invalid {dot_path} value `{value}`; expected a positive finite number."
        )
    return number


def _validate_sampling_ranges(
    carrier: LLMConfig | AgentLLMOverride,
    *,
    dot_prefix: str,
) -> None:
    temperature = carrier.temperature
    top_p = carrier.top_p
    top_k = carrier.top_k
    repetition_penalty = carrier.repetition_penalty

    if temperature is not None and temperature < 0:
        raise ConfigError(
            f"Invalid {dot_prefix}.temperature value `{temperature}`; must be >= 0."
        )
    if top_p is not None and not (0 < top_p <= 1):
        raise ConfigError(
            f"Invalid {dot_prefix}.top_p value `{top_p}`; must be in the range (0, 1]."
        )
    if top_k is not None:
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ConfigError(
                f"Invalid {dot_prefix}.top_k value `{top_k}`; top_k must be an integer."
            )
        if top_k < 1:
            raise ConfigError(
                f"Invalid {dot_prefix}.top_k value `{top_k}`; must be >= 1."
            )
    if repetition_penalty is not None and repetition_penalty <= 0:
        raise ConfigError(
            f"Invalid {dot_prefix}.repetition_penalty value `{repetition_penalty}`; must be > 0."
        )


def _normalize_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError(f"Invalid {field_name} value `{value}`. Supported values: true or false.")


def _parse_coder_cli_command_env(raw: Any) -> tuple[str, ...]:
    """Parse the `XAUDITOR_CODER_CLI_COMMAND` env var into a list-form tuple.

    Accepts a bare string (single executable) or a JSON-encoded array.
    Validation of the resulting tokens is deferred to ``_build_coder_config``
    so the env-var path and the YAML path share one validator.
    """

    if raw is None:
        return ()
    text = str(raw).strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Invalid coder.cli_command env value `{text}`; expected a JSON array."
            ) from exc
        if not isinstance(decoded, list):
            raise ConfigError(
                "Invalid coder.cli_command env value; JSON form must be an array of strings."
            )
        return tuple(str(item) for item in decoded)
    return (text,)


def _validate_coder_cli_command(value: Any) -> tuple[str, ...]:
    """Validate `coder.cli_command` and return its canonical tuple form.

    Accepts either a non-empty string (the executable name or absolute path)
    or a non-empty list of strings whose tokens match
    ``[A-Za-z0-9_\\-./=]+``.
    """

    if isinstance(value, str):
        token = value.strip()
        if not token:
            raise ConfigError(
                "Invalid coder.cli_command value; expected a non-empty string or list."
            )
        return _check_cli_tokens((token,))
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            raise ConfigError(
                "Invalid coder.cli_command value; list form must contain at least one element."
            )
        tokens: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(
                    f"Invalid coder.cli_command[{index}]; expected a non-empty string."
                )
            tokens.append(item)
        return _check_cli_tokens(tuple(tokens))
    raise ConfigError(
        "Invalid coder.cli_command value; expected a non-empty string or a list of strings."
    )


def _check_cli_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    for index, token in enumerate(tokens):
        if not _CODER_CLI_TOKEN_PATTERN.match(token):
            raise ConfigError(
                f"Invalid coder.cli_command[{index}] value `{token}`; "
                "only the characters [A-Za-z0-9_-./=] are allowed."
            )
    return tokens


def _build_coder_config(
    data: Mapping[str, Any],
    *,
    runtime_root: Path | None = None,
) -> CoderConfig:
    coder_raw = data.get("coder") or {}
    if not isinstance(coder_raw, Mapping):
        raise ConfigError("Invalid coder configuration; expected a mapping.")
    defaults = CoderConfig()

    enabled = _normalize_bool(coder_raw.get("enabled", defaults.enabled), field_name="coder.enabled")
    cli_command_raw = coder_raw.get("cli_command", defaults.cli_command)
    cli_command = _validate_coder_cli_command(cli_command_raw)

    concurrency = _normalize_positive_int(
        coder_raw.get("concurrency", defaults.concurrency),
        field_name="coder.concurrency",
        minimum=1,
    )
    request_timeout_seconds = _normalize_positive_int(
        coder_raw.get("request_timeout_seconds", defaults.request_timeout_seconds),
        field_name="coder.request_timeout_seconds",
        minimum=1,
    )

    thinking_effort_raw = coder_raw.get("thinking_effort", defaults.thinking_effort)
    if thinking_effort_raw is None or (isinstance(thinking_effort_raw, str) and not thinking_effort_raw.strip()):
        thinking_effort: str | None = None
    else:
        normalized = str(thinking_effort_raw).strip().lower()
        if normalized not in CODER_THINKING_EFFORTS:
            raise ConfigError(
                f"Invalid coder.thinking_effort value `{thinking_effort_raw}`; "
                f"supported values: {', '.join(CODER_THINKING_EFFORTS)}."
            )
        thinking_effort = normalized

    def _optional_string(raw_value: Any, field_name: str) -> str | None:
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        if not text:
            return None
        return text

    model_url = _optional_string(coder_raw.get("model_url", defaults.model_url), "coder.model_url")
    model_name = _optional_string(coder_raw.get("model_name", defaults.model_name), "coder.model_name")
    working_directory = _optional_string(
        coder_raw.get("working_directory", defaults.working_directory),
        "coder.working_directory",
    )

    model_api_key_raw = coder_raw.get("model_api_key", defaults.model_api_key)
    if model_api_key_raw is None:
        model_api_key = ""
    else:
        model_api_key = str(model_api_key_raw).strip()

    transport_raw = str(coder_raw.get("transport", defaults.transport)).strip().lower()
    if transport_raw not in ("subprocess", "http"):
        raise ConfigError(
            f"Invalid coder.transport value `{transport_raw}`; "
            "supported values: subprocess, http."
        )
    transport = transport_raw

    endpoint_raw = coder_raw.get("endpoint", defaults.endpoint)
    endpoint = "" if endpoint_raw is None else str(endpoint_raw).strip()
    # Smart default: when transport=http and endpoint is unset, fall back
    # to the conventional local-sidecar Unix socket under runtime.root_dir.
    # Operators who want a remote endpoint set it explicitly. The startup
    # INFO log surfaces the resolved value so SIEM / log-scrapers see what
    # xauditor is actually connecting to.
    if transport == "http" and not endpoint and runtime_root is not None:
        endpoint = "unix://" + str(Path(runtime_root) / "coder.sock")
    if transport == "http" and not endpoint:
        # Only fires when the caller didn't supply runtime_root AND the
        # operator didn't write an endpoint; defensive — load_config
        # always passes runtime_root.
        raise ConfigError(
            "Missing required setting: coder.endpoint (required when "
            "coder.transport: http and no runtime_root is available "
            "to derive the default local socket path)."
        )
    if endpoint:
        if not (
            endpoint.startswith("http://")
            or endpoint.startswith("https://")
            or endpoint.startswith("unix://")
        ):
            raise ConfigError(
                f"Invalid coder.endpoint value `{endpoint}`; expected scheme "
                "http://, https://, or unix://."
            )
        if endpoint.startswith("unix://"):
            socket_path = endpoint[len("unix://"):]
            if not socket_path.startswith("/"):
                raise ConfigError(
                    f"Invalid coder.endpoint value `{endpoint}`; the unix:// "
                    "path must be absolute (start with '/')."
                )

    enable_auth = _normalize_bool(
        coder_raw.get("enable_auth", defaults.enable_auth),
        field_name="coder.enable_auth",
    )

    endpoint_token_raw = coder_raw.get("endpoint_token", defaults.endpoint_token)
    if endpoint_token_raw is None:
        endpoint_token = ""
    else:
        endpoint_token = str(endpoint_token_raw).strip()

    if transport == "http" and enable_auth and not endpoint_token:
        raise ConfigError(
            "Missing required setting: coder.endpoint_token (required when "
            "coder.enable_auth: true). Either supply the token or set "
            "coder.enable_auth: false."
        )

    poll_interval_raw = coder_raw.get(
        "poll_interval_seconds", defaults.poll_interval_seconds
    )
    try:
        poll_interval_seconds = float(poll_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Invalid coder.poll_interval_seconds value `{poll_interval_raw}`; "
            "expected a float."
        ) from exc
    if poll_interval_seconds < 0.1:
        raise ConfigError(
            f"Invalid coder.poll_interval_seconds value `{poll_interval_seconds}`; "
            "must be >= 0.1."
        )

    preflight_timeout_seconds = _normalize_positive_int(
        coder_raw.get("preflight_timeout_seconds", defaults.preflight_timeout_seconds),
        field_name="coder.preflight_timeout_seconds",
        minimum=1,
    )

    container_image_raw = coder_raw.get("container_image", defaults.container_image)
    container_image = (
        str(container_image_raw).strip() if container_image_raw is not None else ""
    )
    if not container_image:
        container_image = defaults.container_image
    container_name_raw = coder_raw.get("container_name", defaults.container_name)
    container_name = (
        str(container_name_raw).strip() if container_name_raw is not None else ""
    )
    if not container_name:
        container_name = defaults.container_name
    runtime_socket_path_raw = coder_raw.get(
        "runtime_socket_path", defaults.runtime_socket_path
    )
    runtime_socket_path = (
        str(runtime_socket_path_raw).strip()
        if runtime_socket_path_raw is not None
        else ""
    )
    # When unset AND endpoint is unix://..., derive from the URL path so
    # the lifecycle commands have a single source of truth.
    if not runtime_socket_path and endpoint.startswith("unix://"):
        runtime_socket_path = endpoint[len("unix://"):]
    repo_mount_path_raw = coder_raw.get("repo_mount_path", defaults.repo_mount_path)
    repo_mount_path = (
        str(repo_mount_path_raw).strip() if repo_mount_path_raw is not None else ""
    )

    workspace_root_raw = coder_raw.get("workspace_root", defaults.workspace_root)
    workspace_root = (
        str(workspace_root_raw).strip() if workspace_root_raw is not None else ""
    )
    project_name_raw = coder_raw.get("project_name", defaults.project_name)
    project_name = (
        str(project_name_raw).strip() if project_name_raw is not None else ""
    )
    if project_name and not _CODER_PROJECT_NAME_RE.fullmatch(project_name):
        raise ConfigError(
            f"Invalid coder.project_name value `{project_name}`; must match "
            r"^[a-zA-Z0-9._-]+$ (no slashes, traversal segments, or shell "
            "metacharacters)."
        )

    effective_workspace_root, effective_project_name = _resolve_coder_workspace(
        workspace_root=workspace_root,
        project_name=project_name,
        repo_mount_path=repo_mount_path,
    )

    # Nudge the operator who set ``workspace_root`` (or ``project_name``)
    # under subprocess transport. The fields are silently ignored there
    # — subprocess runs ``claude`` on the host with ``cwd=repo_root``.
    # This catches the easy mis-configuration where the operator
    # follows the multi-project doc but forgets ``transport: http``.
    if transport != "http" and (workspace_root or project_name):
        _emit_coder_workspace_warn(
            "workspace_root_under_subprocess",
            "coder.workspace_root / coder.project_name are ignored "
            "under coder.transport: subprocess (the default). They "
            "only apply to coder.transport: http (the long-lived "
            "container that bind-mounts /workspace). Set "
            "coder.transport: http and coder.enabled: true to "
            "activate the multi-project workspace, or remove "
            "workspace_root / project_name to silence this warning.",
        )
    if transport == "http" and not enabled and (workspace_root or project_name):
        _emit_coder_workspace_warn(
            "workspace_root_with_coder_disabled",
            "coder.workspace_root / coder.project_name are set but "
            "coder.enabled is false. The coder stage will not run. "
            "Set coder.enabled: true to activate it, or remove "
            "workspace_root / project_name to silence this warning.",
        )

    return CoderConfig(
        enabled=enabled,
        transport=transport,
        cli_command=cli_command,
        concurrency=concurrency,
        thinking_effort=thinking_effort,
        model_url=model_url,
        model_name=model_name,
        model_api_key=model_api_key,
        request_timeout_seconds=request_timeout_seconds,
        working_directory=working_directory,
        endpoint=endpoint,
        enable_auth=enable_auth,
        endpoint_token=endpoint_token,
        poll_interval_seconds=poll_interval_seconds,
        preflight_timeout_seconds=preflight_timeout_seconds,
        container_image=container_image,
        container_name=container_name,
        runtime_socket_path=runtime_socket_path,
        repo_mount_path=repo_mount_path,
        workspace_root=workspace_root,
        project_name=project_name,
        effective_workspace_root=effective_workspace_root,
        effective_project_name=effective_project_name,
    )


__all__ = [
    "AgentLLMOverride",
    "AnalyzerTeamConfig",
    "AuditConfig",
    "ConfigError",
    "ExploiterTeamConfig",
    "GraphBuildConfig",
    "GraphConfig",
    "LLMConfig",
    "LLMSettings",
    "LoggingConfig",
    "Neo4jConfig",
    "LLM_THINKING_EFFORTS",
    "PortalConfig",
    "PortalServiceConfig",
    "PROVIDER_KIND_ANTHROPIC",
    "PROVIDER_KIND_OPENAI",
    "PROVIDER_KINDS",
    "RemoteConnectionConfig",
    "ReportDBConfig",
    "RepositoryConfig",
    "RuntimeConfig",
    "TeamingConfig",
    "ValidatorTeamConfig",
    "XAuditorConfig",
    "load_config",
]
