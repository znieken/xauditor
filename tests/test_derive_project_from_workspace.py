"""Tests for `preflight.derive_project_from_workspace` + the
agentic-side auto-derivation in `build_stage_runner`.

The verification coder and the agentic stage transport both
land project names from `repo_root` relative to
`coder.workspace_root`. Operators no longer need to repeat the
project name in `audit.agentic.transport.coder_service.project`
unless they want to override the derivation explicitly.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.preflight import derive_project_from_workspace
from xauditor.audit.stage_runner import build_stage_runner
from xauditor.config import (
    AuditAgenticConfig,
    AuditAgenticTransportConfig,
    AuditAgenticTransportCoderServiceConfig,
    AuditModeConfig,
    CoderConfig,
)


class _StubTransport:
    async def invoke(self, *args, **kwargs) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        return None


class DeriveProjectFromWorkspaceTests(unittest.TestCase):
    def test_explicit_override_always_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "team" / "repo").mkdir(parents=True)
            project = derive_project_from_workspace(
                workspace_root=str(ws),
                repo_root=ws / "team" / "repo",
                override="explicit-name",
            )
        self.assertEqual(project, "explicit-name")

    def test_repo_root_inside_workspace_root_emits_relative_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "team" / "repo").mkdir(parents=True)
            project = derive_project_from_workspace(
                workspace_root=str(ws),
                repo_root=ws / "team" / "repo",
            )
        self.assertEqual(project, "team/repo")

    def test_repo_root_at_workspace_root_falls_back_to_basename(
        self,
    ) -> None:
        # When repo_root == workspace_root (single-repo container
        # layout where the bind-mount target IS the audit repo),
        # we fall back to basename — `relative_to` of self yields
        # ``.`` which is meaningless as a project name.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "myapp"
            ws.mkdir()
            project = derive_project_from_workspace(
                workspace_root=str(ws),
                repo_root=ws,
            )
        self.assertEqual(project, "myapp")

    def test_repo_root_outside_workspace_root_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            other = Path(tmp) / "elsewhere" / "myapp"
            other.mkdir(parents=True)
            project = derive_project_from_workspace(
                workspace_root=str(ws),
                repo_root=other,
            )
        self.assertEqual(project, "myapp")

    def test_empty_workspace_root_uses_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myapp"
            repo.mkdir()
            project = derive_project_from_workspace(
                workspace_root="",
                repo_root=repo,
            )
        self.assertEqual(project, "myapp")

    def test_nested_three_level_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "a" / "b" / "c").mkdir(parents=True)
            project = derive_project_from_workspace(
                workspace_root=str(ws),
                repo_root=ws / "a" / "b" / "c",
            )
        self.assertEqual(project, "a/b/c")


def _agentic_mode(*, project: str = "") -> AuditModeConfig:
    return AuditModeConfig(
        stages_form="agentic",
        agentic=AuditAgenticConfig(
            timeout_seconds=600,
            transport=AuditAgenticTransportConfig(
                kind="coder_service",
                coder_service=AuditAgenticTransportCoderServiceConfig(
                    endpoint="http://127.0.0.1:8090",
                    bearer_token_env="",
                    request_timeout_safety_seconds=60,
                    project=project,
                ),
            ),
        ),
    )


def _coder_cfg(*, workspace_root: str = "") -> CoderConfig:
    return CoderConfig(
        enabled=True,
        transport="http",
        endpoint="http://127.0.0.1:8090",
        workspace_root=workspace_root,
        effective_workspace_root=workspace_root,
    )


class BuildStageRunnerProjectDerivationTests(unittest.TestCase):
    def test_explicit_agentic_project_wins_over_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "team" / "repo").mkdir(parents=True)
            runner, _ = build_stage_runner(
                audit_mode=_agentic_mode(project="explicit-name"),
                analyzer_agent=None,
                validator_agent=None,
                exploitation_agent=None,
                transport=_StubTransport(),
                coder_cfg=_coder_cfg(workspace_root=str(ws)),
                repo_root=ws / "team" / "repo",
            )
        self.assertEqual(runner.project, "explicit-name")

    def test_derives_project_when_explicit_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "myapp").mkdir(parents=True)
            runner, _ = build_stage_runner(
                audit_mode=_agentic_mode(project=""),
                analyzer_agent=None,
                validator_agent=None,
                exploitation_agent=None,
                transport=_StubTransport(),
                coder_cfg=_coder_cfg(workspace_root=str(ws)),
                repo_root=ws / "myapp",
            )
        self.assertEqual(runner.project, "myapp")

    def test_no_coder_cfg_passed_keeps_explicit_project_only(
        self,
    ) -> None:
        # Legacy callers that didn't pass coder_cfg + repo_root
        # still get the explicit override behaviour, just without
        # auto-derivation. project stays None when explicit empty.
        runner, _ = build_stage_runner(
            audit_mode=_agentic_mode(project="explicit"),
            analyzer_agent=None,
            validator_agent=None,
            exploitation_agent=None,
            transport=_StubTransport(),
        )
        self.assertEqual(runner.project, "explicit")

    def test_no_coder_cfg_passed_with_empty_explicit_yields_none(
        self,
    ) -> None:
        runner, _ = build_stage_runner(
            audit_mode=_agentic_mode(project=""),
            analyzer_agent=None,
            validator_agent=None,
            exploitation_agent=None,
            transport=_StubTransport(),
        )
        self.assertIsNone(runner.project)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
