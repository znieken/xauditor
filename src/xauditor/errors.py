class XAuditorError(Exception):
    """Base application error."""


class ValidationError(XAuditorError):
    """Raised when external data cannot be validated safely."""


class ConfigError(XAuditorError):
    """Raised when configuration is invalid or incomplete."""


class GraphdbError(XAuditorError):
    """Raised when the managed graph database cannot be operated."""


class ReportDBError(XAuditorError):
    """Raised when the managed report database cannot be operated."""


class PortalError(XAuditorError):
    """Raised when the managed portal runtime cannot be operated."""


class ToolValidationError(XAuditorError):
    """Raised when a graph-discovery tool input or command is unsafe."""


class PreflightError(XAuditorError):
    """Raised when a required dependency is missing."""


class RetryExhaustedError(XAuditorError):
    """Raised when a retry loop runs out of attempts."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        attempts: int,
        last_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error


class CircuitOpenError(XAuditorError):
    """Raised when a circuit breaker blocks a call."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        cooldown_remaining: float | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.cooldown_remaining = cooldown_remaining


class LLMError(XAuditorError):
    """Raised when an LLM-backed operation cannot complete."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        provider: str = "",
        model_name: str = "",
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.provider = provider
        self.model_name = model_name


class ContextWindowExceededError(LLMError):
    """Raised when an LLM rejects a request because the prompt exceeds the model's context window."""


_CONTEXT_WINDOW_PATTERNS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "too many tokens",
    "token limit",
    "request too large",
    "this model's maximum",
    "exceeds the model's",
    "max_tokens_to_sample",
    "reduce the length",
)


def is_context_window_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if any(pattern in message for pattern in _CONTEXT_WINDOW_PATTERNS):
            return True
        current = current.__cause__ or current.__context__
    return False


class Neo4jError(XAuditorError):
    """Raised when a Neo4j-backed operation cannot complete."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        fingerprint: str = "",
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.fingerprint = fingerprint


class SecretNotFoundError(XAuditorError):
    """Raised when a required secret cannot be resolved."""

    def __init__(self, message: str, *, name: str = "") -> None:
        super().__init__(message)
        self.name = name


class SecurityError(XAuditorError):
    """Raised when user-supplied input fails security validation."""

    def __init__(self, message: str, *, field: str = "", value: str = "") -> None:
        super().__init__(message)
        self.field = field
        self.value = value


class UserCancelledError(XAuditorError):
    """Raised when a long-running workflow is cancelled by the user."""

    def __init__(self, message: str, *, partial_audit_run=None) -> None:
        super().__init__(message)
        self.partial_audit_run = partial_audit_run


__all__ = [
    "CircuitOpenError",
    "ConfigError",
    "ContextWindowExceededError",
    "GraphdbError",
    "LLMError",
    "Neo4jError",
    "PortalError",
    "PreflightError",
    "ReportDBError",
    "RetryExhaustedError",
    "SecurityError",
    "ToolValidationError",
    "UserCancelledError",
    "ValidationError",
    "XAuditorError",
    "is_context_window_error",
]
