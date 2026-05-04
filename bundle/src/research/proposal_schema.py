from __future__ import annotations

import re
from typing import Any

from .param_space import normalize_params_to_space
from .types import CandidateProposal


PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_scalar(value: Any) -> int | float | str:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    text = str(value).strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d+\.\d+", text):
        parsed = float(text)
        return int(parsed) if parsed.is_integer() else parsed
    return text


def reject_illegal_mutation(candidate: CandidateProposal) -> None:
    if not candidate.params:
        raise ValueError("candidate.params is empty")
    for name, value in candidate.params.items():
        if not PARAM_NAME_RE.fullmatch(str(name)):
            raise ValueError(f"invalid param name: {name}")
        normalized = _normalize_scalar(value)
        if not isinstance(normalized, (int, float, str)):
            raise ValueError(f"invalid param value type: {name}")
    for name in candidate.template_choices:
        if not PARAM_NAME_RE.fullmatch(str(name)):
            raise ValueError(f"invalid template choice name: {name}")


def normalize_candidate(raw: dict[str, Any]) -> CandidateProposal:
    if not isinstance(raw, dict):
        raise TypeError("candidate must be a dict")

    params_raw = raw.get("params")
    if not isinstance(params_raw, dict):
        raise ValueError("candidate.params must be a dict")

    params = {str(name): _normalize_scalar(value) for name, value in params_raw.items()}
    template_choices = raw.get("template_choices") or {}
    if not isinstance(template_choices, dict):
        raise ValueError("candidate.template_choices must be a dict")

    candidate = CandidateProposal(
        params=params,
        ai_summary=str(raw.get("ai_summary", "")).strip(),
        parent_strategy_id=None if raw.get("parent_strategy_id") in (None, "") else str(raw.get("parent_strategy_id")),
        template_choices={str(name): _normalize_scalar(value) for name, value in template_choices.items()},
    )
    reject_illegal_mutation(candidate)
    return candidate


def validate_candidate_batch(
    raw: dict[str, Any],
    param_space: dict[str, dict[str, Any]] | None = None,
) -> list[CandidateProposal]:
    if not isinstance(raw, dict):
        raise TypeError("LLM payload must be a dict")
    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list):
        raise ValueError("LLM payload must contain a candidates list")

    normalized: list[CandidateProposal] = []
    for item in candidates_raw:
        candidate = normalize_candidate(item)
        if param_space:
            candidate.params = normalize_params_to_space(candidate.params, param_space)
        normalized.append(candidate)
    return normalized
