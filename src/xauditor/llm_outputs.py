from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
from typing import Any

try:  # pragma: no cover - exercised when optional dependency is installed
    from pydantic import BaseModel as _PydanticBaseModel
    from pydantic import ValidationError as OutputValidationError
    from pydantic import field_validator as _pydantic_field_validator

    PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover - compatibility fallback is covered instead
    _PydanticBaseModel = None

    class OutputValidationError(ValueError):
        """Raised when a structured LLM response cannot be validated."""

    PYDANTIC_AVAILABLE = False


if PYDANTIC_AVAILABLE:  # pragma: no cover - optional dependency path
    class FunctionSummaryOutput(_PydanticBaseModel):
        summary: str


    class ClassSummaryOutput(_PydanticBaseModel):
        summary: str
        business_context: str


    def _coerce_steps_to_string(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list | tuple):
            parts: list[str] = []
            for index, item in enumerate(value, start=1):
                text = str(item).strip()
                if not text:
                    continue
                parts.append(text if text[:1].isdigit() else f"{index}. {text}")
            return "\n".join(parts)
        if value is None:
            return ""
        return str(value)


    class AnalyzerOutput(_PydanticBaseModel):
        status: str
        finding_name: str = ""
        description: str = ""
        analysis: str = ""
        reason: str = ""
        context_notes: str = ""
        suspect_function_id: str = ""
        suspect_line: int = 0
        evidence_strength: str = "low"

        @_pydantic_field_validator(
            "finding_name",
            "description",
            "analysis",
            "reason",
            "context_notes",
            mode="before",
        )
        @classmethod
        def _coerce_text_fields(cls, value):
            return _coerce_steps_to_string(value)


    class ExploitationOutput(_PydanticBaseModel):
        status: str
        steps: str

        @_pydantic_field_validator("steps", mode="before")
        @classmethod
        def _normalize_steps(cls, value):
            return _coerce_steps_to_string(value)


    class ValidationOutput(_PydanticBaseModel):
        status: str
        analysis: str

        @_pydantic_field_validator("analysis", mode="before")
        @classmethod
        def _coerce_analysis(cls, value):
            return _coerce_steps_to_string(value)


    class DedupJudgeOutput(_PydanticBaseModel):
        same: bool
        reason: str = ""

        @_pydantic_field_validator("reason", mode="before")
        @classmethod
        def _coerce_reason(cls, value):
            return _coerce_steps_to_string(value)


    class FindingSummaryOutput(_PydanticBaseModel):
        summary: str

        @_pydantic_field_validator("summary", mode="before")
        @classmethod
        def _coerce_summary(cls, value):
            return _coerce_steps_to_string(value)


    class ValidatorDebateOutput(_PydanticBaseModel):
        verdict: str
        rebuttal: str

        @_pydantic_field_validator("rebuttal", mode="before")
        @classmethod
        def _coerce_rebuttal(cls, value):
            return _coerce_steps_to_string(value)


    class ReconcilerOutput(_PydanticBaseModel):
        """Cross-unit reconciler verdict.

        Consumed by the agentic reconciler when multiple unit
        kinds emit findings sharing the same fingerprint.
        """

        verdict: str = "Inconclusive"
        reasoning: str = ""

        @_pydantic_field_validator("reasoning", mode="before")
        @classmethod
        def _coerce_reasoning(cls, value):
            return _coerce_steps_to_string(value)


else:
    class _CompatModel:
        @classmethod
        def model_validate(cls, data: dict[str, Any]):
            if not isinstance(data, dict):
                raise OutputValidationError(f"{cls.__name__} requires a mapping response.")
            values: dict[str, object] = {}
            for field in fields(cls):
                if field.name in data:
                    raw_value = data[field.name]
                elif field.default is not MISSING:
                    raw_value = field.default
                else:
                    raise OutputValidationError(f"{cls.__name__} missing required field `{field.name}`.")
                values[field.name] = cls._coerce_field(field.name, raw_value, field.type)
            return cls(**values)

        @staticmethod
        def _coerce_field(name: str, value: object, expected_type: object) -> object:
            origin = getattr(expected_type, "__origin__", None)
            args = getattr(expected_type, "__args__", ())
            if origin in {tuple, list} and args:
                item_type = args[0]
                if not isinstance(value, list | tuple):
                    raise OutputValidationError(f"Field `{name}` must be a list.")
                return tuple(_CompatModel._coerce_nested(item_type, item) for item in value)
            if hasattr(expected_type, "model_validate"):
                return expected_type.model_validate(value)
            if expected_type is int:
                try:
                    return int(value)
                except (TypeError, ValueError) as exc:
                    raise OutputValidationError(f"Field `{name}` must be an integer.") from exc
            if expected_type is str:
                if value is None:
                    raise OutputValidationError(f"Field `{name}` must be a string.")
                return str(value)
            return value

        @staticmethod
        def _coerce_nested(expected_type: object, value: object) -> object:
            if hasattr(expected_type, "model_validate"):
                return expected_type.model_validate(value)
            return value

        def model_dump(self) -> dict[str, object]:
            result: dict[str, object] = {}
            for field in fields(self):
                value = getattr(self, field.name)
                if isinstance(value, tuple):
                    result[field.name] = tuple(
                        item.model_dump() if hasattr(item, "model_dump") else item
                        for item in value
                    )
                else:
                    result[field.name] = value
            return result


    @dataclass(frozen=True)
    class FunctionSummaryOutput(_CompatModel):
        summary: str


    @dataclass(frozen=True)
    class ClassSummaryOutput(_CompatModel):
        summary: str
        business_context: str


    @dataclass(frozen=True)
    class AnalyzerOutput(_CompatModel):
        status: str
        finding_name: str = ""
        description: str = ""
        analysis: str = ""
        reason: str = ""
        context_notes: str = ""
        suspect_function_id: str = ""
        suspect_line: int = 0
        evidence_strength: str = "low"


    @dataclass(frozen=True)
    class ExploitationOutput(_CompatModel):
        status: str
        steps: str


    @dataclass(frozen=True)
    class ValidationOutput(_CompatModel):
        status: str
        analysis: str


    @dataclass(frozen=True)
    class DedupJudgeOutput(_CompatModel):
        same: bool
        reason: str = ""


    @dataclass(frozen=True)
    class FindingSummaryOutput(_CompatModel):
        summary: str


    @dataclass(frozen=True)
    class ValidatorDebateOutput(_CompatModel):
        verdict: str
        rebuttal: str


    @dataclass(frozen=True)
    class ReconcilerOutput(_CompatModel):
        verdict: str = "Inconclusive"
        reasoning: str = ""


__all__ = [
    "AnalyzerOutput",
    "ClassSummaryOutput",
    "DedupJudgeOutput",
    "ExploitationOutput",
    "FindingSummaryOutput",
    "FunctionSummaryOutput",
    "OutputValidationError",
    "PYDANTIC_AVAILABLE",
    "ReconcilerOutput",
    "ValidationOutput",
    "ValidatorDebateOutput",
]
