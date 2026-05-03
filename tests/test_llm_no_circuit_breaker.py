"""Regression: the LLM call path SHALL NOT layer a client-side
circuit breaker on top of provider calls (per
``remove-llm-circuit-breaker``).

Pre-fix: ``langchain_support.py`` and ``llm.py`` wrapped each
LLM call with ``breaker.call(...)`` against a shared
``_MODEL_CIRCUIT_BREAKERS[(provider, model)]`` instance. Under
parallel audit (``path_concurrency >= 2``) three concurrent
failures tripped the breaker and condemned the next ~25 paths
to ``CircuitOpenError`` for the cooldown window — surfacing as
"audit produces fewer findings than serial baseline".

Post-fix: provider-side rate limit + ``retry()`` are the only
resilience layers; per-path failure isolation
(``add-failed-path-state-on-llm-error``) keeps a single
exhausted retry from cascading.

These tests pin the shape: no breaker import, no shared
mutable state, N sequential failures produce N independent
``LLMError``s rather than `failure_threshold + cooldown_paths`
``CircuitOpenError``-rewrapped ``LLMError``s.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor import langchain_support, llm  # noqa: E402


class NoCircuitBreakerImportTests(unittest.TestCase):
    def test_langchain_support_no_longer_imports_circuit_breaker(self) -> None:
        # The module-level globals from the pre-fix design are gone.
        self.assertFalse(
            hasattr(langchain_support, "_MODEL_CIRCUIT_BREAKERS"),
            "langchain_support._MODEL_CIRCUIT_BREAKERS should have "
            "been removed by remove-llm-circuit-breaker",
        )
        self.assertFalse(
            hasattr(langchain_support, "CircuitBreaker"),
            "CircuitBreaker import should have been removed from "
            "langchain_support; it stays defined in resilience.py "
            "for the Neo4j integration only",
        )

    def test_llm_module_no_longer_imports_circuit_breaker(self) -> None:
        self.assertFalse(
            hasattr(llm, "CircuitBreaker"),
            "CircuitBreaker import should have been removed from llm.py",
        )
        # CircuitOpenError reference removed from this module's
        # except-arm (it can no longer fire from this code path).
        self.assertFalse(
            hasattr(llm, "CircuitOpenError"),
            "CircuitOpenError import should have been removed from llm.py",
        )

    def test_llm_client_no_longer_holds_a_breaker_instance(self) -> None:
        # The dataclass field is gone. Constructing an LLMClient
        # without a real config fails earlier than that, so just
        # check the dataclass field shape.
        from dataclasses import fields

        from xauditor.llm import LLMClient

        field_names = {f.name for f in fields(LLMClient)}
        self.assertNotIn("circuit_breaker", field_names)


if __name__ == "__main__":
    unittest.main()
