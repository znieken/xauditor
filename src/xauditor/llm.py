from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from xauditor.config import LLMConfig
from xauditor.errors import (
    ContextWindowExceededError,
    LLMError,
    RetryExhaustedError,
    ValidationError,
    XAuditorError,
    is_context_window_error,
)
from xauditor.llm_outputs import (
    ClassSummaryOutput,
    FunctionSummaryOutput,
    OutputValidationError,
    PathSummaryOutput,
)
from xauditor.model_factory import ChatModel, build_chat_model
from xauditor.prompts import (
    CLASS_SUMMARY_PROMPT_SPEC,
    FUNCTION_SUMMARY_PROMPT_SPEC,
    PATH_SUMMARY_PROMPT_SPEC,
)
from xauditor.resilience import retry
from xauditor.runtime_logging import RuntimeLogger


@dataclass(frozen=True)
class LLMClient:
    config: LLMConfig
    provider_name: str = "default"
    logger: RuntimeLogger | None = None
    model: ChatModel = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", build_chat_model(self.config, provider_name=self.provider_name))

    @classmethod
    def from_config(
        cls,
        config,
        logger: RuntimeLogger | None = None,
        *,
        agent: str | None = None,
    ) -> "LLMClient":
        resolved = config.provider_for(agent) if hasattr(config, "provider_for") else config
        provider_name = config.selected_provider(agent) if hasattr(config, "selected_provider") else "default"
        return cls(config=resolved, provider_name=provider_name or "default", logger=logger)

    def summarize_function(self, function_name: str, file_path: str, source: str) -> str:
        if self.logger is not None:
            self.logger.debug_kv(
                "LLM summarize_function request",
                mode="mock" if self.model.is_mock else "shared-model",
                function_name=function_name,
                file_path=file_path,
                prompt_version=FUNCTION_SUMMARY_PROMPT_SPEC.version,
                source_size=len(source),
            )
        heuristic_summary = f"{function_name} in {file_path} handles {self._heuristic_purpose(function_name, source)}."
        if self.model.is_mock:
            summary = heuristic_summary
            if self.logger is not None:
                self.logger.debug_kv("LLM summarize_function response", summary=summary)
            return summary
        payload = self._invoke_json(
            system=FUNCTION_SUMMARY_PROMPT_SPEC.system,
            user={
                "function_name": function_name,
                "file_path": file_path,
                "source": source,
            },
            operation="summarize_function",
            prompt_version=FUNCTION_SUMMARY_PROMPT_SPEC.version,
            response_model=FunctionSummaryOutput,
            fallback_result={"summary": heuristic_summary},
        )
        summary = str(payload["summary"])
        if self.logger is not None:
            self.logger.debug_kv("LLM summarize_function response", summary=summary)
        return summary

    def summarize_class(
        self,
        class_name: str,
        file_path: str,
        source: str,
        *,
        method_names: tuple[str, ...] = (),
        member_names: tuple[str, ...] = (),
    ) -> dict[str, str]:
        if self.logger is not None:
            self.logger.debug_kv(
                "LLM summarize_class request",
                mode="mock" if self.model.is_mock else "shared-model",
                class_name=class_name,
                file_path=file_path,
                prompt_version=CLASS_SUMMARY_PROMPT_SPEC.version,
                method_count=len(method_names),
                member_count=len(member_names),
                source_size=len(source),
            )
        method_part = ", ".join(method_names) if method_names else "class behavior"
        member_part = ", ".join(member_names) if member_names else "no class-scoped members"
        fallback_result = {
            "summary": f"{class_name} in {file_path} coordinates {method_part}.",
            "business_context": f"{class_name} owns class-scoped members {member_part}.",
        }
        if self.model.is_mock:
            result = fallback_result
            if self.logger is not None:
                self.logger.debug_kv(
                    "LLM summarize_class response",
                    summary=result["summary"],
                    business_context=result["business_context"],
                )
            return result
        payload = self._invoke_json(
            system=CLASS_SUMMARY_PROMPT_SPEC.system,
            user={
                "class_name": class_name,
                "file_path": file_path,
                "source": source,
                "method_names": method_names,
                "member_names": member_names,
            },
            operation="summarize_class",
            prompt_version=CLASS_SUMMARY_PROMPT_SPEC.version,
            response_model=ClassSummaryOutput,
            fallback_result=fallback_result,
        )
        result = {
            "summary": str(payload["summary"]),
            "business_context": str(payload["business_context"]),
        }
        if self.logger is not None:
            self.logger.debug_kv(
                "LLM summarize_class response",
                summary=result["summary"],
                business_context=result["business_context"],
            )
        return result

    def summarize_path(self, entry_function: str, function_names: tuple[str, ...]) -> dict[str, str]:
        joined = " -> ".join(function_names)
        if self.logger is not None:
            self.logger.debug_kv(
                "LLM summarize_path request",
                mode="mock" if self.model.is_mock else "shared-model",
                entry_function=entry_function,
                prompt_version=PATH_SUMMARY_PROMPT_SPEC.version,
                function_count=len(function_names),
            )
        trust_boundary = (
            "crosses external input"
            if any("handler" in name or "request" in name for name in function_names)
            else "internal flow"
        )
        fallback_result = {
            "business_context": f"Path from {entry_function} through {joined}.",
            "trust_boundary": trust_boundary,
        }
        if self.model.is_mock:
            result = fallback_result
            if self.logger is not None:
                self.logger.debug_kv(
                    "LLM summarize_path response",
                    business_context=result["business_context"],
                    trust_boundary=result["trust_boundary"],
                )
            return result
        payload = self._invoke_json(
            system=PATH_SUMMARY_PROMPT_SPEC.system,
            user={"entry_function": entry_function, "function_names": function_names},
            operation="summarize_path",
            prompt_version=PATH_SUMMARY_PROMPT_SPEC.version,
            response_model=PathSummaryOutput,
            fallback_result=fallback_result,
        )
        result = {
            "business_context": str(payload["business_context"]),
            "trust_boundary": str(payload["trust_boundary"]),
        }
        if self.logger is not None:
            self.logger.debug_kv(
                "LLM summarize_path response",
                business_context=result["business_context"],
                trust_boundary=result["trust_boundary"],
            )
        return result

    def _heuristic_purpose(self, function_name: str, source: str) -> str:
        lowered = source.lower()
        if "subprocess" in lowered:
            return "process execution"
        if "request" in lowered or "handler" in function_name:
            return "request handling"
        return "application logic"

    def _invoke_json(
        self,
        system: str,
        user: dict[str, Any],
        *,
        operation: str,
        prompt_version: str,
        response_model: type[Any] | None = None,
        fallback_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.logger is not None:
            # Log ``base_url`` + ``wire_protocol`` instead of synthesizing an
            # ``endpoint`` string. The chat-model adapter (built per
            # ``provider.kind`` in ``build_chat_model``) owns the actual URL
            # path — ``/v1/messages`` for ``kind: anthropic``,
            # ``/chat/completions`` for ``kind: openai``. Re-declaring the
            # path here would only repeat what the SDK already knows and
            # gives operators a false signal when the two diverge.
            self.logger.debug_kv(
                "LLM request",
                base_url=self.config.base_url,
                wire_protocol=self.config.kind,
                model=self.model.model_name,
                provider=self.model.provider_name,
                prompt_version=prompt_version,
                thinking_enabled=self.config.thinking_enabled,
                response_format="json_object",
                payload_keys=tuple(sorted(user.keys())),
                payload_sizes={key: len(str(value)) for key, value in user.items()},
            )
            self.logger.debug(f"LLM request system [{operation}]:\n{system}")
            self.logger.debug(
                f"LLM request user [{operation}]:\n{json.dumps(user, indent=2, default=str, ensure_ascii=False)}"
            )
        def _call_model() -> dict[str, Any]:
            try:
                return self.model.invoke_json(system, user)
            except Exception as exc:
                if is_context_window_error(exc):
                    raise ContextWindowExceededError(
                        f"LLM context window exceeded during {operation}.",
                        operation=f"llm.{operation}",
                        provider=self.model.provider_name,
                        model_name=self.model.model_name,
                    ) from exc
                raise

        # No client-side circuit breaker: see
        # ``langchain_support.invoke_agent`` for rationale —
        # provider-side rate limiting is the authoritative source
        # of overload back-pressure, ``retry`` already caps
        # recursion, and per-path failure isolation already
        # routes exhausted retries to ``PathOutcome.LLM_FAILED``
        # cleanly.
        try:
            parsed = retry(
                _call_model,
                operation=f"llm.{operation}",
                attempts=3,
                base_delay=0.5,
                max_delay=2.0,
                exceptions=(Exception,),
                should_retry=lambda exc: not isinstance(exc, XAuditorError),
                logger=self.logger,
            )
        except RetryExhaustedError as exc:
            if self.logger is not None:
                self.logger.error_kv(
                    "LLM request failed",
                    operation=f"llm.{operation}",
                    provider=self.model.provider_name,
                    model=self.model.model_name,
                    attempts=getattr(exc, "attempts", None),
                    cause=str(getattr(exc, "last_error", exc)),
                    prompt_version=prompt_version,
                )
            raise LLMError(
                f"LLM request failed during {operation}.",
                operation=f"llm.{operation}",
                provider=self.model.provider_name,
                model_name=self.model.model_name,
            ) from exc
        if response_model is not None:
            try:
                parsed = response_model.model_validate(parsed).model_dump()
            except OutputValidationError as exc:
                if self.logger is not None:
                    self.logger.error_kv(
                        "LLM structured output validation failed",
                        operation=f"llm.{operation}",
                        provider=self.model.provider_name,
                        model=self.model.model_name,
                        prompt_version=prompt_version,
                        cause=str(exc),
                    )
                    if fallback_result is not None:
                        self.logger.warning(
                            f"Using heuristic fallback for {operation} after structured output validation failed."
                        )
                if fallback_result is not None:
                    return dict(fallback_result)
                raise ValidationError(
                    f"Structured output validation failed during {operation}."
                ) from exc
        if self.logger is not None:
            rendered = json.dumps(parsed, sort_keys=True, default=str)
            self.logger.debug_kv(
                "LLM response",
                prompt_version=prompt_version,
                response_keys=tuple(sorted(parsed.keys())),
                response_key_count=len(parsed),
                response_size=len(rendered),
            )
            self.logger.debug(
                f"LLM response body [{operation}]:\n"
                f"{json.dumps(parsed, indent=2, sort_keys=True, default=str, ensure_ascii=False)}"
            )
        return parsed
