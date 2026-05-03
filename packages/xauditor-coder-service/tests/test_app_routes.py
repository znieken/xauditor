"""End-to-end route tests for the FastAPI app using TestClient.

We bypass the real worker by patching ``run_verification`` to a stub
that flips the job to a terminal state immediately. That lets us exercise
the full HTTP surface (POST → GET → DELETE → 404) without spawning real
subprocesses, which would slow tests down and need a fake-claude binary.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from xauditor_coder_service.app import ServiceSettings, create_app
from xauditor_coder_service.auth import AuthSettings
from xauditor_coder_service.job import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PENDING,
    JobStore,
)


def _settings(*, auth: AuthSettings | None = None, max_concurrent: int = 4) -> ServiceSettings:
    return ServiceSettings(
        auth=auth or AuthSettings(enable_auth=False, token=""),
        max_concurrent_jobs=max_concurrent,
        job_ttl_seconds=3600.0,
        cli_command="claude",
        cli_path="/usr/bin/claude",  # we won't actually exec it
        cli_version="claude 0.5.7",
    )


def _stub_run_verification_done(verdict_status: str = "Verified"):
    """Return an async stub that immediately marks the job ``done``."""

    async def _stub(job, *, payload, claude_args, request_timeout_seconds, worker_config, job_store, cwd=None, home=None):
        job_store.mark_terminal(
            job.job_id,
            status=STATUS_DONE,
            result={
                "status": verdict_status,
                "analysis": "stubbed analysis",
                "reason": "stub",
                "evidence": [],
                "cli_exit_code": 0,
                "cli_stderr": None,
                "duration_ms": 1,
            },
        )

    return _stub


def _stub_run_verification_slow():
    """Return an async stub that blocks until cancelled."""

    async def _stub(job, *, payload, claude_args, request_timeout_seconds, worker_config, job_store, cwd=None, home=None):
        try:
            await asyncio.wait_for(job.cancel_event.wait(), timeout=10)
            job_store.mark_terminal(
                job.job_id,
                status=STATUS_CANCELLED,
                error="cancelled by client",
            )
        except asyncio.TimeoutError:
            job_store.mark_terminal(
                job.job_id,
                status=STATUS_DONE,
                result={
                    "status": "Inconclusive",
                    "analysis": "",
                    "reason": "test never cancelled",
                    "evidence": [],
                    "cli_exit_code": 0,
                    "cli_stderr": None,
                    "duration_ms": 1,
                },
            )

    return _stub


class HealthRouteTests(unittest.TestCase):
    def test_health_is_unauth_and_returns_status(self) -> None:
        # Even with auth enabled, /health is bypassed.
        store = JobStore()
        settings = _settings(auth=AuthSettings(enable_auth=True, token="t"))
        with TestClient(create_app(settings=settings, job_store=store)) as client:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["claude_cli_version"], "claude 0.5.7")
            self.assertEqual(body["max_concurrent_jobs"], 4)
            self.assertEqual(body["in_flight"], 0)


class SubmitGetDeleteFlowTests(unittest.TestCase):
    def test_submit_returns_201_then_get_returns_done(self) -> None:
        store = JobStore()
        settings = _settings()
        with mock.patch(
            "xauditor_coder_service.app.run_verification",
            _stub_run_verification_done("Verified"),
        ):
            with TestClient(create_app(settings=settings, job_store=store)) as client:
                submit = client.post(
                    "/verifications",
                    json={
                        "idempotency_key": "run-1::F-0001",
                        "payload": {"finding": {"finding_id": "F-0001"}},
                        "claude_args": {},
                        "request_timeout_seconds": 30,
                    },
                )
                self.assertEqual(submit.status_code, 201)
                job_id = submit.json()["job_id"]
                self.assertTrue(job_id)
                # Poll until done. Stub completes synchronously; one GET
                # should be enough but be defensive.
                for _ in range(20):
                    poll = client.get(f"/verifications/{job_id}")
                    self.assertEqual(poll.status_code, 200)
                    state = poll.json()
                    if state["status"] != STATUS_PENDING:
                        break
                self.assertEqual(state["status"], STATUS_DONE)
                self.assertEqual(state["result"]["status"], "Verified")
                self.assertEqual(state["result"]["analysis"], "stubbed analysis")

    def test_idempotent_resubmit_returns_200(self) -> None:
        store = JobStore()
        settings = _settings()
        with mock.patch(
            "xauditor_coder_service.app.run_verification",
            _stub_run_verification_done(),
        ):
            with TestClient(create_app(settings=settings, job_store=store)) as client:
                first = client.post(
                    "/verifications",
                    json={
                        "idempotency_key": "k",
                        "payload": {},
                        "claude_args": {},
                        "request_timeout_seconds": 30,
                    },
                )
                self.assertEqual(first.status_code, 201)
                job_id = first.json()["job_id"]
                second = client.post(
                    "/verifications",
                    json={
                        "idempotency_key": "k",
                        "payload": {},
                        "claude_args": {},
                        "request_timeout_seconds": 30,
                    },
                )
                # Status code is either 200 (already done) or 201 (still
                # pending → idempotent same-id). Either way the job_id
                # must match.
                self.assertIn(second.status_code, (200, 201))
                self.assertEqual(second.json()["job_id"], job_id)

    def test_get_unknown_returns_404(self) -> None:
        store = JobStore()
        settings = _settings()
        with TestClient(create_app(settings=settings, job_store=store)) as client:
            resp = client.get("/verifications/totally-not-a-real-uuid")
            self.assertEqual(resp.status_code, 404)

    def test_delete_pending_marks_cancelled(self) -> None:
        store = JobStore()
        settings = _settings()
        with mock.patch(
            "xauditor_coder_service.app.run_verification",
            _stub_run_verification_slow(),
        ):
            with TestClient(create_app(settings=settings, job_store=store)) as client:
                submit = client.post(
                    "/verifications",
                    json={
                        "idempotency_key": "k",
                        "payload": {},
                        "claude_args": {},
                        "request_timeout_seconds": 30,
                    },
                )
                self.assertEqual(submit.status_code, 201)
                job_id = submit.json()["job_id"]
                cancel = client.delete(f"/verifications/{job_id}")
                self.assertEqual(cancel.status_code, 204)
                # Poll until terminal
                for _ in range(20):
                    poll = client.get(f"/verifications/{job_id}")
                    if poll.json()["status"] != STATUS_PENDING:
                        break
                self.assertEqual(poll.json()["status"], STATUS_CANCELLED)

    def test_delete_unknown_returns_404(self) -> None:
        store = JobStore()
        settings = _settings()
        with TestClient(create_app(settings=settings, job_store=store)) as client:
            resp = client.delete("/verifications/no-such-job")
            self.assertEqual(resp.status_code, 404)


class AuthEnforcementTests(unittest.TestCase):
    def test_post_without_authorization_returns_401_when_auth_on(self) -> None:
        store = JobStore()
        settings = _settings(auth=AuthSettings(enable_auth=True, token="secret"))
        with TestClient(create_app(settings=settings, job_store=store)) as client:
            resp = client.post(
                "/verifications",
                json={
                    "idempotency_key": "k",
                    "payload": {},
                    "claude_args": {},
                    "request_timeout_seconds": 30,
                },
            )
            self.assertEqual(resp.status_code, 401)

    def test_post_with_correct_authorization_succeeds(self) -> None:
        store = JobStore()
        settings = _settings(auth=AuthSettings(enable_auth=True, token="secret"))
        with mock.patch(
            "xauditor_coder_service.app.run_verification",
            _stub_run_verification_done(),
        ):
            with TestClient(create_app(settings=settings, job_store=store)) as client:
                resp = client.post(
                    "/verifications",
                    headers={"Authorization": "Bearer secret"},
                    json={
                        "idempotency_key": "k",
                        "payload": {},
                        "claude_args": {},
                        "request_timeout_seconds": 30,
                    },
                )
                self.assertEqual(resp.status_code, 201)

    def test_post_with_wrong_token_returns_401(self) -> None:
        store = JobStore()
        settings = _settings(auth=AuthSettings(enable_auth=True, token="secret"))
        with TestClient(create_app(settings=settings, job_store=store)) as client:
            resp = client.post(
                "/verifications",
                headers={"Authorization": "Bearer wrong"},
                json={
                    "idempotency_key": "k",
                    "payload": {},
                    "claude_args": {},
                    "request_timeout_seconds": 30,
                },
            )
            self.assertEqual(resp.status_code, 401)

    def test_health_bypasses_auth(self) -> None:
        store = JobStore()
        settings = _settings(auth=AuthSettings(enable_auth=True, token="secret"))
        with TestClient(create_app(settings=settings, job_store=store)) as client:
            resp = client.get("/health")  # no Authorization
            self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
