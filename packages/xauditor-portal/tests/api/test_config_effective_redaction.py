"""Unit tests for ``build_effective_config_out`` — the synchronous
helper that composes the effective-config response from yml + DB + env.

End-to-end TestClient tests against the live database live in
``tests/portal/test_settings_redaction.py`` (gated by
``XAUDITOR_TEST_DATABASE_URL``). These tests cover the redaction +
source-tagging logic without spinning up a database session, so they
remain runnable in any environment.
"""

from __future__ import annotations

import json
import unittest

from xauditor_portal.api.config import build_effective_config_out


class RedactedSecretsOmittedFromValuesTests(unittest.TestCase):
    def _build(self, **kwargs):
        return build_effective_config_out(
            yml=kwargs.get("yml", {}),
            db_body=kwargs.get("db_body", {}),
            env_overrides=kwargs.get("env_overrides", None),
        )

    def test_api_key_in_yml_is_redacted(self) -> None:
        out = self._build(
            yml={"llm": {"providers": {"shared": {"api_key": "sk-ABCDEF"}}}}
        )
        # Literal must not appear anywhere in the serialised response.
        self.assertNotIn("sk-ABCDEF", out.model_dump_json())
        self.assertIsNone(out.values["llm.providers.shared.api_key"])
        self.assertIn("llm.providers.shared.api_key", out.redacted_keys)
        entry = out.redacted_keys["llm.providers.shared.api_key"]
        self.assertTrue(entry.present)
        self.assertEqual(entry.source, "yml")

    def test_coder_model_api_key_is_redacted(self) -> None:
        out = self._build(yml={"coder": {"model_api_key": "TOK-X"}})
        self.assertNotIn("TOK-X", out.model_dump_json())
        self.assertIsNone(out.values["coder.model_api_key"])
        self.assertTrue(out.redacted_keys["coder.model_api_key"].present)
        self.assertEqual(out.redacted_keys["coder.model_api_key"].source, "yml")

    def test_coder_endpoint_token_is_redacted(self) -> None:
        out = self._build(yml={"coder": {"endpoint_token": "BEARER-Y"}})
        self.assertNotIn("BEARER-Y", out.model_dump_json())
        self.assertTrue(out.redacted_keys["coder.endpoint_token"].present)

    def test_graph_db_password_is_redacted(self) -> None:
        out = self._build(yml={"graph": {"db": {"password": "GREMLIN"}}})
        self.assertNotIn("GREMLIN", out.model_dump_json())
        self.assertTrue(out.redacted_keys["graph.db.password"].present)

    def test_reportdb_password_is_redacted(self) -> None:
        out = self._build(yml={"reportdb": {"password": "PG-PASS"}})
        self.assertNotIn("PG-PASS", out.model_dump_json())
        self.assertTrue(out.redacted_keys["reportdb.password"].present)

    def test_remote_url_with_inline_credentials_is_redacted(self) -> None:
        url = "postgresql://u:secret@host/db"
        out = self._build(yml={"reportdb": {"remote": {"url": url}}})
        body = out.model_dump_json()
        self.assertNotIn("secret", body)
        self.assertNotIn(url, body)
        self.assertTrue(out.redacted_keys["reportdb.remote.url"].present)
        self.assertEqual(out.redacted_keys["reportdb.remote.url"].source, "yml")

    def test_non_secret_keys_are_carried_through(self) -> None:
        out = self._build(
            yml={
                "logging": {"level": "info"},
                "audit": {"worker_count": 4},
            }
        )
        self.assertEqual(out.values["logging.level"], "info")
        self.assertEqual(out.values["audit.worker_count"], 4)


class EnvSourceTaggingTests(unittest.TestCase):
    def test_env_overridden_key_reports_source_env(self) -> None:
        out = build_effective_config_out(
            yml={"graph": {"build": {"neo4j_chunk_size": 5000}}},
            db_body={},
            env_overrides={"graph.build.neo4j_chunk_size": "10000"},
        )
        field = out.fields["graph.build.neo4j_chunk_size"]
        # env_overrides values come through as raw strings; the audit
        # runtime parses + clamps them later. Source is what we care
        # about here.
        self.assertEqual(field.source, "env")

    def test_yml_only_field_reports_source_yml(self) -> None:
        out = build_effective_config_out(
            yml={"audit": {"worker_count": 8}}, db_body={}, env_overrides=None
        )
        self.assertEqual(out.fields["audit.worker_count"].source, "yml")
        self.assertEqual(out.fields["audit.worker_count"].value, 8)


class RedactedKeysShapeTests(unittest.TestCase):
    def test_present_false_for_empty_secret(self) -> None:
        out = build_effective_config_out(
            yml={"coder": {"model_api_key": ""}}, db_body={}, env_overrides=None
        )
        self.assertFalse(out.redacted_keys["coder.model_api_key"].present)

    def test_redacted_keys_round_trips_through_json(self) -> None:
        out = build_effective_config_out(
            yml={"coder": {"endpoint_token": "BEARER"}},
            db_body={},
            env_overrides=None,
        )
        body = json.loads(out.model_dump_json())
        self.assertIn("redacted_keys", body)
        self.assertIn("coder.endpoint_token", body["redacted_keys"])
        self.assertEqual(
            body["redacted_keys"]["coder.endpoint_token"],
            {"present": True, "source": "yml"},
        )


if __name__ == "__main__":
    unittest.main()
