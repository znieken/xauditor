from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from xauditor.config import XAuditorConfig
from xauditor.graph.canonical import (
    CanonicalGraphTransformer,
    DiscoveredCall,
    DiscoveredClass,
    DiscoveredClassMember,
    DiscoveredFile,
    DiscoveredFunction,
    DiscoveredGraph,
    DiscoveredModuleSymbol,
    DiscoveredSymbolUse,
)
from xauditor.graph.fingerprint import compute_build_fingerprint
from xauditor.graph.parsers import _ParsedFile, get_parser, set_parser_logger
from xauditor.graph.scope import module_name_for_path
from xauditor.graph.tools import RepositoryTools
from xauditor.integrations.lsp import LanguageServerRegistry
from xauditor.langchain_support import invoke_agent
from xauditor.llm import LLMClient
from xauditor.models import GraphProvenance, RepositoryScope
from xauditor.prompts import (
    GRAPH_INVENTORY_PROMPT,
    GRAPH_INVENTORY_PROMPT_VERSION,
    GRAPH_REFERENCE_TRACER_PROMPT,
    GRAPH_REFERENCE_TRACER_PROMPT_VERSION,
    GRAPH_SYNTHESIZER_PROMPT,
    GRAPH_SYNTHESIZER_PROMPT_VERSION,
)
from xauditor.runtime_logging import RuntimeLogger


_INVENTORY_READ_SAMPLE_CAP = 64
_DEBUG_BODIES_ENV = "XAUDITOR_DEBUG_GRAPH_BODIES"
_INVENTORY_HEARTBEAT_FILES = 1000
_INVENTORY_HEARTBEAT_SECONDS = 10.0


@contextmanager
def _stage_timer(logger: RuntimeLogger | None, name: str) -> Iterator[None]:
    """Log stage start/done at info level with elapsed seconds."""

    if logger is not None:
        logger.info(f"graph build stage={name}: starting")
    started = time.monotonic()
    try:
        yield
    finally:
        if logger is not None:
            elapsed = time.monotonic() - started
            logger.info(f"graph build stage={name}: done in {elapsed:.2f}s")


class _InventoryHeartbeat:
    """Throttled progress heartbeat for the inventory per-file loop.

    Mirrors the cadence of `_FingerprintHeartbeat` (info line every N
    files or every M seconds, whichever first). No-op when logger is None.
    """

    def __init__(self, *, total: int, logger: RuntimeLogger | None) -> None:
        self.total = total
        self.logger = logger
        self._last_emitted_idx = 0
        self._last_emitted_at = time.monotonic()

    def tick(self, processed: int) -> None:
        if self.logger is None:
            return
        now = time.monotonic()
        hit_count = processed - self._last_emitted_idx >= _INVENTORY_HEARTBEAT_FILES
        hit_time = now - self._last_emitted_at >= _INVENTORY_HEARTBEAT_SECONDS
        if not (hit_count or hit_time):
            return
        self.logger.info(f"graph build inventory: {processed}/{self.total} files")
        self._last_emitted_idx = processed
        self._last_emitted_at = now


def _debug_bodies_enabled() -> bool:
    return os.environ.get(_DEBUG_BODIES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _slice_source(content: str, start_line: int, end_line: int) -> str:
    if not content:
        return ""
    lines = content.splitlines()
    start = max(start_line - 1, 0)
    end = max(end_line, start)
    return "\n".join(lines[start:end])


def _read_file_content(
    repo_root: Path,
    rel_path: str,
    *,
    max_bytes: int,
    logger: RuntimeLogger | None,
) -> str | None:
    file_path = repo_root / rel_path
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        if logger is not None:
            logger.warning(f"Graph build: unable to stat {rel_path}: {exc}")
        return None
    if size > max_bytes:
        if logger is not None:
            logger.warning(
                f"Graph build: skipping parse for {rel_path} "
                f"({size} bytes > graph.build.max_file_bytes={max_bytes})"
            )
        return None
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if logger is not None:
            logger.warning(f"Graph build: unable to read {rel_path}: {exc}")
        return None


class InventoryAgent:
    def run(
        self,
        *,
        repo_root: Path,
        scope: RepositoryScope,
        tools: RepositoryTools,
        logger: RuntimeLogger | None = None,
    ) -> tuple[tuple[DiscoveredFile, ...], dict[str, object]]:
        def _collect_inventory(
            _payload: dict[str, object],
        ) -> tuple[tuple[DiscoveredFile, ...], dict[str, object]]:
            sample_reads: dict[str, object] = {}
            state: dict[str, object] = {
                "ls": tools.ls("."),
                "glob": tools.glob("**/*"),
                "bash": tools.bash("pwd", description="inspect repository root"),
                "reads": sample_reads,
            }
            files: list[DiscoveredFile] = []
            heartbeat = _InventoryHeartbeat(
                total=len(scope.included_files), logger=logger
            )
            for idx, file_path in enumerate(scope.included_files):
                rel = file_path.relative_to(repo_root).as_posix()
                if idx < _INVENTORY_READ_SAMPLE_CAP:
                    sample_reads[rel] = tools.read(rel, offset=0, limit=200)
                files.append(
                    DiscoveredFile(
                        path=rel,
                        module_name=module_name_for_path(repo_root, file_path),
                        language=_language_for_path(file_path),
                    )
                )
                heartbeat.tick(idx + 1)
            state["reads_sample_cap"] = _INVENTORY_READ_SAMPLE_CAP
            state["included_file_count"] = len(scope.included_files)
            return tuple(files), state

        (files, state), meta = invoke_agent(
            agent_name="inventory",
            system_prompt=GRAPH_INVENTORY_PROMPT,
            payload={"repo_root": str(repo_root), "file_count": len(scope.included_files)},
            executor=_collect_inventory,
            prompt_version=GRAPH_INVENTORY_PROMPT_VERSION,
        )
        state["runtime"] = meta["runtime"]
        return files, state


class ReferenceTracerAgent:
    def run(
        self,
        *,
        repo_root: Path,
        files: tuple[DiscoveredFile, ...],
        tools: RepositoryTools,
        parsed_cache: dict[str, _ParsedFile] | None = None,
        max_file_bytes: int = 10_000_000,
        logger: RuntimeLogger | None = None,
    ) -> tuple[tuple[DiscoveredCall, ...], dict[str, object]]:
        def _trace_references(
            _payload: dict[str, object],
        ) -> tuple[tuple[DiscoveredCall, ...], dict[str, object]]:
            state: dict[str, object] = {
                "rg": tools.rg(r"def\s+\w+|class\s+\w+", path=".", include="*.py"),
                "grep": tools.grep(r"shell=True|eval\(|exec\(", path=".", include="*.py"),
                "codesearch": tools.codesearch("python subprocess.run shell=True", max_tokens=128),
            }
            calls: list[DiscoveredCall] = []
            for file in files:
                parser = get_parser(file.language)
                if parser is None:
                    continue
                content = _read_file_content(
                    repo_root, file.path, max_bytes=max_file_bytes, logger=logger
                )
                if content is None:
                    continue
                parsed = parser.parse(file, content)
                if parsed_cache is not None:
                    parsed_cache[file.path] = parsed
                for parsed_call in parsed.calls:
                    calls.append(
                        DiscoveredCall(
                            caller_function_id=parsed_call.caller_function_id,
                            callee_name=parsed_call.callee_name,
                            callee_qualified_name=parsed_call.callee_qualified_name,
                            caller_class_id=parsed_call.caller_class_id,
                            file_path=file.path,
                            line_number=parsed_call.line_number,
                            evidence=parsed_call.evidence,
                        )
                    )
            return tuple(calls), state

        (calls, state), meta = invoke_agent(
            agent_name="reference-tracer",
            system_prompt=GRAPH_REFERENCE_TRACER_PROMPT,
            payload={"repo_root": str(repo_root), "file_count": len(files)},
            executor=_trace_references,
            prompt_version=GRAPH_REFERENCE_TRACER_PROMPT_VERSION,
        )
        state["runtime"] = meta["runtime"]
        return calls, state


class GraphSynthesizerAgent:
    def __init__(
        self,
        *,
        llm_client: LLMClient,
        lsp_registry: LanguageServerRegistry,
        enable_enrichment: bool = True,
    ) -> None:
        self.llm_client = llm_client
        self.lsp_registry = lsp_registry
        self.enable_enrichment = enable_enrichment

    def run(
        self,
        *,
        repo_root: Path,
        files: tuple[DiscoveredFile, ...],
        calls: tuple[DiscoveredCall, ...],
        lsp_messages: tuple[str, ...],
        lsp_availability: dict[str, object],
        parsed_cache: dict[str, _ParsedFile] | None = None,
        max_file_bytes: int = 10_000_000,
        logger: RuntimeLogger | None = None,
    ) -> DiscoveredGraph:
        def _synthesize_graph(_payload: dict[str, object]) -> DiscoveredGraph:
            classes: list[DiscoveredClass] = []
            class_members: list[DiscoveredClassMember] = []
            functions: list[DiscoveredFunction] = []
            provenance: list[GraphProvenance] = []
            module_symbols: list[DiscoveredModuleSymbol] = []
            symbol_uses: list[DiscoveredSymbolUse] = []
            lsp_state: dict[str, object] = {"hover": {}, "diagnostics": {}}

            for file in files:
                file_path = repo_root / file.path
                language = _language_for_path(file_path)
                availability = lsp_availability.get(language)
                if getattr(availability, "available", False):
                    lsp_state["diagnostics"][file.path] = self.lsp_registry.diagnostics(file_path)
                parser = get_parser(file.language)
                if parser is None:
                    continue

                cached = parsed_cache.get(file.path) if parsed_cache is not None else None
                if cached is None:
                    content = _read_file_content(
                        repo_root, file.path, max_bytes=max_file_bytes, logger=logger
                    )
                    if content is None:
                        continue
                    parsed = parser.parse(file, content)
                else:
                    parsed = cached
                    content = None

                for symbol in parsed.module_symbols:
                    module_symbols.append(
                        DiscoveredModuleSymbol(
                            symbol_id=symbol.symbol_id,
                            name=symbol.name,
                            kind=symbol.kind,
                            module_name=file.module_name,
                            file_path=file.path,
                            start_line=symbol.start_line,
                            end_line=symbol.end_line,
                            type_annotation=symbol.type_annotation,
                            value_repr=symbol.value_repr,
                            is_placeholder=symbol.is_placeholder,
                        )
                    )
                symbol_id_by_name = {
                    symbol.name: symbol.symbol_id for symbol in parsed.module_symbols
                }
                for use in parsed.symbol_uses:
                    symbol_id = symbol_id_by_name.get(use.symbol_name)
                    if symbol_id is None:
                        continue
                    symbol_uses.append(
                        DiscoveredSymbolUse(
                            function_id=use.function_id,
                            symbol_id=symbol_id,
                            line_number=use.line_number,
                            evidence=use.evidence,
                        )
                    )

                enrichment_source: str | None = None
                if self.enable_enrichment and (parsed.classes or parsed.functions):
                    if content is None:
                        enrichment_source = _read_file_content(
                            repo_root, file.path, max_bytes=max_file_bytes, logger=logger
                        )
                    else:
                        enrichment_source = content

                for parsed_class in parsed.classes:
                    class_hover = None
                    class_source = ""
                    if self.enable_enrichment:
                        if getattr(availability, "available", False):
                            class_hover = self.lsp_registry.hover(file_path, parsed_class.name)
                            lsp_state["hover"][parsed_class.name] = class_hover
                        if enrichment_source:
                            class_source = _slice_source(
                                enrichment_source, parsed_class.start_line, parsed_class.end_line
                            )
                    class_summary = (
                        self.llm_client.summarize_class(
                            parsed_class.name,
                            file.path,
                            class_source,
                            method_names=tuple(item.name for item in parsed_class.methods),
                            member_names=tuple(item.name for item in parsed_class.members),
                        )
                        if self.enable_enrichment
                        else {"summary": "", "business_context": ""}
                    )
                    classes.append(
                        DiscoveredClass(
                            class_id=parsed_class.class_id,
                            name=parsed_class.name,
                            file_path=file.path,
                            module_name=file.module_name,
                            start_line=parsed_class.start_line,
                            end_line=parsed_class.end_line,
                            summary=class_summary["summary"],
                            business_context=(
                                (class_hover or class_summary["business_context"])
                                if self.enable_enrichment
                                else ""
                            ),
                        )
                    )
                    provenance.append(
                        GraphProvenance(
                            file_path=file.path,
                            line_number=parsed_class.start_line,
                            evidence=f"Defined class {parsed_class.name}",
                        )
                    )
                    for member in parsed_class.members:
                        class_members.append(
                            DiscoveredClassMember(
                                member_id=member.member_id,
                                name=member.name,
                                class_id=member.class_id,
                                file_path=file.path,
                                module_name=file.module_name,
                                line_number=member.line_number,
                                summary=(
                                    f"{member.name} stores class-scoped configuration for {parsed_class.name}."
                                    if self.enable_enrichment
                                    else ""
                                ),
                            )
                        )
                        provenance.append(
                            GraphProvenance(
                                file_path=file.path,
                                line_number=member.line_number,
                                evidence=f"Defined class member {parsed_class.name}.{member.name}",
                            )
                        )

                for function in parsed.functions:
                    hover = None
                    function_source = ""
                    trust_probe = ""
                    if self.enable_enrichment:
                        if getattr(availability, "available", False):
                            hover = self.lsp_registry.hover(file_path, function.qualified_name)
                            lsp_state["hover"][function.qualified_name] = hover
                        if enrichment_source:
                            function_source = _slice_source(
                                enrichment_source, function.start_line, function.end_line
                            )
                            trust_probe = function_source.lower()
                    functions.append(
                        DiscoveredFunction(
                            function_id=function.function_id,
                            name=function.name,
                            qualified_name=function.qualified_name,
                            file_path=file.path,
                            module_name=file.module_name,
                            start_line=function.start_line,
                            end_line=function.end_line,
                            summary=(
                                self.llm_client.summarize_function(
                                    function.qualified_name, file.path, function_source
                                )
                                if self.enable_enrichment
                                else ""
                            ),
                            business_context=(
                                hover
                                or f"Function {function.qualified_name} participates in repository control flow."
                            )
                            if self.enable_enrichment
                            else "",
                            trust_boundary=(
                                "crosses external input"
                                if "handler" in function.qualified_name.lower()
                                or "input" in trust_probe
                                else "internal flow"
                            )
                            if self.enable_enrichment
                            else "",
                            class_id=function.class_id,
                        )
                    )
                    provenance.append(
                        GraphProvenance(
                            file_path=file.path,
                            line_number=function.start_line,
                            evidence=f"Defined function {function.qualified_name}",
                        )
                    )
            return DiscoveredGraph(
                files=files,
                classes=tuple(classes),
                class_members=tuple(class_members),
                functions=tuple(functions),
                calls=calls,
                provenance=tuple(provenance),
                lsp_messages=lsp_messages,
                agent_state={"graph_synthesizer": lsp_state},
                module_symbols=tuple(module_symbols),
                symbol_uses=tuple(symbol_uses),
            )

        discovered, meta = invoke_agent(
            agent_name="graph-synthesizer",
            system_prompt=GRAPH_SYNTHESIZER_PROMPT,
            payload={"repo_root": str(repo_root), "file_count": len(files), "call_count": len(calls)},
            executor=_synthesize_graph,
            prompt_version=GRAPH_SYNTHESIZER_PROMPT_VERSION,
        )
        discovered.agent_state["graph_synthesizer"]["runtime"] = meta["runtime"]
        return discovered


class LangChainGraphBuilder:
    def __init__(
        self,
        *,
        config: XAuditorConfig,
        llm_client: LLMClient,
        repository: Any = None,
        lsp_registry: LanguageServerRegistry | None = None,
        tools_factory: Callable[[Path], RepositoryTools] | None = None,
        inventory_agent: InventoryAgent | None = None,
        reference_tracer_agent: ReferenceTracerAgent | None = None,
        graph_synthesizer_agent: GraphSynthesizerAgent | None = None,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        # 0.9.0 streaming canonical_finalize: the repository (Neo4j or
        # in-memory test stub) is the persistence target for the
        # chunk writes. Pre-0.9.0 builders persisted via a separate
        # neo4j_sync stage; that stage is gone. The repository is
        # injected so tests can drive the in-memory variant.
        self.repository = repository
        self.lsp_registry = lsp_registry or LanguageServerRegistry()
        self.tools_factory = tools_factory or (lambda repo_root: RepositoryTools(repo_root=repo_root))
        self.inventory_agent = inventory_agent or InventoryAgent()
        self.reference_tracer_agent = reference_tracer_agent or ReferenceTracerAgent()
        self.graph_synthesizer_agent = graph_synthesizer_agent or GraphSynthesizerAgent(
            llm_client=llm_client,
            lsp_registry=self.lsp_registry,
            enable_enrichment=config.graph.build.enable_llm_enrichment,
        )
        self.logger = logger
        set_parser_logger(logger)

    @classmethod
    def from_config(
        cls,
        config: XAuditorConfig,
        *,
        repository: Any = None,
        lsp_registry: LanguageServerRegistry | None = None,
        tools_factory: Callable[[Path], RepositoryTools] | None = None,
        logger: RuntimeLogger | None = None,
    ) -> "LangChainGraphBuilder":
        return cls(
            config=config,
            llm_client=LLMClient.from_config(config.llm, logger=logger, agent="graph_builder"),
            repository=repository,
            lsp_registry=lsp_registry,
            tools_factory=tools_factory,
            logger=logger,
        )

    def build(
        self,
        *,
        repo_root: Path,
        scope: RepositoryScope,
        build_fingerprint: str | None = None,
        state_store: Any | None = None,
    ):
        tools = self.tools_factory(repo_root)
        if state_store is not None and self.logger is not None and hasattr(state_store, "logger"):
            state_store.logger = self.logger
        enrichment_enabled = self.config.graph.build.enable_llm_enrichment
        with _stage_timer(self.logger, "fingerprint"):
            fingerprint = build_fingerprint or compute_build_fingerprint(
                scope,
                self.config,
                workflow_version=self.config.llm.graph_workflow_version,
                logger=self.logger,
            )
        if state_store is not None and hasattr(state_store, "save_manifest_placeholder"):
            state_store.save_manifest_placeholder(
                fingerprint, enrichment_enabled=enrichment_enabled
            )
        if self.logger is not None:
            self.logger.info(
                f"graph build starting: fingerprint={fingerprint[:12]}… "
                f"file_count={len(scope.included_files)} "
                f"enrichment={'on' if enrichment_enabled else 'off'}"
            )
        max_file_bytes = self.config.graph.build.max_file_bytes
        dump_bodies = _debug_bodies_enabled()
        lsp_messages: list[str] = []
        lsp_availability = self.lsp_registry.availability_for_paths(list(scope.included_files))
        for language, availability in lsp_availability.items():
            if self.logger is not None:
                state = "available" if availability.available else "missing"
                self.logger.debug(f"LSP availability for {language}: {state} ({availability.binary})")
            if not availability.available:
                lsp_messages.append(
                    f"Missing {language} language server `{availability.binary}`. Install with: {availability.install_hint}"
                )
        if state_store is not None and state_store.is_stage_complete(fingerprint, "inventory"):
            if self.logger is not None:
                self.logger.debug("Loaded graph build checkpoint: inventory")
            files, inventory_state = state_store.load_inventory_stage(fingerprint)
        else:
            if self.logger is not None:
                self.logger.debug_kv(
                    "Graph build stage inventory: starting", file_count=len(scope.included_files)
                )
            with _stage_timer(self.logger, "inventory"):
                files, inventory_state = _invoke_inventory(
                    self.inventory_agent,
                    repo_root=repo_root,
                    scope=scope,
                    tools=tools,
                    logger=self.logger,
                )
            if state_store is not None:
                state_store.save_inventory_stage(fingerprint, files=files, agent_state=inventory_state)
        if self.logger is not None:
            self.logger.info("Completed graph build stage: inventory")
            self.logger.debug_kv(
                "Inventory stage output",
                file_count=len(files),
                runtime=inventory_state.get("runtime"),
                state_keys=tuple(sorted(key for key in inventory_state.keys() if key != "runtime")),
            )
            self.logger.debug(
                "Inventory tool activity: "
                + ", ".join(sorted(key for key in inventory_state.keys() if key != "runtime"))
            )
            for discovered_file in files:
                self.logger.debug(
                    f"Inventory file: path={discovered_file.path} "
                    f"module={discovered_file.module_name} language={discovered_file.language}"
                )
                if dump_bodies:
                    body = _read_file_content(
                        repo_root,
                        discovered_file.path,
                        max_bytes=max_file_bytes,
                        logger=None,
                    )
                    if body is not None:
                        self.logger.debug(
                            f"Inventory file body [{discovered_file.path}]\n{body}"
                        )

        parsed_cache: dict[str, _ParsedFile] = {}
        if state_store is not None and state_store.is_stage_complete(fingerprint, "reference_tracer"):
            if self.logger is not None:
                self.logger.debug("Loaded graph build checkpoint: reference_tracer")
            calls, reference_state = state_store.load_reference_stage(fingerprint)
        else:
            if self.logger is not None:
                self.logger.debug("Graph build stage reference_tracer: starting")
            with _stage_timer(self.logger, "reference_tracer"):
                calls, reference_state = _invoke_reference_tracer(
                    self.reference_tracer_agent,
                    repo_root=repo_root,
                    files=files,
                    tools=tools,
                    parsed_cache=parsed_cache,
                    max_file_bytes=max_file_bytes,
                    logger=self.logger,
                )
            if state_store is not None:
                state_store.save_reference_stage(fingerprint, calls=calls, agent_state=reference_state)
        if self.logger is not None:
            self.logger.info("Completed graph build stage: reference_tracer")
            self.logger.debug(
                "Reference tracer tool activity: "
                + ", ".join(sorted(key for key in reference_state.keys() if key != "runtime"))
            )
            for call in calls:
                self.logger.debug(
                    f"Reference call: caller={call.caller_function_id} "
                    f"callee={call.callee_name} qualified={call.callee_qualified_name} "
                    f"file={call.file_path}:{call.line_number}"
                )
                if dump_bodies:
                    self.logger.debug(f"Reference evidence: {call.evidence!r}")

        if state_store is not None and state_store.is_stage_complete(fingerprint, "graph_synthesizer"):
            if self.logger is not None:
                self.logger.debug("Loaded graph build checkpoint: graph_synthesizer")
            discovered = state_store.load_synthesized_stage(fingerprint)
        else:
            if self.logger is not None:
                self.logger.debug_kv(
                    "Graph build stage graph_synthesizer: starting",
                    file_count=len(files),
                    call_count=len(calls),
                    lsp_messages=len(lsp_messages),
                )
            with _stage_timer(self.logger, "graph_synthesizer"):
                discovered = _invoke_graph_synthesizer(
                    self.graph_synthesizer_agent,
                    repo_root=repo_root,
                    files=files,
                    calls=calls,
                    lsp_messages=tuple(lsp_messages),
                    lsp_availability=lsp_availability,
                    parsed_cache=parsed_cache,
                    max_file_bytes=max_file_bytes,
                    logger=self.logger,
                )
            discovered = DiscoveredGraph(
                files=discovered.files,
                classes=discovered.classes,
                class_members=discovered.class_members,
                functions=discovered.functions,
                calls=discovered.calls,
                provenance=discovered.provenance,
                lsp_messages=discovered.lsp_messages,
                agent_state={
                    "inventory": inventory_state,
                    "reference_tracer": reference_state,
                    **discovered.agent_state,
                },
                module_symbols=discovered.module_symbols,
                symbol_uses=discovered.symbol_uses,
            )
            if state_store is not None:
                state_store.save_synthesized_stage(fingerprint, discovered)
        parsed_cache.clear()
        if self.logger is not None:
            self.logger.info("Completed graph build stage: graph_synthesizer")
            self.logger.debug_kv(
                "Graph synthesizer stage output",
                function_count=len(discovered.functions),
                class_count=len(discovered.classes),
                call_count=len(discovered.calls),
                runtime=discovered.agent_state.get("graph_synthesizer", {}).get("runtime"),
            )
            self.logger.debug(
                "Graph synthesizer summary: "
                f"{len(discovered.classes)} classes, {len(discovered.class_members)} class members, "
                f"{len(discovered.functions)} functions, {len(discovered.calls)} calls"
            )
            for cls in discovered.classes:
                self.logger.debug(
                    f"Graph class: id={cls.class_id} name={cls.name} file={cls.file_path} "
                    f"module={cls.module_name} lines={cls.start_line}-{cls.end_line}"
                )
                if dump_bodies:
                    body = _read_file_content(
                        repo_root, cls.file_path, max_bytes=max_file_bytes, logger=None
                    )
                    if body is not None:
                        self.logger.debug(
                            f"Graph class body [{cls.class_id}]:\n"
                            + _slice_source(body, cls.start_line, cls.end_line)
                        )
            for member in discovered.class_members:
                self.logger.debug(
                    f"Graph class member: id={member.member_id} name={member.name} "
                    f"class_id={member.class_id} file={member.file_path}:{member.line_number}"
                )
            for func in discovered.functions:
                self.logger.debug(
                    f"Graph function: id={func.function_id} qualified={func.qualified_name} "
                    f"file={func.file_path} module={func.module_name} "
                    f"lines={func.start_line}-{func.end_line} class_id={func.class_id}"
                )
                if dump_bodies:
                    body = _read_file_content(
                        repo_root, func.file_path, max_bytes=max_file_bytes, logger=None
                    )
                    if body is not None:
                        self.logger.debug(
                            f"Graph function body [{func.function_id}]:\n"
                            + _slice_source(body, func.start_line, func.end_line)
                        )

        if state_store is not None and state_store.is_final_build_ready(fingerprint):
            if self.logger is not None:
                self.logger.debug("Loaded graph build checkpoint: canonical_finalize")
            return fingerprint

        if self.repository is None:
            raise ValueError(
                "LangChainGraphBuilder.build: repository is required so "
                "canonical_finalize can stream records into Neo4j. Construct "
                "the builder with `from_config(..., repository=...)`."
            )

        with _stage_timer(self.logger, "canonical_finalize"):
            result = CanonicalGraphTransformer(
                config=self.config,
                repository=self.repository,
                llm_client=self.llm_client,
            ).transform_and_persist(
                repo_root=repo_root,
                scope=scope,
                build_fingerprint=fingerprint,
                discovered=discovered,
            )
        del discovered
        if result.paths_truncated and self.logger is not None:
            cap = result.paths_truncation_cap
            reason = result.paths_truncation_reason
            self.logger.info(
                "Path enumeration truncated: "
                f"reason={reason} cap={cap}. "
                "Raise graph.build.paths_max_depth / graph.build.paths_max_count in configuration to enumerate more paths."
            )
        if state_store is not None:
            state_store.mark_canonical_complete(
                fingerprint,
                enrichment_enabled=self.config.graph.build.enable_llm_enrichment,
            )
        if self.logger is not None:
            self.logger.info("Completed graph build stage: canonical_finalize")
            self.logger.debug(
                f"Canonical graph build {fingerprint} streamed to Neo4j; "
                f"truncated={result.paths_truncated}, "
                f"reason={result.paths_truncation_reason or 'n/a'}, "
                f"cap={result.paths_truncation_cap}"
            )
        return fingerprint


def _invoke_inventory(
    agent: InventoryAgent,
    *,
    repo_root: Path,
    scope: RepositoryScope,
    tools: RepositoryTools,
    logger: RuntimeLogger | None,
) -> tuple[tuple[DiscoveredFile, ...], dict[str, object]]:
    """Call an inventory agent, tolerating stubs that lack the ``logger`` kwarg.

    Test doubles don't need the heartbeat; production stays wired up.
    """

    try:
        return agent.run(repo_root=repo_root, scope=scope, tools=tools, logger=logger)
    except TypeError:
        return agent.run(repo_root=repo_root, scope=scope, tools=tools)


def _invoke_reference_tracer(
    agent: ReferenceTracerAgent,
    *,
    repo_root: Path,
    files: tuple[DiscoveredFile, ...],
    tools: RepositoryTools,
    parsed_cache: dict[str, _ParsedFile],
    max_file_bytes: int,
    logger: RuntimeLogger | None,
) -> tuple[tuple[DiscoveredCall, ...], dict[str, object]]:
    try:
        return agent.run(
            repo_root=repo_root,
            files=files,
            tools=tools,
            parsed_cache=parsed_cache,
            max_file_bytes=max_file_bytes,
            logger=logger,
        )
    except TypeError:
        return agent.run(repo_root=repo_root, files=files, tools=tools)


def _invoke_graph_synthesizer(
    agent: GraphSynthesizerAgent,
    *,
    repo_root: Path,
    files: tuple[DiscoveredFile, ...],
    calls: tuple[DiscoveredCall, ...],
    lsp_messages: tuple[str, ...],
    lsp_availability: Any,
    parsed_cache: dict[str, _ParsedFile],
    max_file_bytes: int,
    logger: RuntimeLogger | None,
) -> DiscoveredGraph:
    try:
        return agent.run(
            repo_root=repo_root,
            files=files,
            calls=calls,
            lsp_messages=lsp_messages,
            lsp_availability=lsp_availability,
            parsed_cache=parsed_cache,
            max_file_bytes=max_file_bytes,
            logger=logger,
        )
    except TypeError:
        return agent.run(
            repo_root=repo_root,
            files=files,
            calls=calls,
            lsp_messages=lsp_messages,
            lsp_availability=lsp_availability,
        )


def _language_for_path(path: Path) -> str:
    if path.suffix == ".py":
        return "python"
    if path.suffix in {".js", ".jsx"}:
        return "javascript"
    if path.suffix in {".ts", ".tsx"}:
        return "typescript"
    if path.suffix == ".go":
        return "go"
    if path.suffix == ".java":
        return "java"
    if path.suffix in {".c", ".h"}:
        return "c"
    if path.suffix in {".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}:
        return "cpp"
    if path.suffix == ".cs":
        return "csharp"
    if path.suffix == ".rb":
        return "ruby"
    if path.suffix == ".php":
        return "php"
    if path.suffix in {".mjs", ".cjs"}:
        return "javascript"
    if path.suffix == ".rs":
        return "rust"
    if path.suffix in {".kt", ".kts"}:
        return "kotlin"
    if path.suffix == ".lua":
        return "lua"
    if path.suffix == ".swift":
        return "swift"
    return path.suffix.lstrip(".") or "text"
