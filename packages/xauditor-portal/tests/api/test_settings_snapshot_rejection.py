"""Endpoint-level rejection tests for ``PUT /api/config/snapshot``.

Verifies that the rejection branches (secret-shaped key + out-of-range
numeric) reach the wire with HTTP 400 and the documented error envelope,
not just at the validator function level.

Uses ``fastapi.testclient.TestClient`` with the auth + DB session
dependencies overridden so the test does not require a live database.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from xauditor_portal.app import create_app
from xauditor_portal.auth import forbid_must_change_password, get_session


class _FakeUser:
    """Minimal stand-in for ``User`` — only ``id``, ``role``, and
    ``must_change_password`` are touched by the endpoint."""

    id = "00000000-0000-0000-0000-000000000001"
    must_change_password = False
    role = "admin"


def _build_client() -> TestClient:
    app = create_app()
    # The save-snapshot endpoint now requires admin; override both gates
    # so the validation-rejection paths still land before any DB work.
    from xauditor_portal.auth import require_admin

    app.dependency_overrides[forbid_must_change_password] = lambda: _FakeUser()
    app.dependency_overrides[require_admin] = lambda: _FakeUser()

    async def _no_session():
        # save_snapshot only touches the session AFTER the validation
        # branches we're testing. The MagicMock is enough for failing-fast
        # paths; if a future change moves the session work earlier, this
        # test will start hitting it and we'll need a real-ish stub.
        yield MagicMock()

    app.dependency_overrides[get_session] = _no_session
    return TestClient(app)


class SecretShapedKeysAreRejectedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = _build_client()

    def _put(self, body: dict) -> tuple[int, dict]:
        response = self.client.put("/api/config/snapshot", json={"body": body})
        return response.status_code, response.json()

    def test_api_key_rejected(self) -> None:
        status, body = self._put(
            {"llm": {"providers": {"shared": {"api_key": "sk-x"}}}}
        )
        self.assertEqual(status, 400)
        detail = body["detail"]
        self.assertIn("llm.providers.shared.api_key", detail["forbidden_keys"])
        self.assertIn("api_key", detail["message"])

    def test_password_rejected(self) -> None:
        status, body = self._put({"graph": {"db": {"password": "p"}}})
        self.assertEqual(status, 400)
        detail = body["detail"]
        self.assertIn("graph.db.password", detail["forbidden_keys"])

    def test_model_api_key_rejected(self) -> None:
        status, body = self._put({"coder": {"model_api_key": "tok"}})
        self.assertEqual(status, 400)
        detail = body["detail"]
        self.assertIn("coder.model_api_key", detail["forbidden_keys"])

    def test_endpoint_token_rejected(self) -> None:
        status, body = self._put({"coder": {"endpoint_token": "bearer"}})
        self.assertEqual(status, 400)
        detail = body["detail"]
        self.assertIn("coder.endpoint_token", detail["forbidden_keys"])

    def test_remote_url_rejected(self) -> None:
        status, body = self._put(
            {"reportdb": {"remote": {"url": "postgres://u@h/db"}}}
        )
        self.assertEqual(status, 400)
        detail = body["detail"]
        self.assertIn("reportdb.remote.url", detail["forbidden_keys"])


class NumericRangeRejectedAtEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = _build_client()

    def _put(self, body: dict) -> tuple[int, dict]:
        response = self.client.put("/api/config/snapshot", json={"body": body})
        return response.status_code, response.json()

    def test_audit_worker_count_below_minimum_rejected(self) -> None:
        status, body = self._put({"audit": {"worker_count": 0}})
        self.assertEqual(status, 400)
        detail = body["detail"]
        joined = " | ".join(detail["errors"])
        self.assertIn("audit.worker_count", joined)
        self.assertIn("[1, 16]", joined)

    def test_audit_worker_count_above_maximum_rejected(self) -> None:
        status, body = self._put({"audit": {"worker_count": 32}})
        self.assertEqual(status, 400)
        joined = " | ".join(body["detail"]["errors"])
        self.assertIn("audit.worker_count", joined)

    def test_coder_concurrency_above_max_rejected(self) -> None:
        status, body = self._put({"coder": {"concurrency": 65}})
        self.assertEqual(status, 400)
        joined = " | ".join(body["detail"]["errors"])
        self.assertIn("coder.concurrency", joined)

    def test_neo4j_chunk_size_below_min_rejected(self) -> None:
        status, body = self._put(
            {"graph": {"build": {"neo4j_chunk_size": 50}}}
        )
        self.assertEqual(status, 400)
        joined = " | ".join(body["detail"]["errors"])
        self.assertIn("graph.build.neo4j_chunk_size", joined)

    def test_request_timeout_seconds_zero_rejected(self) -> None:
        status, body = self._put(
            {"llm": {"providers": {"x": {"request_timeout_seconds": 0}}}}
        )
        self.assertEqual(status, 400)
        joined = " | ".join(body["detail"]["errors"])
        self.assertIn("llm.providers.x.request_timeout_seconds", joined)

    def test_top_p_above_one_rejected(self) -> None:
        status, body = self._put(
            {"llm": {"providers": {"x": {"top_p": 1.5}}}}
        )
        self.assertEqual(status, 400)
        joined = " | ".join(body["detail"]["errors"])
        self.assertIn("llm.providers.x.top_p", joined)


if __name__ == "__main__":
    unittest.main()
