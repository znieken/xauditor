from __future__ import annotations

import json
import pickle
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from xauditor.graph.canonical import DiscoveredCall, DiscoveredFile, DiscoveredGraph
from xauditor.models import AuditRun
from xauditor.runtime import RuntimeLayout
from xauditor.runtime_logging import RuntimeLogger


STATE_SCHEMA_VERSION = 3
INVENTORY_STAGE = "inventory"
REFERENCE_STAGE = "reference_tracer"
SYNTHESIZED_STAGE = "graph_synthesizer"
CANONICAL_STAGE = "canonical_finalize"
GRAPH_ENRICHMENT_STAGE = "graph_enrichment"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GraphBuildManifest:
    """Persisted progress envelope for one graph build.

    The pre-0.9.0 manifest carried two flags ``final_build_ready`` and
    ``neo4j_synced`` corresponding to the (now removed) split between
    ``canonical_finalize`` (in-memory assembly) and ``neo4j_sync``
    (separate Bolt write). 0.9.0 collapses these into one streaming
    stage: ``canonical_finalize`` writes records directly into Neo4j
    in chunks. Manifest readers see ``neo4j_synced`` as an alias of
    ``final_build_ready`` so legacy code that checks one or the
    other keeps working.
    """

    fingerprint: str
    started_at: str
    updated_at: str
    completed_stages: tuple[str, ...] = ()
    pending_stages: tuple[str, ...] = ()
    final_build_ready: bool = False
    enrichment_enabled: bool = False
    graph_enrichment_complete: bool = False

    @property
    def neo4j_synced(self) -> bool:
        # Pre-0.9.0 alias kept for callers (and the persisted JSON
        # shape, see ``_manifest_to_dict``) that still read this field.
        return self.final_build_ready


ALL_BUILD_STAGES = (
    INVENTORY_STAGE,
    REFERENCE_STAGE,
    SYNTHESIZED_STAGE,
    CANONICAL_STAGE,
    GRAPH_ENRICHMENT_STAGE,
)


def _pending_stages(
    *,
    completed_stages: tuple[str, ...],
    final_build_ready: bool,
    enrichment_enabled: bool,
    graph_enrichment_complete: bool,
) -> tuple[str, ...]:
    completed = set(completed_stages)
    if final_build_ready:
        completed.add(CANONICAL_STAGE)
    if enrichment_enabled and graph_enrichment_complete:
        completed.add(GRAPH_ENRICHMENT_STAGE)
    return tuple(
        stage
        for stage in ALL_BUILD_STAGES
        if stage not in completed and (stage != GRAPH_ENRICHMENT_STAGE or enrichment_enabled)
    )


def _manifest_to_dict(manifest: GraphBuildManifest) -> dict[str, Any]:
    """Serialise a manifest, preserving the ``neo4j_synced`` field for
    on-disk back-compat with 0.8.x manifest readers.
    """

    data = {
        "fingerprint": manifest.fingerprint,
        "started_at": manifest.started_at,
        "updated_at": manifest.updated_at,
        "completed_stages": list(manifest.completed_stages),
        "pending_stages": list(manifest.pending_stages),
        "final_build_ready": manifest.final_build_ready,
        "neo4j_synced": manifest.final_build_ready,
        "enrichment_enabled": manifest.enrichment_enabled,
        "graph_enrichment_complete": manifest.graph_enrichment_complete,
    }
    return data


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield rows lazily from a JSONL file. Returns an empty iterator
    if the file is missing.

    Used by ``canonical_finalize`` to stream per-stage records into
    bounded Neo4j write chunks without materialising a full stage's
    records in heap.
    """

    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _write_records_as_jsonl(path: Path, records: tuple[Any, ...]) -> None:
    """Write each record (a dataclass) as one JSON line."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            payload = asdict(record) if hasattr(record, "__dataclass_fields__") else record
            fh.write(json.dumps(payload, default=_jsonable_default))
            fh.write("\n")


def _jsonable_default(value: Any) -> Any:
    """Default encoder for ``json.dumps`` that handles enums + Path."""

    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class InMemoryStateStore:
    """In-memory test double for ``FileStateStore``.

    0.9.0 streaming canonical_finalize: the store no longer keeps a
    final ``GraphBuildResult``; it only tracks per-stage cache state
    (inventory / reference / synthesized records, build manifest, and
    audit-run records). Final-build records live in Neo4j (or the
    ``InMemoryNeo4jAdapter`` test double).
    """

    def __init__(self) -> None:
        self.latest_build_fingerprint: str | None = None
        self.audit_runs: dict[str, AuditRun] = {}
        self.manifests: dict[str, GraphBuildManifest] = {}
        self.inventory_stages: dict[str, tuple[tuple[DiscoveredFile, ...], dict[str, object]]] = {}
        self.reference_stages: dict[str, tuple[tuple[DiscoveredCall, ...], dict[str, object]]] = {}
        self.synthesized_stages: dict[str, DiscoveredGraph] = {}

    def get_manifest(self, fingerprint: str) -> GraphBuildManifest | None:
        return self.manifests.get(fingerprint)

    def has_started_build(self, fingerprint: str) -> bool:
        return fingerprint in self.manifests

    def is_stage_complete(self, fingerprint: str, stage: str) -> bool:
        manifest = self.get_manifest(fingerprint)
        if manifest is None:
            return False
        if stage == CANONICAL_STAGE:
            return manifest.final_build_ready
        if stage == GRAPH_ENRICHMENT_STAGE:
            return not manifest.enrichment_enabled or manifest.graph_enrichment_complete
        return stage in manifest.completed_stages

    def is_final_build_ready(self, fingerprint: str) -> bool:
        manifest = self.get_manifest(fingerprint)
        return manifest is not None and manifest.final_build_ready

    def is_build_reusable(self, fingerprint: str) -> bool:
        manifest = self.get_manifest(fingerprint)
        return (
            manifest is not None
            and manifest.final_build_ready
            and (not manifest.enrichment_enabled or manifest.graph_enrichment_complete)
        )

    def clear_build(self, fingerprint: str) -> None:
        self.manifests.pop(fingerprint, None)
        self.inventory_stages.pop(fingerprint, None)
        self.reference_stages.pop(fingerprint, None)
        self.synthesized_stages.pop(fingerprint, None)
        if self.latest_build_fingerprint == fingerprint:
            self.latest_build_fingerprint = None

    def save_manifest_placeholder(
        self, fingerprint: str, *, enrichment_enabled: bool
    ) -> None:
        """Seed a manifest at build start so observers see progress before any stage lands.

        No-op when a manifest already exists (resume safety): an existing
        manifest reflects real progress and MUST NOT be overwritten by
        the placeholder.
        """

        if fingerprint in self.manifests:
            return
        started_at = _now()
        self.manifests[fingerprint] = GraphBuildManifest(
            fingerprint=fingerprint,
            started_at=started_at,
            updated_at=started_at,
            completed_stages=(),
            pending_stages=_pending_stages(
                completed_stages=(),
                final_build_ready=False,
                enrichment_enabled=enrichment_enabled,
                graph_enrichment_complete=False,
            ),
            final_build_ready=False,
            enrichment_enabled=enrichment_enabled,
            graph_enrichment_complete=False,
        )

    def save_inventory_stage(
        self,
        fingerprint: str,
        *,
        files: tuple[DiscoveredFile, ...],
        agent_state: dict[str, object],
    ) -> None:
        self.inventory_stages[fingerprint] = (files, agent_state)
        self._mark_stage_complete(fingerprint, INVENTORY_STAGE)

    def load_inventory_stage(self, fingerprint: str) -> tuple[tuple[DiscoveredFile, ...], dict[str, object]]:
        return self.inventory_stages[fingerprint]

    def save_reference_stage(
        self,
        fingerprint: str,
        *,
        calls: tuple[DiscoveredCall, ...],
        agent_state: dict[str, object],
    ) -> None:
        self.reference_stages[fingerprint] = (calls, agent_state)
        self._mark_stage_complete(fingerprint, REFERENCE_STAGE)

    def load_reference_stage(self, fingerprint: str) -> tuple[tuple[DiscoveredCall, ...], dict[str, object]]:
        return self.reference_stages[fingerprint]

    def save_synthesized_stage(self, fingerprint: str, discovered: DiscoveredGraph) -> None:
        self.synthesized_stages[fingerprint] = discovered
        self._mark_stage_complete(fingerprint, SYNTHESIZED_STAGE)

    def load_synthesized_stage(self, fingerprint: str) -> DiscoveredGraph:
        return self.synthesized_stages[fingerprint]

    def mark_canonical_complete(
        self,
        fingerprint: str,
        *,
        enrichment_enabled: bool | None = None,
    ) -> None:
        """Mark canonical_finalize done. 0.9.0 streaming variant: this
        replaces the pre-0.9.0 ``save_build`` (which also pickled the
        final ``GraphBuildResult``) and ``mark_neo4j_synced`` (which
        was a separate post-canonical stage)."""

        self.latest_build_fingerprint = fingerprint
        self._mark_stage_complete(
            fingerprint,
            CANONICAL_STAGE,
            final_build_ready=True,
            enrichment_enabled=enrichment_enabled,
        )

    def mark_graph_enrichment_complete(self, fingerprint: str) -> None:
        self._mark_stage_complete(fingerprint, GRAPH_ENRICHMENT_STAGE, graph_enrichment_complete=True)

    def get_latest_build_fingerprint(self) -> str | None:
        return self.latest_build_fingerprint

    def save_audit_run(self, audit_run: AuditRun) -> None:
        self.audit_runs[audit_run.build_fingerprint] = audit_run

    def _mark_stage_complete(
        self,
        fingerprint: str,
        stage: str,
        *,
        final_build_ready: bool | None = None,
        enrichment_enabled: bool | None = None,
        graph_enrichment_complete: bool | None = None,
    ) -> None:
        existing = self.get_manifest(fingerprint)
        completed = set(existing.completed_stages if existing is not None else ())
        if stage not in {CANONICAL_STAGE, GRAPH_ENRICHMENT_STAGE}:
            completed.add(stage)
        resolved_enrichment_enabled = (
            enrichment_enabled if enrichment_enabled is not None else bool(existing and existing.enrichment_enabled)
        )
        resolved_graph_enrichment_complete = (
            graph_enrichment_complete
            if graph_enrichment_complete is not None
            else bool(existing and existing.graph_enrichment_complete)
        )
        resolved_final_build_ready = (
            final_build_ready if final_build_ready is not None else bool(existing and existing.final_build_ready)
        )
        self.manifests[fingerprint] = GraphBuildManifest(
            fingerprint=fingerprint,
            started_at=existing.started_at if existing is not None else _now(),
            updated_at=_now(),
            completed_stages=tuple(sorted(completed)),
            pending_stages=_pending_stages(
                completed_stages=tuple(sorted(completed)),
                final_build_ready=resolved_final_build_ready,
                enrichment_enabled=resolved_enrichment_enabled,
                graph_enrichment_complete=resolved_graph_enrichment_complete,
            ),
            final_build_ready=resolved_final_build_ready,
            enrichment_enabled=resolved_enrichment_enabled,
            graph_enrichment_complete=resolved_graph_enrichment_complete,
        )


class FileStateStore:
    def __init__(self, runtime: RuntimeLayout, *, logger: RuntimeLogger | None = None) -> None:
        self.runtime = runtime.ensure()
        self.graph_builds_dir = self.runtime.cache_dir / "graph-builds"
        self.graph_builds_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.runtime.manifests_dir / "audits"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.latest_file = self.runtime.manifests_dir / "latest-build.txt"
        self.logger = logger

    def get_manifest(self, fingerprint: str) -> GraphBuildManifest | None:
        path = self._manifest_path(fingerprint)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        # ``neo4j_synced`` is an alias for ``final_build_ready`` on disk
        # so legacy 0.8.x manifests upgrade losslessly: if either field
        # is set, the canonical stage is complete.
        final_build_ready = bool(
            data.get("final_build_ready", False) or data.get("neo4j_synced", False)
        )
        return GraphBuildManifest(
            fingerprint=data["fingerprint"],
            started_at=data["started_at"],
            updated_at=data["updated_at"],
            completed_stages=tuple(data.get("completed_stages", ())),
            pending_stages=tuple(
                data.get(
                    "pending_stages",
                    _pending_stages(
                        completed_stages=tuple(data.get("completed_stages", ())),
                        final_build_ready=final_build_ready,
                        enrichment_enabled=bool(data.get("enrichment_enabled", False)),
                        graph_enrichment_complete=bool(data.get("graph_enrichment_complete", False)),
                    ),
                )
            ),
            final_build_ready=final_build_ready,
            enrichment_enabled=bool(data.get("enrichment_enabled", False)),
            graph_enrichment_complete=bool(data.get("graph_enrichment_complete", False)),
        )

    def has_started_build(self, fingerprint: str) -> bool:
        return self._build_dir(fingerprint).exists()

    def is_stage_complete(self, fingerprint: str, stage: str) -> bool:
        manifest = self.get_manifest(fingerprint)
        if manifest is None:
            return False
        if stage == CANONICAL_STAGE:
            return manifest.final_build_ready
        if stage == GRAPH_ENRICHMENT_STAGE:
            return not manifest.enrichment_enabled or manifest.graph_enrichment_complete
        if stage not in manifest.completed_stages:
            return False
        return self._stage_schema_ok(fingerprint, stage)

    def is_final_build_ready(self, fingerprint: str) -> bool:
        manifest = self.get_manifest(fingerprint)
        return manifest is not None and manifest.final_build_ready

    def is_build_reusable(self, fingerprint: str) -> bool:
        manifest = self.get_manifest(fingerprint)
        return (
            manifest is not None
            and manifest.final_build_ready
            and (not manifest.enrichment_enabled or manifest.graph_enrichment_complete)
        )

    def clear_build(self, fingerprint: str) -> None:
        shutil.rmtree(self._build_dir(fingerprint), ignore_errors=True)
        if self.latest_file.exists() and self.latest_file.read_text(encoding="utf-8").strip() == fingerprint:
            self.latest_file.unlink()

    def save_manifest_placeholder(
        self, fingerprint: str, *, enrichment_enabled: bool
    ) -> None:
        """Write a manifest seed at build start so `.xauditor/.../manifest.json` is visible early.

        No-op when `manifest.json` already exists (resume safety).
        """

        manifest_path = self._manifest_path(fingerprint)
        if manifest_path.exists():
            return
        started_at = _now()
        manifest = GraphBuildManifest(
            fingerprint=fingerprint,
            started_at=started_at,
            updated_at=started_at,
            completed_stages=(),
            pending_stages=_pending_stages(
                completed_stages=(),
                final_build_ready=False,
                enrichment_enabled=enrichment_enabled,
                graph_enrichment_complete=False,
            ),
            final_build_ready=False,
            enrichment_enabled=enrichment_enabled,
            graph_enrichment_complete=False,
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(_manifest_to_dict(manifest), indent=2), encoding="utf-8"
        )

    def save_inventory_stage(
        self,
        fingerprint: str,
        *,
        files: tuple[DiscoveredFile, ...],
        agent_state: dict[str, object],
    ) -> None:
        stage_dir = self._stage_dir(fingerprint, "inventory")
        self._write_pickle(stage_dir / "files.pkl", files)
        self._write_pickle(stage_dir / "state.pkl", agent_state)
        self._write_stage_schema(fingerprint, INVENTORY_STAGE)
        self._mark_stage_complete(fingerprint, INVENTORY_STAGE)

    def load_inventory_stage(self, fingerprint: str) -> tuple[tuple[DiscoveredFile, ...], dict[str, object]]:
        stage_dir = self._stage_dir(fingerprint, "inventory")
        try:
            return (
                pickle.loads((stage_dir / "files.pkl").read_bytes()),
                pickle.loads((stage_dir / "state.pkl").read_bytes()),
            )
        except Exception as exc:
            self._discard_stage(fingerprint, INVENTORY_STAGE, exc)
            raise

    def save_reference_stage(
        self,
        fingerprint: str,
        *,
        calls: tuple[DiscoveredCall, ...],
        agent_state: dict[str, object],
    ) -> None:
        stage_dir = self._stage_dir(fingerprint, "references")
        self._write_pickle(stage_dir / "calls.pkl", calls)
        self._write_pickle(stage_dir / "state.pkl", agent_state)
        self._write_stage_schema(fingerprint, REFERENCE_STAGE)
        self._mark_stage_complete(fingerprint, REFERENCE_STAGE)

    def load_reference_stage(self, fingerprint: str) -> tuple[tuple[DiscoveredCall, ...], dict[str, object]]:
        stage_dir = self._stage_dir(fingerprint, "references")
        try:
            return (
                pickle.loads((stage_dir / "calls.pkl").read_bytes()),
                pickle.loads((stage_dir / "state.pkl").read_bytes()),
            )
        except Exception as exc:
            self._discard_stage(fingerprint, REFERENCE_STAGE, exc)
            raise

    def save_synthesized_stage(self, fingerprint: str, discovered: DiscoveredGraph) -> None:
        stage_dir = self._stage_dir(fingerprint, "synthesized")
        self._write_pickle(stage_dir / "discovered.pkl", discovered)
        # 0.9.0: also write per-record-kind JSONL files so
        # ``canonical_finalize`` can stream-read them in bounded
        # chunks without materialising the entire DiscoveredGraph
        # in heap. See ``consolidate-on-neo4j-source`` design D2.
        _write_records_as_jsonl(stage_dir / "files.jsonl", discovered.files)
        _write_records_as_jsonl(stage_dir / "classes.jsonl", discovered.classes)
        _write_records_as_jsonl(stage_dir / "class_members.jsonl", discovered.class_members)
        _write_records_as_jsonl(stage_dir / "functions.jsonl", discovered.functions)
        _write_records_as_jsonl(stage_dir / "calls.jsonl", discovered.calls)
        _write_records_as_jsonl(stage_dir / "module_symbols.jsonl", discovered.module_symbols)
        _write_records_as_jsonl(stage_dir / "symbol_uses.jsonl", discovered.symbol_uses)
        _write_records_as_jsonl(stage_dir / "provenance.jsonl", discovered.provenance)
        self._write_stage_schema(fingerprint, SYNTHESIZED_STAGE)
        self._mark_stage_complete(fingerprint, SYNTHESIZED_STAGE)

    def load_synthesized_stage(self, fingerprint: str) -> DiscoveredGraph:
        stage_dir = self._stage_dir(fingerprint, "synthesized")
        try:
            return pickle.loads((stage_dir / "discovered.pkl").read_bytes())
        except Exception as exc:
            self._discard_stage(fingerprint, SYNTHESIZED_STAGE, exc)
            raise

    def synthesized_jsonl_path(self, fingerprint: str, kind: str) -> Path:
        """Path to one of the per-record-kind JSONL files written by
        ``save_synthesized_stage``. Used by ``canonical_finalize`` for
        streamed reads.

        ``kind`` is one of ``files``, ``classes``, ``class_members``,
        ``functions``, ``calls``, ``module_symbols``, ``symbol_uses``,
        ``provenance``.
        """

        return self._stage_dir(fingerprint, "synthesized") / f"{kind}.jsonl"

    def mark_canonical_complete(
        self,
        fingerprint: str,
        *,
        enrichment_enabled: bool | None = None,
    ) -> None:
        """Mark canonical_finalize done. 0.9.0 streaming variant: this
        replaces the pre-0.9.0 ``save_build`` (which pickled
        ``GraphBuildResult`` to ``canonical/build.pkl``) and
        ``mark_neo4j_synced`` (separate post-canonical stage)."""

        self._write_stage_schema(fingerprint, CANONICAL_STAGE)
        self.latest_file.write_text(fingerprint, encoding="utf-8")
        self._mark_stage_complete(
            fingerprint,
            CANONICAL_STAGE,
            final_build_ready=True,
            enrichment_enabled=enrichment_enabled,
        )

    def mark_graph_enrichment_complete(self, fingerprint: str) -> None:
        status_path = self._stage_dir(fingerprint, "graph-enrichment") / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({"complete": True}, indent=2), encoding="utf-8")
        self._mark_stage_complete(fingerprint, GRAPH_ENRICHMENT_STAGE, graph_enrichment_complete=True)

    def get_latest_build_fingerprint(self) -> str | None:
        if not self.latest_file.exists():
            return None
        text = self.latest_file.read_text(encoding="utf-8").strip()
        return text or None

    def save_audit_run(self, audit_run: AuditRun) -> None:
        self._write_pickle(self.audit_dir / f"audit-{audit_run.build_fingerprint}.pkl", audit_run)

    def _mark_stage_complete(
        self,
        fingerprint: str,
        stage: str,
        *,
        final_build_ready: bool | None = None,
        enrichment_enabled: bool | None = None,
        graph_enrichment_complete: bool | None = None,
    ) -> None:
        existing = self.get_manifest(fingerprint)
        completed = set(existing.completed_stages if existing is not None else ())
        if stage not in {CANONICAL_STAGE, GRAPH_ENRICHMENT_STAGE}:
            completed.add(stage)
        resolved_enrichment_enabled = (
            enrichment_enabled if enrichment_enabled is not None else bool(existing and existing.enrichment_enabled)
        )
        resolved_graph_enrichment_complete = (
            graph_enrichment_complete
            if graph_enrichment_complete is not None
            else bool(existing and existing.graph_enrichment_complete)
        )
        resolved_final_build_ready = (
            final_build_ready if final_build_ready is not None else bool(existing and existing.final_build_ready)
        )
        manifest = GraphBuildManifest(
            fingerprint=fingerprint,
            started_at=existing.started_at if existing is not None else _now(),
            updated_at=_now(),
            completed_stages=tuple(sorted(completed)),
            pending_stages=_pending_stages(
                completed_stages=tuple(sorted(completed)),
                final_build_ready=resolved_final_build_ready,
                enrichment_enabled=resolved_enrichment_enabled,
                graph_enrichment_complete=resolved_graph_enrichment_complete,
            ),
            final_build_ready=resolved_final_build_ready,
            enrichment_enabled=resolved_enrichment_enabled,
            graph_enrichment_complete=resolved_graph_enrichment_complete,
        )
        manifest_path = self._manifest_path(fingerprint)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(_manifest_to_dict(manifest), indent=2), encoding="utf-8"
        )

    def _build_dir(self, fingerprint: str) -> Path:
        return self.graph_builds_dir / fingerprint

    def _manifest_path(self, fingerprint: str) -> Path:
        return self._build_dir(fingerprint) / "manifest.json"

    def _stage_dir(self, fingerprint: str, stage_dir: str) -> Path:
        path = self._build_dir(fingerprint) / stage_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _stage_dir_name(self, stage: str) -> str:
        if stage == REFERENCE_STAGE:
            return "references"
        if stage == SYNTHESIZED_STAGE:
            return "synthesized"
        if stage == CANONICAL_STAGE:
            return "canonical"
        return stage

    def _stage_schema_path(self, fingerprint: str, stage: str) -> Path:
        return self._build_dir(fingerprint) / self._stage_dir_name(stage) / "schema.json"

    def _write_stage_schema(self, fingerprint: str, stage: str) -> None:
        path = self._stage_schema_path(fingerprint, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": STATE_SCHEMA_VERSION}, indent=2),
            encoding="utf-8",
        )

    def _stage_schema_ok(self, fingerprint: str, stage: str) -> bool:
        path = self._stage_schema_path(fingerprint, stage)
        if not path.exists():
            self._discard_stage(fingerprint, stage, reason="missing schema version")
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            version = int(data.get("schema_version", 0))
        except (OSError, ValueError, TypeError) as exc:
            self._discard_stage(fingerprint, stage, reason=f"unreadable schema version: {exc}")
            return False
        if version != STATE_SCHEMA_VERSION:
            self._discard_stage(
                fingerprint,
                stage,
                reason=f"schema version {version} does not match expected {STATE_SCHEMA_VERSION}",
            )
            return False
        return True

    def _discard_stage(
        self,
        fingerprint: str,
        stage: str,
        exc: Exception | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        stage_dir_name = self._stage_dir_name(stage)
        stage_path = self._build_dir(fingerprint) / stage_dir_name
        shutil.rmtree(stage_path, ignore_errors=True)
        if self.logger is not None:
            description = reason or (f"{type(exc).__name__}: {exc}" if exc is not None else "unknown cause")
            self.logger.info(
                f"Discarded graph build cache stage `{stage}` for fingerprint={fingerprint}: {description}"
            )
        manifest = self.get_manifest(fingerprint)
        if manifest is None:
            return
        completed = tuple(s for s in manifest.completed_stages if s != stage)
        kwargs = dict(
            final_build_ready=manifest.final_build_ready and stage != CANONICAL_STAGE,
            enrichment_enabled=manifest.enrichment_enabled,
            graph_enrichment_complete=(
                manifest.graph_enrichment_complete and stage != GRAPH_ENRICHMENT_STAGE
            ),
        )
        new_manifest = GraphBuildManifest(
            fingerprint=fingerprint,
            started_at=manifest.started_at,
            updated_at=_now(),
            completed_stages=completed,
            pending_stages=_pending_stages(
                completed_stages=completed,
                final_build_ready=kwargs["final_build_ready"],
                enrichment_enabled=kwargs["enrichment_enabled"],
                graph_enrichment_complete=kwargs["graph_enrichment_complete"],
            ),
            **kwargs,
        )
        manifest_path = self._manifest_path(fingerprint)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(_manifest_to_dict(new_manifest), indent=2), encoding="utf-8"
        )

    def _write_pickle(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(payload))
