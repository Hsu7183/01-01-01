from __future__ import annotations

import json
from typing import Any

from .modular_0313plus import (
    build_0313plus_template_schema,
    build_modular_candidate_batch,
    is_modular_0313plus_enabled,
)
from .mock_candidates import build_mock_candidate_batch
from .param_space import load_research_param_space
from .secret_config import resolve_openai_api_key
from .types import ResearchConfig


def _param_schema(param_space: dict[str, dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in param_space.items():
        required.append(name)
        if spec["type"] == "int":
            if int(spec["start"]) == 0 and int(spec["stop"]) == 1 and int(spec["step"]) == 1:
                properties[name] = {"type": "integer", "enum": [0, 1]}
            else:
                properties[name] = {
                    "type": "integer",
                    "minimum": int(spec["start"]),
                    "maximum": int(spec["stop"]),
                }
        else:
            properties[name] = {
                "type": "number",
                "minimum": float(spec["start"]),
                "maximum": float(spec["stop"]),
            }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _candidate_schema(config: ResearchConfig) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for AI research request")
    template_schema = (
        build_0313plus_template_schema()
        if is_modular_0313plus_enabled(config)
        else {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        }
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                        "properties": {
                            "parent_strategy_id": {"type": "string"},
                            "ai_summary": {"type": "string"},
                            "params": _param_schema(param_space),
                            "template_choices": template_schema,
                        },
                        "required": [
                            "parent_strategy_id",
                            "ai_summary",
                        "params",
                        "template_choices",
                    ],
                },
            }
        },
        "required": ["candidates"],
    }


def build_client_from_env():
    api_key = resolve_openai_api_key()
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed") from exc
    return OpenAI(api_key=api_key)


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)

    output = getattr(response, "output", None) or []
    for item in output:
        content = getattr(item, "content", None) or []
        for chunk in content:
            text = getattr(chunk, "text", None)
            if text:
                return str(text)
    raise RuntimeError("OpenAI response did not contain text output")


def request_candidate_batch(
    config: ResearchConfig,
    prompt_text: str,
    *,
    current_round: int = 1,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if is_modular_0313plus_enabled(config):
        return build_modular_candidate_batch(config, current_round=current_round, context=context)

    if str(config.model).strip().lower().startswith("mock"):
        return build_mock_candidate_batch(config, current_round=current_round)

    client = build_client_from_env()
    response = client.responses.create(
        model=config.model,
        input=prompt_text,
        text={
            "format": {
                "type": "json_schema",
                "name": "candidate_batch",
                "strict": True,
                "schema": _candidate_schema(config),
            }
        },
    )
    response_text = _extract_response_text(response)
    try:
        return {
            "payload": json.loads(response_text),
            "raw_text": response_text,
            "meta": {},
        }
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI did not return valid JSON: {exc}") from exc
