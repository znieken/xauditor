from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO


LEVEL_ORDER = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}


class RuntimeLogger:
    def __init__(
        self,
        *,
        level: str = "info",
        stream: TextIO | None = None,
        redactions: tuple[str, ...] = (),
        log_file: Path | str | None = None,
        tag: str = "",
    ) -> None:
        self.level = level
        self.stream = stream or sys.stderr
        self.redactions = tuple(sorted({item for item in redactions if item}, key=len, reverse=True))
        self.log_file = Path(log_file) if log_file else None
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        # ``tag`` prefixes every emitted line. Subprocess audit
        # workers set this to their ``worker_id`` (e.g.
        # ``audit-worker-2``) so the operator can tell interleaved
        # multi-worker output apart in the master's stderr / shared
        # log file. Empty by default → behaviour is identical to
        # pre-tag releases.
        self.tag = tag

    def register_secrets(self, values) -> None:
        merged = set(self.redactions)
        for value in values:
            if value:
                merged.add(value)
        self.redactions = tuple(sorted(merged, key=len, reverse=True))

    @classmethod
    def from_config(
        cls,
        config,
        *,
        stream: TextIO | None = None,
        tag: str = "",
    ) -> "RuntimeLogger":
        llm_redactions = (
            config.llm.api_keys()
            if hasattr(config.llm, "api_keys")
            else (getattr(config.llm, "api_key", ""),)
        )
        coder = getattr(config, "coder", None)
        coder_api_key = getattr(coder, "model_api_key", "")
        coder_endpoint_token = getattr(coder, "endpoint_token", "")
        return cls(
            level=config.logging.level,
            stream=stream,
            redactions=(
                llm_redactions
                + (getattr(config.graphdb, "password", ""),)
                + ((coder_api_key,) if coder_api_key else ())
                + ((coder_endpoint_token,) if coder_endpoint_token else ())
            ),
            log_file=getattr(config.logging, "file", None),
            tag=tag,
        )

    def debug(self, message: str) -> None:
        self._emit("debug", message)

    def debug_kv(self, message: str, **details: object) -> None:
        rendered = [message]
        for key, value in details.items():
            if value is None:
                continue
            rendered.append(f"{key}={self._format_detail(value)}")
        self._emit("debug", " | ".join(rendered))

    def info(self, message: str) -> None:
        self._emit("info", message)

    def info_kv(self, message: str, **details: object) -> None:
        rendered = [message]
        for key, value in details.items():
            if value is None:
                continue
            rendered.append(f"{key}={self._format_detail(value)}")
        self._emit("info", " | ".join(rendered))

    def warning(self, message: str) -> None:
        self._emit("warning", message)

    def warning_kv(self, message: str, **details: object) -> None:
        rendered = [message]
        for key, value in details.items():
            if value is None:
                continue
            rendered.append(f"{key}={self._format_detail(value)}")
        self._emit("warning", " | ".join(rendered))

    def error(self, message: str) -> None:
        self._emit("error", message)

    def error_kv(self, message: str, **details: object) -> None:
        rendered = [message]
        for key, value in details.items():
            if value is None:
                continue
            rendered.append(f"{key}={self._format_detail(value)}")
        self._emit("error", " | ".join(rendered))

    def _emit(self, level: str, message: str) -> None:
        if LEVEL_ORDER[level] < LEVEL_ORDER[self.level]:
            return
        module = self._caller_module()
        # Format examples:
        #   tag empty, module known → "INFO [audit.workflow]: msg"
        #   tag empty, module unknown → "INFO: msg"
        #   tag set, module known     → "INFO [audit-worker-2/audit.workflow]: msg"
        #   tag set, module unknown   → "INFO [audit-worker-2]: msg"
        bracket_parts: list[str] = []
        if self.tag:
            bracket_parts.append(self.tag)
        if module:
            bracket_parts.append(module)
        if bracket_parts:
            prefix = f"{level.upper()} [{'/'.join(bracket_parts)}]"
        else:
            prefix = level.upper()
        line = f"{prefix}: {self._redact(message)}\n"
        # Single ``write`` per line keeps line atomicity at the
        # OS layer (writes < PIPE_BUF / file system block are
        # atomic on POSIX) so concurrent multi-worker writes to
        # the shared stderr or log_file don't tear individual
        # lines, only interleave them in event-time order.
        self.stream.write(line)
        # Flush after every emit so subprocess audit workers don't
        # leave debug output stranded in the stderr buffer. Python's
        # default sys.stderr is fully-buffered (8 KB) when stderr is
        # redirected to a file or pipe — without an explicit flush,
        # a worker that emits a few hundred bytes of debug then
        # blocks on a 30s LLM call won't show its log lines until
        # either the buffer fills or the process exits, defeating
        # the point of debug logging during a run. Flushing per
        # line costs ~one syscall per emit; cheap relative to the
        # surrounding LLM I/O.
        flush = getattr(self.stream, "flush", None)
        if flush is not None:
            try:
                flush()
            except Exception:  # noqa: BLE001 - flush failure non-fatal
                pass
        if self.log_file is not None:
            # ``with open("a")`` flushes + closes implicitly so the
            # log_file branch is already prompt; no extra flush
            # needed.
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _caller_module(self) -> str:
        frame = sys._getframe(1) if hasattr(sys, "_getframe") else None
        while frame is not None:
            module_name = frame.f_globals.get("__name__", "")
            if module_name and module_name != __name__:
                return module_name
            frame = frame.f_back
        return ""

    def _redact(self, message: str) -> str:
        redacted = message
        for secret in self.redactions:
            redacted = redacted.replace(secret, "***")
        return redacted

    def redact(self, text: str) -> str:
        """Apply the configured secrets-scrubbing filter to *text*.

        Public alias for :meth:`_redact`. Used by the on-demand
        ``audit export`` verb so exported artefacts share the same
        redaction contract as run-time logs.
        """

        return self._redact(text)

    def _format_detail(self, value: object, *, limit: int = 160) -> str:
        if isinstance(value, dict):
            items = list(value.items())
            value = ", ".join(
                [f"{key}={self._format_detail(item, limit=40)}" for key, item in items[:5]]
                + (["..."] if len(items) > 5 else [])
            )
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
            value = ", ".join([str(item) for item in items[:5]] + (["..."] if len(items) > 5 else []))
        text = self._redact(str(value))
        return text if len(text) <= limit else text[: limit - 3] + "..."


__all__ = ["RuntimeLogger"]
