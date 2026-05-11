from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from xauditor.config import (
    LLMConfig,
    LLMSettings,
    PROVIDER_KIND_ANTHROPIC,
    PROVIDER_KIND_OPENAI,
)
from xauditor.errors import PreflightError


log = logging.getLogger("xauditor.model_factory")


# Sentinel for ``build_chat_model(request_timeout_seconds=...)`` so the
# caller can distinguish "I want None (no timeout kwarg)" from "I haven't
# set this — inherit from provider".
_UNSET: Any = object()
from xauditor.prompts import (
    ANALYZER_PROMPT,
    CLASS_SUMMARY_PROMPT,
    DEDUP_JUDGE_PROMPT,
    EXPLOITATION_PROMPT,
    FINDING_SUMMARY_PROMPT,
    FUNCTION_SUMMARY_PROMPT,
    VALIDATOR_DEBATE_PROMPT,
    VALIDATOR_PROMPT,
)

_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>\s*", re.DOTALL | re.IGNORECASE)

# Matches a leading-and-trailing markdown code fence around the entire
# response. Anchored to the full string with ``fullmatch`` semantics so a
# stray triple-backtick inside a JSON string body cannot accidentally
# trigger the strip. Optional ``json`` / ``JSON`` language tag is
# captured and discarded; ``re.DOTALL`` lets ``.`` consume newlines so
# the fenced body parses as a single group.
_FENCE_RE = re.compile(
    r"\A\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*\Z",
    re.DOTALL,
)


def _strip_markdown_fences(text: str) -> str:
    """Return ``text`` with a wrapping markdown code fence removed.

    Some LLM providers (Anthropic Claude, several OpenAI-compat
    gateways) return structured output wrapped in a markdown code fence
    such as `````json\\n{...}\\n`````. The
    leading backticks make ``json.loads`` fail at column 0 with an
    ``Expecting value`` error, which the resilience layer would then
    burn retry attempts on without changing the prompt or the wrapper.
    Stripping the fence here lets a fence-wrapped well-formed JSON body
    parse on the first attempt while leaving fenceless responses
    untouched.
    """

    match = _FENCE_RE.match(text)
    if match is None:
        return text
    return match.group(1).strip()


def _split_thinking(content: str) -> tuple[str, str]:
    """Separate `<think>...</think>` reasoning blocks from the final answer.

    Returns (answer_without_think, concatenated_thinking). Qwen/vLLM-style providers
    emit thinking inline; we keep the reasoning so callers can surface it in logs.
    """
    thinking_parts = [match.group(1).strip() for match in _THINK_BLOCK_RE.finditer(content)]
    answer = _THINK_BLOCK_RE.sub("", content).lstrip()
    return answer, "\n\n".join(part for part in thinking_parts if part)

try:
    from langchain_openai import ChatOpenAI

    LANGCHAIN_OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    ChatOpenAI = None
    LANGCHAIN_OPENAI_AVAILABLE = False


try:
    from langchain_anthropic import ChatAnthropic

    LANGCHAIN_ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    ChatAnthropic = None
    LANGCHAIN_ANTHROPIC_AVAILABLE = False


# Defaults for the Anthropic provider's ``max_tokens`` and (on the
# legacy ``thinking_enabled: true`` branch) ``thinking.budget_tokens``.
# Both are exposed as ``llm.providers.<name>.{max_tokens,
# thinking_budget_tokens}`` yaml fields so operators can size them to
# the target model's actual ceiling.
#
# 64K fits within every currently-shipping Claude variant we target,
# including preview/private models with smaller output ceilings (e.g.
# ``claude-mythos-preview`` caps at 128K output). Operators on
# larger-context models (Sonnet/Opus 1M-context) should override
# upward in their yaml.
#
# Why we always send ``max_tokens``: passing nothing would let
# langchain-anthropic 1.4.x's ``set_default_max_tokens`` validator
# fall back to 4096 when the model has no registered profile (e.g.
# preview / private Claude models), which extended thinking
# immediately consumes, leaving the response with only
# ``type:"thinking"`` parts and an empty ``type:"text"`` block
# (downstream ``invoke_json`` then blows up on ``json.loads("")``).
_ANTHROPIC_DEFAULT_MAX_TOKENS = 64_000
_ANTHROPIC_DEFAULT_THINKING_BUDGET_TOKENS = 32_000


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class SamplingParams:
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SamplingParams":
        if not data:
            return cls()
        return cls(
            temperature=data.get("temperature"),
            top_p=data.get("top_p"),
            top_k=data.get("top_k"),
            repetition_penalty=data.get("repetition_penalty"),
        )

    def as_manifest(self) -> dict[str, float | int | None]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
        }

    def is_empty(self) -> bool:
        return all(
            getattr(self, field_name) is None
            for field_name in ("temperature", "top_p", "top_k", "repetition_penalty")
        )


def build_openai_kwargs(
    sampling: SamplingParams,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split sampling params into (top-level kwargs, extra_body additions)."""
    top_level: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    if sampling.temperature is not None:
        top_level["temperature"] = sampling.temperature
    if sampling.top_p is not None:
        top_level["top_p"] = sampling.top_p
    if sampling.top_k is not None:
        extra_body["top_k"] = sampling.top_k
    if sampling.repetition_penalty is not None:
        extra_body["repetition_penalty"] = sampling.repetition_penalty
    return top_level, extra_body


class ChatModel(Protocol):
    provider_name: str
    model_name: str
    is_mock: bool

    def invoke_json(self, system: str, user: dict[str, Any], *, user_text: str | None = None) -> dict[str, Any]: ...
    def invoke_text(self, system: str, user: dict[str, Any], *, user_text: str | None = None) -> str: ...


def _mock_response(system: str, user: dict[str, Any]) -> dict[str, Any]:
    if system == FUNCTION_SUMMARY_PROMPT:
        function_name = str(user.get("function_name", "function"))
        file_path = str(user.get("file_path", "unknown"))
        return {"summary": f"{function_name} in {file_path} handles application logic."}
    if system == CLASS_SUMMARY_PROMPT:
        class_name = str(user.get("class_name", "Class"))
        file_path = str(user.get("file_path", "unknown"))
        return {
            "summary": f"{class_name} in {file_path} coordinates class behavior.",
            "business_context": f"{class_name} owns class-scoped members for repository behavior.",
        }
    if system == ANALYZER_PROMPT:
        definitions = user.get("function_definitions", ()) or user.get("path_functions", ())
        for function in definitions if isinstance(definitions, list | tuple) else ():
            source = str(function.get("source", ""))
            if "subprocess.run" in source and "shell=True" in source.replace(" ", ""):
                return {
                    "status": "candidate",
                    "finding_name": "Command Injection via shell=True",
                    "description": "Path reaches a dangerous sink.",
                    "reason": "Source-backed path evidence reaches subprocess.run with shell=True.",
                    "suspect_function_id": str(function.get("function_id", "")),
                    "suspect_line": int(function.get("start_line", 0)) + 1,
                    "evidence_strength": "high",
                }
            if "eval(" in source or "exec(" in source:
                return {
                    "status": "candidate",
                    "finding_name": "Dynamic Code Execution",
                    "description": "Path reaches a dynamic execution sink.",
                    "reason": "Source-backed path evidence reaches eval/exec.",
                    "suspect_function_id": str(function.get("function_id", "")),
                    "suspect_line": int(function.get("start_line", 0)) + 1,
                    "evidence_strength": "medium",
                }
        return {
            "status": "no_issue",
            "finding_name": "",
            "description": "",
            "reason": "No dangerous sink was confirmed on this path.",
            "suspect_function_id": "",
            "suspect_line": 0,
            "evidence_strength": "low",
        }
    if system == EXPLOITATION_PROMPT:
        finding_name = str(user.get("finding_name", ""))
        if "shell=True" in finding_name:
            return {
                "status": "ready",
                "steps": "Supply shell metacharacters in the user-controlled input to execute arbitrary commands.",
            }
        if not finding_name:
            return {
                "status": "not_applicable",
                "steps": "No exploitation guidance was generated because the analyzer did not confirm a candidate issue.",
            }
        return {
            "status": "ready",
            "steps": "Supply attacker-controlled code or expressions to reach the dynamic execution sink.",
        }
    if system == VALIDATOR_PROMPT:
        evidence_strength = str(user.get("evidence_strength", "low"))
        if evidence_strength == "high":
            status = "Valid"
            analysis = "The validator confirmed the dangerous sink and input flow on the recorded path."
        elif evidence_strength == "medium":
            status = "Partial Valid"
            analysis = "The validator confirmed part of the path evidence but the exploitability preconditions are weaker."
        else:
            status = "False Positive"
            analysis = "The validator did not confirm a dangerous sink on this path."
        return {"status": status, "analysis": analysis}
    if system == DEDUP_JUDGE_PROMPT:
        left = user.get("record_a") or {}
        right = user.get("record_b") or {}
        same = False
        if isinstance(left, dict) and isinstance(right, dict):
            same = all(left.get(key) == right.get(key) for key in left.keys() & right.keys()) and bool(
                left.keys() & right.keys()
            )
        return {
            "same": bool(same),
            "reason": "Mock judge treated records with matching shared fields as duplicates.",
        }
    if system == FINDING_SUMMARY_PROMPT:
        record = user.get("record") or user
        if isinstance(record, dict):
            parts = [f"{key}={value}" for key, value in sorted(record.items()) if value not in ("", None)]
            summary = "; ".join(parts)[:240]
        else:
            summary = str(record)[:240]
        return {"summary": summary or "mock summary"}
    if system == VALIDATOR_DEBATE_PROMPT:
        position = str(user.get("my_last_verdict", user.get("current_verdict", ""))).strip().lower()
        verdict = position or "uncertain"
        return {
            "verdict": verdict,
            "rebuttal": "Mock debater restated their prior verdict without new evidence.",
        }
    marker = system.strip().split(".")[0][:80]
    return {
        "summary": f"{marker} ({sorted(user.keys())})",
        "business_context": f"mocked context for {sorted(user.keys())}",
        "trust_boundary": "internal flow",
    }


@dataclass
class MockChatModel:
    provider_name: str
    model_name: str
    is_mock: bool = True
    sampling: SamplingParams = field(default_factory=SamplingParams)

    def invoke_json(self, system: str, user: dict[str, Any], *, user_text: str | None = None) -> dict[str, Any]:
        return _mock_response(system, user)

    def invoke_text(self, system: str, user: dict[str, Any], *, user_text: str | None = None) -> str:
        return json.dumps(_mock_response(system, user))


@dataclass
class LangChainChatModel:
    provider_name: str
    model_name: str
    base_url: str
    api_key: str
    thinking_enabled: bool
    sampling: SamplingParams = field(default_factory=SamplingParams)
    is_mock: bool = False
    # Per-call request timeout (seconds). ``None`` means "do not pass
    # a timeout kwarg" — the OpenAI Python SDK's own default
    # (~600s) applies. See ``add-llm-request-timeout-config``.
    request_timeout_seconds: float | None = None
    # Thread-local stash for the most-recent ``invoke_text`` call's
    # ``thinking`` text and reasoning-token count. Each thread gets
    # its own slot so concurrent ``path_concurrency`` workers
    # sharing one ``LangChainChatModel`` instance don't read each
    # other's response metadata when building their per-call log
    # lines. Pre-fix this state was held as plain instance
    # attributes, which raced under the parallel audit topology
    # (xauditor 0.6.0+) — see
    # ``openspec/changes/remove-llm-circuit-breaker/`` D5.
    _per_thread_meta: threading.local = field(
        default_factory=threading.local, init=False, repr=False, compare=False
    )

    @property
    def last_thinking(self) -> str:
        return getattr(self._per_thread_meta, "thinking", "")

    @property
    def last_reasoning_tokens(self) -> int:
        return getattr(self._per_thread_meta, "reasoning_tokens", 0)

    def _chat(self) -> Any:
        if not LANGCHAIN_OPENAI_AVAILABLE:
            raise PreflightError(_missing_langchain_openai_message(self.provider_name))
        top_level, sampling_extra_body = build_openai_kwargs(self.sampling)
        extra_body: dict[str, Any] = {"enable_thinking": self.thinking_enabled}
        extra_body.update(sampling_extra_body)
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model_name,
            "model_kwargs": {"response_format": {"type": "json_object"}},
            "extra_body": extra_body,
        }
        if self.request_timeout_seconds is not None:
            # ChatOpenAI exposes the timeout as ``request_timeout``;
            # keeping the kwarg name explicit so a future SDK rename
            # surfaces here, not silently elsewhere.
            kwargs["request_timeout"] = self.request_timeout_seconds
        kwargs.update(top_level)
        return ChatOpenAI(**kwargs)

    def invoke_text(self, system: str, user: dict[str, Any], *, user_text: str | None = None) -> str:
        chat = self._chat()
        content = user_text if user_text is not None else json.dumps(user)
        response = chat.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ]
        )
        raw_content = getattr(response, "content", str(response))
        thinking_fragments: list[str] = []
        if isinstance(raw_content, list):
            text_parts: list[str] = []
            for part in raw_content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text_parts.append(part.get("text", ""))
                elif part_type == "thinking":
                    thinking_fragments.append(str(part.get("thinking", "")).strip())
            content = "".join(text_parts)
        else:
            content = str(raw_content)
        answer, inline_thinking = _split_thinking(content)
        if inline_thinking:
            thinking_fragments.append(inline_thinking)
        # Stash on the thread-local view (read-back via the
        # ``last_thinking`` / ``last_reasoning_tokens`` properties)
        # so concurrent callers on the same shared chat model
        # don't observe each other's metadata.
        self._per_thread_meta.thinking = "\n\n".join(
            fragment for fragment in thinking_fragments if fragment
        )
        metadata = getattr(response, "response_metadata", {}) or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
        details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
        if isinstance(details, dict):
            self._per_thread_meta.reasoning_tokens = int(
                details.get("reasoning_tokens", 0) or 0
            )
        else:
            self._per_thread_meta.reasoning_tokens = 0
        return answer

    def invoke_json(self, system: str, user: dict[str, Any], *, user_text: str | None = None) -> dict[str, Any]:
        text = _strip_markdown_fences(self.invoke_text(system, user, user_text=user_text))
        return json.loads(text)


@dataclass
class AnthropicLangChainChatModel:
    """Anthropic-native chat model — sibling of ``LangChainChatModel``.

    Built when a provider declares ``kind: anthropic`` in yaml. Uses
    ``langchain_anthropic.ChatAnthropic`` against the configured
    ``base_url`` (which lets operators target ``api.anthropic.com``
    directly OR a Claude-API-compatible internal endpoint without
    going through Anthropic's OpenAI-compat shim).

    Sampling-field mapping (see ``add-anthropic-provider-kind`` design
    decision D4):
    - ``temperature``, ``top_p``, ``top_k``: top-level kwargs (no
      ``extra_body`` round-trip — Anthropic accepts ``top_k`` natively).
    - ``repetition_penalty``: dropped with a one-time ``WARNING`` per
      instance, because the Anthropic API has no equivalent parameter.
      We do NOT silently pretend the value took effect.

    ``thinking_enabled: true`` translates to
    ``thinking={"type": "enabled", "budget_tokens": ...}`` on the
    underlying ``ChatAnthropic`` constructor, plus a ``max_tokens``
    floor that is required by the SDK whenever extended thinking is on.
    """

    provider_name: str
    model_name: str
    base_url: str
    api_key: str
    thinking_enabled: bool
    sampling: SamplingParams = field(default_factory=SamplingParams)
    is_mock: bool = False
    # Hard ceiling sent to ChatAnthropic as ``max_tokens=``. Required
    # by the Anthropic SDK (see module-level comment). Sized to fit
    # every currently-shipping Claude output ceiling; operators on
    # 1M-context Sonnet/Opus should override upward via the yaml.
    max_tokens: int = _ANTHROPIC_DEFAULT_MAX_TOKENS
    thinking_budget_tokens: int = _ANTHROPIC_DEFAULT_THINKING_BUDGET_TOKENS
    # Discrete extended-thinking dial. ``"low" | "medium" | "high" |
    # "xhigh" | "max"``. ``None`` means "fall back to thinking_enabled
    # + budget_tokens shape" (the legacy path that Anthropic is
    # deprecating). When set this takes precedence over
    # ``thinking_enabled``.
    thinking_effort: str | None = None
    # Per-call request timeout (seconds). ``None`` means "do not pass
    # a timeout kwarg" — the Anthropic Python SDK's own default
    # (~600s) applies. See ``add-llm-request-timeout-config``.
    request_timeout_seconds: float | None = None
    _warned_repetition_penalty: bool = field(default=False, init=False, repr=False)
    _warned_thinking_legacy: bool = field(default=False, init=False, repr=False)
    # Same per-thread metadata stash as ``LangChainChatModel`` —
    # see that class's docstring for the rationale.
    _per_thread_meta: threading.local = field(
        default_factory=threading.local, init=False, repr=False, compare=False
    )

    @property
    def last_thinking(self) -> str:
        return getattr(self._per_thread_meta, "thinking", "")

    @property
    def last_reasoning_tokens(self) -> int:
        return getattr(self._per_thread_meta, "reasoning_tokens", 0)

    def _chat(self) -> Any:
        if not LANGCHAIN_ANTHROPIC_AVAILABLE:
            raise PreflightError(
                _missing_langchain_anthropic_message(self.provider_name)
            )
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "anthropic_api_key": self.api_key,
        }
        # ``base_url`` from the yaml maps to Anthropic SDK's
        # ``anthropic_api_url`` so operators can target an internal
        # Claude-API-compatible endpoint without re-pointing the SDK.
        if self.base_url:
            kwargs["anthropic_api_url"] = self.base_url
        if self.sampling.temperature is not None:
            kwargs["temperature"] = self.sampling.temperature
        if self.sampling.top_p is not None:
            kwargs["top_p"] = self.sampling.top_p
        if self.sampling.top_k is not None:
            kwargs["top_k"] = self.sampling.top_k
        if (
            self.sampling.repetition_penalty is not None
            and not self._warned_repetition_penalty
        ):
            log.warning(
                "Anthropic provider `%s` received "
                "repetition_penalty=%r but the Anthropic API has no "
                "equivalent parameter; ignored.",
                self.provider_name,
                self.sampling.repetition_penalty,
            )
            # Dataclass is non-frozen so direct assignment is fine; the
            # flag dedupes the warning across repeated invocations of
            # this same instance.
            self._warned_repetition_penalty = True
        # Extended-thinking dial. Two paths:
        #   1. ``thinking_effort`` (preferred — discrete level the
        #      Anthropic API now expects). Routes to ChatAnthropic's
        #      ``effort=`` kwarg.
        #   2. legacy ``thinking_enabled: true`` (back-compat) routes
        #      to the old ``thinking={"type":"enabled", "budget_tokens"
        #      :N}`` shape. Anthropic is deprecating this; we emit a
        #      one-time WARNING per instance recommending the operator
        #      switch to ``thinking_effort``.
        # Operators who set BOTH get the new ``effort`` path (it wins).
        if self.thinking_effort is not None:
            kwargs["effort"] = self.thinking_effort
            kwargs["max_tokens"] = self.max_tokens
        elif self.thinking_enabled:
            if not self._warned_thinking_legacy:
                log.warning(
                    "Anthropic provider `%s` has `thinking_enabled: true` "
                    "without `thinking_effort`; using the legacy "
                    "`thinking={\"type\": \"enabled\", \"budget_tokens\": %d}` "
                    "shape, which Anthropic is deprecating. Set "
                    "`thinking_effort: high` (or low/medium/xhigh/max) on "
                    "the provider to migrate to the supported dial.",
                    self.provider_name,
                    self.thinking_budget_tokens,
                )
                self._warned_thinking_legacy = True
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget_tokens,
            }
            # ``max_tokens`` is required by the Anthropic SDK whenever
            # ``thinking`` is on, and the API rejects requests where
            # ``max_tokens <= budget_tokens``. Operators are responsible
            # for keeping the two consistent in their yaml.
            kwargs["max_tokens"] = self.max_tokens
        if self.request_timeout_seconds is not None:
            # ChatAnthropic exposes the timeout as
            # ``default_request_timeout``; keeping the kwarg name
            # explicit so a future SDK rename surfaces here.
            kwargs["default_request_timeout"] = self.request_timeout_seconds
        return ChatAnthropic(**kwargs)

    def invoke_text(
        self, system: str, user: dict[str, Any], *, user_text: str | None = None
    ) -> str:
        chat = self._chat()
        content = user_text if user_text is not None else json.dumps(user)
        # langchain-anthropic recognises ``role: "system"`` in the
        # message list and extracts it into the SDK's top-level
        # ``system`` parameter, so we keep the same dict shape the
        # OpenAI path uses.
        response = chat.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ]
        )
        raw_content = getattr(response, "content", str(response))
        thinking_fragments: list[str] = []
        if isinstance(raw_content, list):
            text_parts: list[str] = []
            for part in raw_content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text_parts.append(part.get("text", ""))
                elif part_type == "thinking":
                    # Anthropic's extended-thinking blocks live in
                    # ``part["thinking"]`` (some SDK versions) or
                    # ``part["text"]`` — accept either shape.
                    thinking_fragments.append(
                        str(part.get("thinking") or part.get("text") or "").strip()
                    )
            content_str = "".join(text_parts)
        else:
            content_str = str(raw_content)
        answer, inline_thinking = _split_thinking(content_str)
        if inline_thinking:
            thinking_fragments.append(inline_thinking)
        # Per-thread stash (see ``_per_thread_meta`` docstring).
        self._per_thread_meta.thinking = "\n\n".join(
            fragment for fragment in thinking_fragments if fragment
        )
        metadata = getattr(response, "response_metadata", {}) or {}
        usage = metadata.get("usage") or metadata.get("token_usage") or {}
        # Anthropic exposes thinking-token usage under
        # ``usage["cache_read_input_tokens"]`` / similar; we surface
        # the count when present, otherwise fall back to 0.
        self._per_thread_meta.reasoning_tokens = (
            int(usage.get("thinking_tokens", 0) or 0)
            if isinstance(usage, dict)
            else 0
        )
        return answer

    def invoke_json(
        self, system: str, user: dict[str, Any], *, user_text: str | None = None
    ) -> dict[str, Any]:
        text = _strip_markdown_fences(
            self.invoke_text(system, user, user_text=user_text)
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Same lenient fallback as the coder stage uses — handles
            # missing-comma / trailing-comma typos that LLMs sometimes
            # emit. Lazy import keeps model_factory free of an
            # otherwise-unnecessary dependency on audit/coder.py.
            from xauditor.audit.coder import _repair_json_typos

            return json.loads(_repair_json_typos(text))


def build_chat_model(
    provider: LLMConfig,
    *,
    provider_name: str = "default",
    sampling: SamplingParams | Mapping[str, Any] | None = None,
    request_timeout_seconds: float | None = _UNSET,
) -> ChatModel:
    """Build a workflow-ready chat model for a single provider config.

    ``request_timeout_seconds`` is the resolved per-call timeout (in
    seconds) for the underlying SDK. The sentinel ``_UNSET`` (the
    default) means "inherit from ``provider.request_timeout_seconds``"
    so callers that don't know about the field still get the right
    value. Pass ``None`` explicitly to force "no timeout kwarg".
    """

    if isinstance(sampling, Mapping):
        resolved_sampling = SamplingParams.from_mapping(sampling)
    elif sampling is None:
        resolved_sampling = SamplingParams.from_mapping(provider.sampling_dict())
    else:
        resolved_sampling = sampling
    if request_timeout_seconds is _UNSET:
        request_timeout_seconds = provider.request_timeout_seconds
    if not provider.base_url or provider.base_url.startswith("mock://"):
        return MockChatModel(
            provider_name=provider_name,
            model_name=provider.model_name or "mock-model",
            sampling=resolved_sampling,
        )
    if provider.kind == PROVIDER_KIND_ANTHROPIC:
        anthropic_kwargs: dict[str, Any] = {
            "provider_name": provider_name,
            "model_name": provider.model_name,
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "thinking_enabled": provider.thinking_enabled,
            "thinking_effort": provider.thinking_effort,
            "sampling": resolved_sampling,
            "request_timeout_seconds": request_timeout_seconds,
        }
        if provider.max_tokens is not None:
            anthropic_kwargs["max_tokens"] = provider.max_tokens
        if provider.thinking_budget_tokens is not None:
            anthropic_kwargs["thinking_budget_tokens"] = provider.thinking_budget_tokens
        return AnthropicLangChainChatModel(**anthropic_kwargs)
    return LangChainChatModel(
        provider_name=provider_name,
        model_name=provider.model_name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        thinking_enabled=provider.thinking_enabled,
        sampling=resolved_sampling,
        request_timeout_seconds=request_timeout_seconds,
    )


def ensure_provider_runtime_available(provider: LLMConfig, *, provider_name: str = "default") -> None:
    """Fail fast for real providers when optional runtime dependencies are unavailable."""
    if not provider.base_url or provider.base_url.startswith("mock://"):
        return
    if provider.kind == PROVIDER_KIND_ANTHROPIC:
        if not LANGCHAIN_ANTHROPIC_AVAILABLE:
            raise PreflightError(
                _missing_langchain_anthropic_message(provider_name)
            )
        return
    if not LANGCHAIN_OPENAI_AVAILABLE:
        raise PreflightError(_missing_langchain_openai_message(provider_name))


def missing_temperature_warning(llm: LLMSettings) -> str | None:
    """Return a warning message if no real provider has `temperature` configured.

    The implicit `temperature=0` default was removed; operators who relied on it
    should set `temperature: 0` explicitly. Returns None when a warning is not
    warranted (e.g. no real providers, or at least one has temperature set).
    """
    real_providers = [
        provider
        for provider in llm.providers.values()
        if provider.base_url and not provider.base_url.startswith("mock://")
    ]
    if not real_providers:
        return None
    if any(provider.temperature is not None for provider in real_providers):
        return None
    return (
        "No LLM provider has `temperature` configured. The implicit `temperature=0` "
        "default has been removed; set `temperature: 0` on the active provider to "
        "preserve deterministic sampling, or pick a value that matches your use case."
    )


def resolve_chat_model(llm: LLMSettings, *, agent: str | None = None) -> ChatModel:
    """Resolve the selected provider for *agent* and return its chat model."""
    provider_name = llm.selected_provider(agent) or "default"
    provider = llm.provider_for(agent)
    sampling = SamplingParams.from_mapping(llm.sampling_for(agent))
    request_timeout_seconds = llm.request_timeout_for(agent)
    return build_chat_model(
        provider,
        provider_name=provider_name,
        sampling=sampling,
        request_timeout_seconds=request_timeout_seconds,
    )


def _missing_langchain_openai_message(provider_name: str) -> str:
    return (
        f"Real LLM provider `{provider_name}` requires `langchain-openai`. "
        "Run `uv sync` to install project dependencies, or use a `mock://` base_url for tests."
    )


def _missing_langchain_anthropic_message(provider_name: str) -> str:
    return (
        f"Real LLM provider `{provider_name}` declared `kind: anthropic` but "
        "`langchain-anthropic` is not installed. Run "
        "`pip install langchain-anthropic` (or `uv sync` to pick up project "
        "dependencies), or set a `mock://` base_url for tests."
    )


__all__ = [
    "AnthropicLangChainChatModel",
    "ChatMessage",
    "ChatModel",
    "LangChainChatModel",
    "MockChatModel",
    "SamplingParams",
    "build_chat_model",
    "build_openai_kwargs",
    "ensure_provider_runtime_available",
    "missing_temperature_warning",
    "resolve_chat_model",
]
