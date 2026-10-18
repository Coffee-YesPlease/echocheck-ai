"""Strict boundaries for AI suggestions. A passing result is not a work permit."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ProviderUnavailable(RuntimeError):
    """The configured external processing service could not be used."""


class InvalidProviderResponse(RuntimeError):
    """A response cannot safely be used as verification evidence."""


MAX_TEXT_LENGTH = 20_000
MAX_REQUIREMENTS = 100


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Requirement(StrictModel):
    id: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
    text: Annotated[str, Field(min_length=1, max_length=2_000)]

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank text")
        return value


class RequirementsList(StrictModel):
    requirements: Annotated[list[Requirement], Field(min_length=1, max_length=MAX_REQUIREMENTS)]


Issue = Annotated[str, Field(min_length=1, max_length=2_000)]


class Extraction(RequirementsList):
    complete: bool
    uncertainties: Annotated[list[Issue], Field(max_length=100)]

    @field_validator("uncertainties")
    @classmethod
    def issues_not_blank(cls, values: list[str]) -> list[str]:
        if any(not item.strip() for item in values):
            raise ValueError("blank issue")
        return values


class RequirementEvaluation(StrictModel):
    id: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
    status: Literal["matched", "contradiction", "missing", "uncertain"]
    evidence: Annotated[str, Field(max_length=MAX_TEXT_LENGTH)]


class Evaluation(StrictModel):
    requirements: Annotated[list[RequirementEvaluation], Field(min_length=1, max_length=MAX_REQUIREMENTS)]
    complete: bool
    extra_actions: Annotated[list[Issue], Field(max_length=100)]
    uncertainties: Annotated[list[Issue], Field(max_length=100)]
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]

    @field_validator("extra_actions", "uncertainties")
    @classmethod
    def issues_not_blank(cls, values: list[str]) -> list[str]:
        if any(not item.strip() for item in values):
            raise ValueError("blank issue")
        return values

    @field_validator("summary")
    @classmethod
    def summary_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank summary")
        return value


class Transcription(StrictModel):
    transcript: Annotated[str, Field(max_length=MAX_TEXT_LENGTH)]
    speech_detected: bool
    uncertainties: Annotated[list[Issue], Field(max_length=100)]


def validate_text(value: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > MAX_TEXT_LENGTH:
        raise InvalidProviderResponse("A nonempty instruction or transcript within the size limit is required.")
    return value


def validate_requirements(requirements: list[dict]) -> list[dict]:
    """Reject omitted, duplicated, coerced, or unrecognized requirement fields."""
    try:
        parsed = RequirementsList.model_validate({"requirements": requirements})
    except (ValidationError, TypeError, ValueError):
        raise InvalidProviderResponse("The requirement list is invalid or incomplete.") from None
    values = [item.model_dump() for item in parsed.requirements]
    ids = [item["id"] for item in values]
    texts = [" ".join(item["text"].casefold().split()) for item in values]
    if len(set(ids)) != len(ids) or len(set(texts)) != len(texts):
        raise InvalidProviderResponse("The requirement list contains duplicates.")
    return values


def validate_extraction(payload: dict) -> dict:
    try:
        parsed = Extraction.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        raise InvalidProviderResponse("The instruction extraction response is invalid.") from None
    result = parsed.model_dump()
    result["requirements"] = validate_requirements(result["requirements"])
    return result


def validate_evaluation(requirements: list[dict], transcript: str, payload: dict) -> dict:
    """Derive the decision locally; the model cannot supply its own pass flag."""
    requirements = validate_requirements(requirements)
    validate_text(transcript)
    try:
        parsed = Evaluation.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        raise InvalidProviderResponse("The verification response is invalid.") from None

    expected_ids = {item["id"] for item in requirements}
    actual_ids = [item.id for item in parsed.requirements]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != expected_ids:
        raise InvalidProviderResponse("The verification response does not cover each requirement exactly once.")

    for item in parsed.requirements:
        # The provider is instructed to quote a complete phrase, preserving nearby
        # negation and qualifiers. Here the original transcript remains immutable.
        if item.status == "matched" and not item.evidence.strip():
            raise InvalidProviderResponse("A matched requirement has no recorded evidence.")
        if item.evidence and (not item.evidence.strip() or item.evidence not in transcript):
            raise InvalidProviderResponse("Verification evidence is not present in the recording transcript.")

    statuses = {item.status for item in parsed.requirements}
    review_required = not parsed.complete or bool(parsed.uncertainties) or "uncertain" in statuses
    passed = not review_required and not parsed.extra_actions and statuses == {"matched"}
    decision = "passed" if passed else "review_required" if review_required else "discrepancy"
    result = parsed.model_dump()
    result.update(passed=passed, decision=decision)
    return result
