"""Unit tests for the relaxed ``graph.build.paths_max_count`` semantics
(``audit-stream-path-loading`` Section 9 / D9).

Asserts:

- ``max_count <= 0`` enumerates every reachable path and emits no
  ``_TruncationMarker(reason="max_count_reached", ...)``.
- A positive ``max_count`` retains the historical truncation
  behavior (one marker, then enumeration stops).
- ``paths_max_count`` still participates in the build fingerprint
  so cap changes invalidate cached builds.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xauditor.config import load_config
from xauditor.graph.canonical import CanonicalGraphTransformer, _TruncationMarker
from xauditor.graph.fingerprint import compute_build_fingerprint
from xauditor.graph.scope import resolve_repository_scope
from xauditor.models import EdgeRecord, FunctionRecord, GraphProvenance, PathRecord


def _fn(name: str, *, file_path: str = "app.py") -> FunctionRecord:
    return FunctionRecord(
        function_id=f"{file_path}:{name}:1",
        name=name,
        qualified_name=name,
        file_path=file_path,
        module_name="",
        start_line=1,
        end_line=2,
        source="",
    )


def _edge(src: FunctionRecord, dst: FunctionRecord) -> EdgeRecord:
    return EdgeRecord(
        source_function=src.function_id,
        target_function=dst.function_id,
        edge_type="CALL",
        provenance=GraphProvenance(file_path=src.file_path, line_number=1, evidence=""),
    )


def _make_chain(n: int) -> tuple[tuple[FunctionRecord, ...], tuple[EdgeRecord, ...]]:
    """Return n functions linked f0 → f1 → … → f_{n-1}.

    Each entry yields exactly one path of length n (f0 → … → f_{n-1}).
    """

    fns = tuple(_fn(f"fn{i}") for i in range(n))
    edges = tuple(_edge(fns[i], fns[i + 1]) for i in range(n - 1))
    return fns, edges


def _make_fanout(branches: int, depth: int = 2) -> tuple[
    tuple[FunctionRecord, ...], tuple[EdgeRecord, ...]
]:
    """Return ``branches`` independent f0_b → f1_b chains so the
    transformer enumerates ``branches`` distinct paths."""

    fns: list[FunctionRecord] = []
    edges: list[EdgeRecord] = []
    for b in range(branches):
        chain = [_fn(f"fn{b}_{d}") for d in range(depth)]
        fns.extend(chain)
        for i in range(depth - 1):
            edges.append(_edge(chain[i], chain[i + 1]))
    return tuple(fns), tuple(edges)


def _build_transformer(tmp: str) -> CanonicalGraphTransformer:
    repo_root = Path(tmp)
    (repo_root / "app.py").write_text("# stub\n", encoding="utf-8")
    config = load_config(
        repo_root=repo_root,
        env={
            "XAUDITOR_LLM_BASE_URL": "mock://offline",
            "XAUDITOR_LLM_API_KEY": "secret",
            "XAUDITOR_LLM_MODEL_NAME": "mock-model",
        },
        require_llm=True,
    )
    # ``_enumerate_paths`` doesn't touch the repository — pass a
    # placeholder so the dataclass-style ``__init__`` is satisfied.
    return CanonicalGraphTransformer(config=config, repository=object())


class WalkPathsUncappedTests(unittest.TestCase):
    def _enumerate(self, transformer, fns, edges, *, max_count: int, max_depth: int = 40):
        return list(
            transformer._enumerate_paths(  # noqa: SLF001 — test introspection
                fns,
                edges,
                max_depth=max_depth,
                max_count=max_count,
                on_truncate=lambda reason, cap: None,
            )
        )

    def test_zero_max_count_emits_every_path_no_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transformer = _build_transformer(tmp)
            fns, edges = _make_fanout(branches=12, depth=2)
            results = self._enumerate(transformer, fns, edges, max_count=0)
        truncations = [r for r in results if isinstance(r, _TruncationMarker)]
        paths = [r for r in results if isinstance(r, PathRecord)]
        self.assertEqual(truncations, [])
        # 12 entries × 1 path each (f0 → f1).
        self.assertEqual(len(paths), 12)

    def test_negative_max_count_treated_same_as_zero(self) -> None:
        # Defensive: ``_walk_paths_lazy`` predicate is ``max_count > 0``
        # so any non-positive value disables capping. Public config
        # rejects negatives, but the internal predicate is robust.
        with tempfile.TemporaryDirectory() as tmp:
            transformer = _build_transformer(tmp)
            fns, edges = _make_fanout(branches=5, depth=2)
            results = self._enumerate(transformer, fns, edges, max_count=-1)
        truncations = [r for r in results if isinstance(r, _TruncationMarker)]
        self.assertEqual(truncations, [])

    def test_positive_max_count_still_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transformer = _build_transformer(tmp)
            fns, edges = _make_fanout(branches=20, depth=2)
            results = self._enumerate(transformer, fns, edges, max_count=5)
        truncations = [r for r in results if isinstance(r, _TruncationMarker)]
        paths = [r for r in results if isinstance(r, PathRecord)]
        self.assertEqual(len(truncations), 1)
        self.assertEqual(truncations[0].reason, "max_count_reached")
        self.assertEqual(truncations[0].cap, 5)
        # Enumeration stopped after emitting 5 paths.
        self.assertEqual(len(paths), 5)


class FingerprintIncludesPathsMaxCountTests(unittest.TestCase):
    def test_zero_vs_positive_yield_distinct_fingerprints(self) -> None:
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text("print('a')\n", encoding="utf-8")
            scope = resolve_repository_scope(repo_root, ())
            base_config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            # Synthesize two configs differing only in
            # ``paths_max_count`` so the assertion isolates that
            # input's effect on the digest.
            cfg_zero = replace(
                base_config,
                graph=replace(
                    base_config.graph,
                    build=replace(base_config.graph.build, paths_max_count=0),
                ),
            )
            cfg_high = replace(
                base_config,
                graph=replace(
                    base_config.graph,
                    build=replace(base_config.graph.build, paths_max_count=50_000),
                ),
            )
            fp_zero = compute_build_fingerprint(
                scope=scope, config=cfg_zero, workflow_version="test"
            )
            fp_high = compute_build_fingerprint(
                scope=scope, config=cfg_high, workflow_version="test"
            )
        self.assertNotEqual(fp_zero, fp_high)


if __name__ == "__main__":
    unittest.main()
