"""``coder.workspace_root`` + ``coder.project_name`` + deprecation-shim tests.

Covers the new config fields added by the ``multi-project-coder-service``
change:

- ``workspace_root`` only / ``project_name`` only: passes through verbatim
  into ``effective_*`` fields; no WARN.
- ``repo_mount_path`` only: emits a one-time deprecation WARN and derives
  ``effective_workspace_root = parent(repo_mount_path)``,
  ``effective_project_name = basename(repo_mount_path)``.
- Both set: prefers ``workspace_root``; emits a one-time WARN naming
  ``repo_mount_path`` as the ignored field.
- ``project_name`` allowlist rejection at ``ConfigError`` time.
- Env-overlay paths for the two new env vars.

The shim WARN bookkeeping uses a process-global set
(``_CODER_WORKSPACE_WARNED``); each test resets it so warning emission
is observable independently.
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

import xauditor.config as cfgmod
from xauditor.config import (
    ConfigError,
    _resolve_coder_workspace,
    load_config,
)


def _yaml(body: str) -> Path:
    """Write ``body`` to a temp xauditor.yml and return its Path."""

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, encoding="utf-8"
    )
    tmp.write(textwrap.dedent(body))
    tmp.close()
    return Path(tmp.name)


def _reset_shim_warnings() -> None:
    cfgmod._CODER_WORKSPACE_WARNED.clear()


class WorkspaceRootOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_shim_warnings()

    def test_workspace_root_alone_drives_effective_fields(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /home/user/coder-workspace
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                cfg = load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        self.assertEqual(cfg.coder.workspace_root, "/home/user/coder-workspace")
        self.assertEqual(cfg.coder.project_name, "")
        self.assertEqual(
            cfg.coder.effective_workspace_root, "/home/user/coder-workspace"
        )
        # project_name unset → audit-startup will infer; the shim does
        # not populate it from anything.
        self.assertEqual(cfg.coder.effective_project_name, "")
        self.assertNotIn("deprecated", stderr.getvalue())

    def test_workspace_root_plus_project_name_passes_through(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /home/user/coder-workspace
              project_name: secmind
            """
        )
        try:
            cfg = load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        self.assertEqual(cfg.coder.project_name, "secmind")
        self.assertEqual(cfg.coder.effective_project_name, "secmind")


class RepoMountPathDeprecationShimTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_shim_warnings()

    def test_repo_mount_path_only_drives_shim_with_warn(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              repo_mount_path: /home/user/git/secmind
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                cfg = load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        self.assertEqual(cfg.coder.repo_mount_path, "/home/user/git/secmind")
        self.assertEqual(cfg.coder.workspace_root, "")
        self.assertEqual(cfg.coder.effective_workspace_root, "/home/user/git")
        self.assertEqual(cfg.coder.effective_project_name, "secmind")
        # WARN names the replacement field and the migration target.
        warn = stderr.getvalue()
        self.assertIn("WARN", warn)
        self.assertIn("coder.repo_mount_path", warn)
        self.assertIn("coder.workspace_root", warn)
        self.assertIn("/home/user/git", warn)

    def test_warn_fires_once_per_process(self) -> None:
        """Two consecutive load_config calls SHALL only WARN once.

        Operators running the binary for many audits in the same
        process (tests, scripts) shouldn't see the same WARN line
        repeated.
        """

        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              repo_mount_path: /home/user/git/secmind
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        # exactly one occurrence of the deprecation marker.
        self.assertEqual(stderr.getvalue().count("coder.repo_mount_path is deprecated"), 1)

    def test_both_set_warns_with_repo_mount_path_ignored(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /home/user/coder-workspace
              repo_mount_path: /home/user/git/secmind
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                cfg = load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        self.assertEqual(
            cfg.coder.effective_workspace_root, "/home/user/coder-workspace"
        )
        self.assertEqual(cfg.coder.effective_project_name, "")
        warn = stderr.getvalue()
        self.assertIn("WARN", warn)
        self.assertIn("ignored", warn)
        self.assertIn("coder.repo_mount_path", warn)


class ProjectNameAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_shim_warnings()

    def test_traversal_segment_rejected(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /tmp/ws
              project_name: ../etc
            """
        )
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        self.assertIn("coder.project_name", str(ctx.exception))

    def test_slash_rejected(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /tmp/ws
              project_name: monorepo/sub
            """
        )
        try:
            with self.assertRaises(ConfigError):
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()

    def test_whitespace_rejected(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /tmp/ws
              project_name: foo bar
            """
        )
        try:
            with self.assertRaises(ConfigError):
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()

    def test_allowlisted_chars_pass(self) -> None:
        for name in ("secmind", "alpha-1", "a.b_c", "X9-Y", "0-1.2_3"):
            path = _yaml(
                f"""
                llm:
                  default_provider: shared
                  providers:
                    shared:
                      base_url: https://x/v1
                      api_key: k
                      model_name: m
                coder:
                  enabled: true
                  transport: http
                  workspace_root: /tmp/ws
                  project_name: {name}
                """
            )
            try:
                cfg = load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
                self.assertEqual(cfg.coder.project_name, name, name)
            finally:
                path.unlink()


class EnvOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_shim_warnings()

    def test_workspace_root_env_overlays_yaml(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
            """
        )
        try:
            cfg = load_config(
                repo_root=Path("/tmp/repo"),
                config_path=path,
                env={"XAUDITOR_CODER_WORKSPACE_ROOT": "/from/env"},
            )
        finally:
            path.unlink()
        self.assertEqual(cfg.coder.workspace_root, "/from/env")
        self.assertEqual(cfg.coder.effective_workspace_root, "/from/env")

    def test_project_name_env_overlays_yaml(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /tmp/ws
              project_name: yaml-name
            """
        )
        try:
            cfg = load_config(
                repo_root=Path("/tmp/repo"),
                config_path=path,
                env={"XAUDITOR_CODER_PROJECT_NAME": "env-name"},
            )
        finally:
            path.unlink()
        self.assertEqual(cfg.coder.project_name, "env-name")
        self.assertEqual(cfg.coder.effective_project_name, "env-name")

    def test_empty_env_value_is_treated_as_unset(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /from/yaml
            """
        )
        try:
            cfg = load_config(
                repo_root=Path("/tmp/repo"),
                config_path=path,
                env={"XAUDITOR_CODER_WORKSPACE_ROOT": "   "},
            )
        finally:
            path.unlink()
        self.assertEqual(cfg.coder.workspace_root, "/from/yaml")


class WorkspaceUnderWrongTransportWarnTests(unittest.TestCase):
    """workspace_root / project_name only apply under transport: http
    AND enabled: true. When the operator sets them but forgets one,
    emit a one-time WARN naming the missing prerequisite."""

    def setUp(self) -> None:
        _reset_shim_warnings()

    def test_workspace_root_under_subprocess_warns(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              workspace_root: /home/user/coder-workspace
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                cfg = load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        warn = stderr.getvalue()
        self.assertIn("WARN", warn)
        self.assertIn("ignored", warn)
        self.assertIn("transport: subprocess", warn)
        self.assertIn("transport: http", warn)
        # The field IS still populated on the dataclass (callers that
        # explicitly use it can; we just nudge the typical operator).
        self.assertEqual(cfg.coder.workspace_root, "/home/user/coder-workspace")

    def test_workspace_root_with_coder_disabled_warns(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              transport: http
              enabled: false
              workspace_root: /home/user/coder-workspace
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        warn = stderr.getvalue()
        self.assertIn("coder.enabled is false", warn)
        self.assertIn("Set coder.enabled: true", warn)

    def test_correct_config_does_not_warn(self) -> None:
        path = _yaml(
            """
            llm:
              default_provider: shared
              providers:
                shared:
                  base_url: https://x/v1
                  api_key: k
                  model_name: m
            coder:
              enabled: true
              transport: http
              workspace_root: /home/user/coder-workspace
            """
        )
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                load_config(repo_root=Path("/tmp/repo"), config_path=path, env={})
        finally:
            path.unlink()
        # No "ignored" / "enabled is false" warnings under the correct
        # multi-project config.
        warn = stderr.getvalue()
        self.assertNotIn("ignored", warn)
        self.assertNotIn("coder.enabled is false", warn)


class ResolveCoderWorkspaceUnitTests(unittest.TestCase):
    """Direct-call coverage for ``_resolve_coder_workspace`` distinct from
    the load_config-driven cases above. Exercises edge inputs the YAML
    layer wouldn't naturally produce."""

    def setUp(self) -> None:
        _reset_shim_warnings()

    def test_neither_returns_empty_pair_no_warn(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            ws, name = _resolve_coder_workspace(
                workspace_root="", project_name="", repo_mount_path=""
            )
        self.assertEqual((ws, name), ("", ""))
        self.assertEqual(stderr.getvalue(), "")

    def test_workspace_with_explicit_project_no_warn(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            ws, name = _resolve_coder_workspace(
                workspace_root="/ws", project_name="p", repo_mount_path=""
            )
        self.assertEqual((ws, name), ("/ws", "p"))
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
