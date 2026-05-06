"""Tests for `_resolve_runtime_coder_cfg` -- workflow's verification-
coder project-name auto-derivation.

The verification coder (HttpCoderTransport) reads
`coder.effective_project_name` and POSTs it to coder-service. When
the operator sets `coder.workspace_root` but not `coder.project_name`,
config-load leaves `effective_project_name=""`. Without runtime
derivation, claude runs in `/workspace` (the whole bind mount) and
its tool calls walk every project on the host -- catastrophic on
slow filesystems.

This regression locks in the agentic-style derivation behaviour
the verification coder gained in
`audit.workflow._resolve_runtime_coder_cfg`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.workflow import _resolve_runtime_coder_cfg
from xauditor.config import CoderConfig


def _coder_cfg(
    *,
    enabled: bool = True,
    transport: str = "http",
    workspace_root: str = "",
    project_name: str = "",
) -> CoderConfig:
    return CoderConfig(
        enabled=enabled,
        transport=transport,
        endpoint="http://127.0.0.1:8090" if transport == "http" else "",
        workspace_root=workspace_root,
        effective_workspace_root=workspace_root,
        project_name=project_name,
        effective_project_name=project_name,
    )


class ResolveRuntimeCoderCfgTests(unittest.TestCase):
    def test_derives_relative_path_when_repo_root_under_workspace_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "team" / "xauditor").mkdir(parents=True)
            cfg = _coder_cfg(workspace_root=str(ws))
            out = _resolve_runtime_coder_cfg(
                cfg, repo_root=ws / "team" / "xauditor"
            )
        self.assertEqual(out.effective_project_name, "team/xauditor")
        self.assertEqual(out.effective_workspace_root, cfg.effective_workspace_root)

    def test_explicit_project_name_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "myapp").mkdir(parents=True)
            cfg = _coder_cfg(
                workspace_root=str(ws),
                project_name="manual-override",
            )
            out = _resolve_runtime_coder_cfg(cfg, repo_root=ws / "myapp")
        self.assertEqual(out.effective_project_name, "manual-override")

    def test_returns_unchanged_when_coder_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "myapp").mkdir(parents=True)
            cfg = _coder_cfg(enabled=False, workspace_root=str(ws))
            out = _resolve_runtime_coder_cfg(cfg, repo_root=ws / "myapp")
        # Disabled -- resolve_coder_project returns None -- helper
        # leaves the cfg untouched.
        self.assertEqual(out.effective_project_name, "")

    def test_returns_unchanged_when_subprocess_transport(self) -> None:
        # subprocess transport doesn't have a project namespace --
        # claude runs locally with cwd=repo_root directly.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "myapp").mkdir(parents=True)
            cfg = _coder_cfg(transport="subprocess", workspace_root=str(ws))
            out = _resolve_runtime_coder_cfg(cfg, repo_root=ws / "myapp")
        self.assertEqual(out.effective_project_name, "")

    def test_derives_basename_when_workspace_root_empty(self) -> None:
        # Single-repo container layout (no workspace_root): falls
        # back to basename(repo_root) so the dispatcher still has
        # *some* identifier the coder-service can validate.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myapp"
            repo.mkdir()
            cfg = _coder_cfg(workspace_root="")
            out = _resolve_runtime_coder_cfg(cfg, repo_root=repo)
        # workspace_root empty -> resolve_coder_project still
        # returns the basename via derive_project_from_workspace.
        self.assertEqual(out.effective_project_name, "myapp")

    def test_three_level_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "a" / "b" / "c").mkdir(parents=True)
            cfg = _coder_cfg(workspace_root=str(ws))
            out = _resolve_runtime_coder_cfg(
                cfg, repo_root=ws / "a" / "b" / "c"
            )
        self.assertEqual(out.effective_project_name, "a/b/c")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
