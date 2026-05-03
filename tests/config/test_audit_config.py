"""``audit.worker_count`` yaml + env tests.

0.10.0 ``consolidate-on-worker-count`` collapsed the two-knob
``audit.path_concurrency`` + ``audit.worker_count`` model down to a
single knob (``worker_count``). This file covers:

- ``audit.worker_count`` defaults, yaml parsing, env-var overlay, and
  out-of-range rejection.
- ``audit.path_concurrency`` (yaml) → ``ConfigError`` with a
  translation message walking the operator through the upgrade.
- ``XAUDITOR_AUDIT_PATH_CONCURRENCY`` env var → WARN-then-ignore so
  stale shell rcs don't block startup.
"""

from __future__ import annotations

import io
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.config import AuditConfig, ConfigError, load_config


def _write_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


_BASE_LLM = """
llm:
  default_provider: p1
  providers:
    p1:
      base_url: mock://p1
      api_key: k
      model_name: m
"""


class AuditConfigDefaultsTests(unittest.TestCase):
    def test_dataclass_defaults(self) -> None:
        cfg = AuditConfig()
        self.assertEqual(cfg.worker_count, 1)

    def test_omitted_block_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(repo_root, _BASE_LLM)
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.audit.worker_count, 1)


class AuditConfigYamlTests(unittest.TestCase):
    def test_inline_mode_explicit_value_parses(self) -> None:
        # worker_count == 1 → InlineExecutor at runtime.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    audit:
                      worker_count: 1
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.audit.worker_count, 1)

    def test_subprocess_mode_explicit_value_parses(self) -> None:
        # worker_count >= 2 → LocalSubprocessPool at runtime.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    audit:
                      worker_count: 4
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.audit.worker_count, 4)

    def test_worker_count_zero_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM + "\naudit:\n  worker_count: 0\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("audit.worker_count", str(ctx.exception))

    def test_worker_count_above_max_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM + "\naudit:\n  worker_count: 17\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("audit.worker_count", str(ctx.exception))


class AuditConfigEnvOverlayTests(unittest.TestCase):
    def test_env_overlays_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM + "\naudit:\n  worker_count: 1\n",
            )
            config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": tmp,
                    "XAUDITOR_AUDIT_WORKER_COUNT": "8",
                },
            )
            self.assertEqual(config.audit.worker_count, 8)

    def test_env_empty_string_does_not_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM + "\naudit:\n  worker_count: 4\n",
            )
            config = load_config(
                repo_root=repo_root,
                env={"HOME": tmp, "XAUDITOR_AUDIT_WORKER_COUNT": ""},
            )
            self.assertEqual(config.audit.worker_count, 4)

    def test_env_out_of_range_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(repo_root, _BASE_LLM)
            with self.assertRaises(ConfigError) as ctx:
                load_config(
                    repo_root=repo_root,
                    env={"HOME": tmp, "XAUDITOR_AUDIT_WORKER_COUNT": "32"},
                )
            self.assertIn("audit.worker_count", str(ctx.exception))


class AuditConfigPathConcurrencyRemovedTests(unittest.TestCase):
    """0.10.0 ``consolidate-on-worker-count``: ``path_concurrency`` is
    a removed field. yaml hard-fails; env var WARN-then-ignores."""

    def test_yaml_path_concurrency_raises_with_translation_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    audit:
                      path_concurrency: 4
                    """
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            message = str(ctx.exception)
            self.assertIn("audit.path_concurrency", message)
            self.assertIn("removed in 0.10.0", message)
            self.assertIn("worker_count", message)

    def test_yaml_path_concurrency_with_worker_count_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    audit:
                      path_concurrency: 4
                      worker_count: 1
                    """
                ),
            )
            with self.assertRaises(ConfigError):
                load_config(repo_root=repo_root, env={"HOME": tmp})

    def test_env_path_concurrency_emits_warn_and_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM + "\naudit:\n  worker_count: 1\n",
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                config = load_config(
                    repo_root=repo_root,
                    env={
                        "HOME": tmp,
                        "XAUDITOR_AUDIT_PATH_CONCURRENCY": "8",
                    },
                )
            self.assertEqual(config.audit.worker_count, 1)
            warn_text = stderr.getvalue()
            self.assertIn("XAUDITOR_AUDIT_PATH_CONCURRENCY", warn_text)
            self.assertIn("no longer recognized", warn_text)

    def test_env_path_concurrency_empty_string_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(repo_root, _BASE_LLM)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                load_config(
                    repo_root=repo_root,
                    env={
                        "HOME": tmp,
                        "XAUDITOR_AUDIT_PATH_CONCURRENCY": "",
                    },
                )
            # Empty string is treated as unset — no WARN line.
            self.assertNotIn(
                "XAUDITOR_AUDIT_PATH_CONCURRENCY", stderr.getvalue()
            )


if __name__ == "__main__":
    unittest.main()
