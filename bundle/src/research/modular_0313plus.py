from __future__ import annotations

import json
import random
import re
from itertools import product
from typing import Any

from .param_space import (
    load_persistent_best_params,
    load_persistent_top10_rows,
    load_research_param_space,
    normalize_params_to_space,
)
from .paths import session_dir
from .types import ResearchConfig


MODULE_FAMILY_CODE = "0313plus_modular_v1"

BIAS_MODE_CHOICES: list[tuple[str, str]] = [
    ("ma_or_ema_cdp", "MA 或 EMA 搭配 CDP"),
    ("ma_only_cdp", "MA 搭配 CDP"),
    ("ema_only_cdp", "EMA 搭配 CDP"),
    ("ma_and_ema_cdp", "MA 與 EMA 同時確認 CDP"),
]

ENTRY_MODE_CHOICES: list[tuple[str, str]] = [
    ("nhnl_or_don", "NH/NL 或 Don"),
    ("nhnl_only", "只用 NH/NL"),
    ("don_only", "只用 Don"),
    ("nhnl_and_don", "NH/NL 與 Don 同時成立"),
]

ATR_FILTER_MODE_CHOICES: list[tuple[str, str]] = [
    ("on", "啟用 ATR 最小門檻"),
    ("off", "關閉 ATR 最小門檻"),
]

KEY_SWEEP_PARAMS = [
    "DonLen",
    "ATRLen",
    "EMAWarmBars",
    "EntryBufferPts",
    "DonBufferPts",
    "MinATRD",
    "ATRStopK",
    "ATRTakeProfitK",
    "MaxEntriesPerDay",
    "TimeStopBars",
    "MinRunPctAnchor",
    "TrailStartPctAnchor",
    "TrailGivePctAnchor",
    "AnchorBackPct",
    "UseAnchorExit",
]

SWEEP_KEEP_TOP_N = 3
PLANNER_STATE_FILE = "modular_sweep_state.json"


def is_modular_0313plus_enabled(config: ResearchConfig) -> bool:
    if str(config.base_xs_path).strip().lower().endswith("0313plus.xs"):
        if bool(config.allow_template_mutation):
            return True
        if str(config.exploration_mode).strip().lower() in {
            "module_loop",
            "modular_loop",
            "xq_modular_loop",
        }:
            return True
    return False


def is_modular_0313plus_template_choices(template_choices: dict[str, Any] | None) -> bool:
    if not isinstance(template_choices, dict) or not template_choices:
        return False
    return str(template_choices.get("module_family") or "").strip() == MODULE_FAMILY_CODE


def build_0313plus_template_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "module_family": {"type": "string", "enum": [MODULE_FAMILY_CODE]},
            "bias_mode": {"type": "string", "enum": [item[0] for item in BIAS_MODE_CHOICES]},
            "entry_mode": {"type": "string", "enum": [item[0] for item in ENTRY_MODE_CHOICES]},
            "atr_filter_mode": {"type": "string", "enum": [item[0] for item in ATR_FILTER_MODE_CHOICES]},
            "use_atr_stop": {"type": "integer", "enum": [0, 1]},
            "use_atr_tp": {"type": "integer", "enum": [0, 1]},
            "use_time_stop": {"type": "integer", "enum": [0, 1]},
            "use_trail_exit": {"type": "integer", "enum": [0, 1]},
            "strategy_group_code": {"type": "string"},
            "strategy_group_label": {"type": "string"},
            "strategy_group_summary": {"type": "string"},
        },
        "required": [
            "module_family",
            "bias_mode",
            "entry_mode",
            "atr_filter_mode",
            "use_atr_stop",
            "use_atr_tp",
            "use_time_stop",
            "use_trail_exit",
        ],
    }


def build_0313plus_module_prompt_payload() -> dict[str, Any]:
    return {
        "module_family": MODULE_FAMILY_CODE,
        "bias_modes": [{"code": code, "label": label} for code, label in BIAS_MODE_CHOICES],
        "entry_modes": [{"code": code, "label": label} for code, label in ENTRY_MODE_CHOICES],
        "atr_filter_modes": [{"code": code, "label": label} for code, label in ATR_FILTER_MODE_CHOICES],
        "exit_toggles": [
            {"code": "use_atr_stop", "label": "ATR 停損"},
            {"code": "use_atr_tp", "label": "ATR 停利"},
            {"code": "use_time_stop", "label": "Time Stop"},
            {"code": "use_trail_exit", "label": "Trail Exit"},
            {"code": "UseAnchorExit", "label": "Anchor Exit（參數層）"},
        ],
        "hard_rules": [
            "至少保留一種進場型態",
            "至少保留一種固定出場或追蹤出場機制",
            "Anchor Exit 仍由參數 UseAnchorExit 控制",
            "固定市場結構層不列入自由亂改",
        ],
    }


def _module_label(value: str, choices: list[tuple[str, str]]) -> str:
    for code, label in choices:
        if code == value:
            return label
    return value


def describe_0313plus_template_choices(template_choices: dict[str, Any]) -> str:
    bias_mode = str(template_choices.get("bias_mode") or "ma_or_ema_cdp")
    entry_mode = str(template_choices.get("entry_mode") or "nhnl_or_don")
    atr_filter_mode = str(template_choices.get("atr_filter_mode") or "on")
    exit_parts: list[str] = []
    if int(template_choices.get("use_atr_stop", 1)):
        exit_parts.append("ATR 停損")
    if int(template_choices.get("use_atr_tp", 1)):
        exit_parts.append("ATR 停利")
    if int(template_choices.get("use_time_stop", 1)):
        exit_parts.append("Time Stop")
    if int(template_choices.get("use_trail_exit", 1)):
        exit_parts.append("Trail Exit")
    if not exit_parts:
        exit_parts.append("無固定出場")
    return " / ".join(
        [
            _module_label(bias_mode, BIAS_MODE_CHOICES),
            _module_label(entry_mode, ENTRY_MODE_CHOICES),
            _module_label(atr_filter_mode, ATR_FILTER_MODE_CHOICES),
            "+".join(exit_parts),
        ]
    )


def _combo_signature(combo: dict[str, Any]) -> str:
    return json.dumps(
        {
            "module_family": MODULE_FAMILY_CODE,
            "bias_mode": combo.get("bias_mode"),
            "entry_mode": combo.get("entry_mode"),
            "atr_filter_mode": combo.get("atr_filter_mode"),
            "use_atr_stop": int(combo.get("use_atr_stop", 0)),
            "use_atr_tp": int(combo.get("use_atr_tp", 0)),
            "use_time_stop": int(combo.get("use_time_stop", 0)),
            "use_trail_exit": int(combo.get("use_trail_exit", 0)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _group_code(combo: dict[str, Any]) -> str:
    return (
        f"{MODULE_FAMILY_CODE}:"
        f"{combo.get('bias_mode')}|{combo.get('entry_mode')}|{combo.get('atr_filter_mode')}|"
        f"{int(combo.get('use_atr_stop', 0))}{int(combo.get('use_atr_tp', 0))}"
        f"{int(combo.get('use_time_stop', 0))}{int(combo.get('use_trail_exit', 0))}"
    )


def _enrich_combo_metadata(combo: dict[str, Any], summary_text: str | None = None) -> dict[str, Any]:
    enriched = {
        **combo,
        "strategy_group_code": _group_code(combo),
        "strategy_group_label": describe_0313plus_template_choices(combo),
    }
    if summary_text:
        enriched["strategy_group_summary"] = summary_text
    return enriched


def _context_module_lookup(context: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    lookup: dict[str, dict[str, dict[str, Any]]] = {}
    payload = (context or {}).get("module_learning") or {}
    dimensions = payload.get("dimensions") or {}
    if not isinstance(dimensions, dict):
        return lookup
    for dimension_key, rows in dimensions.items():
        if not isinstance(rows, list):
            continue
        lookup[str(dimension_key)] = {
            str(row.get("code")): dict(row)
            for row in rows
            if isinstance(row, dict) and row.get("code") is not None
        }
    return lookup


def _context_top_combo_lookup(context: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = (context or {}).get("module_learning") or {}
    rows = payload.get("top_combos") or []
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("combo_signature")): dict(row)
        for row in rows
        if isinstance(row, dict) and row.get("combo_signature")
    }


def _context_param_lookup(context: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = (context or {}).get("param_learning") or {}
    rows = payload.get("param_ranges") or []
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("name")): dict(row)
        for row in rows
        if isinstance(row, dict) and row.get("name")
    }


def _combo_learning_score(combo: dict[str, Any], context: dict[str, Any] | None) -> float:
    module_lookup = _context_module_lookup(context)
    top_combo_lookup = _context_top_combo_lookup(context)
    combo_signature = _combo_signature(combo)
    score = 0.0

    top_combo = top_combo_lookup.get(combo_signature)
    if top_combo:
        score += float(top_combo.get("best_score") or 0.0) * 0.9
        score += float(top_combo.get("avg_score") or 0.0) * 0.5
        score += min(int(top_combo.get("valid_runs") or 0), 12) * 0.12

    for dimension_key in ("bias_mode", "entry_mode", "atr_filter_mode"):
        learned_row = module_lookup.get(dimension_key, {}).get(str(combo.get(dimension_key)))
        if not learned_row:
            continue
        score += float(learned_row.get("best_score") or 0.0) * 0.65
        score += float(learned_row.get("avg_score") or 0.0) * 0.3
        score += min(int(learned_row.get("valid_runs") or 0), 12) * 0.08

    for dimension_key in ("use_atr_stop", "use_atr_tp", "use_time_stop", "use_trail_exit"):
        learned_row = module_lookup.get(dimension_key, {}).get(str(int(combo.get(dimension_key, 0))))
        if not learned_row:
            continue
        score += float(learned_row.get("best_score") or 0.0) * 0.22
        score += float(learned_row.get("avg_score") or 0.0) * 0.12
        score += min(int(learned_row.get("valid_runs") or 0), 12) * 0.03
    return score


def _ordered_combos_by_learning(combos: list[dict[str, Any]], context: dict[str, Any] | None) -> list[dict[str, Any]]:
    ranked = list(combos)
    ranked.sort(
        key=lambda combo: (
            -_combo_learning_score(combo, context),
            _combo_signature(combo),
        )
    )
    return ranked


def _load_context_template_choices(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_context_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _matching_context_seed_rows(combo: dict[str, Any], context: dict[str, Any] | None) -> list[dict[str, Any]]:
    context = context or {}
    target_signature = _combo_signature(combo)
    rows: list[dict[str, Any]] = []
    top_combo_lookup = _context_top_combo_lookup(context)
    exact_combo = top_combo_lookup.get(target_signature)
    if exact_combo and isinstance(exact_combo.get("best_params"), dict):
        rows.append(dict(exact_combo["best_params"]))

    ranked_matches: list[tuple[int, dict[str, Any]]] = []
    for row in context.get("top_runs") or []:
        if not isinstance(row, dict):
            continue
        template = _load_context_template_choices(row.get("template_choices_json"))
        params = _load_context_params(row.get("params_json"))
        if not params:
            continue
        match_count = sum(1 for key in ("bias_mode", "entry_mode", "atr_filter_mode") if template.get(key) == combo.get(key))
        match_count += sum(
            1
            for key in ("use_atr_stop", "use_atr_tp", "use_time_stop", "use_trail_exit")
            if int(template.get(key, -1)) == int(combo.get(key, -2))
        )
        if match_count >= 4:
            ranked_matches.append((match_count, dict(params)))
    ranked_matches.sort(key=lambda item: item[0], reverse=True)
    rows.extend(item[1] for item in ranked_matches[:6])

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        signature = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(row)
    return deduped


def _learning_reason_text(combo: dict[str, Any], context: dict[str, Any] | None) -> str:
    module_lookup = _context_module_lookup(context)
    reason_bits: list[str] = []
    for dimension_key in ("bias_mode", "entry_mode", "atr_filter_mode"):
        learned_row = module_lookup.get(dimension_key, {}).get(str(combo.get(dimension_key)))
        if not learned_row or int(learned_row.get("valid_runs") or 0) <= 0:
            continue
        reason_bits.append(
            f"{learned_row.get('dimension_label')}偏向「{learned_row.get('label')}」"
        )
    if not reason_bits:
        return "目前還在探索新的模組排列，先固定這組進出場邏輯，再把本輪參數完整掃完。"
    return (
        "累積研究記憶顯示 "
        + "、".join(reason_bits[:3])
        + "，所以這一輪先固定這組邏輯，再把本輪參數完整掃完。"
    )


def _build_module_combos() -> list[dict[str, Any]]:
    combos: list[dict[str, Any]] = []
    for bias_mode, entry_mode, atr_filter_mode, use_atr_stop, use_atr_tp, use_time_stop, use_trail_exit in product(
        [item[0] for item in BIAS_MODE_CHOICES],
        [item[0] for item in ENTRY_MODE_CHOICES],
        [item[0] for item in ATR_FILTER_MODE_CHOICES],
        [0, 1],
        [0, 1],
        [0, 1],
        [0, 1],
    ):
        if int(use_atr_stop) + int(use_atr_tp) + int(use_time_stop) + int(use_trail_exit) <= 0:
            continue
        combos.append(
            {
                "module_family": MODULE_FAMILY_CODE,
                "bias_mode": bias_mode,
                "entry_mode": entry_mode,
                "atr_filter_mode": atr_filter_mode,
                "use_atr_stop": int(use_atr_stop),
                "use_atr_tp": int(use_atr_tp),
                "use_time_stop": int(use_time_stop),
                "use_trail_exit": int(use_trail_exit),
            }
        )
    return combos


def _spec_values(spec: dict[str, Any]) -> list[int | float]:
    start = spec["start"]
    stop = spec["stop"]
    step = spec["step"]
    if spec["type"] == "int":
        return list(range(int(start), int(stop) + 1, int(step)))
    values: list[float] = []
    current = float(start)
    stop_f = float(stop)
    step_f = float(step)
    while current <= stop_f + 1e-12:
        values.append(round(current, 10))
        current += step_f
    return values


def _nearest_index(values: list[int | float], target: int | float) -> int:
    return min(range(len(values)), key=lambda idx: abs(float(values[idx]) - float(target)))


def _format_value(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _build_param_scope_text(param_space: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    if not param_space:
        return ""
    parts: list[str] = []
    for name in KEY_SWEEP_PARAMS:
        if name not in param_space:
            continue
        spec = param_space[name]
        configured_low = _format_value(spec["start"])
        configured_high = _format_value(spec["stop"])
        values = [candidate.get("params", {}).get(name) for candidate in candidates if name in candidate.get("params", {})]
        if values:
            sampled_low = min(values, key=float)
            sampled_high = max(values, key=float)
            if float(sampled_low) == float(sampled_high):
                parts.append(f"{name}={configured_low}~{configured_high}（本輪 { _format_value(sampled_low) }）")
            else:
                parts.append(
                    f"{name}={configured_low}~{configured_high}（本輪 {_format_value(sampled_low)}~{_format_value(sampled_high)}）"
                )
        else:
            parts.append(f"{name}={configured_low}~{configured_high}")
    return " / ".join(parts)


def _seed_rows(config: ResearchConfig) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    if config.seed_params:
        seeds.append(dict(config.seed_params))
    best_params = load_persistent_best_params()
    if best_params:
        seeds.append(dict(best_params))
    for row in load_persistent_top10_rows(limit=10):
        if isinstance(row, dict):
            seeds.append(dict(row))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in seeds:
        signature = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(row)
    return deduped or [dict(config.seed_params or {})]


def _mutate_params(
    *,
    seed_params: dict[str, Any],
    param_space: dict[str, dict[str, Any]],
    current_round: int,
    combo_idx: int,
    param_learning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rng = random.Random(f"{current_round}:{combo_idx}:{json.dumps(seed_params, ensure_ascii=False, sort_keys=True, default=str)}")
    params = normalize_params_to_space(seed_params, param_space)
    param_lookup = _context_param_lookup({"param_learning": param_learning or {}})
    for name in KEY_SWEEP_PARAMS:
        spec = param_space.get(name)
        if spec is None:
            continue
        values = _spec_values(spec)
        if not values:
            continue
        anchor = params.get(name, spec["start"])
        base_idx = _nearest_index(values, float(anchor))
        max_span = min(len(values) - 1, 3 if current_round <= 3 else 6)
        if max_span <= 0:
            params[name] = values[base_idx]
            continue
        shift = rng.randint(-max_span, max_span)
        target_idx = min(max(base_idx + shift, 0), len(values) - 1)
        params[name] = values[target_idx]

        learning_row = param_lookup.get(name)
        if learning_row:
            preferred_low = learning_row.get("preferred_low")
            preferred_high = learning_row.get("preferred_high")
            best_value = learning_row.get("best_value")
            bias_probability = 0.35 if current_round <= 2 else 0.55 if current_round <= 6 else 0.72
            if preferred_low is not None and preferred_high is not None and rng.random() < bias_probability:
                preferred_values = [
                    value
                    for value in values
                    if float(preferred_low) - 1e-12 <= float(value) <= float(preferred_high) + 1e-12
                ]
                if preferred_values:
                    params[name] = preferred_values[rng.randrange(len(preferred_values))]
                    continue
            if best_value is not None and rng.random() < 0.2:
                params[name] = values[_nearest_index(values, float(best_value))]
    return normalize_params_to_space(params, param_space)


def _planner_state_path(session_id: str) -> Any:
    return session_dir(session_id) / PLANNER_STATE_FILE


def _load_planner_state(session_id: str) -> dict[str, Any]:
    path = _planner_state_path(session_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_planner_state(session_id: str, payload: dict[str, Any]) -> None:
    path = _planner_state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_binary_spec(spec: dict[str, Any]) -> bool:
    return (
        str(spec.get("type") or "") == "int"
        and int(spec.get("start") or 0) == 0
        and int(spec.get("stop") or 0) == 1
        and int(spec.get("step") or 0) == 1
    )


def _ordered_sweep_param_names(param_space: dict[str, dict[str, Any]]) -> list[str]:
    ordered = [name for name in KEY_SWEEP_PARAMS if name in param_space]
    ordered.extend(name for name in param_space if name not in ordered)
    return ordered


def _build_focus_param_groups(param_space: dict[str, dict[str, Any]]) -> list[list[str]]:
    ordered_names = _ordered_sweep_param_names(param_space)
    int_names = [
        name
        for name in ordered_names
        if name in param_space and str(param_space[name].get("type") or "") == "int" and not _is_binary_spec(param_space[name])
    ]
    float_names = [
        name
        for name in ordered_names
        if name in param_space and str(param_space[name].get("type") or "") == "float"
    ]
    bool_names = [name for name in ordered_names if name in param_space and _is_binary_spec(param_space[name])]

    groups: list[list[str]] = []
    while int_names or float_names or bool_names:
        group: list[str] = []
        while len(group) < 2 and int_names:
            group.append(int_names.pop(0))
        if len(group) < 3 and float_names:
            group.append(float_names.pop(0))
        while len(group) < 3 and int_names:
            group.append(int_names.pop(0))
        while len(group) < 3 and float_names:
            group.append(float_names.pop(0))
        while len(group) < 3 and bool_names:
            group.append(bool_names.pop(0))
        if group:
            groups.append(group)
    return groups or [[name] for name in ordered_names]


def _default_param_value(spec: dict[str, Any]) -> int | float:
    if str(spec.get("type") or "") == "int":
        return int(spec["start"])
    return round(float(spec["start"]), 10)


def _build_anchor_params(seed_params: dict[str, Any], param_space: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized_seed = normalize_params_to_space(seed_params or {}, param_space)
    anchor: dict[str, Any] = {}
    for name, spec in param_space.items():
        anchor[name] = normalized_seed.get(name, _default_param_value(spec))
    return anchor


def _initial_anchor_params(
    config: ResearchConfig,
    param_space: dict[str, dict[str, Any]],
    combo: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    seed_candidates = _matching_context_seed_rows(combo, context) + _seed_rows(config)
    for row in seed_candidates:
        if isinstance(row, dict) and row:
            return _build_anchor_params(row, param_space)
    return _build_anchor_params(dict(config.seed_params or {}), param_space)


def _serialize_value_list(values: list[int | float]) -> list[int | float]:
    unique: list[int | float] = []
    seen: set[str] = set()
    for value in values:
        key = _format_value(value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _init_top_values_for_focus_group(
    focus_params: list[str],
    anchor_params: dict[str, Any],
) -> dict[str, list[int | float]]:
    payload: dict[str, list[int | float]] = {}
    for name in focus_params:
        payload[name] = [anchor_params.get(name)]
    return payload


def _snapshot_top_values(state: dict[str, Any], focus_params: list[str]) -> dict[str, list[int | float]]:
    retained = state.get("top_values_by_param") or {}
    payload: dict[str, list[int | float]] = {}
    for name in focus_params:
        raw_values = retained.get(name) or []
        payload[name] = _serialize_value_list(list(raw_values))
    return payload


def _top_values_equal(left: dict[str, list[int | float]], right: dict[str, list[int | float]]) -> bool:
    left_keys = set(left.keys())
    right_keys = set(right.keys())
    if left_keys != right_keys:
        return False
    for key in left_keys:
        if [_format_value(value) for value in left.get(key) or []] != [_format_value(value) for value in right.get(key) or []]:
            return False
    return True


def _strategy_combo_queue(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    ordered = _ordered_combos_by_learning(_build_module_combos(), context)
    return [dict(combo) for combo in ordered]


def _normalize_combo_queue(raw_queue: Any, context: dict[str, Any] | None) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    if isinstance(raw_queue, list):
        for row in raw_queue:
            if isinstance(row, dict):
                queue.append(dict(row))
    if queue:
        return queue
    return _strategy_combo_queue(context)


def _load_or_init_planner_state(
    config: ResearchConfig,
    param_space: dict[str, dict[str, Any]],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_state = _load_planner_state(config.session_id)
    combo_queue = _normalize_combo_queue(raw_state.get("combo_queue"), context)
    focus_groups = raw_state.get("focus_groups")
    if not isinstance(focus_groups, list) or not focus_groups:
        focus_groups = _build_focus_param_groups(param_space)

    combo_index = int(raw_state.get("combo_index") or 0)
    if combo_index >= len(combo_queue):
        combo_index = 0

    focus_group_index = int(raw_state.get("focus_group_index") or 0)
    if focus_group_index >= len(focus_groups):
        focus_group_index = 0

    current_combo = dict(combo_queue[combo_index])
    anchor_params = raw_state.get("anchor_params")
    if not isinstance(anchor_params, dict) or not anchor_params:
        anchor_params = _initial_anchor_params(config, param_space, current_combo, context)
    else:
        anchor_params = _build_anchor_params(anchor_params, param_space)

    focus_params = [str(name) for name in focus_groups[focus_group_index] if str(name) in param_space]
    top_values_by_param = raw_state.get("top_values_by_param")
    if not isinstance(top_values_by_param, dict) or not top_values_by_param:
        top_values_by_param = _init_top_values_for_focus_group(focus_params, anchor_params)
    else:
        top_values_by_param = {
            str(name): _serialize_value_list(list(values if isinstance(values, list) else [values]))
            for name, values in top_values_by_param.items()
        }
        for name in focus_params:
            if not top_values_by_param.get(name):
                top_values_by_param[name] = [anchor_params.get(name)]

    cycle_reference = raw_state.get("cycle_reference_top_values")
    if not isinstance(cycle_reference, dict) or not cycle_reference:
        cycle_reference = _snapshot_top_values({"top_values_by_param": top_values_by_param}, focus_params)

    state = {
        "version": 1,
        "combo_queue": combo_queue,
        "combo_index": combo_index,
        "focus_groups": focus_groups,
        "focus_group_index": focus_group_index,
        "cycle_no": max(1, int(raw_state.get("cycle_no") or 1)),
        "sweep_param_index": max(0, int(raw_state.get("sweep_param_index") or 0)),
        "anchor_params": anchor_params,
        "top_values_by_param": top_values_by_param,
        "cycle_reference_top_values": cycle_reference,
        "stable_focus_groups_completed": int(raw_state.get("stable_focus_groups_completed") or 0),
        "stable_cycles_completed": int(raw_state.get("stable_cycles_completed") or 0),
    }
    _write_planner_state(config.session_id, state)
    return state


def _planner_round_detail_lines(
    *,
    combo: dict[str, Any],
    focus_params: list[str],
    sweep_param: str,
    value_map: dict[str, list[int | float]],
    param_space: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> list[str]:
    strategy_label = describe_0313plus_template_choices(combo)
    fixed_lines: list[str] = []
    for name in focus_params:
        if name == sweep_param:
            continue
        values = value_map.get(name) or []
        if len(values) <= 1:
            fixed_lines.append(f"{name} 固定 {_format_value(values[0]) if values else '-'}")
        else:
            fixed_lines.append(
                f"{name} 固定前 {len(values)} 名：{', '.join(_format_value(value) for value in values)}"
            )

    sweep_spec = param_space[sweep_param]
    sweep_values = value_map.get(sweep_param) or []
    total_candidates = 1
    for name in focus_params:
        total_candidates *= max(len(value_map.get(name) or []), 1)

    detail_lines = [
        f"策略家族：{strategy_label}",
        f"焦點變數：{' / '.join(focus_params)}",
        f"目前第 {int(state.get('cycle_no') or 1)} 環，正在掃描 {sweep_param}（第 {int(state.get('sweep_param_index') or 0) + 1}/{len(focus_params)} 個變數）",
        (
            f"{sweep_param} 全範圍掃描：{_format_value(sweep_spec['start'])}"
            f"~{_format_value(sweep_spec['stop'])}，間距 {_format_value(sweep_spec['step'])}"
            f"，共 {len(sweep_values)} 個值"
        ),
        f"固定條件：{'；'.join(fixed_lines) if fixed_lines else '其餘參數維持核心值'}",
        f"本輪總組合：{total_candidates} 組",
        "本輪跑完會保留掃描變數前 3 名；若整個三變數環跑完前 3 名都不變，就切到下一組變數 / 下一個策略家族。",
    ]
    return detail_lines


def _planner_scope_text(
    focus_params: list[str],
    sweep_param: str,
    value_map: dict[str, list[int | float]],
    param_space: dict[str, dict[str, Any]],
) -> str:
    parts: list[str] = []
    for name in focus_params:
        values = value_map.get(name) or []
        if name == sweep_param:
            spec = param_space[name]
            parts.append(
                f"{name}={_format_value(spec['start'])}~{_format_value(spec['stop'])}"
                f"（本輪全掃，共 {len(values)} 個值）"
            )
            continue
        if len(values) <= 1:
            parts.append(f"{name} 固定 {_format_value(values[0]) if values else '-'}")
        else:
            parts.append(f"{name} 固定前 {len(values)} 名：{', '.join(_format_value(value) for value in values)}")
    return " / ".join(parts)


def _candidate_value_map(
    state: dict[str, Any],
    focus_params: list[str],
    sweep_param: str,
    param_space: dict[str, dict[str, Any]],
) -> dict[str, list[int | float]]:
    retained = state.get("top_values_by_param") or {}
    anchor_params = state.get("anchor_params") or {}
    value_map: dict[str, list[int | float]] = {}
    for name in focus_params:
        if name == sweep_param:
            value_map[name] = _spec_values(param_space[name])
            continue
        retained_values = retained.get(name) or [anchor_params.get(name, _default_param_value(param_space[name]))]
        value_map[name] = _serialize_value_list(list(retained_values))
    return value_map


def _build_candidates_for_sweep(
    *,
    combo: dict[str, Any],
    anchor_params: dict[str, Any],
    focus_params: list[str],
    sweep_param: str,
    value_map: dict[str, list[int | float]],
    param_space: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    fixed_params = [name for name in focus_params if name != sweep_param]
    fixed_value_lists = [value_map.get(name) or [anchor_params.get(name)] for name in fixed_params]

    candidates: list[dict[str, Any]] = []
    for fixed_values in product(*fixed_value_lists) if fixed_value_lists else [()]:
        fixed_mapping = dict(zip(fixed_params, fixed_values))
        for sweep_value in value_map.get(sweep_param) or []:
            params = dict(anchor_params)
            params.update(fixed_mapping)
            params[sweep_param] = sweep_value
            normalized_params = normalize_params_to_space(params, param_space)
            fixed_desc = []
            for name in fixed_params:
                values = value_map.get(name) or []
                if len(values) > 1:
                    fixed_desc.append(f"{name}={_format_value(fixed_mapping[name])}")
            ai_summary = f"{describe_0313plus_template_choices(combo)}｜掃描 {sweep_param}={_format_value(sweep_value)}"
            if fixed_desc:
                ai_summary = ai_summary + "｜固定 " + " / ".join(fixed_desc)
            candidates.append(
                {
                    "parent_strategy_id": MODULE_FAMILY_CODE,
                    "ai_summary": ai_summary,
                    "params": normalized_params,
                    "template_choices": dict(combo),
                }
            )
    return candidates


def _top_unique_param_values(round_results: list[dict[str, Any]], param_name: str) -> list[int | float]:
    ranked_rows = sorted(
        [dict(row) for row in round_results if isinstance(row, dict)],
        key=lambda row: (
            float(row.get("composite_score") or -1e18),
            -float(row.get("mdd_pct") or 1e18),
            float(row.get("total_return") or -1e18),
        ),
        reverse=True,
    )
    values: list[int | float] = []
    seen: set[str] = set()
    for row in ranked_rows:
        params = row.get("params") or {}
        if param_name not in params:
            continue
        value = params[param_name]
        value_key = _format_value(value)
        if value_key in seen:
            continue
        seen.add(value_key)
        values.append(value)
        if len(values) >= SWEEP_KEEP_TOP_N:
            break
    return values


def _best_round_params(round_results: list[dict[str, Any]]) -> dict[str, Any]:
    ranked_rows = sorted(
        [dict(row) for row in round_results if isinstance(row, dict)],
        key=lambda row: (
            float(row.get("composite_score") or -1e18),
            -float(row.get("mdd_pct") or 1e18),
            float(row.get("total_return") or -1e18),
        ),
        reverse=True,
    )
    if not ranked_rows:
        return {}
    params = ranked_rows[0].get("params") or {}
    return dict(params) if isinstance(params, dict) else {}


def build_modular_candidate_batch(
    config: ResearchConfig,
    current_round: int = 1,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for 0313plus modular mode")

    state = _load_or_init_planner_state(config, param_space, context)
    combo_queue = state["combo_queue"]
    combo_index = int(state.get("combo_index") or 0) % max(len(combo_queue), 1)
    focus_groups = state["focus_groups"]
    focus_group_index = int(state.get("focus_group_index") or 0) % max(len(focus_groups), 1)
    focus_params = [str(name) for name in focus_groups[focus_group_index] if str(name) in param_space]
    if not focus_params:
        raise RuntimeError("focus params are empty for 0313plus modular mode")

    state["sweep_param_index"] = int(state.get("sweep_param_index") or 0) % len(focus_params)
    selected_combo_base = dict(combo_queue[combo_index])
    strategy_group_summary = _learning_reason_text(selected_combo_base, context)
    selected_combo = _enrich_combo_metadata(selected_combo_base, strategy_group_summary)
    anchor_params = _build_anchor_params(dict(state.get("anchor_params") or {}), param_space)
    state["anchor_params"] = anchor_params

    sweep_param = focus_params[int(state.get("sweep_param_index") or 0)]
    value_map = _candidate_value_map(state, focus_params, sweep_param, param_space)
    candidates = _build_candidates_for_sweep(
        combo=selected_combo,
        anchor_params=anchor_params,
        focus_params=focus_params,
        sweep_param=sweep_param,
        value_map=value_map,
        param_space=param_space,
    )
    detail_lines = _planner_round_detail_lines(
        combo=selected_combo,
        focus_params=focus_params,
        sweep_param=sweep_param,
        value_map=value_map,
        param_space=param_space,
        state=state,
    )
    param_scope_text = _planner_scope_text(focus_params, sweep_param, value_map, param_space)
    payload = {"candidates": candidates}
    _write_planner_state(config.session_id, state)
    return {
        "payload": payload,
        "raw_text": json.dumps(
            {
                "planner": "0313plus_modular_sweep",
                "strategy_group_label": selected_combo["strategy_group_label"],
                "focus_params": focus_params,
                "sweep_param": sweep_param,
                "candidate_count": len(candidates),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "meta": {
            "strategy_group_code": selected_combo["strategy_group_code"],
            "strategy_group_label": selected_combo["strategy_group_label"],
            "strategy_group_summary": strategy_group_summary + " 目前採用三變數逐輪固定 / 掃描 / 保留前 3 的收斂流程。",
            "strategy_group_index": combo_index + 1,
            "strategy_group_count": len(combo_queue),
            "params_total_in_group": len(candidates),
            "param_scope_text": param_scope_text,
            "anchor_text": selected_combo["strategy_group_label"],
            "focus_param_group_index": focus_group_index + 1,
            "focus_param_group_count": len(focus_groups),
            "focus_params": focus_params,
            "sweep_param": sweep_param,
            "cycle_no": int(state.get("cycle_no") or 1),
            "candidate_limit": len(candidates),
            "round_detail_lines": detail_lines,
            "top_values_by_param": _snapshot_top_values(state, focus_params),
        },
    }


def advance_modular_sweep_state(
    config: ResearchConfig,
    *,
    context: dict[str, Any] | None = None,
    round_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        return {"transition_text": "參數空間為空，無法推進 modular sweep。"}

    state = _load_or_init_planner_state(config, param_space, context)
    combo_queue = state["combo_queue"]
    focus_groups = state["focus_groups"]
    combo_index = int(state.get("combo_index") or 0) % max(len(combo_queue), 1)
    focus_group_index = int(state.get("focus_group_index") or 0) % max(len(focus_groups), 1)
    focus_params = [str(name) for name in focus_groups[focus_group_index] if str(name) in param_space]
    if not focus_params:
        return {"transition_text": "本輪焦點變數不存在，保留目前狀態。"}

    sweep_param_index = int(state.get("sweep_param_index") or 0) % len(focus_params)
    sweep_param = focus_params[sweep_param_index]
    result_rows = [dict(row) for row in (round_results or []) if isinstance(row, dict)]

    top_values = _top_unique_param_values(result_rows, sweep_param)
    if not top_values:
        top_values = state.get("top_values_by_param", {}).get(sweep_param) or [
            state.get("anchor_params", {}).get(sweep_param, _default_param_value(param_space[sweep_param]))
        ]

    state.setdefault("top_values_by_param", {})
    state["top_values_by_param"][sweep_param] = _serialize_value_list(list(top_values))

    best_params = _best_round_params(result_rows)
    if best_params:
        state["anchor_params"] = _build_anchor_params(best_params, param_space)

    transition_bits = [
        f"{sweep_param} 前 {len(state['top_values_by_param'][sweep_param])} 名："
        + ", ".join(_format_value(value) for value in state["top_values_by_param"][sweep_param])
    ]

    if sweep_param_index + 1 < len(focus_params):
        state["sweep_param_index"] = sweep_param_index + 1
        next_param = focus_params[state["sweep_param_index"]]
        transition_bits.append(f"下一輪改掃 {next_param}。")
        _write_planner_state(config.session_id, state)
        return {
            "transition_text": "；".join(transition_bits),
            "converged": False,
            "next_sweep_param": next_param,
        }

    current_snapshot = _snapshot_top_values(state, focus_params)
    previous_snapshot = state.get("cycle_reference_top_values") or {}
    converged = _top_values_equal(current_snapshot, previous_snapshot)

    if converged:
        state["stable_cycles_completed"] = int(state.get("stable_cycles_completed") or 0) + 1
        state["stable_focus_groups_completed"] = int(state.get("stable_focus_groups_completed") or 0) + 1
        transition_bits.append("本組三變數前 3 名已穩定。")

        if focus_group_index + 1 < len(focus_groups):
            state["focus_group_index"] = focus_group_index + 1
        else:
            state["focus_group_index"] = 0
            state["combo_index"] = (combo_index + 1) % max(len(combo_queue), 1)
            transition_bits.append("已切換到下一個策略家族。")

        next_combo = dict(combo_queue[int(state["combo_index"]) % max(len(combo_queue), 1)])
        next_focus_params = [
            str(name)
            for name in focus_groups[int(state["focus_group_index"]) % max(len(focus_groups), 1)]
            if str(name) in param_space
        ]
        if not next_focus_params:
            next_focus_params = focus_params
        state["cycle_no"] = 1
        state["sweep_param_index"] = 0
        state["top_values_by_param"] = _init_top_values_for_focus_group(next_focus_params, state["anchor_params"])
        state["cycle_reference_top_values"] = _snapshot_top_values(state, next_focus_params)
        _write_planner_state(config.session_id, state)
        return {
            "transition_text": "；".join(transition_bits),
            "converged": True,
            "next_strategy_group_label": describe_0313plus_template_choices(next_combo),
            "next_focus_params": next_focus_params,
            "next_sweep_param": next_focus_params[0],
        }

    state["cycle_no"] = int(state.get("cycle_no") or 1) + 1
    state["sweep_param_index"] = 0
    state["cycle_reference_top_values"] = current_snapshot
    _write_planner_state(config.session_id, state)
    transition_bits.append("三變數前 3 名仍有變動，下一環會回到第 1 個變數再跑一次。")
    return {
        "transition_text": "；".join(transition_bits),
        "converged": False,
        "next_sweep_param": focus_params[0],
    }


def _replace_once(text: str, pattern: str, replacement: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"unable to replace pattern: {pattern}")
    return updated


def render_modular_0313plus_xs(base_xs_text: str, template_choices: dict[str, Any]) -> str:
    choice = {
        "module_family": MODULE_FAMILY_CODE,
        "bias_mode": str(template_choices.get("bias_mode") or "ma_or_ema_cdp"),
        "entry_mode": str(template_choices.get("entry_mode") or "nhnl_or_don"),
        "atr_filter_mode": str(template_choices.get("atr_filter_mode") or "on"),
        "use_atr_stop": int(template_choices.get("use_atr_stop", 1)),
        "use_atr_tp": int(template_choices.get("use_atr_tp", 1)),
        "use_time_stop": int(template_choices.get("use_time_stop", 1)),
        "use_trail_exit": int(template_choices.get("use_trail_exit", 1)),
    }

    long_bias_expr_map = {
        "ma_or_ema_cdp": "((ma2D > ma3D) or (ema2D > ema3D)) and (yC > cdpVal)",
        "ma_only_cdp": "(ma2D > ma3D) and (yC > cdpVal)",
        "ema_only_cdp": "(ema2D > ema3D) and (yC > cdpVal)",
        "ma_and_ema_cdp": "((ma2D > ma3D) and (ema2D > ema3D)) and (yC > cdpVal)",
    }
    short_bias_expr_map = {
        "ma_or_ema_cdp": "((ma2D < ma3D) or (ema2D < ema3D)) and (yC < cdpVal)",
        "ma_only_cdp": "(ma2D < ma3D) and (yC < cdpVal)",
        "ema_only_cdp": "(ema2D < ema3D) and (yC < cdpVal)",
        "ma_and_ema_cdp": "((ma2D < ma3D) and (ema2D < ema3D)) and (yC < cdpVal)",
    }
    long_entry_expr_map = {
        "nhnl_or_don": "((Open >= longEntryLevelNH) or (Open >= longEntryLevelDon))",
        "nhnl_only": "(Open >= longEntryLevelNH)",
        "don_only": "(Open >= longEntryLevelDon)",
        "nhnl_and_don": "((Open >= longEntryLevelNH) and (Open >= longEntryLevelDon))",
    }
    short_entry_expr_map = {
        "nhnl_or_don": "((Open <= shortEntryLevelNL) or (Open <= shortEntryLevelDon))",
        "nhnl_only": "(Open <= shortEntryLevelNL)",
        "don_only": "(Open <= shortEntryLevelDon)",
        "nhnl_and_don": "((Open <= shortEntryLevelNL) and (Open <= shortEntryLevelDon))",
    }
    atr_filter_expr = "(atrD >= MinATRD)" if choice["atr_filter_mode"] == "on" else "(1 = 1)"

    rendered = base_xs_text
    rendered = _replace_once(
        rendered,
        r'if\s+\(\(ma2D > ma3D\) or \(ema2D > ema3D\)\)\s+and\s+\(yC > cdpVal\)\s+then',
        f"if {long_bias_expr_map[choice['bias_mode']]} then",
    )
    rendered = _replace_once(
        rendered,
        r'if\s+\(\(ma2D < ma3D\) or \(ema2D < ema3D\)\)\s+and\s+\(yC < cdpVal\)\s+then',
        f"if {short_bias_expr_map[choice['bias_mode']]} then",
    )
    rendered = _replace_once(
        rendered,
        r'if LongBias and \(atrD >= MinATRD\) and\s+\(\(Open >= longEntryLevelNH\) or \(Open >= longEntryLevelDon\)\) then',
        f"if LongBias and {atr_filter_expr} and {long_entry_expr_map[choice['entry_mode']]} then",
    )
    rendered = _replace_once(
        rendered,
        r'if ShortBias and \(atrD >= MinATRD\) and\s+\(\(Open <= shortEntryLevelNL\) or \(Open <= shortEntryLevelDon\)\) then',
        f"if ShortBias and {atr_filter_expr} and {short_entry_expr_map[choice['entry_mode']]} then",
    )

    exit_replacements = {
        r'LongExitByATR = .*;': "LongExitByATR = (entryATRD > 0) and (Open <= atrStopLong);" if choice["use_atr_stop"] else "LongExitByATR = false;",
        r'ShortExitByATR = .*;': "ShortExitByATR = (entryATRD > 0) and (Open >= atrStopShort);" if choice["use_atr_stop"] else "ShortExitByATR = false;",
        r'LongExitByTP\s+= .*;': "LongExitByTP  = (entryATRD > 0) and (Open >= atrTPPriceLong);" if choice["use_atr_tp"] else "LongExitByTP  = false;",
        r'ShortExitByTP\s+= .*;': "ShortExitByTP  = (entryATRD > 0) and (Open <= atrTPPriceShort);" if choice["use_atr_tp"] else "ShortExitByTP  = false;",
        r'LongExitByTime = .*;': "LongExitByTime = (barsHeld >= TimeStopBars) and (maxRunUpPts < minRunPtsByAnchor);" if choice["use_time_stop"] else "LongExitByTime = false;",
        r'ShortExitByTime = .*;': "ShortExitByTime = (barsHeld >= TimeStopBars) and (maxRunUpPts < minRunPtsByAnchor);" if choice["use_time_stop"] else "ShortExitByTime = false;",
        r'LongExitByTrail = .*;': "LongExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((bestHighSinceEntry - Open) >= trailGivePtsByAnchor);" if choice["use_trail_exit"] else "LongExitByTrail = false;",
        r'ShortExitByTrail = .*;': "ShortExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((Open - bestLowSinceEntry) >= trailGivePtsByAnchor);" if choice["use_trail_exit"] else "ShortExitByTrail = false;",
    }
    for pattern, replacement in exit_replacements.items():
        rendered = _replace_once(rendered, pattern, replacement)

    concept_text = describe_0313plus_template_choices(choice).replace('"', "'")
    rendered = re.sub(
        r'Strategy=DailyBiasSoft\(MAorEMA\+CDP\)\+NHNLorDon\+ATRFilter\+ATRStop\+ATRTakeProfit\+TimeStop\+TrailExitPctAnchor\+AnchorExitPctAnchor',
        f"Strategy={concept_text}",
        rendered,
        count=1,
    )

    header = (
        "//====================== Codex Modular Variant ======================\n"
        f"// ModuleFamily : {MODULE_FAMILY_CODE}\n"
        f"// TemplateChoices : {json.dumps(choice, ensure_ascii=False, sort_keys=True)}\n"
        f"// Concept : {describe_0313plus_template_choices(choice)}\n"
        "//==================================================================\n\n"
    )
    return header + rendered
