from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from xauditor_portal.config_resolver import (
    FORBIDDEN_UI_KEY_PREFIXES,
    FORBIDDEN_UI_KEY_SUFFIXES,
    SECRET_VALUE_KEY_PATTERNS,
    SOURCE_DB,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_YML,
    UI_EDITABLE_KEY_PREFIXES,
    flatten,
    forbidden_keys_in_payload,
    is_secret_key,
    is_ui_editable,
    redact_values,
    resolve_effective,
    unflatten,
    write_effective_config_yml,
)


class FlattenUnflattenTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        nested = {
            "a": 1,
            "b": {"c": 2, "d": {"e": 3}},
            "f": [1, 2, 3],
        }
        flat = flatten(nested)
        self.assertEqual(
            flat,
            {"a": 1, "b.c": 2, "b.d.e": 3, "f": [1, 2, 3]},
        )
        self.assertEqual(unflatten(flat), nested)


class UiEditabilityTests(unittest.TestCase):
    def test_remote_urls_are_forbidden(self) -> None:
        for key in (
            "reportdb.remote.url",
            "reportdb.remote.ssl_ca",
            "graph.db.remote.url",
            "graph.db.remote.pool_size",
        ):
            self.assertFalse(is_ui_editable(key), msg=key)

    def test_api_keys_are_forbidden(self) -> None:
        self.assertFalse(is_ui_editable("llm.providers.p1.api_key"))

    def test_typical_ui_keys_are_editable(self) -> None:
        for key in (
            "logging.level",
            "repository.excludes",
            "graph.build.enable_llm_enrichment",
            "teaming.enabled",
            "teaming.analyzer.subagent_count",
            "llm.default_provider",
            "llm.providers.openai.base_url",
            "llm.providers.openai.model_name",
            "agents.auditor.llm.provider",
        ):
            self.assertTrue(is_ui_editable(key), msg=key)

    def test_forbidden_keys_in_payload(self) -> None:
        payload = {
            "reportdb": {"remote": {"url": "postgresql://..."}},
            "llm": {"providers": {"openai": {"api_key": "sk-..."}}},
            "logging": {"level": "debug"},
        }
        forbidden = forbidden_keys_in_payload(payload)
        self.assertIn("reportdb.remote.url", forbidden)
        self.assertIn("llm.providers.openai.api_key", forbidden)
        self.assertNotIn("logging.level", forbidden)

    def test_forbidden_prefixes_and_suffixes_are_exposed(self) -> None:
        self.assertIn("reportdb.remote.", FORBIDDEN_UI_KEY_PREFIXES)
        self.assertIn("graph.db.remote.", FORBIDDEN_UI_KEY_PREFIXES)
        self.assertIn(".api_key", FORBIDDEN_UI_KEY_SUFFIXES)


class ResolveEffectiveTests(unittest.TestCase):
    def test_yml_wins_over_db(self) -> None:
        yml = {"logging": {"level": "debug"}, "teaming": {"enabled": True}}
        db = {"logging": {"level": "info"}, "teaming": {"enabled": False}}
        result = resolve_effective(yml=yml, db_snapshot=db)
        self.assertEqual(result.values["logging.level"], "debug")
        self.assertEqual(result.values["teaming.enabled"], True)
        self.assertEqual(result.per_field["logging.level"].source, SOURCE_YML)
        self.assertTrue(result.per_field["logging.level"].overridden_by_yml)
        self.assertTrue(result.per_field["teaming.enabled"].overridden_by_yml)
        self.assertEqual(
            set(result.overridden_keys),
            {"logging.level", "teaming.enabled"},
        )

    def test_db_fills_in_missing_yml_keys(self) -> None:
        yml: dict = {}
        db = {"logging": {"level": "info"}}
        result = resolve_effective(yml=yml, db_snapshot=db)
        self.assertEqual(result.per_field["logging.level"].source, SOURCE_DB)
        self.assertFalse(result.per_field["logging.level"].overridden_by_yml)

    def test_defaults_used_when_neither_yml_nor_db_defines(self) -> None:
        result = resolve_effective(
            yml={}, db_snapshot={}, defaults={"repository": {"excludes": []}}
        )
        self.assertEqual(result.per_field["repository.excludes"].source, SOURCE_DEFAULT)

    def test_forbidden_keys_stay_in_values_but_out_of_per_field(self) -> None:
        yml = {"reportdb": {"remote": {"url": "postgresql://h/db"}}}
        result = resolve_effective(yml=yml, db_snapshot={})
        self.assertEqual(
            result.values["reportdb.remote.url"], "postgresql://h/db"
        )
        self.assertNotIn("reportdb.remote.url", result.per_field)


class ExtendedAllowListTests(unittest.TestCase):
    def test_audit_keys_are_editable(self) -> None:
        self.assertTrue(is_ui_editable("audit.worker_count"))

    def test_coder_keys_are_editable(self) -> None:
        for key in (
            "coder.enabled",
            "coder.transport",
            "coder.concurrency",
            "coder.workspace_root",
            "coder.project_name",
            "coder.cli_command",
        ):
            self.assertTrue(is_ui_editable(key), msg=key)

    def test_audit_and_coder_in_allow_list_constant(self) -> None:
        self.assertIn("audit.", UI_EDITABLE_KEY_PREFIXES)
        self.assertIn("coder.", UI_EDITABLE_KEY_PREFIXES)


class ExtendedForbiddenSuffixTests(unittest.TestCase):
    def test_password_suffix_is_forbidden(self) -> None:
        for key in (
            "reportdb.password",
            "graph.db.password",
        ):
            self.assertFalse(is_ui_editable(key), msg=key)

    def test_model_api_key_suffix_is_forbidden(self) -> None:
        self.assertFalse(is_ui_editable("coder.model_api_key"))

    def test_endpoint_token_suffix_is_forbidden(self) -> None:
        self.assertFalse(is_ui_editable("coder.endpoint_token"))

    def test_new_suffixes_in_constant(self) -> None:
        for suffix in (".api_key", ".password", ".model_api_key", ".endpoint_token"):
            self.assertIn(suffix, FORBIDDEN_UI_KEY_SUFFIXES)

    def test_remote_url_pattern_is_in_secret_value_patterns(self) -> None:
        self.assertIn("*.remote.url", SECRET_VALUE_KEY_PATTERNS)


class IsSecretKeyTests(unittest.TestCase):
    def test_api_key_suffix(self) -> None:
        self.assertTrue(is_secret_key("llm.providers.shared.api_key"))

    def test_password_suffix(self) -> None:
        self.assertTrue(is_secret_key("graph.db.password"))
        self.assertTrue(is_secret_key("reportdb.password"))

    def test_model_api_key_suffix(self) -> None:
        self.assertTrue(is_secret_key("coder.model_api_key"))

    def test_endpoint_token_suffix(self) -> None:
        self.assertTrue(is_secret_key("coder.endpoint_token"))

    def test_remote_url_pattern(self) -> None:
        self.assertTrue(is_secret_key("reportdb.remote.url"))
        self.assertTrue(is_secret_key("graph.db.remote.url"))

    def test_non_secret_keys(self) -> None:
        for key in (
            "logging.level",
            "graph.build.neo4j_chunk_size",
            "audit.worker_count",
            "coder.transport",
            "llm.providers.shared.base_url",
            "llm.providers.shared.model_name",
        ):
            self.assertFalse(is_secret_key(key), msg=key)

    def test_remote_non_url_fields_are_not_secret(self) -> None:
        # ssl_ca / pool_size live next to remote.url; they aren't secret
        # values themselves.
        self.assertFalse(is_secret_key("reportdb.remote.ssl_ca"))
        self.assertFalse(is_secret_key("reportdb.remote.pool_size"))


class ForbiddenKeysInPayloadDelegationTests(unittest.TestCase):
    def test_forbidden_keys_uses_is_secret_key(self) -> None:
        forbidden = forbidden_keys_in_payload(
            {
                "coder": {
                    "model_api_key": "x",
                    "endpoint_token": "y",
                },
                "graph": {"db": {"password": "p"}},
                "logging": {"level": "info"},
            }
        )
        self.assertIn("coder.model_api_key", forbidden)
        self.assertIn("coder.endpoint_token", forbidden)
        self.assertIn("graph.db.password", forbidden)
        self.assertNotIn("logging.level", forbidden)


class RedactValuesTests(unittest.TestCase):
    def test_redacts_known_secret_keys(self) -> None:
        values = {
            "logging.level": "info",
            "llm.providers.shared.api_key": "sk-123",
            "graph.db.password": "p",
            "coder.model_api_key": "tok",
            "coder.endpoint_token": "bearer",
            "reportdb.remote.url": "postgresql://u:p@h/db",
            "audit.worker_count": 4,
        }
        redacted_values, redacted_keys_map = redact_values(values)
        # Non-secret keys are preserved.
        self.assertEqual(redacted_values["logging.level"], "info")
        self.assertEqual(redacted_values["audit.worker_count"], 4)
        # Secret keys are nulled out in values.
        for k in (
            "llm.providers.shared.api_key",
            "graph.db.password",
            "coder.model_api_key",
            "coder.endpoint_token",
            "reportdb.remote.url",
        ):
            self.assertIsNone(redacted_values[k], msg=k)
            self.assertIn(k, redacted_keys_map, msg=k)
            self.assertTrue(redacted_keys_map[k]["present"], msg=k)

    def test_present_false_when_value_is_empty(self) -> None:
        values = {"coder.model_api_key": "", "coder.endpoint_token": None}
        _, redacted_keys_map = redact_values(values)
        self.assertFalse(redacted_keys_map["coder.model_api_key"]["present"])
        self.assertFalse(redacted_keys_map["coder.endpoint_token"]["present"])

    def test_does_not_leak_secret_literals(self) -> None:
        values = {"llm.providers.shared.api_key": "sk-LEAKAGE"}
        redacted_values, _ = redact_values(values)
        self.assertNotIn("sk-LEAKAGE", repr(redacted_values))


class EnvSourceTests(unittest.TestCase):
    def test_source_env_constant_exists(self) -> None:
        self.assertEqual(SOURCE_ENV, "env")

    def test_env_wins_over_yml(self) -> None:
        yml = {"graph": {"build": {"neo4j_chunk_size": 5000}}}
        env = {"graph.build.neo4j_chunk_size": 10000}
        result = resolve_effective(yml=yml, db_snapshot={}, env_overrides=env)
        field = result.per_field["graph.build.neo4j_chunk_size"]
        self.assertEqual(field.value, 10000)
        self.assertEqual(field.source, SOURCE_ENV)

    def test_yml_still_wins_over_db_when_no_env(self) -> None:
        yml = {"logging": {"level": "debug"}}
        db = {"logging": {"level": "info"}}
        result = resolve_effective(yml=yml, db_snapshot=db, env_overrides=None)
        field = result.per_field["logging.level"]
        self.assertEqual(field.value, "debug")
        self.assertEqual(field.source, SOURCE_YML)

    def test_env_overrides_omitted_arg_is_back_compat(self) -> None:
        # Existing two-arg call must still work.
        yml = {"logging": {"level": "info"}}
        result = resolve_effective(yml=yml, db_snapshot={})
        self.assertEqual(result.per_field["logging.level"].source, SOURCE_YML)


class WriteEffectiveConfigYmlTests(unittest.TestCase):
    def test_writes_nested_yml(self) -> None:
        yml = {"logging": {"level": "debug"}}
        db = {"repository": {"excludes": ["vendor"]}}
        result = resolve_effective(yml=yml, db_snapshot=db)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "effective.yml"
            write_effective_config_yml(result, target=target)
            self.assertTrue(target.exists())
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            self.assertEqual(loaded["logging"]["level"], "debug")
            self.assertEqual(loaded["repository"]["excludes"], ["vendor"])


if __name__ == "__main__":
    unittest.main()
