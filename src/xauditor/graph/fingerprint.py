from __future__ import annotations

import hashlib
import time

from xauditor.config import XAuditorConfig
from xauditor.models import RepositoryScope
from xauditor.runtime_logging import RuntimeLogger


FINGERPRINT_CHUNK_BYTES = 1 << 20  # 1 MiB
FINGERPRINT_HEARTBEAT_FILES = 1000
FINGERPRINT_HEARTBEAT_SECONDS = 10.0


def compute_build_fingerprint(
    scope: RepositoryScope,
    config: XAuditorConfig,
    *,
    workflow_version: str,
    logger: RuntimeLogger | None = None,
) -> str:
    provider_name = config.llm.selected_provider("graph_builder")
    provider = config.llm.provider_for("graph_builder")
    digest = hashlib.sha256()
    digest.update(workflow_version.encode("utf-8"))
    digest.update(provider_name.encode("utf-8"))
    digest.update(provider.base_url.encode("utf-8"))
    digest.update(provider.model_name.encode("utf-8"))
    digest.update(str(provider.thinking_enabled).lower().encode("utf-8"))
    digest.update(
        str(config.graph.build.enable_llm_enrichment).lower().encode("utf-8")
    )
    digest.update(str(config.graph.build.max_file_bytes).encode("utf-8"))
    digest.update(str(config.graph.build.paths_max_depth).encode("utf-8"))
    digest.update(str(config.graph.build.paths_max_count).encode("utf-8"))
    digest.update("|".join(sorted(scope.excludes)).encode("utf-8"))

    total = len(scope.included_files)
    heartbeat = _FingerprintHeartbeat(total=total, logger=logger)
    for idx, path in enumerate(scope.included_files):
        rel = path.relative_to(scope.repo_root).as_posix()
        digest.update(rel.encode("utf-8"))
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        digest.update(str(size).encode("utf-8"))
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(FINGERPRINT_CHUNK_BYTES), b""):
                    digest.update(chunk)
        except OSError:
            pass
        heartbeat.tick(idx + 1)
    heartbeat.final()
    return digest.hexdigest()


class _FingerprintHeartbeat:
    """Throttled per-file progress heartbeat for the fingerprint loop.

    Emits an `info` log line every `FINGERPRINT_HEARTBEAT_FILES` files
    OR every `FINGERPRINT_HEARTBEAT_SECONDS` seconds, whichever happens
    first. No-ops silently when `logger` is `None`, so callers that
    don't opt in keep today's silent behavior.
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
        hit_count_threshold = (
            processed - self._last_emitted_idx >= FINGERPRINT_HEARTBEAT_FILES
        )
        hit_time_threshold = now - self._last_emitted_at >= FINGERPRINT_HEARTBEAT_SECONDS
        if not (hit_count_threshold or hit_time_threshold):
            return
        self.logger.info(
            f"graph build fingerprint: {processed}/{self.total} files"
        )
        self._last_emitted_idx = processed
        self._last_emitted_at = now

    def final(self) -> None:
        if self.logger is None or self.total == 0:
            return
        if self._last_emitted_idx == self.total:
            return
        self.logger.info(f"graph build fingerprint: {self.total}/{self.total} files")
