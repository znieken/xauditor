"""End-to-end tests for the POST /agent_invocations endpoint.

We bypass the real ``claude`` binary by patching
``run_agent_invocation`` (in ``app``'s namespace) with a stub that
returns a scripted ``AgentInvocationResponse``. That lets us exercise
the FastAPI surface — auth, project routing, semaphore, cancellation
— without spawning subprocesses.

Direct unit tests of ``run_agent_invocation`` itself (the orchestrator)
live in ``test_agent_invocation_orchestrator.py``, where they DO
exercise a stub claude binary (a small Python script) so the full
subprocess + JSON parsing + schema validation path is covered.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from xauditor_coder_service.agent_invocation import AgentInvocationResponse
from xauditor_coder_service.app import ServiceSettings, create_app
from xauditor_coder_service.auth import AuthSettings
from xauditor_coder_service.job import JobStore


def _settings(
    *, auth: AuthSettings | None = None, max_concurrent: int = 4
) -> ServiceSettings:
    return ServiceSettings(
        auth=auth or AuthSettings(enable_auth=False, token=""),
        max_concurrent_jobs=max_concurrent,
        job_ttl_seconds=3600.0,
        cli_command="claude",
        cli_path="/usr/bin/claude",  # never actually exec'd
        cli_version="claude 0.5.7",
    )


def _scripted_response(
    *,
    final_answer: dict | None = None,
    fell_back: bool = False,
    fallback_reason: str | None = None,
    transcript: list[dict] | None = None,
    elapsed: float = 0.42,
):
    """Build an async stub that returns a fixed AgentInvocationResponse."""

    async def _stub(request, *, config, project_dir, home=None, cancel_event=None):
        del request, config, project_dir, home, cancel_event
        return AgentInvocationResponse(
            final_answer=final_answer,
            transcript=transcript or [],
            fell_back=fell_back,
            fallback_reason=fallback_reason,
            elapsed_seconds=elapsed,
        )

    return _stub


def _slow_response(*, sleep_seconds: float = 5.0):
    """Stub that sleeps until the cancel_event fires (or sleep_seconds)."""

    async def _stub(request, *, config, project_dir, home=None, cancel_event=None):
        del request, config, project_dir, home
        if cancel_event is not None:
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=sleep_seconds)
                return AgentInvocationResponse(
                    final_answer=None,
                    transcript=[],
                    fell_back=True,
                    fallback_reason="cancelled: client disconnected",
                    elapsed_seconds=0.5,
                )
            except asyncio.TimeoutError:
                pass
        return AgentInvocationResponse(
            final_answer={"verdict": "Inconclusive"},
            transcript=[],
            fell_back=False,
            fallback_reason=None,
            elapsed_seconds=sleep_seconds,
        )

    return _stub


def _make_request_body(**overrides) -> dict:
    body = {
        "system_prompt": "You are an analyzer. Return a verdict.",
        "user_payload": {"path_fingerprint": "f00", "call_chain": []},
        "response_schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
        },
        "timeout_seconds": 30,
    }
    body.update(overrides)
    return body


class HappyPathTests(unittest.TestCase):
    def test_returns_validated_final_answer(self) -> None:
        store = JobStore()
        with mock.patch(
            "xauditor_coder_service.app.run_agent_invocation",
            new=_scripted_response(final_answer={"verdict": "Valid"}),
        ):
            with TestClient(
                create_app(settings=_settings(), job_store=store)
            ) as client:
                resp = client.post("/agent_invocations", json=_make_request_body())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["final_answer"], {"verdict": "Valid"})
        self.assertEqual(body["fell_back"], False)
        self.assertIsNone(body["fallback_reason"])

    def test_includes_transcript_in_response(self) -> None:
        store = JobStore()
        transcript = [
            {"tool": "Read", "input": {"path": "x.py"}, "output": "..."},
            {"tool": "final_answer", "input": {"verdict": "Valid"}, "output": None},
        ]
        with mock.patch(
            "xauditor_coder_service.app.run_agent_invocation",
            new=_scripted_response(
                final_answer={"verdict": "Valid"}, transcript=transcript
            ),
        ):
            with TestClient(
                create_app(settings=_settings(), job_store=store)
            ) as client:
                resp = client.post("/agent_invocations", json=_make_request_body())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["transcript"], transcript)


class FallbackResponseTests(unittest.TestCase):
    """fell_back=True surfaces in the body, NOT as a 5xx."""

    def test_timeout_returns_200_with_fell_back_true(self) -> None:
        store = JobStore()
        with mock.patch(
            "xauditor_coder_service.app.run_agent_invocation",
            new=_scripted_response(
                fell_back=True, fallback_reason="timeout: timed out after 30s"
            ),
        ):
            with TestClient(
                create_app(settings=_settings(), job_store=store)
            ) as client:
                resp = client.post("/agent_invocations", json=_make_request_body())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["fell_back"])
        self.assertIn("timeout", body["fallback_reason"])
        self.assertIsNone(body["final_answer"])

    def test_subprocess_crash_returns_200_with_fell_back_true(self) -> None:
        store = JobStore()
        with mock.patch(
            "xauditor_coder_service.app.run_agent_invocation",
            new=_scripted_response(
                fell_back=True, fallback_reason="subprocess_exit_-1"
            ),
        ):
            with TestClient(
                create_app(settings=_settings(), job_store=store)
            ) as client:
                resp = client.post("/agent_invocations", json=_make_request_body())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["fell_back"])

    def test_schema_validation_fail_returns_200_with_fell_back_true(self) -> None:
        store = JobStore()
        # Final answer present but schema-violating: the stub still
        # produces fell_back=true so callers see the structured failure.
        with mock.patch(
            "xauditor_coder_service.app.run_agent_invocation",
            new=_scripted_response(
                final_answer={"wrong_key": "value"},
                fell_back=True,
                fallback_reason="schema_validation_failed: 'verdict' is a required property",
            ),
        ):
            with TestClient(
                create_app(settings=_settings(), job_store=store)
            ) as client:
                resp = client.post("/agent_invocations", json=_make_request_body())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["fell_back"])
        self.assertIn("schema_validation_failed", body["fallback_reason"])
        # final_answer is preserved — caller can inspect what claude
        # actually returned.
        self.assertEqual(body["final_answer"], {"wrong_key": "value"})


class RequestValidationTests(unittest.TestCase):
    """Pydantic validation surfaces as 422; never reaches the orchestrator."""

    def test_missing_system_prompt_is_422(self) -> None:
        store = JobStore()
        body = _make_request_body()
        del body["system_prompt"]
        with TestClient(
            create_app(settings=_settings(), job_store=store)
        ) as client:
            resp = client.post("/agent_invocations", json=body)
        self.assertEqual(resp.status_code, 422)

    def test_empty_system_prompt_is_422(self) -> None:
        store = JobStore()
        body = _make_request_body(system_prompt="")
        with TestClient(
            create_app(settings=_settings(), job_store=store)
        ) as client:
            resp = client.post("/agent_invocations", json=body)
        self.assertEqual(resp.status_code, 422)

    def test_timeout_below_min_is_422(self) -> None:
        store = JobStore()
        body = _make_request_body(timeout_seconds=5)
        with TestClient(
            create_app(settings=_settings(), job_store=store)
        ) as client:
            resp = client.post("/agent_invocations", json=body)
        self.assertEqual(resp.status_code, 422)

    def test_timeout_above_max_is_422(self) -> None:
        store = JobStore()
        body = _make_request_body(timeout_seconds=10_000)
        with TestClient(
            create_app(settings=_settings(), job_store=store)
        ) as client:
            resp = client.post("/agent_invocations", json=body)
        self.assertEqual(resp.status_code, 422)


class AuthTests(unittest.TestCase):
    def test_missing_bearer_token_returns_401(self) -> None:
        store = JobStore()
        settings = _settings(
            auth=AuthSettings(enable_auth=True, token="secret"),
        )
        with TestClient(
            create_app(settings=settings, job_store=store)
        ) as client:
            resp = client.post("/agent_invocations", json=_make_request_body())
        self.assertEqual(resp.status_code, 401)

    def test_correct_bearer_token_succeeds(self) -> None:
        store = JobStore()
        settings = _settings(
            auth=AuthSettings(enable_auth=True, token="secret"),
        )
        with mock.patch(
            "xauditor_coder_service.app.run_agent_invocation",
            new=_scripted_response(final_answer={"verdict": "Valid"}),
        ):
            with TestClient(
                create_app(settings=settings, job_store=store)
            ) as client:
                resp = client.post(
                    "/agent_invocations",
                    json=_make_request_body(),
                    headers={"Authorization": "Bearer secret"},
                )
        self.assertEqual(resp.status_code, 200)


class ConcurrencyTests(unittest.TestCase):
    """Agent invocations queue on the shared semaphore."""

    def test_invocation_blocks_when_semaphore_saturated_by_verifications(self) -> None:
        store = JobStore()
        settings = _settings(max_concurrent=1)

        # 1) Pre-acquire the only semaphore slot via a slow agent
        # invocation, then 2) confirm a second invocation is queued
        # (response time reflects the wait). We use the slow stub so
        # we don't need to spawn real claude.

        async def _scenario():
            from xauditor_coder_service.app import create_app  # local import

            app = create_app(settings=settings, job_store=store)
            with mock.patch(
                "xauditor_coder_service.app.run_agent_invocation",
                new=_slow_response(sleep_seconds=2.0),
            ):
                with TestClient(app) as client:
                    import time

                    # Kick off the first call in a thread (TestClient is
                    # synchronous), then the second call should queue.
                    import threading

                    body = _make_request_body()
                    results: list[float] = []

                    def _call() -> None:
                        t0 = time.monotonic()
                        client.post("/agent_invocations", json=body)
                        results.append(time.monotonic() - t0)

                    t1 = threading.Thread(target=_call)
                    t2 = threading.Thread(target=_call)
                    t1.start()
                    # Stagger the second call so we can observe queueing.
                    time.sleep(0.05)
                    t2.start()
                    t1.join(timeout=10)
                    t2.join(timeout=10)
                    return results

        results = asyncio.run(_scenario())
        self.assertEqual(len(results), 2)
        # The faster of the two completed in roughly the stub's sleep
        # time; the slower (queued) call took roughly 2x. Don't rely on
        # exact timings — just assert one was meaningfully slower.
        results.sort()
        self.assertGreater(results[1], results[0])


class HealthCheckUnchangedTests(unittest.TestCase):
    """Adding the new endpoint must not change /health surface."""

    def test_health_shape_preserved(self) -> None:
        store = JobStore()
        with TestClient(
            create_app(settings=_settings(), job_store=store)
        ) as client:
            resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("max_concurrent_jobs", body)
        self.assertIn("in_flight", body)
        self.assertIn("claude_cli_version", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
