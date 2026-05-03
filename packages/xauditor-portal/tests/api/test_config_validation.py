from __future__ import annotations

import unittest

from xauditor_portal.api.config import (
    NUMERIC_RANGE_REGISTRY,
    _collect_env_overrides,
    _validate_numeric_payload,
    _validate_sampling_field,
    _validate_sampling_payload,
)
from xauditor_portal.config_resolver import forbidden_keys_in_payload


class ValidateSamplingFieldTests(unittest.TestCase):
    def test_non_sampling_keys_are_ignored(self) -> None:
        self.assertIsNone(_validate_sampling_field("logging.level", "info"))
        self.assertIsNone(_validate_sampling_field("llm.default_provider", "shared"))

    def test_none_values_are_ignored(self) -> None:
        for key in (
            "llm.providers.x.temperature",
            "llm.providers.x.top_p",
            "llm.providers.x.top_k",
            "llm.providers.x.repetition_penalty",
        ):
            self.assertIsNone(_validate_sampling_field(key, None))

    def test_valid_temperature_passes(self) -> None:
        for value in (0, 0.0, 0.5, 1.0, 2.0):
            self.assertIsNone(
                _validate_sampling_field("llm.providers.x.temperature", value)
            )

    def test_negative_temperature_is_rejected(self) -> None:
        message = _validate_sampling_field(
            "llm.providers.x.temperature", -0.1
        )
        self.assertIsNotNone(message)
        self.assertIn("temperature", message or "")

    def test_top_p_must_be_in_open_zero_one(self) -> None:
        self.assertIsNone(_validate_sampling_field("agents.auditor.llm.top_p", 0.5))
        self.assertIsNone(_validate_sampling_field("agents.auditor.llm.top_p", 1.0))
        self.assertIsNotNone(
            _validate_sampling_field("agents.auditor.llm.top_p", 0.0)
        )
        self.assertIsNotNone(
            _validate_sampling_field("agents.auditor.llm.top_p", 1.01)
        )
        self.assertIsNotNone(
            _validate_sampling_field("agents.auditor.llm.top_p", -0.1)
        )

    def test_top_k_must_be_positive_integer(self) -> None:
        self.assertIsNone(_validate_sampling_field("llm.providers.x.top_k", 1))
        self.assertIsNone(_validate_sampling_field("llm.providers.x.top_k", 40))
        self.assertIsNotNone(_validate_sampling_field("llm.providers.x.top_k", 0))
        self.assertIsNotNone(
            _validate_sampling_field("llm.providers.x.top_k", 1.5)
        )
        self.assertIsNotNone(_validate_sampling_field("llm.providers.x.top_k", True))

    def test_repetition_penalty_must_be_positive(self) -> None:
        self.assertIsNone(
            _validate_sampling_field("llm.providers.x.repetition_penalty", 1.1)
        )
        self.assertIsNotNone(
            _validate_sampling_field("llm.providers.x.repetition_penalty", 0)
        )
        self.assertIsNotNone(
            _validate_sampling_field("llm.providers.x.repetition_penalty", -0.1)
        )


class ValidateSamplingPayloadTests(unittest.TestCase):
    def test_nested_payload_is_flattened_and_checked(self) -> None:
        errors = _validate_sampling_payload(
            {
                "llm": {
                    "providers": {
                        "shared": {"temperature": 0, "top_p": 0.9},
                        "vllm": {"top_k": 40, "repetition_penalty": 1.05},
                    },
                },
                "agents": {
                    "validator": {"llm": {"temperature": 0}},
                },
            }
        )
        self.assertEqual(errors, [])

    def test_reports_one_error_per_offending_key(self) -> None:
        errors = _validate_sampling_payload(
            {
                "llm": {
                    "providers": {
                        "shared": {"temperature": -1, "top_p": 1.1},
                    },
                },
                "agents": {
                    "auditor": {"llm": {"top_k": 0}},
                },
            }
        )
        self.assertEqual(len(errors), 3)
        joined = " | ".join(errors)
        self.assertIn("llm.providers.shared.temperature", joined)
        self.assertIn("llm.providers.shared.top_p", joined)
        self.assertIn("agents.auditor.llm.top_k", joined)


class ForbiddenApiKeyTests(unittest.TestCase):
    def test_provider_api_key_is_always_forbidden_from_ui(self) -> None:
        forbidden = forbidden_keys_in_payload(
            {
                "llm": {
                    "providers": {
                        "shared": {"api_key": "secret"},
                    },
                },
            }
        )
        self.assertIn("llm.providers.shared.api_key", forbidden)

    def test_remote_db_endpoints_are_forbidden(self) -> None:
        forbidden = forbidden_keys_in_payload(
            {
                "reportdb": {"remote": {"url": "postgres://x"}},
                "graph": {"db": {"remote": {"url": "bolt://y"}}},
            }
        )
        self.assertIn("reportdb.remote.url", forbidden)
        self.assertIn("graph.db.remote.url", forbidden)

    def test_regular_editable_keys_are_not_forbidden(self) -> None:
        forbidden = forbidden_keys_in_payload(
            {
                "llm": {"default_provider": "shared"},
                "logging": {"level": "debug"},
                "agents": {
                    "validator": {"llm": {"temperature": 0}},
                },
            }
        )
        self.assertEqual(forbidden, [])


class NumericRangeRegistryTests(unittest.TestCase):
    """The registry must cover every UI-editable numeric key so client and
    server validation stay in lock-step. Keys are matched by exact dot-path
    OR by trailing segment (so e.g. ``temperature`` matches both
    ``llm.providers.x.temperature`` and ``agents.auditor.llm.temperature``)."""

    def test_registry_covers_every_design_d9_key(self) -> None:
        for key in (
            "audit.worker_count",
            "graph.build.max_file_bytes",
            "graph.build.paths_max_depth",
            "graph.build.paths_max_count",
            "graph.build.neo4j_chunk_size",
            "coder.concurrency",
            "coder.request_timeout_seconds",
            "coder.poll_interval_seconds",
            "coder.preflight_timeout_seconds",
            # Trailing-segment matches:
            "temperature",
            "top_p",
            "top_k",
            "repetition_penalty",
            "request_timeout_seconds",
        ):
            self.assertIn(key, NUMERIC_RANGE_REGISTRY, msg=key)


class ValidateNumericPayloadTests(unittest.TestCase):
    def test_audit_worker_count_in_range(self) -> None:
        self.assertEqual(_validate_numeric_payload({"audit": {"worker_count": 8}}), [])

    def test_audit_worker_count_below_minimum(self) -> None:
        errors = _validate_numeric_payload({"audit": {"worker_count": 0}})
        self.assertEqual(len(errors), 1)
        self.assertIn("audit.worker_count", errors[0])
        self.assertIn("[1, 16]", errors[0])

    def test_audit_worker_count_above_maximum(self) -> None:
        errors = _validate_numeric_payload({"audit": {"worker_count": 32}})
        self.assertEqual(len(errors), 1)
        self.assertIn("audit.worker_count", errors[0])
        self.assertIn("[1, 16]", errors[0])

    def test_coder_concurrency_range(self) -> None:
        self.assertEqual(
            _validate_numeric_payload({"coder": {"concurrency": 16}}), []
        )
        self.assertEqual(len(_validate_numeric_payload({"coder": {"concurrency": 0}})), 1)
        self.assertEqual(len(_validate_numeric_payload({"coder": {"concurrency": 65}})), 1)

    def test_graph_build_neo4j_chunk_size_bounds(self) -> None:
        self.assertEqual(
            _validate_numeric_payload(
                {"graph": {"build": {"neo4j_chunk_size": 5000}}}
            ),
            [],
        )
        errors = _validate_numeric_payload(
            {"graph": {"build": {"neo4j_chunk_size": 50}}}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("graph.build.neo4j_chunk_size", errors[0])

    def test_request_timeout_seconds_must_be_positive(self) -> None:
        self.assertEqual(
            _validate_numeric_payload(
                {
                    "llm": {"providers": {"x": {"request_timeout_seconds": 600}}},
                    "agents": {"auditor": {"llm": {"request_timeout_seconds": 1.5}}},
                }
            ),
            [],
        )
        errors = _validate_numeric_payload(
            {"llm": {"providers": {"x": {"request_timeout_seconds": 0}}}}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("llm.providers.x.request_timeout_seconds", errors[0])

    def test_existing_sampling_rules_still_apply(self) -> None:
        # _validate_numeric_payload subsumes _validate_sampling_payload's rules.
        errors = _validate_numeric_payload(
            {
                "llm": {"providers": {"x": {"temperature": -0.1, "top_p": 1.5, "top_k": 0}}},
            }
        )
        self.assertEqual(len(errors), 3)
        joined = " | ".join(errors)
        self.assertIn("llm.providers.x.temperature", joined)
        self.assertIn("llm.providers.x.top_p", joined)
        self.assertIn("llm.providers.x.top_k", joined)


class CollectEnvOverridesTests(unittest.TestCase):
    """``_collect_env_overrides`` walks ``os.environ``-shaped input and
    returns a flat dotted-key dict that ``resolve_effective`` accepts."""

    def test_returns_empty_dict_when_no_env_keys(self) -> None:
        self.assertEqual(_collect_env_overrides({}), {})

    def test_picks_up_known_env_key(self) -> None:
        env = {"XAUDITOR_LOGGING_LEVEL": "debug"}
        overrides = _collect_env_overrides(env)
        self.assertEqual(overrides.get("logging.level"), "debug")

    def test_picks_up_neo4j_chunk_size_opt_out(self) -> None:
        env = {"XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE": "10000"}
        overrides = _collect_env_overrides(env)
        self.assertEqual(overrides.get("graph.build.neo4j_chunk_size"), "10000")

    def test_ignores_unrelated_env_vars(self) -> None:
        env = {"PATH": "/usr/bin", "HOME": "/home/x"}
        self.assertEqual(_collect_env_overrides(env), {})

    def test_picks_up_audit_worker_count(self) -> None:
        env = {"XAUDITOR_AUDIT_WORKER_COUNT": "4"}
        overrides = _collect_env_overrides(env)
        self.assertEqual(overrides.get("audit.worker_count"), "4")


if __name__ == "__main__":
    unittest.main()
