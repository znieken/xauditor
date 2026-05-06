"""Unit tests for the legacy `teaming.*` → `audit.*` migration shim.

Covers `restructure-audit-modes-and-coverage` Phase 1 tasks 1.6.1,
1.6.2, and 1.6.6:

- Mapping table: every legacy `teaming.*` field maps to its new
  `audit.*` location, with semantic preservation
  (`teaming.enabled: true` → `audit.mode: "deep"`,
  `teaming.<stage>.subagent_count` → `audit.replication.<stage>`,
  `teaming.validator.debate_rounds` → `audit.validator.debate.max_rounds`
  + implicit `debate.enabled: true`).
- `DeprecationWarning` is emitted exactly once per populated legacy
  field, naming the new field path in the warning text so an
  operator reading their stderr can mechanically rewrite their
  yaml.
- The `validator_teaming` prompt key was deleted in Phase 1B and
  the deletion is intentional — `get_prompt_definition("validator_teaming")`
  now raises `KeyError`.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import (
    AUDIT_MODE_VALUES,
    AuditModeConfig,
    _migrate_legacy_teaming,
    load_config,
)
from xauditor.prompts import get_prompt_definition


def _write(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


class MigrateLegacyTeamingMappingTests(unittest.TestCase):
    """Direct, side-effect-free tests of the in-place mutation table."""

    def test_empty_input_is_noop(self) -> None:
        data = {}
        _migrate_legacy_teaming(data)
        self.assertEqual(data, {})

    def test_teaming_block_absent_is_noop(self) -> None:
        data = {"audit": {"worker_count": 4}}
        _migrate_legacy_teaming(data)
        self.assertEqual(data, {"audit": {"worker_count": 4}})

    def test_teaming_enabled_true_maps_to_deep(self) -> None:
        data = {"teaming": {"enabled": True}}
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        self.assertEqual(data["audit"]["mode"], "deep")

    def test_teaming_enabled_false_maps_to_fast(self) -> None:
        data = {"teaming": {"enabled": False}}
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        self.assertEqual(data["audit"]["mode"], "fast")

    def test_per_stage_subagent_count_maps_to_replication(self) -> None:
        data = {
            "teaming": {
                "enabled": True,
                "analyzer": {"subagent_count": 5},
                "validator": {"subagent_count": 4},
                "exploiter": {"subagent_count": 2},
            }
        }
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        self.assertEqual(data["audit"]["replication"]["analyzer"], 5)
        self.assertEqual(data["audit"]["replication"]["validator"], 4)
        self.assertEqual(data["audit"]["replication"]["exploiter"], 2)

    def test_validator_debate_rounds_maps_and_implies_enabled(self) -> None:
        data = {
            "teaming": {
                "enabled": True,
                "validator": {"subagent_count": 2, "debate_rounds": 7},
            }
        }
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        debate = data["audit"]["validator"]["debate"]
        self.assertEqual(debate["max_rounds"], 7)
        # Setting debate_rounds historically implied debate-on; the
        # shim mirrors that under the new shape.
        self.assertTrue(debate["enabled"])

    def test_existing_audit_mode_is_not_overwritten(self) -> None:
        """Operator who set both shapes wins with the new shape."""

        data = {
            "audit": {"mode": "fast"},
            "teaming": {
                "enabled": True,  # would imply "deep" but mode already set
            },
        }
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        self.assertEqual(data["audit"]["mode"], "fast")

    def test_existing_replication_field_is_not_overwritten(self) -> None:
        data = {
            "audit": {"replication": {"analyzer": 9}},
            "teaming": {
                "enabled": True,
                "analyzer": {"subagent_count": 3},
            },
        }
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        # New-shape value wins.
        self.assertEqual(data["audit"]["replication"]["analyzer"], 9)

    def test_provider_list_intentionally_not_migrated(self) -> None:
        """Provider rotation lives on legacy shape until Phase 1B+ rewrite."""

        data = {
            "teaming": {
                "enabled": True,
                "analyzer": {
                    "subagent_count": 3,
                    "provider_list": ["p1", "p2"],
                },
            }
        }
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _migrate_legacy_teaming(data)
        # Only the subagent_count was migrated; provider_list is NOT
        # in the new audit.replication or anywhere else under audit.*.
        self.assertEqual(data["audit"]["replication"]["analyzer"], 3)
        self.assertNotIn("provider_list", data.get("audit", {}))


class DeprecationWarningEmissionTests(unittest.TestCase):
    """`load_config` emits a `DeprecationWarning` per legacy field present."""

    def test_no_warning_when_audit_shape_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(
                repo,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1: {base_url: mock://p1, api_key: k, model_name: m}
                audit:
                  mode: deep
                  replication:
                    analyzer: 3
                """,
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                load_config(repo_root=repo, env={"HOME": tmp})
            depr = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertEqual(depr, [])

    def test_one_warning_per_legacy_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(
                repo,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1: {base_url: mock://p1, api_key: k, model_name: m}
                    p2: {base_url: mock://p2, api_key: k, model_name: m}
                teaming:
                  enabled: true
                  analyzer: {subagent_count: 5, provider_list: [p1, p2]}
                  validator: {subagent_count: 4, provider_list: [p1], debate_rounds: 7}
                  exploiter: {subagent_count: 2, provider_list: [p2]}
                """,
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                load_config(repo_root=repo, env={"HOME": tmp})

            depr_messages = [
                str(w.message)
                for w in caught
                if issubclass(w.category, DeprecationWarning)
            ]
            # Nine legacy fields populated (Phase 1B added the three
            # provider_list migrations):
            #   teaming.enabled, teaming.<3-stage>.subagent_count,
            #   teaming.validator.debate_rounds, the "implies
            #   debate.enabled" notice, plus
            #   teaming.<3-stage>.provider_list.
            self.assertEqual(len(depr_messages), 9)
            joined = "\n".join(depr_messages)
            self.assertIn("teaming.enabled → audit.mode", joined)
            self.assertIn(
                "teaming.analyzer.subagent_count → audit.replication.analyzer",
                joined,
            )
            self.assertIn(
                "teaming.validator.subagent_count → audit.replication.validator",
                joined,
            )
            self.assertIn(
                "teaming.exploiter.subagent_count → audit.replication.exploiter",
                joined,
            )
            self.assertIn(
                "teaming.validator.debate_rounds → audit.validator.debate.max_rounds",
                joined,
            )
            self.assertIn("audit.validator.debate.enabled = true", joined)
            self.assertIn(
                "teaming.analyzer.provider_list → audit.analyzer.provider_list",
                joined,
            )
            self.assertIn(
                "teaming.validator.provider_list → audit.validator.provider_list",
                joined,
            )
            self.assertIn(
                "teaming.exploiter.provider_list → audit.exploiter.provider_list",
                joined,
            )

    def test_warning_message_names_the_new_field_path(self) -> None:
        """Each warning includes the new field path verbatim."""

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(
                repo,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1: {base_url: mock://p1, api_key: k, model_name: m}
                teaming:
                  enabled: false
                """,
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                load_config(repo_root=repo, env={"HOME": tmp})
            depr = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertEqual(len(depr), 1)
            self.assertIn("audit.mode", str(depr[0].message))

    def test_resolved_audit_mode_matches_expected_new_shape(self) -> None:
        """End-to-end: legacy yaml → load_config → AuditModeConfig values."""

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(
                repo,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1: {base_url: mock://p1, api_key: k, model_name: m}
                    p2: {base_url: mock://p2, api_key: k, model_name: m}
                teaming:
                  enabled: true
                  analyzer: {subagent_count: 5, provider_list: [p1, p2]}
                  validator: {subagent_count: 4, provider_list: [p1], debate_rounds: 7}
                  exploiter: {subagent_count: 2, provider_list: [p2]}
                """,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                cfg = load_config(repo_root=repo, env={"HOME": tmp})
        self.assertIsInstance(cfg.audit_mode, AuditModeConfig)
        self.assertEqual(cfg.audit_mode.mode, "deep")
        self.assertEqual(cfg.audit_mode.replication.analyzer, 5)
        self.assertEqual(cfg.audit_mode.replication.validator, 4)
        self.assertEqual(cfg.audit_mode.replication.exploiter, 2)
        self.assertTrue(cfg.audit_mode.validator.debate.enabled)
        self.assertEqual(cfg.audit_mode.validator.debate.max_rounds, 7)

    def test_audit_mode_values_export(self) -> None:
        """Closed set is the contract `_build_audit_mode_config` validates against."""

        self.assertEqual(set(AUDIT_MODE_VALUES), {"fast", "deep"})


class ValidatorTeamingPromptRemovalTests(unittest.TestCase):
    def test_get_prompt_definition_raises_key_error(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            get_prompt_definition("validator_teaming")
        # The error names the missing key so a future re-introduction
        # is obvious.
        self.assertIn("validator_teaming", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
