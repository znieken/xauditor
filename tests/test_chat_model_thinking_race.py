"""Per-thread isolation of ``last_thinking`` /
``last_reasoning_tokens`` on the LangChain chat-model wrappers.

Pre-fix: these were plain instance attributes that
``invoke_text`` wrote and surrounding code read back. Under
concurrent audits sharing one chat-model instance, one path's
log line could surface another path's thinking text. Post-fix:
they're properties backed by a per-instance ``threading.local``
so each thread sees its own most-recent values.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.model_factory import (  # noqa: E402
    AnthropicLangChainChatModel,
    LangChainChatModel,
    SamplingParams,
)


def _make_openai_model() -> LangChainChatModel:
    return LangChainChatModel(
        provider_name="test-openai",
        model_name="test-model",
        base_url="mock://test",
        api_key="k",
        thinking_enabled=False,
        sampling=SamplingParams(),
        is_mock=True,
    )


def _make_anthropic_model() -> AnthropicLangChainChatModel:
    return AnthropicLangChainChatModel(
        provider_name="test-anthropic",
        model_name="test-model",
        base_url="mock://test",
        api_key="k",
        thinking_enabled=False,
        sampling=SamplingParams(),
        is_mock=True,
    )


class PerThreadMetadataTests(unittest.TestCase):
    def test_default_values_are_empty(self) -> None:
        model = _make_openai_model()
        self.assertEqual(model.last_thinking, "")
        self.assertEqual(model.last_reasoning_tokens, 0)

    def test_each_thread_sees_its_own_writes(self) -> None:
        # Two threads writing to the same chat-model's per-thread
        # meta SHALL each read back their own values. Pre-fix this
        # would race on shared instance attributes.
        model = _make_openai_model()
        observed: dict[str, tuple[str, int]] = {}
        barrier = threading.Barrier(2)

        def worker(name: str, thinking: str, tokens: int) -> None:
            barrier.wait()
            model._per_thread_meta.thinking = thinking
            model._per_thread_meta.reasoning_tokens = tokens
            # Read back via the public properties — must reflect
            # THIS thread's writes, not the other thread's.
            observed[name] = (model.last_thinking, model.last_reasoning_tokens)

        t_a = threading.Thread(
            target=worker, args=("A", "thinking-from-A", 100)
        )
        t_b = threading.Thread(
            target=worker, args=("B", "thinking-from-B", 200)
        )
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        self.assertEqual(observed["A"], ("thinking-from-A", 100))
        self.assertEqual(observed["B"], ("thinking-from-B", 200))

    def test_main_thread_isolated_from_workers(self) -> None:
        # Main thread reads default values even after worker
        # threads write to their per-thread slots.
        model = _make_openai_model()

        def worker() -> None:
            model._per_thread_meta.thinking = "worker-only"
            model._per_thread_meta.reasoning_tokens = 999

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        # Main thread's per_thread_meta is untouched → defaults.
        self.assertEqual(model.last_thinking, "")
        self.assertEqual(model.last_reasoning_tokens, 0)

    def test_anthropic_model_has_same_per_thread_isolation(self) -> None:
        model = _make_anthropic_model()
        self.assertEqual(model.last_thinking, "")
        self.assertEqual(model.last_reasoning_tokens, 0)

        observed: dict[str, tuple[str, int]] = {}

        def worker(name: str, thinking: str, tokens: int) -> None:
            model._per_thread_meta.thinking = thinking
            model._per_thread_meta.reasoning_tokens = tokens
            observed[name] = (model.last_thinking, model.last_reasoning_tokens)

        t_a = threading.Thread(target=worker, args=("A", "ant-A", 11))
        t_b = threading.Thread(target=worker, args=("B", "ant-B", 22))
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        self.assertEqual(observed["A"], ("ant-A", 11))
        self.assertEqual(observed["B"], ("ant-B", 22))


if __name__ == "__main__":
    unittest.main()
