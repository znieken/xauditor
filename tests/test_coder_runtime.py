"""Tests for the coder lifecycle layer (Phase 2: add-coder-runtime-management).

Covers:
- helpers.endpoint_kind / endpoint_is_local / build_run_argv classification
- CoderRuntimeManager via a fake docker command runner
- InMemoryCoderRuntimeManager state machine
- Pre-flight container-state inspection branch (`check_coder_runtime`)
- ApplicationServices.coder_* dispatch (gating + delegation)
- `xauditor init` umbrella's coder-skip messages
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.preflight import check_coder_runtime
from xauditor.config import CoderConfig
from xauditor.errors import PreflightError, XAuditorError
from xauditor.integrations.coder import (
    InMemoryCoderRuntimeManager,
    build_run_argv,
    derive_socket_path,
    endpoint_is_local,
    endpoint_kind,
)
from xauditor.integrations.coder.runtime import CoderRuntimeManager
from xauditor.integrations.docker import CommandResult


class EndpointKindTests(unittest.TestCase):
    def test_unix(self) -> None:
        cfg = CoderConfig(endpoint="unix:///var/run/x.sock")
        self.assertEqual(endpoint_kind(cfg), "unix")
        self.assertTrue(endpoint_is_local(cfg))

    def test_loopback_variants(self) -> None:
        for endpoint in (
            "http://127.0.0.1:8090",
            "http://localhost:8090",
            "http://[::1]:8090",
        ):
            cfg = CoderConfig(endpoint=endpoint)
            self.assertEqual(endpoint_kind(cfg), "loopback", endpoint)
            self.assertTrue(endpoint_is_local(cfg), endpoint)

    def test_remote_variants(self) -> None:
        for endpoint in (
            "https://coder.internal/",
            "http://coder.internal:8090",
            "",
        ):
            cfg = CoderConfig(endpoint=endpoint)
            self.assertEqual(endpoint_kind(cfg), "remote", endpoint)
            self.assertFalse(endpoint_is_local(cfg), endpoint)

    def test_https_loopback_is_still_remote(self) -> None:
        # We only treat plain HTTP loopback as "local" — HTTPS goes
        # through a TLS terminator in production.
        cfg = CoderConfig(endpoint="https://127.0.0.1:8443")
        self.assertEqual(endpoint_kind(cfg), "remote")


class DeriveSocketPathTests(unittest.TestCase):
    def test_explicit_socket_path_wins(self) -> None:
        cfg = CoderConfig(
            endpoint="unix:///derived.sock",
            runtime_socket_path="/explicit.sock",
        )
        self.assertEqual(derive_socket_path(cfg), "/explicit.sock")

    def test_derived_from_unix_endpoint(self) -> None:
        cfg = CoderConfig(endpoint="unix:///var/run/x.sock")
        self.assertEqual(derive_socket_path(cfg), "/var/run/x.sock")

    def test_runtime_root_fallback(self) -> None:
        cfg = CoderConfig(endpoint="http://127.0.0.1:8090")
        self.assertEqual(
            derive_socket_path(cfg, runtime_root="/var/lib/x"),
            "/var/lib/x/coder.sock",
        )

    def test_empty_when_nothing_resolvable(self) -> None:
        cfg = CoderConfig(endpoint="https://coder.internal/")
        self.assertEqual(derive_socket_path(cfg), "")


class BuildRunArgvTests(unittest.TestCase):
    def test_unix_argv_contains_required_flags(self) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="unix:///var/run/x.sock",
            runtime_socket_path="/var/run/x.sock",
            container_name="cs",
            container_image="img:tag",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertIn("--detach", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("ALL", argv)
        self.assertIn("/r:/workspace:ro", argv)
        self.assertIn("XAUDITOR_CODER_SERVICE_BIND=unix:/var/run/x.sock", argv)
        self.assertIn("XAUDITOR_CODER_SERVICE_ENABLE_AUTH=false", argv)
        self.assertEqual(argv[-1], "img:tag")

    def test_loopback_argv_publishes_port(self) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:9090",
            container_name="cs",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        self.assertIn("127.0.0.1:9090:9090", argv)
        self.assertIn("XAUDITOR_CODER_SERVICE_BIND=0.0.0.0:9090", argv)

    def test_enable_auth_passes_token(self) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:8090",
            enable_auth=True,
            endpoint_token="sek",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        self.assertIn("XAUDITOR_CODER_SERVICE_ENABLE_AUTH=true", argv)
        self.assertIn("XAUDITOR_CODER_SERVICE_TOKEN=sek", argv)

    def test_model_config_is_baked_into_container_env(self) -> None:
        """Claude Code 2.x is env-driven: model selection / endpoint /
        auth all flow through env vars (no CLI flag for the base URL,
        and we standardise on env-only for uniformity). ``build_run_argv``
        bakes all three into the container at creation time. Operators
        rotate values via ``coder reset --yes && coder init``."""

        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:8090",
            model_api_key="sk-test-key",
            model_url="https://gateway.example/v1",
            model_name="claude-sonnet-4-7",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        self.assertIn("ANTHROPIC_API_KEY=sk-test-key", argv)
        self.assertIn("ANTHROPIC_BASE_URL=https://gateway.example/v1", argv)
        self.assertIn("ANTHROPIC_MODEL=claude-sonnet-4-7", argv)

    def test_home_coder_is_named_volume_for_claude_state(self) -> None:
        """Claude Code 2.x writes session state, agent caches, and
        settings to ``~/.claude/`` on startup. Under ``--read-only``
        rootfs those writes block (claude hangs waiting for a
        directory it can't mkdir). The container SHALL expose
        ``/home/coder`` as a docker NAMED VOLUME — NOT tmpfs.

        Tmpfs would be wrong: claude's session state grows over time
        and a long-running coder service would gradually consume
        host RAM. A named volume sits on disk, is owned by docker,
        and is wiped by ``coder reset`` rather than living through
        container churn until the host OOMs."""

        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="my-coder",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        # /home/coder MUST NOT be backed by tmpfs — that's the
        # exact bug the named volume change fixes.
        for i, a in enumerate(argv):
            if a == "--tmpfs" and i + 1 < len(argv):
                self.assertFalse(
                    argv[i + 1].startswith("/home/coder"),
                    f"/home/coder must not be tmpfs (RAM-backed): {argv[i + 1]}",
                )
        # /home/coder MUST be a named volume mount whose name is
        # derived from the container name so multiple containers
        # don't share state.
        volume_args = [
            argv[i + 1]
            for i, a in enumerate(argv)
            if a == "--volume" and i + 1 < len(argv) and ":/home/coder" in argv[i + 1]
        ]
        self.assertEqual(
            len(volume_args), 1,
            "expected exactly one named-volume mount at /home/coder",
        )
        self.assertEqual(volume_args[0], "my-coder-home:/home/coder")

    def test_model_config_omitted_when_unset(self) -> None:
        """Unset config keys SHALL NOT produce empty env entries —
        operator can leave any of the three blank to inherit from
        whatever their container image / shell defaults provide."""

        cfg = CoderConfig(transport="http", endpoint="http://127.0.0.1:8090")
        argv = build_run_argv(cfg, repo_root="/r")
        joined = " ".join(argv)
        self.assertNotIn("ANTHROPIC_API_KEY", joined)
        self.assertNotIn("ANTHROPIC_BASE_URL", joined)
        self.assertNotIn("ANTHROPIC_MODEL", joined)

    def test_repo_mount_path_override(self) -> None:
        cfg = CoderConfig(
            transport="http",
            endpoint="http://127.0.0.1:8090",
            repo_mount_path="/srv/repo",
        )
        argv = build_run_argv(cfg, repo_root="/r")
        self.assertIn("/srv/repo:/workspace:ro", argv)
        self.assertNotIn("/r:/workspace:ro", argv)

    def test_remote_endpoint_refused(self) -> None:
        cfg = CoderConfig(transport="http", endpoint="https://coder.internal/")
        with self.assertRaises(ValueError):
            build_run_argv(cfg, repo_root="/r")


class FakeRunner:
    """Records calls and returns scripted CommandResult per argv prefix."""

    def __init__(self, scripts: list[tuple[list[str], CommandResult]]) -> None:
        self.scripts = list(scripts)
        self.invocations: list[list[str]] = []

    def __call__(self, args, *, timeout=None):  # noqa: D401
        self.invocations.append(list(args))
        for argv_prefix, result in self.scripts:
            if args[: len(argv_prefix)] == argv_prefix:
                return result
        # Default to OK with empty output
        return CommandResult(returncode=0, stdout="", stderr="")


def _ok() -> CommandResult:
    return CommandResult(returncode=0, stdout="", stderr="")


def _fail(message: str = "no") -> CommandResult:
    return CommandResult(returncode=1, stdout="", stderr=message)


class CoderRuntimeManagerLifecycleTests(unittest.TestCase):
    def _cfg(self) -> CoderConfig:
        return CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            container_image="img:tag",
        )

    def test_init_when_image_and_container_missing_builds_then_creates_then_health_probes(self) -> None:
        cfg = self._cfg()
        runner = FakeRunner([
            (["docker", "version"], _ok()),
            (["docker", "image", "inspect", "img:tag"], _fail()),
            (["docker", "build"], _ok()),
            (["docker", "container", "inspect", "cs"], _fail()),
            (["docker", "run"], _ok()),
        ])
        probe_calls: list[CoderConfig] = []

        def probe(coder_cfg):
            probe_calls.append(coder_cfg)
            return {"claude_cli_version": "claude X.Y.Z", "in_flight": 0}

        with tempfile.TemporaryDirectory() as pkg_dir:
            pkg = Path(pkg_dir)
            (pkg / "docker").mkdir()
            (pkg / "docker" / "Dockerfile").write_text("FROM scratch\n")

            fake_info = mock.MagicMock(
                dockerfile=pkg / "docker" / "Dockerfile",
                context=pkg,
            )
            with mock.patch(
                "xauditor.integrations.coder.runtime.resolve_coder_package",
                return_value=fake_info,
            ):
                mgr = CoderRuntimeManager(
                    cfg, repo_root=Path("/r"), command_runner=runner, http_probe=probe,
                )
                mgr.init_runtime()

        # docker version → image inspect → build → container inspect → run
        self.assertEqual(runner.invocations[0], ["docker", "version"])
        self.assertEqual(runner.invocations[1], ["docker", "image", "inspect", "img:tag"])
        self.assertEqual(runner.invocations[2][:2], ["docker", "build"])
        # Build context is the resolved package directory (mirrors portal pattern).
        build_argv = runner.invocations[2]
        self.assertEqual(build_argv[-1], str(pkg))
        self.assertIn("--file", build_argv)
        self.assertEqual(runner.invocations[3], ["docker", "container", "inspect", "cs"])
        self.assertEqual(runner.invocations[4][:2], ["docker", "run"])
        self.assertEqual(len(probe_calls), 1)

    def test_start_against_missing_container_directs_at_init(self) -> None:
        cfg = self._cfg()
        runner = FakeRunner([
            (["docker", "version"], _ok()),
            (["docker", "container", "inspect", "cs"], _fail()),
        ])
        mgr = CoderRuntimeManager(cfg, repo_root=Path("/r"), command_runner=runner)
        with self.assertRaises(XAuditorError) as ctx:
            mgr.start_runtime()
        self.assertIn("xauditor coder init", str(ctx.exception))

    def test_remote_endpoint_refuses_lifecycle(self) -> None:
        cfg = CoderConfig(
            enabled=True, transport="http", endpoint="https://coder.internal/",
        )
        mgr = CoderRuntimeManager(cfg, repo_root=Path("/r"))
        for verb in ("init_runtime", "start_runtime", "stop_runtime"):
            with self.assertRaises(XAuditorError) as ctx:
                getattr(mgr, verb)()
            self.assertIn("remote", str(ctx.exception).lower())

    def test_status_works_for_remote_endpoint(self) -> None:
        cfg = CoderConfig(
            enabled=True, transport="http", endpoint="https://coder.internal/",
        )
        probe_calls = []

        def probe(coder_cfg):
            probe_calls.append(coder_cfg)
            return {"claude_cli_version": "claude X", "in_flight": 3}

        mgr = CoderRuntimeManager(
            cfg, repo_root=Path("/r"), http_probe=probe,
        )
        status = mgr.runtime_status()
        self.assertEqual(status.endpoint_kind, "remote")
        self.assertFalse(status.image_present)
        self.assertEqual(status.health_status, 200)
        self.assertEqual(status.in_flight, 3)
        self.assertEqual(status.claude_cli_version, "claude X")

    def test_wait_until_healthy_retries_through_non_oserror_transport_errors(self) -> None:
        """Regression for the 0.3.3 bug where httpx.ConnectError bubbled past
        the retry catch and killed `coder init` on a healthy container."""

        cfg = self._cfg()
        runner = FakeRunner([
            (["docker", "version"], _ok()),
            (["docker", "image", "inspect", "img:tag"], _ok()),
            (["docker", "container", "inspect", "cs"], _ok()),
            (
                ["docker", "container", "inspect", "--format", "{{.State.Running}}", "cs"],
                CommandResult(returncode=0, stdout="true", stderr=""),
            ),
        ])

        # First N probes raise non-OSError transport errors (mimicking
        # httpx.ConnectError during container startup); next probe succeeds.
        class _FakeNonOSError(Exception):
            pass

        attempts = {"n": 0}

        def probe(coder_cfg):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _FakeNonOSError("connection reset by peer")
            return {"claude_cli_version": "claude X", "in_flight": 0}

        mgr = CoderRuntimeManager(
            cfg, repo_root=Path("/r"), command_runner=runner, http_probe=probe,
            ready_timeout_seconds=10,
        )
        # Enter at start_runtime, which is the same wait path as init_runtime
        # but skips build/create — easier to set up.
        mgr.start_runtime()
        self.assertEqual(attempts["n"], 3)

    def test_wait_until_healthy_surfaces_deadline_when_probe_never_settles(self) -> None:
        """A persistent transport error still produces a deadline-exhausted error."""

        cfg = self._cfg()
        runner = FakeRunner([
            (["docker", "version"], _ok()),
            (["docker", "container", "inspect", "cs"], _ok()),
            (
                ["docker", "container", "inspect", "--format", "{{.State.Running}}", "cs"],
                CommandResult(returncode=0, stdout="true", stderr=""),
            ),
        ])

        class _FakeNonOSError(Exception):
            pass

        def probe(coder_cfg):
            raise _FakeNonOSError("permanent fail")

        mgr = CoderRuntimeManager(
            cfg, repo_root=Path("/r"), command_runner=runner, http_probe=probe,
            ready_timeout_seconds=1,  # Quick deadline so the test stays snappy
        )
        with self.assertRaises(XAuditorError) as ctx:
            mgr.start_runtime()
        msg = str(ctx.exception)
        self.assertIn("did not respond to /health", msg)
        self.assertIn("permanent fail", msg)

    def test_reset_removes_home_named_volume(self) -> None:
        """Regression: ``coder reset --yes`` MUST also wipe the
        named volume backing ``/home/coder``. Otherwise stale
        claude session state (potentially a large amount after a
        long campaign) lingers indefinitely and the next ``coder
        init`` re-mounts it."""

        cfg = self._cfg()
        runner = FakeRunner([
            (["docker", "version"], _ok()),
            (["docker", "container", "inspect", "cs"], _ok()),
            (
                ["docker", "container", "inspect", "--format", "{{.State.Running}}", "cs"],
                CommandResult(returncode=0, stdout="false", stderr=""),
            ),
            (["docker", "rm", "cs"], _ok()),
            (["docker", "image", "inspect", "img:tag"], _ok()),
            (["docker", "image", "rm", "img:tag"], _ok()),
            (["docker", "volume", "rm", "cs-home"], _ok()),
        ])
        mgr = CoderRuntimeManager(cfg, repo_root=Path("/r"), command_runner=runner)
        deleted = mgr.reset_runtime(confirmed=True)
        self.assertIn("cs", deleted)
        self.assertIn("img:tag", deleted)
        self.assertIn("cs-home", deleted)
        # The volume rm command must have been issued.
        self.assertTrue(
            any(inv[:3] == ["docker", "volume", "rm"] and inv[3] == "cs-home"
                for inv in runner.invocations),
            f"docker volume rm cs-home not in invocations: {runner.invocations}",
        )

    def test_reset_without_yes_refuses(self) -> None:
        cfg = self._cfg()
        runner = FakeRunner([(["docker", "version"], _ok())])
        mgr = CoderRuntimeManager(cfg, repo_root=Path("/r"), command_runner=runner)
        with self.assertRaises(XAuditorError) as ctx:
            mgr.reset_runtime(confirmed=False)
        self.assertIn("--yes", str(ctx.exception))


class CheckCoderRuntimePreflightTests(unittest.TestCase):
    """The pre-flight branch that auto-starts a stopped container."""

    def _cfg_local(self) -> CoderConfig:
        # Skip the actual /health probe: subclass behavior is what we
        # care about in these tests. We assert the runtime callback got
        # the right `state` mapping, then short-circuit.
        return CoderConfig(
            enabled=True, transport="http",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
        )

    def test_running_container_proceeds_to_health_probe(self) -> None:
        cfg = self._cfg_local()
        runtime = InMemoryCoderRuntimeManager(cfg)
        runtime.container_state = "running"
        with mock.patch(
            "xauditor.audit.preflight.check_coder_http",
            return_value=mock.MagicMock(version="claude X"),
        ):
            check_coder_runtime(cfg, coder_runtime=runtime)
        self.assertEqual(runtime.calls, [("inspect_for_preflight",)])

    def test_stopped_container_is_auto_started_with_info_log(self) -> None:
        cfg = self._cfg_local()
        runtime = InMemoryCoderRuntimeManager(cfg)
        runtime.container_state = "stopped"

        class _Logger:
            messages: list[str] = []

            def info(self, m): self.messages.append(m)

        logger = _Logger()
        with mock.patch(
            "xauditor.audit.preflight.check_coder_http",
            return_value=mock.MagicMock(version="claude X"),
        ):
            check_coder_runtime(cfg, coder_runtime=runtime, runtime_logger=logger)
        self.assertEqual(runtime.container_state, "running")
        self.assertTrue(any("was stopped" in m for m in logger.messages))

    def test_missing_container_raises_preflight_error(self) -> None:
        cfg = self._cfg_local()
        runtime = InMemoryCoderRuntimeManager(cfg)
        runtime.container_state = "absent"
        with self.assertRaises(PreflightError) as ctx:
            check_coder_runtime(cfg, coder_runtime=runtime)
        message = str(ctx.exception)
        self.assertIn("xauditor coder init", message)
        self.assertIn("subprocess", message)

    def test_remote_endpoint_skips_container_check(self) -> None:
        cfg = CoderConfig(
            enabled=True, transport="http", endpoint="https://coder.internal/",
        )
        runtime = InMemoryCoderRuntimeManager(cfg)
        with mock.patch(
            "xauditor.audit.preflight.check_coder_http",
            return_value=mock.MagicMock(version="claude X"),
        ):
            check_coder_runtime(cfg, coder_runtime=runtime)
        # Inspection SHALL NOT be called on remote endpoints
        self.assertEqual(runtime.calls, [])


class ApplicationServicesCoderTests(unittest.TestCase):
    """Test the gating on coder_*: subprocess transport + disabled coder refuse."""

    def _services(self, *, coder_enabled: bool, transport: str = "http"):
        # Build a thin stub-services object so we don't need a real ApplicationServices
        from dataclasses import dataclass

        @dataclass
        class _Cfg:
            coder: CoderConfig

        cfg = _Cfg(coder=CoderConfig(enabled=coder_enabled, transport=transport))

        class _S:
            config = cfg
            coder_runtime = InMemoryCoderRuntimeManager(cfg.coder)

        # Bind the real method onto the stub
        from xauditor.services import ApplicationServices

        s = _S()
        s.coder_init = ApplicationServices.coder_init.__get__(s)
        s._ensure_coder_runtime_applicable = (
            ApplicationServices._ensure_coder_runtime_applicable.__get__(s)
        )
        return s

    def test_coder_init_refuses_when_disabled(self) -> None:
        s = self._services(coder_enabled=False)
        with self.assertRaises(XAuditorError) as ctx:
            s.coder_init()
        self.assertIn("coder.enabled: false", str(ctx.exception))

    def test_coder_init_refuses_under_subprocess_transport(self) -> None:
        s = self._services(coder_enabled=True, transport="subprocess")
        with self.assertRaises(XAuditorError) as ctx:
            s.coder_init()
        self.assertIn("subprocess", str(ctx.exception))


# --------------------------------------------------------------------------
# Package discovery: resolve_coder_package mirrors portal's pattern —
# Dockerfile lives inside the installed xauditor-coder-service package.
# --------------------------------------------------------------------------


class ResolveCoderPackageTests(unittest.TestCase):
    def test_returns_dockerfile_inside_installed_package(self) -> None:
        from xauditor.integrations.coder import package as pkg

        with tempfile.TemporaryDirectory() as install_dir:
            install = Path(install_dir)
            (install / "docker").mkdir()
            (install / "docker" / "Dockerfile").write_text("FROM scratch\n")
            (install / "docker" / "requirements.txt").write_text("fastapi\n")

            class FakeServicePackage:
                __file__ = str(install / "__init__.py")

            fake_dist = mock.MagicMock()
            fake_dist.metadata = {"Name": "xauditor-coder-service"}
            fake_dist.version = "0.2.0"

            with mock.patch.dict(
                "sys.modules",
                {"xauditor_coder_service": FakeServicePackage()},
            ), mock.patch(
                "xauditor.integrations.coder.package.metadata.distribution",
                return_value=fake_dist,
            ):
                info = pkg.resolve_coder_package()

            self.assertEqual(info.distribution_name, "xauditor-coder-service")
            self.assertEqual(info.distribution_version, "0.2.0")
            self.assertEqual(info.dockerfile, install / "docker" / "Dockerfile")
            self.assertEqual(info.context, install)

    def test_missing_distribution_raises_install_hint(self) -> None:
        from importlib.metadata import PackageNotFoundError
        from xauditor.integrations.coder.package import (
            CoderPackageMissing,
            resolve_coder_package,
        )

        with mock.patch(
            "xauditor.integrations.coder.package.metadata.distribution",
            side_effect=PackageNotFoundError("xauditor-coder-service"),
        ):
            with self.assertRaises(CoderPackageMissing) as ctx:
                resolve_coder_package()
        self.assertIn("pip install xauditor-coder-service", str(ctx.exception))

    def test_missing_dockerfile_inside_package_raises(self) -> None:
        from xauditor.integrations.coder import package as pkg

        with tempfile.TemporaryDirectory() as install_dir:
            install = Path(install_dir)
            # NB: docker/ subdir intentionally absent

            class FakeServicePackage:
                __file__ = str(install / "__init__.py")

            fake_dist = mock.MagicMock()
            fake_dist.metadata = {"Name": "xauditor-coder-service"}
            fake_dist.version = "0.2.0"

            with mock.patch.dict(
                "sys.modules",
                {"xauditor_coder_service": FakeServicePackage()},
            ), mock.patch(
                "xauditor.integrations.coder.package.metadata.distribution",
                return_value=fake_dist,
            ):
                with self.assertRaises(pkg.CoderPackageMissing) as ctx:
                    pkg.resolve_coder_package()
            self.assertIn("docker/Dockerfile", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
