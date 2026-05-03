from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from xauditor.errors import (
    ContextWindowExceededError,
    LLMError,
    RetryExhaustedError,
    ValidationError,
    XAuditorError,
    is_context_window_error,
)
from xauditor.llm_outputs import OutputValidationError
from xauditor.model_factory import ChatModel
from xauditor.resilience import retry
from xauditor.runtime_logging import RuntimeLogger


T = TypeVar("T")


def invoke_agent(
    *,
    agent_name: str,
    system_prompt: str,
    payload: dict[str, Any],
    chat_model: ChatModel | None = None,
    executor: Callable[[dict[str, Any]], T] | None = None,
    logger: RuntimeLogger | None = None,
    response_model: type[Any] | None = None,
    response_parser: Callable[[dict[str, Any]], T] | None = None,
    prompt_version: str = "v1",
    user_message: str | None = None,
) -> tuple[T, dict[str, str]]:
    if chat_model is None and executor is None:
        raise ValueError("invoke_agent requires either a chat_model or an executor.")

    payload_json = json.dumps(payload, sort_keys=True, default=str)
    rendered_user = user_message if user_message is not None else payload_json

    def _invoke_chat_model() -> dict[str, Any]:
        assert chat_model is not None
        if logger is not None:
            logger.debug_kv(
                "LLM request",
                agent=agent_name,
                model=chat_model.model_name,
                provider=chat_model.provider_name,
                prompt_version=prompt_version,
                payload_keys=tuple(sorted(payload.keys())) if isinstance(payload, dict) else (),
                user_message_format="markdown" if user_message is not None else "json",
            )
            logger.debug(f"LLM request system [{agent_name}]:\n{system_prompt}")
            logger.debug(f"LLM request user [{agent_name}]:\n{rendered_user}")
        def _call_model() -> dict[str, Any]:
            try:
                return chat_model.invoke_json(
                    system_prompt,
                    payload,
                    user_text=user_message,
                )
            except Exception as exc:
                if is_context_window_error(exc):
                    raise ContextWindowExceededError(
                        f"LLM context window exceeded for agent `{agent_name}`.",
                        operation=f"llm.{agent_name}",
                        provider=chat_model.provider_name,
                        model_name=chat_model.model_name,
                    ) from exc
                raise

        # No client-side circuit breaker: provider-side rate
        # limiting (429 / Retry-After) is the authoritative source
        # of overload back-pressure. Per-call ``retry`` already
        # caps recursion against an unresponsive provider, and a
        # path whose retries exhaust gets classified as
        # ``PathOutcome.LLM_FAILED`` in isolation by the per-path
        # workflow handler — exactly the per-path failure
        # isolation ``add-failed-path-state-on-llm-error`` was
        # designed to deliver. See
        # ``openspec/changes/remove-llm-circuit-breaker/`` for
        # the diagnosis of how the prior shared breaker amplified
        # one path's transient failure into a 30-second cascade
        # killing dozens of paths.
        return retry(
            _call_model,
            operation=f"llm.{agent_name}",
            attempts=3,
            base_delay=0.5,
            max_delay=2.0,
            exceptions=(Exception,),
            should_retry=lambda exc: not isinstance(exc, XAuditorError),
            logger=logger,
        )

    try:
        if executor is not None:
            response = executor(payload)
            runtime = "langchain-executor"
        else:
            response = _invoke_chat_model()
            runtime = "shared-model"
        if logger is not None:
            response_keys = tuple(sorted(response.keys())) if isinstance(response, dict) else ()
            thinking = getattr(chat_model, "last_thinking", "") if chat_model is not None else ""
            reasoning_tokens = getattr(chat_model, "last_reasoning_tokens", 0) if chat_model is not None else 0
            logger.debug_kv(
                "LLM response",
                agent=agent_name,
                prompt_version=prompt_version,
                response_keys=response_keys,
                reasoning_tokens=reasoning_tokens,
                thinking_chars=len(thinking),
            )
            if thinking:
                logger.debug(f"LLM thinking [{agent_name}]:\n{thinking}")
            logger.debug(
                f"LLM response body [{agent_name}]:\n"
                f"{json.dumps(response, indent=2, sort_keys=True, default=str, ensure_ascii=False)}"
            )
        if response_model is not None:
            try:
                response = response_model.model_validate(response).model_dump()
            except OutputValidationError as exc:
                if logger is not None:
                    logger.error_kv(
                        "LLM structured output validation failed",
                        operation=f"llm.{agent_name}",
                        prompt_version=prompt_version,
                        provider=getattr(chat_model, "provider_name", None),
                        cause=str(exc),
                    )
                raise ValidationError(
                    f"Structured output validation failed for agent `{agent_name}`."
                ) from exc
        parsed = response_parser(response) if response_parser is not None else response
        meta = {
            "runtime": runtime,
            "agent_name": agent_name,
            "prompt_version": prompt_version,
        }
        if chat_model is not None:
            meta["provider_name"] = chat_model.provider_name
            thinking = getattr(chat_model, "last_thinking", "")
            if thinking:
                meta["thinking"] = thinking
            reasoning_tokens = getattr(chat_model, "last_reasoning_tokens", 0)
            if reasoning_tokens:
                meta["reasoning_tokens"] = str(reasoning_tokens)
        return parsed, meta
    except RetryExhaustedError as exc:
        if logger is not None:
            logger.error_kv(
                "LLM agent call failed",
                operation=f"llm.{agent_name}",
                provider=getattr(chat_model, "provider_name", None),
                attempts=getattr(exc, "attempts", None),
                cause=str(getattr(exc, "last_error", exc)),
                prompt_version=prompt_version,
            )
        raise LLMError(
            f"LLM agent call failed for `{agent_name}`.",
            operation=f"llm.{agent_name}",
            provider=getattr(chat_model, "provider_name", ""),
            model_name=getattr(chat_model, "model_name", ""),
        ) from exc
