from __future__ import annotations

import time
from typing import Callable, TypeVar

from xauditor.errors import CircuitOpenError, RetryExhaustedError


T = TypeVar("T")


def retry(
    call: Callable[[], T],
    *,
    operation: str,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    should_retry: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    logger=None,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    sleep_fn = time.sleep if sleep is None else sleep
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except exceptions as exc:
            if should_retry is not None and not should_retry(exc):
                raise
            if attempt >= attempts:
                raise RetryExhaustedError(
                    f"{operation} failed after {attempts} attempts.",
                    operation=operation,
                    attempts=attempts,
                    last_error=exc if isinstance(exc, Exception) else None,
                ) from exc
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) if base_delay > 0 else 0.0
            if logger is not None:
                logger.warning(
                    f"Retrying {operation} after attempt {attempt}/{attempts} in {delay:.2f}s: {exc}"
                )
            if delay > 0:
                sleep_fn(delay)
    raise AssertionError("retry() exhausted without returning or raising")


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        time_source: Callable[[], float] | None = None,
        service_name: str = "service",
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.time_source = time.monotonic if time_source is None else time_source
        self.service_name = service_name
        self.state = "closed"
        self.failure_count = 0
        self._opened_at: float | None = None

    def call(
        self,
        func: Callable[[], T],
        *,
        operation: str,
        exceptions: tuple[type[BaseException], ...] = (Exception,),
    ) -> T:
        now = self.time_source()
        if self.state == "open":
            cooldown_elapsed = 0.0 if self._opened_at is None else now - self._opened_at
            if cooldown_elapsed < self.cooldown_seconds:
                raise CircuitOpenError(
                    f"Circuit open for {self.service_name}.",
                    operation=operation,
                    cooldown_remaining=max(0.0, self.cooldown_seconds - cooldown_elapsed),
                )
            self.state = "half_open"
        try:
            result = func()
        except exceptions:
            self.failure_count += 1
            if self.state == "half_open" or self.failure_count >= self.failure_threshold:
                self.state = "open"
                self._opened_at = now
            raise
        else:
            self.failure_count = 0
            self.state = "closed"
            self._opened_at = None
            return result


__all__ = ["CircuitBreaker", "retry"]
