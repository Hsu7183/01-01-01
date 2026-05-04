from __future__ import annotations

from typing import Any

from src.research.param_space import parse_param_preset_file
from src.xs.xs_input_parser import parse_xs_file

from .config import DEFAULT_ENABLED_PARAMS


def _value_type(default_value: Any, preset_spec: dict[str, Any] | None) -> str:
    if preset_spec and preset_spec.get("type") in {"int", "float"}:
        return str(preset_spec["type"])
    if isinstance(default_value, int) and not isinstance(default_value, bool):
        return "int"
    if isinstance(default_value, float):
        return "float"
    return "float"


def _coerce_numeric(value: Any, value_type: str) -> int | float:
    if value_type == "int":
        return int(float(value))
    return float(value)


def load_strategy_metadata(
    xs_path: str,
    param_preset_path: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    xs_result = parse_xs_file(xs_path)
    preset_space = parse_param_preset_file(param_preset_path)
    params_meta: list[dict[str, Any]] = []
    ui_specs: list[dict[str, Any]] = []

    for param in xs_result.params:
        preset_spec = preset_space.get(param.name)
        value_type = _value_type(param.default, preset_spec)
        default_value = _coerce_numeric(param.default, value_type)
        start_value = _coerce_numeric(
            preset_spec["start"] if preset_spec else default_value,
            value_type,
        )
        stop_value = _coerce_numeric(
            preset_spec["stop"] if preset_spec else default_value,
            value_type,
        )
        step_value = _coerce_numeric(
            preset_spec["step"] if preset_spec else (1 if value_type == "int" else 0.01),
            value_type,
        )

        params_meta.append(
            {
                "name": param.name,
                "label": param.label or param.name,
                "default": default_value,
                "type": value_type,
            }
        )
        ui_specs.append(
            {
                "name": param.name,
                "label": param.label or param.name,
                "default": default_value,
                "type": value_type,
                "enabled": param.name in DEFAULT_ENABLED_PARAMS,
                "start": start_value,
                "stop": stop_value,
                "step": step_value,
            }
        )

    script_name = xs_result.script_name or "0313plus"
    return script_name, params_meta, ui_specs
