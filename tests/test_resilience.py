from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.errors import CircuitOpenError, RetryExhaustedError
from xauditor.resilience import CircuitBreaker, retry


class RetryTests(unittest.TestCase):
    def test_retry_succeeds_after_transient_failures(self) -> None:
        attempts = {"count": 0}
        sleeps: list[float] = []

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("temporary failure")
            return "ok"

        result = retry(
            flaky,
            operation="llm.invoke_json",
            attempts=3,
            base_delay=0.5,
            max_delay=2.0,
            exceptions=(RuntimeError,),
            sleep=sleeps.append,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_retry_raises_after_exhaustion(self) -> None:
        attempts = {"count": 0}

        def always_fail() -> None:
            attempts["count"] += 1
            raise RuntimeError("persistent failure")

        with self.assertRaises(RetryExhaustedError) as ctx:
            retry(
                always_fail,
                operation="neo4j.exec_cypher",
                attempts=3,
                base_delay=0.25,
                max_delay=1.0,
                exceptions=(RuntimeError,),
                sleep=lambda _delay: None,
            )

        self.assertEqual(attempts["count"], 3)
        self.assertEqual(ctx.exception.operation, "neo4j.exec_cypher")
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertIsInstance(ctx.exception.last_error, RuntimeError)


class CircuitBreakerTests(unittest.TestCase):
    def test_circuit_opens_after_threshold(self) -> None:
        clock = [100.0]
        breaker = CircuitBreaker(
            failure_threshold=2,
            cooldown_seconds=30.0,
            time_source=lambda: clock[0],
            service_name="neo4j",
        )
        calls = {"count": 0}

        def always_fail() -> None:
            calls["count"] += 1
            raise RuntimeError("neo4j down")

        with self.assertRaises(RuntimeError):
            breaker.call(always_fail, operation="neo4j.bootstrap_schema", exceptions=(RuntimeError,))
        with self.assertRaises(RuntimeError):
            breaker.call(always_fail, operation="neo4j.bootstrap_schema", exceptions=(RuntimeError,))
        with self.assertRaises(CircuitOpenError):
            breaker.call(always_fail, operation="neo4j.bootstrap_schema", exceptions=(RuntimeError,))

        self.assertEqual(calls["count"], 2)
        self.assertEqual(breaker.state, "open")

    def test_circuit_half_opens_after_cooldown(self) -> None:
        clock = [100.0]
        breaker = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=30.0,
            time_source=lambda: clock[0],
            service_name="llm",
        )

        def fail_once() -> None:
            raise RuntimeError("provider down")

        with self.assertRaises(RuntimeError):
            breaker.call(fail_once, operation="llm.summarize_function", exceptions=(RuntimeError,))

        clock[0] += 31.0
        result = breaker.call(lambda: "ok", operation="llm.summarize_function")

        self.assertEqual(result, "ok")
        self.assertEqual(breaker.state, "closed")


if __name__ == "__main__":
    unittest.main()
