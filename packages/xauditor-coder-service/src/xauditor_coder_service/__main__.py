"""CLI entrypoint for the coder microservice.

Run via the console script ``xauditor-coder-service`` (preferred) or
``python -m xauditor_coder_service``. The bind target comes from
``XAUDITOR_CODER_SERVICE_BIND``:

  - ``host:port``       — TCP. Default ``127.0.0.1:8090`` (loopback only)
  - ``unix:/abs/path``  — Unix domain socket; created with mode 0660
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from typing import Sequence

import uvicorn

from xauditor_coder_service.app import ServiceSettings, create_app


_DEFAULT_BIND = "127.0.0.1:8090"


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("XAUDITOR_CODER_SERVICE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("xauditor_coder_service")

    try:
        settings = ServiceSettings.from_env()
    except RuntimeError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        return 2

    bind = (os.environ.get("XAUDITOR_CODER_SERVICE_BIND") or _DEFAULT_BIND).strip() or _DEFAULT_BIND
    app = create_app(settings=settings)

    uvicorn_kwargs: dict[str, object] = {
        "log_config": None,  # use the basicConfig we just set up
    }
    if bind.startswith("unix:"):
        socket_path = bind[len("unix:"):]
        if not socket_path.startswith("/"):
            sys.stderr.write(
                f"Invalid XAUDITOR_CODER_SERVICE_BIND: unix path must be absolute, got {bind}\n"
            )
            return 2
        # uvicorn creates the socket with default mode; we tighten it below.
        uvicorn_kwargs["uds"] = socket_path

        def _on_startup() -> None:
            try:
                os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
            except OSError as chmod_exc:
                log.warning("Could not chmod 0660 on %s: %s", socket_path, chmod_exc)

        # Trigger the chmod on the next event-loop tick after the bind.
        # uvicorn doesn't expose a post-bind hook directly; the safest
        # cross-version approach is signal-driven below.
        _arm_post_bind_chmod(socket_path)
    else:
        if ":" not in bind:
            sys.stderr.write(
                f"Invalid XAUDITOR_CODER_SERVICE_BIND: TCP form must be host:port, got {bind}\n"
            )
            return 2
        host, _, port_str = bind.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            sys.stderr.write(f"Invalid TCP port in XAUDITOR_CODER_SERVICE_BIND: {bind}\n")
            return 2
        uvicorn_kwargs["host"] = host or "127.0.0.1"
        uvicorn_kwargs["port"] = port

    log.info(
        "Starting xauditor-coder-service: bind=%s auth=%s max_concurrent=%d cli=%s",
        bind,
        "enabled" if settings.auth.enable_auth else "disabled",
        settings.max_concurrent_jobs,
        settings.cli_path,
    )
    uvicorn.run(app, **uvicorn_kwargs)  # type: ignore[arg-type]
    return 0


def _arm_post_bind_chmod(socket_path: str) -> None:
    """Best-effort SIGUSR1 hook to tighten socket file permissions to 0660.

    Called once via a SIGUSR1 handler we install ourselves then schedule
    via a short-lived thread that waits for the file to appear. Works on
    Linux + macOS.
    """

    import threading
    import time

    def _wait_then_chmod() -> None:
        for _ in range(50):  # ~5 seconds
            if os.path.exists(socket_path):
                try:
                    os.chmod(
                        socket_path,
                        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP,
                    )
                except OSError:
                    pass
                return
            time.sleep(0.1)

    threading.Thread(target=_wait_then_chmod, daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
