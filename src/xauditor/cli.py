from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from xauditor.errors import (
    ConfigError,
    GraphdbError,
    PortalError,
    PreflightError,
    ReportDBError,
    UserCancelledError,
    XAuditorError,
)
from xauditor.audit._cancellation import RunCancellation, install_sigint_handler
from xauditor.runtime_logging import RuntimeLogger
from xauditor.services import ApplicationServices


def _check_neo4j_reachable_or_die(services, *, logger=None) -> None:
    """Ping the configured Neo4j; on failure, raise ``XAuditorError``
    with a numbered three-option remediation block.

    Called at the start of ``xauditor graph build`` and
    ``xauditor audit run``. The block deliberately walks operators
    through every realistic remediation path because the pre-0.9.0
    one-liner ("Neo4j unreachable") was found to be skimmed in
    practice. See ``consolidate-on-neo4j-source`` design D5.
    """

    try:
        services.neo4j.ping()
    except Exception as exc:
        bolt_url = (
            f"bolt://{services.config.graphdb.remote.url}"
            if services.config.graphdb.remote is not None
            else f"bolt://127.0.0.1:{services.config.graphdb.bolt_port}"
        )
        cause = f"{type(exc).__name__}: {exc}"
        message = (
            f"Cannot connect to Neo4j at {bolt_url} ({cause}).\n"
            "\n"
            "xauditor 0.9+ requires a reachable Neo4j instance. Options:\n"
            "\n"
            "  1. First-time setup of a local managed container:\n"
            "       xauditor graphdb init\n"
            "  2. Resume a stopped local managed container:\n"
            "       xauditor graphdb start\n"
            "  3. Configure a remote Neo4j endpoint in xauditor.yml:\n"
            "       graph:\n"
            "         db:\n"
            "           remote:\n"
            "             url: bolt://your-host:7687\n"
            "             ssl_ca: /path/to/ca.pem  # optional\n"
        )
        if logger is not None:
            logger.error_kv(
                "Neo4j unreachable at startup",
                bolt_url=bolt_url,
                cause=cause,
            )
        raise XAuditorError(message) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xauditor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init",
        help="Initialize both the graph database and the report database.",
    )

    graphdb = subparsers.add_parser(
        "graphdb",
        help="Manage the Neo4j graph database container.",
    )
    graphdb_subparsers = graphdb.add_subparsers(dest="graphdb_command", required=True)
    graphdb_subparsers.add_parser("init", help="Create and start the managed Neo4j container.")
    graphdb_subparsers.add_parser("start", help="Start an existing managed Neo4j container.")
    graphdb_subparsers.add_parser("stop", help="Stop the managed Neo4j container.")
    reset = graphdb_subparsers.add_parser(
        "reset", help="Remove the managed Neo4j container, image, and volume."
    )
    reset.add_argument("--yes", action="store_true", help="Confirm destructive reset")

    reportdb = subparsers.add_parser(
        "reportdb",
        help="Manage the PostgreSQL report database container.",
    )
    reportdb_subparsers = reportdb.add_subparsers(
        dest="reportdb_command", required=True
    )
    reportdb_subparsers.add_parser(
        "init", help="Create and start the managed PostgreSQL container; run migrations if installed."
    )
    reportdb_subparsers.add_parser(
        "start", help="Start an existing managed PostgreSQL container."
    )
    reportdb_subparsers.add_parser(
        "stop", help="Stop the managed PostgreSQL container."
    )
    reportdb_reset = reportdb_subparsers.add_parser(
        "reset",
        help="Remove the managed PostgreSQL container, image, and volume.",
    )
    reportdb_reset.add_argument(
        "--yes", action="store_true", help="Confirm destructive reset"
    )

    portal = subparsers.add_parser(
        "portal",
        help="Manage the web portal (requires the `xauditor-portal` package).",
    )
    portal_subparsers = portal.add_subparsers(dest="portal_command", required=True)
    portal_subparsers.add_parser(
        "init",
        help="First-time setup: build images, create containers, start, healthcheck.",
    )
    portal_subparsers.add_parser(
        "start",
        help="Start existing portal containers (refuses if missing — run `init` first).",
    )
    portal_subparsers.add_parser(
        "stop",
        help="Stop the managed portal containers (preserves images and network).",
    )
    portal_reset = portal_subparsers.add_parser(
        "reset",
        help="Remove managed portal containers, images, and network.",
    )
    portal_reset.add_argument(
        "--yes", action="store_true", help="Confirm destructive reset"
    )
    portal_subparsers.add_parser(
        "status", help="Print the state of the managed portal."
    )

    coder = subparsers.add_parser(
        "coder",
        help="Manage the xauditor-coder-service container (HTTP-transport only).",
    )
    coder_subparsers = coder.add_subparsers(dest="coder_command", required=True)
    coder_build = coder_subparsers.add_parser(
        "build",
        help="Build the coder service container image.",
    )
    coder_build.add_argument(
        "--push",
        action="store_true",
        help="Push the built image to its tagged registry (image must include a registry prefix).",
    )
    coder_subparsers.add_parser(
        "init",
        help="Build (if missing), create, and start the coder container; wait for /health.",
    )
    coder_subparsers.add_parser(
        "start",
        help="Start an existing coder container.",
    )
    coder_subparsers.add_parser(
        "stop",
        help="Stop the coder container (preserves image and container).",
    )
    coder_reset = coder_subparsers.add_parser(
        "reset",
        help="Remove the coder container, image, and socket file.",
    )
    coder_reset.add_argument(
        "--yes", action="store_true", help="Confirm destructive reset"
    )
    coder_subparsers.add_parser(
        "status",
        help="Print the state of the coder runtime (works for local AND remote endpoints).",
    )

    graph = subparsers.add_parser("graph")
    graph_subparsers = graph.add_subparsers(dest="graph_command", required=True)
    graph_build = graph_subparsers.add_parser("build")
    graph_build.add_argument("--exclude", action="append", default=[])
    graph_build.add_argument("-f", "--force", action="store_true")
    graph_subparsers.add_parser("list")
    graph_callstack = graph_subparsers.add_parser("callstack")
    graph_callstack.add_argument("function", help="Function name or qualified name to inspect")
    graph_callstack.add_argument("--build", dest="build", default=None, help="Graph build fingerprint (default: latest)")
    graph_callstack.add_argument("--source", action="store_true", help="Also render source snippets for each node")
    graph_paths = graph_subparsers.add_parser("paths")
    graph_paths.add_argument("--build", dest="build", default=None, help="Graph build fingerprint (default: latest)")

    audit = subparsers.add_parser(
        "audit",
        help="Run, resume, or export audits. xauditor 0.5.0+ persists every "
        "audit run into the report database; use `audit export` to render "
        "JSON or Markdown bundles on demand.",
    )
    audit_subparsers = audit.add_subparsers(dest="audit_command", required=True)
    audit_run = audit_subparsers.add_parser(
        "run", help="Start a fresh audit against a graph build."
    )
    audit_run.add_argument(
        "--build",
        dest="build",
        default=None,
        help="Audit a specific graph build fingerprint (default: latest).",
    )
    audit_resume = audit_subparsers.add_parser(
        "resume",
        help="Resume a previously failed or cancelled audit run from the "
        "report database. Completed paths are not re-executed; the runtime "
        "reads its checkpoint from audit_runs.resume_state.",
    )
    audit_resume.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="Run id to resume (timestamp form, e.g. 20260419-083512). "
        "When omitted, the most recent failed / cancelled run is used.",
    )
    audit_resume.add_argument(
        "--build",
        dest="build",
        default=None,
        help="Sanity check: refuse to resume if the resolved run's "
        "recorded build fingerprint does not match this value.",
    )
    audit_export = audit_subparsers.add_parser(
        "export",
        help="Export the persisted state of a completed audit run from the "
        "report database into JSON or Markdown.",
    )
    audit_export.add_argument(
        "run_id",
        help="Run label to export (the YYYYMMDD-HHMMSS string printed by "
        "`xauditor audit run`).",
    )
    audit_export.add_argument(
        "--format",
        dest="export_format",
        choices=("json", "markdown"),
        default="json",
        help="Output format. JSON is the canonical portable contract "
        "(emitted to stdout); Markdown writes a multi-file bundle.",
    )
    audit_export.add_argument(
        "--output-dir",
        dest="export_output_dir",
        default=None,
        help="Destination directory for the Markdown bundle. Required "
        "when --format markdown; ignored when --format json.",
    )
    audit_export.add_argument(
        "--include-debug",
        dest="export_include_debug",
        action="store_true",
        default=False,
        help="Include operator-debug fields (cli_exit_code, cli_stderr, "
        "etc.) in the output. Off by default.",
    )
    return parser


def _apply_legacy_audit_shim(argv: list[str] | None) -> tuple[list[str], bool]:
    """Map legacy `xauditor audit [flags]` invocations onto `audit run`.

    Three forms activate the shim:

    - ``xauditor audit`` (bare) → ``xauditor audit run``.
    - ``xauditor audit --build FP`` (no sub-verb, one or more ``--``-flags)
      → ``xauditor audit run --build FP``.

    Forms that already use ``run`` / ``resume`` (or ask for help) pass
    through unchanged. Returns the (possibly rewritten) argv and a
    boolean indicating whether the shim fired.
    """

    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] != "audit":
        return list(argv), False
    if len(argv) == 1:
        # Bare `xauditor audit` → `xauditor audit run`.
        return ["audit", "run"], True
    second = argv[1]
    if second in {"run", "resume", "-h", "--help"}:
        return list(argv), False
    if second.startswith("--"):
        return ["audit", "run", *argv[1:]], True
    return list(argv), False


def main(
    argv: list[str] | None = None,
    *,
    services: ApplicationServices | None = None,
    stdout=None,
    stderr=None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    effective_argv, legacy_shim_active = _apply_legacy_audit_shim(argv)
    if legacy_shim_active:
        _write_line(
            stderr,
            "DeprecationWarning: `xauditor audit` without a sub-verb is deprecated; "
            "use `xauditor audit run` instead. The shim will be removed in a future release.",
        )
    args = parser.parse_args(effective_argv)

    try:
        services = services or ApplicationServices.default(repo_root=Path.cwd())
        logger = RuntimeLogger.from_config(services.config, stream=stderr)
        if args.command == "init":
            messages = services.init_all(logger=logger)
            for message in messages:
                _write_line(stdout, message)
            return 0
        if args.command == "graphdb":
            return _handle_graphdb(args, services, stdout, logger)
        if args.command == "reportdb":
            return _handle_reportdb(args, services, stdout, logger)
        if args.command == "portal":
            return _handle_portal(args, services, stdout, logger)
        if args.command == "coder":
            return _handle_coder(args, services, stdout, logger)
        if args.command == "graph" and args.graph_command == "build":
            _check_neo4j_reachable_or_die(services, logger=logger)
            fingerprint, status = services.build_graph(
                excludes=tuple(args.exclude), force=args.force, logger=logger
            )
            _write_line(stdout, f"Graph build {status.value}: {fingerprint}")
            if status.name == "REUSED" and not args.force:
                _write_line(stdout, "Build is already complete. Pass --force to rebuild.")
            return 0
        if args.command == "graph" and args.graph_command == "callstack":
            return _handle_callstack(args, services, stdout, stderr)
        if args.command == "graph" and args.graph_command == "paths":
            build_fingerprint = services.resolve_build(args.build)
            summaries = services.list_graph_paths(build_fingerprint)
            if not summaries:
                _write_line(stderr, f"No audit paths are persisted for build {build_fingerprint}.")
                return 1
            rows = [
                (
                    summary.path_fingerprint[:12],
                    summary.entry_function,
                    " -> ".join(summary.function_chain) if summary.function_chain else "(empty)",
                )
                for summary in summaries
            ]
            for line in _render_table(("PATH", "ENTRY", "CHAIN"), rows):
                _write_line(stdout, line)
            _write_line(stdout, f"Total: {len(summaries)} paths")
            return 0
        if args.command == "graph" and args.graph_command == "list":
            summaries = services.list_graph_builds()
            if not summaries:
                _write_line(stderr, "No graph builds are available in Neo4j yet. Run `xauditor graph build` first.")
                return 1
            rows = [
                (summary.build_fingerprint[:12], _format_timestamp(summary.created_at), summary.status or "-")
                for summary in summaries
            ]
            for line in _render_table(("BUILD", "CREATED", "STATUS"), rows):
                _write_line(stdout, line)
            return 0
        if args.command == "audit" and args.audit_command == "run":
            _check_neo4j_reachable_or_die(services, logger=logger)
            cancellation = _build_run_cancellation(services)
            with install_sigint_handler(cancellation, logger=logger):
                audit_run, run_label = services.run_audit(
                    build_fingerprint=args.build,
                    logger=logger,
                    cancellation=cancellation,
                )
            _write_line(
                stdout,
                f"Audit completed for {audit_run.build_fingerprint} "
                f"(run id: {run_label}). "
                f"Export findings via `xauditor audit export {run_label}`.",
            )
            return 0
        if args.command == "audit" and args.audit_command == "resume":
            cancellation = _build_run_cancellation(services)
            with install_sigint_handler(cancellation, logger=logger):
                audit_run, run_label = services.resume_audit(
                    run_id=args.run_id,
                    build_fingerprint=args.build,
                    logger=logger,
                    cancellation=cancellation,
                )
            _write_line(
                stdout,
                f"Resumed audit completed for {audit_run.build_fingerprint} "
                f"(run id: {run_label}). "
                f"Export findings via `xauditor audit export {run_label}`.",
            )
            return 0
        if args.command == "audit" and args.audit_command == "export":
            output = services.audit_export(
                run_label=args.run_id,
                fmt=args.export_format,
                output_dir=(
                    Path(args.export_output_dir)
                    if args.export_output_dir is not None
                    else None
                ),
                include_debug=args.export_include_debug,
                logger=logger,
            )
            if isinstance(output, str):
                _write_line(stdout, output)
            else:
                rendered_paths = ", ".join(str(p) for p in output)
                _write_line(stdout, f"Exported: {rendered_paths}")
            return 0
    except UserCancelledError as exc:
        _write_line(stderr, str(exc))
        return 130
    except KeyboardInterrupt:
        _write_line(stderr, _cancellation_message(args))
        return 130
    except (
        ConfigError,
        GraphdbError,
        PortalError,
        PreflightError,
        ReportDBError,
        FileNotFoundError,
        XAuditorError,
    ) as exc:
        _write_line(stderr, str(exc))
        return 1
    except Exception as exc:
        # Lazy-detect ``RunDeletedExternallyError`` (defined in the
        # optional portal package) so we surface a friendly one-liner
        # instead of a traceback when an admin deletes the run mid-audit.
        # Exit code 75 (EX_TEMPFAIL) so wrapper scripts can distinguish
        # this from generic failure (exit 1) — re-running the audit will
        # produce a fresh run row.
        #
        # We exit via ``os._exit(75)`` rather than ``return 75`` to bypass
        # Python's interpreter finalisation chain — specifically the
        # ``concurrent.futures.thread._python_exit`` atexit hook that
        # would call ``executor.shutdown(wait=True)`` on the coder
        # dispatcher's executor and block on in-flight Claude CLI
        # subprocesses. The subprocesses are NOT terminated by
        # ``cancel_all`` (per coder.py docstrings) and would otherwise
        # keep the process alive for the LLM call's duration. Mirrors
        # the existing ``os._exit(130)`` pattern at
        # ``xauditor.audit._cancellation.py:179`` for repeat Ctrl+C.
        try:
            from xauditor_portal.sinks import RunDeletedExternallyError
        except ImportError:
            raise
        if isinstance(exc, RunDeletedExternallyError):
            _write_line(stderr, str(exc))
            # Explicit flush — ``os._exit`` skips Python's I/O flush at
            # interpreter shutdown; without this, buffered stderr can
            # be lost between TextIOWrapper and the OS file descriptor.
            try:
                stderr.flush()
            except Exception:  # noqa: BLE001 - flush best-effort
                pass
            os._exit(75)
            # Unreachable in production (``os._exit`` terminates the
            # process). The explicit ``return 75`` exists so unit tests
            # that patch ``os._exit`` can observe the call without the
            # below ``raise`` propagating the original exception.
            return 75  # noqa: RET505 - intentional dead-code-in-production
        raise
    parser.error("Unsupported command")
    return 2


def _handle_graphdb(args, services: ApplicationServices, stdout, logger: RuntimeLogger) -> int:
    if args.graphdb_command == "init":
        _write_line(stdout, services.graphdb_init(logger=logger))
        return 0
    if args.graphdb_command == "start":
        _write_line(stdout, services.graphdb_start(logger=logger))
        return 0
    if args.graphdb_command == "stop":
        _write_line(stdout, services.graphdb_stop(logger=logger))
        return 0
    if args.graphdb_command == "reset":
        deleted = services.graphdb_reset(confirmed=args.yes, logger=logger)
        message = "Deleted " + ", ".join(deleted) if deleted else "Deleted no managed resources"
        _write_line(stdout, message)
        return 0
    raise ValueError(f"Unsupported graphdb command: {args.graphdb_command}")


def _handle_reportdb(args, services: ApplicationServices, stdout, logger: RuntimeLogger) -> int:
    if args.reportdb_command == "init":
        _write_line(stdout, services.reportdb_init(logger=logger))
        return 0
    if args.reportdb_command == "start":
        _write_line(stdout, services.reportdb_start(logger=logger))
        return 0
    if args.reportdb_command == "stop":
        _write_line(stdout, services.reportdb_stop(logger=logger))
        return 0
    if args.reportdb_command == "reset":
        deleted = services.reportdb_reset(confirmed=args.yes, logger=logger)
        message = (
            "Deleted " + ", ".join(deleted) if deleted else "Deleted no managed resources"
        )
        _write_line(stdout, message)
        return 0
    raise ValueError(f"Unsupported reportdb command: {args.reportdb_command}")


def _handle_portal(args, services: ApplicationServices, stdout, logger: RuntimeLogger) -> int:
    if args.portal_command == "init":
        _write_line(stdout, services.portal_init(logger=logger))
        return 0
    if args.portal_command == "start":
        _write_line(stdout, services.portal_start(logger=logger))
        return 0
    if args.portal_command == "stop":
        _write_line(stdout, services.portal_stop(logger=logger))
        return 0
    if args.portal_command == "reset":
        deleted = services.portal_reset(confirmed=args.yes, logger=logger)
        message = (
            "Deleted " + ", ".join(deleted) if deleted else "Deleted no managed resources"
        )
        _write_line(stdout, message)
        return 0
    if args.portal_command == "status":
        status = services.portal_status(logger=logger)
        for line in _render_portal_status(status):
            _write_line(stdout, line)
        return 0
    raise ValueError(f"Unsupported portal command: {args.portal_command}")


def _render_portal_status(status) -> list[str]:
    def _svc(label: str, svc) -> str:
        running = "running" if svc.container_running else (
            "stopped" if svc.container_exists else "absent"
        )
        image_state = "image present" if svc.image_present else "image missing"
        return f"  {label}: {running} ({image_state}, container={svc.container_name}, image={svc.image})"

    lines = [
        f"Portal network {status.network_name}: "
        + ("exists" if status.network_exists else "absent"),
        _svc("backend", status.backend),
        _svc("frontend", status.frontend),
    ]
    if status.host_url:
        lines.append(f"Host URL: {status.host_url}")
    return lines


def _handle_coder(args, services: ApplicationServices, stdout, logger: RuntimeLogger) -> int:
    if args.coder_command == "build":
        result = services.coder_build(push=args.push, logger=logger)
        _write_line(stdout, f"Built coder image: {result}")
        return 0
    if args.coder_command == "init":
        _write_line(stdout, services.coder_init(logger=logger))
        return 0
    if args.coder_command == "start":
        _write_line(stdout, services.coder_start(logger=logger))
        return 0
    if args.coder_command == "stop":
        _write_line(stdout, services.coder_stop(logger=logger))
        return 0
    if args.coder_command == "reset":
        deleted = services.coder_reset(confirmed=args.yes, logger=logger)
        message = (
            "Deleted " + ", ".join(deleted) if deleted else "Deleted no managed resources"
        )
        _write_line(stdout, message)
        return 0
    if args.coder_command == "status":
        status = services.coder_status(logger=logger)
        for line in _render_coder_status(status):
            _write_line(stdout, line)
        return 0
    raise ValueError(f"Unsupported coder command: {args.coder_command}")


def _render_coder_status(status) -> list[str]:
    """Format ``services.coder_status()`` output for human reading."""

    lines = [
        f"Coder transport: {status.transport}",
        f"Coder endpoint: {status.endpoint or '(unset)'} (kind={status.endpoint_kind})",
    ]
    if status.endpoint_kind == "remote":
        lines.append(
            "  container: (remote endpoint; container lifecycle managed externally)"
        )
    else:
        container_state = (
            "running" if status.container_running
            else ("stopped" if status.container_exists else "absent")
        )
        image_state = "present" if status.image_present else "missing"
        lines.append(
            f"  container: {container_state} (name={status.container_name})"
        )
        lines.append(f"  image: {image_state} (tag={status.image})")
    if status.health_status is not None:
        version = status.claude_cli_version or "unknown"
        in_flight = (
            f", in_flight={status.in_flight}" if status.in_flight is not None else ""
        )
        lines.append(
            f"  health: HTTP {status.health_status} (claude_cli_version={version!r}{in_flight})"
        )
    elif status.error:
        lines.append(f"  health: unreachable ({status.error})")
    else:
        lines.append("  health: not probed (container not running)")
    if status.workspace_root or status.projects:
        lines.append(f"  workspace: {status.workspace_root or '(unset)'}")
        if status.projects:
            project_list = ", ".join(status.projects)
            parts: list[str] = []
            if status.projects_source == "host_scan":
                parts.append("host scan; container unreachable")
            annotation = f" ({'; '.join(parts)})" if parts else ""
            lines.append(f"  projects: [{project_list}]{annotation}")
        else:
            lines.append("  projects: (none discovered)")
    return lines


def entrypoint() -> int:
    return main()


def _build_run_cancellation(services: ApplicationServices) -> RunCancellation:
    """Construct a ``RunCancellation`` using the configured timeout.

    Eagerly loads the config (idempotent — ``run_audit`` does the same
    thing internally) so the SIGINT handler is armed with the correct
    deadline before any audit work begins.
    """
    config = services.require_llm_config()
    return RunCancellation(
        timeout_seconds=float(config.audit.shutdown_timeout_seconds)
    )


def _cancellation_message(args) -> str:
    if args.command == "graph" and getattr(args, "graph_command", None) == "build":
        return "Graph build cancelled by user."
    if args.command == "audit":
        return "Audit cancelled by user."
    return "Operation cancelled by user."


def _write_line(stream, message: str) -> None:
    stream.write(f"{message}\n")


def _handle_callstack(args, services: ApplicationServices, stdout, stderr) -> int:
    from xauditor.reporting.snippets import render_source_snippet

    build_fingerprint = services.resolve_build(args.build)
    candidates = services.find_functions(build_fingerprint, args.function)
    if not candidates:
        _write_line(stderr, f"Function `{args.function}` not found in build {build_fingerprint}.")
        return 1
    if len(candidates) > 1:
        _write_line(stdout, f"Multiple candidates for `{args.function}` in build {build_fingerprint}:")
        for candidate in candidates:
            _write_line(stdout, f"  {candidate.qualified_name}  [{candidate.file_path}:{candidate.start_line}]")
        return 0
    tree = services.get_call_stack(build_fingerprint, candidates[0].function_id)
    for line in _render_callstack(tree, include_source=args.source, renderer=render_source_snippet):
        _write_line(stdout, line)
    return 0


def _render_callstack(node, *, include_source: bool, renderer, indent: int = 0) -> list[str]:
    prefix = "  " * indent
    fn = node.function
    suffix = ""
    if node.cycle:
        suffix = "  (cycle)"
    elif node.truncated:
        suffix = "  (max depth)"
    lines = [f"{prefix}- {fn.qualified_name}  [{fn.file_path}:{fn.start_line}]{suffix}"]
    if include_source and not node.cycle:
        try:
            snippet = renderer(
                file_path=str((Path.cwd() / fn.file_path).resolve()),
                start_line=fn.start_line,
                end_line=fn.end_line,
                focus_lines=(),
                language=language_for_path(fn.file_path),
            )
        except FileNotFoundError:
            snippet = f"{fn.file_path} (source not available)"
        for snippet_line in snippet.splitlines():
            lines.append(f"{prefix}    {snippet_line}")
    for child in node.children:
        lines.extend(_render_callstack(child, include_source=include_source, renderer=renderer, indent=indent + 1))
    return lines


def _render_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    all_rows = [headers, *rows]
    widths = [max(len(str(row[i])) for row in all_rows) for i in range(len(headers))]
    last = len(headers) - 1

    def fmt(row: tuple[str, ...]) -> str:
        parts = []
        for i, cell in enumerate(row):
            cell = str(cell)
            parts.append(cell if i == last else cell.ljust(widths[i]))
        return "  ".join(parts)

    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    return [fmt(headers), separator, *(fmt(row) for row in rows)]


def _format_timestamp(value: int) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
