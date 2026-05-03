"""``coder init`` workspace-root bind-mount + ``coder status`` projects discovery.

Covers task 8.1 (build_run_argv consumes ``effective_workspace_root``)
and task 8.3 (runtime_status populates ``projects`` from
``GET /projects`` with a host-side ``os.scandir`` fallback).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import CoderConfig
from xauditor.integrations.coder.helpers import build_run_argv
from xauditor.integrations.coder.runtime import CoderRuntimeManager
from xauditor.integrations.docker import CommandResult


class _Runner:
    """Minimal CommandRunner stand-in that scripts CommandResult per argv."""

    def __init__(self, scripts: list[tuple[list[str], CommandResult]]) -> None:
        self.scripts = list(scripts)
        self.invocations: list[list[str]] = []

    def __call__(self, args, *, timeout=None):
        self.invocations.append(list(args))
        for prefix, result in self.scripts:
            if args[: len(prefix)] == prefix:
                return result
        return CommandResult(returncode=0, stdout="", stderr="")


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail() -> CommandResult:
    return CommandResult(returncode=1, stdout="", stderr="")


class BuildRunArgvWorkspaceRootTests(unittest.TestCase):
    """§8.1 — bind-mount source comes from ``effective_workspace_root``."""

    def test_workspace_root_drives_bind_mount_source(self) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:9090",
            container_name="cs",
            container_image="img:tag",
            workspace_root="/host/coder-workspace",
            effective_workspace_root="/host/coder-workspace",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        # The implementation uses the established --volume<src>:<dst>:ro
        # syntax (matching the existing single-repo path); the spec's
        # task wording mentioned --mount type=bind but the codebase
        # convention is --volume, and the bind semantic is identical.
        self.assertIn("/host/coder-workspace:/workspace:ro", argv)
        # repo_root is NOT in the argv when workspace_root is set —
        # the workspace IS the source of truth.
        self.assertNotIn("/r:/workspace:ro", argv)

    def test_workspace_root_takes_precedence_over_repo_mount_path(self) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:9090",
            container_name="cs",
            container_image="img:tag",
            workspace_root="/host/coder-workspace",
            effective_workspace_root="/host/coder-workspace",
            repo_mount_path="/legacy/path",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        self.assertIn("/host/coder-workspace:/workspace:ro", argv)
        self.assertNotIn("/legacy/path:/workspace:ro", argv)

    def test_repo_mount_path_alone_drives_bind_when_workspace_root_unset(
        self,
    ) -> None:
        # Deprecation-shim parity: load_config would populate the
        # effective_* fields from the legacy field, but
        # build_run_argv ALSO falls back to repo_mount_path when its
        # caller passes a config that wasn't shimmed (e.g. older
        # call sites or unit tests constructing CoderConfig directly).
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:9090",
            container_name="cs",
            container_image="img:tag",
            repo_mount_path="/legacy/repo",
        )
        argv = build_run_argv(cfg, repo_root="/audit-root")
        self.assertIn("/legacy/repo:/workspace:ro", argv)
        self.assertNotIn("/audit-root:/workspace:ro", argv)

    def test_falls_back_to_repo_root_when_neither_workspace_nor_repo_mount_set(
        self,
    ) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:9090",
            container_name="cs",
            container_image="img:tag",
        )
        argv = build_run_argv(cfg, repo_root="/audit-root")
        self.assertIn("/audit-root:/workspace:ro", argv)


class RuntimeStatusProjectsDiscoveryTests(unittest.TestCase):
    """§8.3 — runtime_status populates ``projects`` (service or host scan)."""

    def _mgr(
        self, *, workspace_root: str, runner: _Runner | None = None
    ) -> CoderRuntimeManager:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            container_image="img:tag",
            workspace_root=workspace_root,
            effective_workspace_root=workspace_root,
        )
        return CoderRuntimeManager(
            cfg,
            repo_root=Path("/r"),
            command_runner=runner or _Runner([]),
        )

    def test_projects_from_service_when_container_running(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id": "sha256:x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "--format"],
                    _ok(stdout="true\n"),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{"State": {"Running": true}}]'),
                ),
            ]
        )
        mgr = self._mgr(workspace_root="/ws", runner=runner)
        # Service responds to /health and /projects.
        with mock.patch.object(
            CoderRuntimeManager,
            "_probe_health_safely",
            return_value=(200, "claude 2.1.0", 0, None),
        ), mock.patch.object(
            CoderRuntimeManager,
            "_probe_projects",
            return_value=["alpha", "secmind"],
        ):
            status = mgr.runtime_status()
        self.assertEqual(status.workspace_root, "/ws")
        self.assertEqual(status.projects, ("alpha", "secmind"))
        self.assertEqual(status.projects_source, "service")

    def test_projects_falls_back_to_host_scan_when_container_unreachable(
        self,
    ) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id": "sha256:x"}]'),
                ),
                # Container exists but stopped → status doesn't probe
                # health, falls through to host_scan.
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{"State": {"Running": false}}]'),
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "alpha").mkdir()
            (Path(tmp) / "secmind").mkdir()
            (Path(tmp) / ".cache").mkdir()  # dotfile-prefixed; filtered
            mgr = self._mgr(workspace_root=tmp, runner=runner)
            status = mgr.runtime_status()
        self.assertEqual(status.workspace_root, tmp)
        self.assertEqual(set(status.projects), {"alpha", "secmind"})
        self.assertEqual(status.projects_source, "host_scan")

    def test_no_workspace_root_returns_empty_projects(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id": "sha256:x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{"State": {"Running": false}}]'),
                ),
            ]
        )
        mgr = self._mgr(workspace_root="", runner=runner)
        status = mgr.runtime_status()
        self.assertEqual(status.workspace_root, "")
        self.assertEqual(status.projects, ())
        self.assertEqual(status.projects_source, "")

    def test_host_scan_returns_empty_when_workspace_dir_missing(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id": "sha256:x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{"State": {"Running": false}}]'),
                ),
            ]
        )
        mgr = self._mgr(
            workspace_root="/does/not/exist/anywhere", runner=runner
        )
        status = mgr.runtime_status()
        self.assertEqual(status.projects, ())
        # ``""`` means we couldn't sourcing — neither service nor host
        # scan produced a list.
        self.assertEqual(status.projects_source, "")

    def test_projects_filters_dotfile_entries_in_host_scan(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id": "sha256:x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{"State": {"Running": false}}]'),
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "real").mkdir()
            (Path(tmp) / ".hidden").mkdir()
            (Path(tmp) / "..weird").mkdir()
            mgr = self._mgr(workspace_root=tmp, runner=runner)
            status = mgr.runtime_status()
        self.assertEqual(status.projects, ("real",))
        self.assertEqual(status.projects_source, "host_scan")


class InitWorkspaceMismatchRefusalTests(unittest.TestCase):
    """§7.2 / §8.2 — coder init refuses when an existing container's
    /workspace mount differs from the configured workspace_root.

    The bug this guards against: bind-mounts are baked at docker run
    time. A `docker start` on an existing container preserves the OLD
    bind-mount source, so running `coder init` after changing
    workspace_root in xauditor.yml silently keeps auditing the wrong
    tree. Without this refusal, the operator's mental model and
    claude's actual cwd silently disagree.
    """

    def _cfg(self, workspace_root: str) -> CoderConfig:
        return CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            container_image="img:tag",
            workspace_root=workspace_root,
            effective_workspace_root=workspace_root,
        )

    def test_mismatch_refuses_with_actionable_reset_hint(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id":"x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{}]'),  # container exists
                ),
                (
                    [
                        "docker", "container", "inspect", "--format",
                        '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}',
                    ],
                    _ok(stdout="/old/workspace\n"),
                ),
            ]
        )
        cfg = self._cfg("/new/workspace")
        from xauditor.errors import XAuditorError
        mgr = CoderRuntimeManager(
            cfg, repo_root=Path("/r"), command_runner=runner,
        )
        with self.assertRaises(XAuditorError) as ctx:
            mgr.init_runtime()
        msg = str(ctx.exception)
        self.assertIn("/old/workspace", msg)
        self.assertIn("/new/workspace", msg)
        self.assertIn("coder reset --yes", msg)
        self.assertIn("coder init", msg)

    def test_match_proceeds_to_start_and_health_probe(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id":"x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{}]'),  # container exists
                ),
                (
                    [
                        "docker", "container", "inspect", "--format",
                        '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}',
                    ],
                    _ok(stdout="/same/workspace\n"),
                ),
                (
                    ["docker", "container", "inspect", "--format", "{{.State.Running}}"],
                    _ok(stdout="true\n"),
                ),
            ]
        )
        cfg = self._cfg("/same/workspace")
        mgr = CoderRuntimeManager(
            cfg,
            repo_root=Path("/r"),
            command_runner=runner,
            http_probe=lambda _c: {"claude_cli_version": "x", "in_flight": 0},
        )
        # Should not raise; the existing-container branch proceeds to
        # docker start (no-op since already running) + health probe.
        mgr.init_runtime()

    def test_no_workspace_mount_data_does_not_refuse(self) -> None:
        # A container whose /workspace mount can't be read (older
        # docker, format change, mount missing) → don't refuse;
        # fall through to start + health probe. Better than blocking
        # the operator on a defensive check.
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id":"x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{}]'),
                ),
                (
                    [
                        "docker", "container", "inspect", "--format",
                        '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}',
                    ],
                    _ok(stdout=""),
                ),
                (
                    ["docker", "container", "inspect", "--format", "{{.State.Running}}"],
                    _ok(stdout="true\n"),
                ),
            ]
        )
        cfg = self._cfg("/new/workspace")
        mgr = CoderRuntimeManager(
            cfg,
            repo_root=Path("/r"),
            command_runner=runner,
            http_probe=lambda _c: {"claude_cli_version": "x", "in_flight": 0},
        )
        mgr.init_runtime()  # does not raise

    def test_no_workspace_root_configured_skips_check(self) -> None:
        # Legacy single-repo deployment: workspace_root unset →
        # the verifier returns silently regardless of what mount
        # the existing container has.
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id":"x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{}]'),
                ),
                # Mount-source inspect is NOT called — verifier exits
                # early on configured == "".
                (
                    ["docker", "container", "inspect", "--format", "{{.State.Running}}"],
                    _ok(stdout="true\n"),
                ),
            ]
        )
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            container_image="img:tag",
        )
        mgr = CoderRuntimeManager(
            cfg,
            repo_root=Path("/r"),
            command_runner=runner,
            http_probe=lambda _c: {"claude_cli_version": "x", "in_flight": 0},
        )
        mgr.init_runtime()  # does not raise


class NestedHostScanTests(unittest.TestCase):
    """§5.1 — _discover_projects falls back to walk_projects on host."""

    def _mgr(
        self, *, workspace_root: str, runner: _Runner | None = None
    ) -> CoderRuntimeManager:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            container_image="img:tag",
            workspace_root=workspace_root,
            effective_workspace_root=workspace_root,
        )
        return CoderRuntimeManager(
            cfg,
            repo_root=Path("/r"),
            command_runner=runner or _Runner([]),
        )

    def test_host_scan_yields_every_directory_at_every_depth(self) -> None:
        runner = _Runner(
            [
                (["docker", "version"], _ok()),
                (
                    ["docker", "image", "inspect", "img:tag"],
                    _ok(stdout='[{"Id": "sha256:x"}]'),
                ),
                (
                    ["docker", "container", "inspect", "cs"],
                    _ok(stdout='[{"State": {"Running": false}}]'),
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "flat").mkdir()
            (Path(tmp) / "team" / "repo").mkdir(parents=True)
            (Path(tmp) / "team" / "another").mkdir(parents=True)
            mgr = self._mgr(workspace_root=tmp, runner=runner)
            status = mgr.runtime_status()
        self.assertIn("flat", status.projects)
        self.assertIn("team", status.projects)
        self.assertIn("team/repo", status.projects)
        self.assertIn("team/another", status.projects)
        self.assertEqual(status.projects_source, "host_scan")


class RenderCoderStatusAnnotationTests(unittest.TestCase):
    """``_render_coder_status`` projects-line annotation behaviour."""

    def _status(self, *, projects, source):
        from xauditor.integrations.coder.runtime import CoderRuntimeStatus

        return CoderRuntimeStatus(
            transport="http",
            endpoint_kind="loopback",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            image="img:tag",
            image_present=True,
            container_exists=True,
            container_running=True,
            health_status=200,
            claude_cli_version="claude 2.1.0",
            in_flight=0,
            error=None,
            workspace_root="/ws",
            projects=projects,
            projects_source=source,
        )

    def test_service_source_renders_no_annotation(self) -> None:
        from xauditor.cli import _render_coder_status

        lines = _render_coder_status(
            self._status(projects=("alpha", "beta"), source="service")
        )
        proj_line = next(line for line in lines if "projects:" in line)
        self.assertIn("[alpha, beta]", proj_line)
        # No annotation parentheses on the projects line for the
        # service-sourced listing.
        self.assertNotIn("(", proj_line.split("projects:", 1)[1])

    def test_nested_listing_renders_no_annotation(self) -> None:
        from xauditor.cli import _render_coder_status

        lines = _render_coder_status(
            self._status(
                projects=("flat", "team", "team/repo"), source="service"
            )
        )
        proj_line = next(line for line in lines if "projects:" in line)
        # Nested paths are no longer annotated — every dir is a project.
        self.assertNotIn("(", proj_line.split("projects:", 1)[1])

    def test_host_scan_renders_host_scan_annotation_only(self) -> None:
        from xauditor.cli import _render_coder_status

        lines = _render_coder_status(
            self._status(
                projects=("flat", "team/repo"), source="host_scan"
            )
        )
        proj_line = next(line for line in lines if "projects:" in line)
        self.assertIn("host scan; container unreachable", proj_line)
        self.assertNotIn("marker-anchored", proj_line)


if __name__ == "__main__":
    unittest.main()
