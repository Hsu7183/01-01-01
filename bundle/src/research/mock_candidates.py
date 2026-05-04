from __future__ import annotations

import json
import random
import time
from typing import Any

from .param_space import (
    load_persistent_best_params,
    load_persistent_top10_rows,
    load_research_param_space,
    normalize_params_to_space,
)
from .types import ResearchConfig


MOCK_STRATEGY_GROUPS: list[dict[str, Any]] = [
    {
        "code": "opening_breakout_guarded",
        "label": "開盤突破保守型",
        "summary": "偏向開盤突破，但停損與回吐控制較緊。",
        "anchors": {
            "DonLen": 12,
            "ATRLen": 18,
            "EntryBufferPts": 10,
            "DonBufferPts": 8,
            "ATRStopK": 0.85,
            "ATRTakeProfitK": 1.10,
            "MaxEntriesPerDay": 2,
            "TimeStopBars": 14,
            "UseAnchorExit": 1,
            "AnchorBackPct": 0.35,
        },
    },
    {
        "code": "trend_follow_runner",
        "label": "趨勢延伸追價型",
        "summary": "拉長停利與時間停損，讓趨勢單跑得更遠。",
        "anchors": {
            "DonLen": 24,
            "ATRLen": 28,
            "EntryBufferPts": 16,
            "DonBufferPts": 12,
            "ATRStopK": 1.15,
            "ATRTakeProfitK": 1.55,
            "MaxEntriesPerDay": 2,
            "TimeStopBars": 28,
            "UseAnchorExit": 1,
            "AnchorBackPct": 0.55,
        },
    },
    {
        "code": "tight_scalp_reentry",
        "label": "短打快出再進型",
        "summary": "縮短停損與停利，允許較多日內嘗試次數。",
        "anchors": {
            "DonLen": 8,
            "ATRLen": 12,
            "EntryBufferPts": 6,
            "DonBufferPts": 5,
            "ATRStopK": 0.65,
            "ATRTakeProfitK": 0.9,
            "MaxEntriesPerDay": 4,
            "TimeStopBars": 8,
            "UseAnchorExit": 0,
            "AnchorBackPct": 0.2,
        },
    },
    {
        "code": "high_volatility_filter",
        "label": "高波動濾網型",
        "summary": "提高波動濾網門檻，集中在波動夠大的日子出手。",
        "anchors": {
            "DonLen": 16,
            "ATRLen": 22,
            "EntryBufferPts": 12,
            "DonBufferPts": 10,
            "MinATRD": 28,
            "ATRStopK": 0.95,
            "ATRTakeProfitK": 1.35,
            "MaxEntriesPerDay": 2,
            "TimeStopBars": 18,
            "UseAnchorExit": 1,
            "AnchorBackPct": 0.45,
        },
    },
]

KEY_SWEEP_PARAMS = [
    "DonLen",
    "ATRLen",
    "EntryBufferPts",
    "DonBufferPts",
    "ATRStopK",
    "ATRTakeProfitK",
    "TimeStopBars",
    "AnchorBackPct",
]


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
    if not candidates:
        return ""
    parts: list[str] = []
    for name in KEY_SWEEP_PARAMS:
        if name not in param_space:
            continue
        values = [candidate.get("params", {}).get(name) for candidate in candidates if name in candidate.get("params", {})]
        if not values:
            continue
        low = min(values, key=float)
        high = max(values, key=float)
        if float(low) == float(high):
            parts.append(f"{name}={_format_value(low)}")
        else:
            parts.append(f"{name}={_format_value(low)}~{_format_value(high)}")
    return "；".join(parts[:6])


def _build_anchor_text(anchors: dict[str, Any]) -> str:
    if not anchors:
        return ""
    parts: list[str] = []
    for name in KEY_SWEEP_PARAMS:
        if name in anchors:
            parts.append(f"{name}={_format_value(anchors[name])}")
    return "；".join(parts[:5])


def _pick_group_value(
    rng: random.Random,
    name: str,
    spec: dict[str, Any],
    bootstrap_params: dict[str, Any],
    anchors: dict[str, Any],
    candidate_index: int,
) -> int | float:
    values = _spec_values(spec)
    if not values:
        raise RuntimeError(f"param space has no values: {name}")

    anchor = anchors.get(name, bootstrap_params.get(name))
    if anchor is None:
        anchor = values[len(values) // 2]

    try:
        base_index = values.index(anchor)
    except ValueError:
        base_index = _nearest_index(values, float(anchor))

    if candidate_index == 0:
        jitter_span = max(1, min(1, len(values) // 20 or 1))
    elif candidate_index < 4:
        jitter_span = max(1, min(3, len(values) // 12 or 1))
    else:
        jitter_span = max(1, min(6, len(values) // 8 or 1))

    target_index = min(max(base_index + rng.randint(-jitter_span, jitter_span), 0), len(values) - 1)
    return values[target_index]


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    bootstrap_params = load_persistent_best_params()
    batch_size = max(1, int(config.batch_size))
    strategy_group = MOCK_STRATEGY_GROUPS[(max(1, int(current_round)) - 1) % len(MOCK_STRATEGY_GROUPS)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(MOCK_STRATEGY_GROUPS)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=bootstrap_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 參數測試 #{candidate_index + 1}",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                },
            }
        )

    payload = {"candidates": candidates}
    param_scope_text = _build_param_scope_text(param_space, candidates)
    anchor_text = _build_anchor_text(strategy_group.get("anchors") or {})
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(MOCK_STRATEGY_GROUPS),
            "params_total_in_group": batch_size,
            "param_scope_text": param_scope_text,
            "anchor_text": anchor_text,
        },
    }


MOCK_STRATEGY_GROUPS_V2: list[dict[str, Any]] = [
    {
        "code": "opening_breakout_guarded",
        "label": "開盤突破保守型",
        "summary": "以開盤突破為主，先看突破後延續性，進場緩衝與停損都偏保守。",
        "anchors": {
            "DonLen": 12,
            "ATRLen": 18,
            "EntryBufferPts": 10,
            "DonBufferPts": 8,
            "ATRStopK": 0.85,
            "ATRTakeProfitK": 1.10,
            "MaxEntriesPerDay": 2,
            "TimeStopBars": 14,
            "UseAnchorExit": 1,
            "AnchorBackPct": 0.35,
        },
    },
    {
        "code": "trend_follow_runner",
        "label": "趨勢延伸跑動型",
        "summary": "順勢追價後讓利潤延伸，停損與停利拉寬，觀察趨勢單能否跑出報酬。",
        "anchors": {
            "DonLen": 24,
            "ATRLen": 28,
            "EntryBufferPts": 16,
            "DonBufferPts": 12,
            "ATRStopK": 1.15,
            "ATRTakeProfitK": 1.55,
            "MaxEntriesPerDay": 2,
            "TimeStopBars": 28,
            "UseAnchorExit": 1,
            "AnchorBackPct": 0.55,
        },
    },
    {
        "code": "tight_scalp_reentry",
        "label": "短打快出再進型",
        "summary": "縮短持有 bars 與停損距離，提高日內進出頻率，觀察快打效率。",
        "anchors": {
            "DonLen": 8,
            "ATRLen": 12,
            "EntryBufferPts": 6,
            "DonBufferPts": 5,
            "ATRStopK": 0.65,
            "ATRTakeProfitK": 0.90,
            "MaxEntriesPerDay": 4,
            "TimeStopBars": 8,
            "UseAnchorExit": 0,
            "AnchorBackPct": 0.20,
        },
    },
    {
        "code": "high_volatility_filter",
        "label": "高波動濾網型",
        "summary": "先用較高 ATR 條件篩行情，只在波動夠大時出手，觀察單筆獲利與勝率。",
        "anchors": {
            "DonLen": 16,
            "ATRLen": 22,
            "EntryBufferPts": 12,
            "DonBufferPts": 10,
            "MinATRD": 28,
            "ATRStopK": 0.95,
            "ATRTakeProfitK": 1.35,
            "MaxEntriesPerDay": 2,
            "TimeStopBars": 18,
            "UseAnchorExit": 1,
            "AnchorBackPct": 0.45,
        },
    },
]


def _format_mock_value_v2(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _build_param_scope_text_v2(param_space: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return ""
    parts: list[str] = []
    for name in KEY_SWEEP_PARAMS:
        if name not in param_space:
            continue
        values = [candidate.get("params", {}).get(name) for candidate in candidates if name in candidate.get("params", {})]
        if not values:
            continue
        low = min(values, key=float)
        high = max(values, key=float)
        if float(low) == float(high):
            parts.append(f"{name}={_format_mock_value_v2(low)}")
        else:
            parts.append(f"{name}={_format_mock_value_v2(low)}~{_format_mock_value_v2(high)}")
    return "; ".join(parts)


def _build_anchor_text_v2(anchors: dict[str, Any]) -> str:
    parts: list[str] = []
    for name in KEY_SWEEP_PARAMS:
        if name in anchors:
            parts.append(f"{name}={_format_mock_value_v2(anchors[name])}")
    return "; ".join(parts)


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    bootstrap_params = load_persistent_best_params()
    batch_size = max(1, int(config.batch_size))
    strategy_group = MOCK_STRATEGY_GROUPS_V2[(max(1, int(current_round)) - 1) % len(MOCK_STRATEGY_GROUPS_V2)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(MOCK_STRATEGY_GROUPS_V2)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}:v2")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=bootstrap_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 第 {candidate_index + 1} 組參數",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                },
            }
        )

    payload = {"candidates": candidates}
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(MOCK_STRATEGY_GROUPS_V2),
            "params_total_in_group": batch_size,
            "param_scope_text": _build_param_scope_text_v2(param_space, candidates),
            "anchor_text": _build_anchor_text_v2(strategy_group.get("anchors") or {}),
        },
    }


def _normalize_seed_params_for_mock(config: ResearchConfig, param_space: dict[str, dict[str, Any]]) -> dict[str, Any]:
    seed_params = dict(config.seed_params or {})
    if not seed_params:
        seed_params = load_persistent_best_params()
    if not seed_params:
        seed_params = {name: spec.get("start") for name, spec in param_space.items()}
    return normalize_params_to_space(seed_params, param_space)


def _build_mock_groups_for_config(config: ResearchConfig, seed_params: dict[str, Any]) -> list[dict[str, Any]]:
    if str(config.exploration_mode or "seed_local") == "seed_local":
        return [
            {
                "code": str(config.seed_source or "seed_local"),
                "label": str(config.seed_label or "0313plus 原始參數延伸"),
                "summary": "先以你指定的起始參數為中心，小幅向外擴張測試，不直接跳到遠離 0313plus 核心的參數區。",
                "anchors": dict(seed_params),
            }
        ]
    return MOCK_STRATEGY_GROUPS_V2


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    seed_params = _normalize_seed_params_for_mock(config, param_space)
    active_groups = _build_mock_groups_for_config(config, seed_params)
    batch_size = max(1, int(config.batch_size))
    strategy_group = active_groups[(max(1, int(current_round)) - 1) % len(active_groups)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(active_groups)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}:v3")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=seed_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 第 {candidate_index + 1} 組",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                    "seed_source": config.seed_source,
                    "seed_label": config.seed_label,
                },
            }
        )

    payload = {"candidates": candidates}
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(active_groups),
            "params_total_in_group": batch_size,
            "param_scope_text": _build_param_scope_text_v2(param_space, candidates),
            "anchor_text": _build_anchor_text_v2(strategy_group.get("anchors") or {}),
            "seed_source": config.seed_source,
            "seed_label": config.seed_label,
        },
    }


def _reference_rows_for_mock(limit: int = 10) -> list[dict[str, Any]]:
    rows = load_persistent_top10_rows(limit=limit)
    return [row for row in rows if isinstance(row, dict)]


def _normalize_seed_params_for_mock(config: ResearchConfig, param_space: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw_seed_params = config.seed_params or {}
    seed_params: dict[str, Any] = {}

    if isinstance(raw_seed_params, dict):
        seed_params = dict(raw_seed_params)
    else:
        for item in raw_seed_params if isinstance(raw_seed_params, (list, tuple)) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    seed_params[name] = item.get("default")
                continue
            name = str(getattr(item, "name", "")).strip()
            if name:
                seed_params[name] = getattr(item, "default", None)

    if not seed_params:
        seed_params = load_persistent_best_params()
    if not seed_params:
        seed_params = {name: spec.get("start") for name, spec in param_space.items()}
    return normalize_params_to_space(seed_params, param_space)


def _apply_reference_memory_bias(
    *,
    params: dict[str, Any],
    param_space: dict[str, dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    candidate_index: int,
) -> dict[str, Any]:
    if not reference_rows or candidate_index <= 0:
        return params

    biased = dict(params)
    ref_row = reference_rows[(candidate_index - 1) % len(reference_rows)]
    focus_names = [name for name in KEY_SWEEP_PARAMS if name in biased and name in ref_row][: 2 + (candidate_index % 2)]

    for name in focus_names:
        spec = param_space.get(name)
        if spec is None:
            continue
        values = _spec_values(spec)
        if not values:
            continue

        current_index = _nearest_index(values, float(biased[name]))
        ref_index = _nearest_index(values, float(ref_row[name]))
        if current_index == ref_index:
            continue

        step_size = min(abs(ref_index - current_index), 1 + candidate_index // 3, 3)
        direction = 1 if ref_index > current_index else -1
        next_index = min(max(current_index + (direction * step_size), 0), len(values) - 1)
        biased[name] = values[next_index]

    return normalize_params_to_space(biased, param_space)


def _build_mock_groups_for_config(config: ResearchConfig, seed_params: dict[str, Any]) -> list[dict[str, Any]]:
    if str(config.exploration_mode or "seed_local") == "seed_local":
        return [
            {
                "code": str(config.seed_source or "seed_local"),
                "label": str(config.seed_label or "0313plus 原始參數延伸"),
                "summary": "先以你指定的起始參數為中心，小幅向外擴張測試，並把最佳化累積 Top10 當成次參考來源，不直接跳到遠離 0313plus 核心的參數區。",
                "anchors": dict(seed_params),
            }
        ]
    return MOCK_STRATEGY_GROUPS_V2


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    seed_params = _normalize_seed_params_for_mock(config, param_space)
    reference_rows = _reference_rows_for_mock(limit=10)
    active_groups = _build_mock_groups_for_config(config, seed_params)
    batch_size = max(1, int(config.batch_size))
    strategy_group = active_groups[(max(1, int(current_round)) - 1) % len(active_groups)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(active_groups)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}:v7")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=seed_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        params = _apply_reference_memory_bias(
            params=params,
            param_space=param_space,
            reference_rows=reference_rows,
            candidate_index=candidate_index,
        )
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 第 {candidate_index + 1} 組",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                    "seed_source": config.seed_source,
                    "seed_label": config.seed_label,
                    "optimization_reference_count": len(reference_rows),
                },
            }
        )

    payload = {"candidates": candidates}
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(active_groups),
            "params_total_in_group": batch_size,
            "param_scope_text": _build_param_scope_text_v2(param_space, candidates),
            "anchor_text": _build_anchor_text_v2(strategy_group.get("anchors") or {}),
            "seed_source": config.seed_source,
            "seed_label": config.seed_label,
            "optimization_reference_count": len(reference_rows),
        },
    }


def _reference_rows_for_mock(limit: int = 10) -> list[dict[str, Any]]:
    rows = load_persistent_top10_rows(limit=limit)
    return [row for row in rows if isinstance(row, dict)]


def _normalize_seed_params_for_mock(config: ResearchConfig, param_space: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw_seed_params = config.seed_params or {}
    seed_params: dict[str, Any] = {}

    if isinstance(raw_seed_params, dict):
        seed_params = dict(raw_seed_params)
    else:
        for item in raw_seed_params if isinstance(raw_seed_params, (list, tuple)) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    seed_params[name] = item.get("default")
                continue
            name = str(getattr(item, "name", "")).strip()
            if name:
                seed_params[name] = getattr(item, "default", None)

    if not seed_params:
        seed_params = load_persistent_best_params()
    if not seed_params:
        seed_params = {name: spec.get("start") for name, spec in param_space.items()}
    return normalize_params_to_space(seed_params, param_space)


def _apply_reference_memory_bias(
    *,
    params: dict[str, Any],
    param_space: dict[str, dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    candidate_index: int,
) -> dict[str, Any]:
    if not reference_rows or candidate_index <= 0:
        return params

    biased = dict(params)
    ref_row = reference_rows[(candidate_index - 1) % len(reference_rows)]
    focus_names = [name for name in KEY_SWEEP_PARAMS if name in biased and name in ref_row][: 2 + (candidate_index % 2)]

    for name in focus_names:
        spec = param_space.get(name)
        if spec is None:
            continue
        values = _spec_values(spec)
        if not values:
            continue

        current_index = _nearest_index(values, float(biased[name]))
        ref_index = _nearest_index(values, float(ref_row[name]))
        if current_index == ref_index:
            continue

        step_size = min(abs(ref_index - current_index), 1 + candidate_index // 3, 3)
        direction = 1 if ref_index > current_index else -1
        next_index = min(max(current_index + (direction * step_size), 0), len(values) - 1)
        biased[name] = values[next_index]

    return normalize_params_to_space(biased, param_space)


def _build_mock_groups_for_config(config: ResearchConfig, seed_params: dict[str, Any]) -> list[dict[str, Any]]:
    if str(config.exploration_mode or "seed_local") == "seed_local":
        return [
            {
                "code": str(config.seed_source or "seed_local"),
                "label": str(config.seed_label or "0313plus 原始參數延伸"),
                "summary": "先以你指定的起始參數為中心，小幅向外擴張測試，並把最佳化累積 Top10 當成次參考來源，不直接跳到遠離 0313plus 核心的參數區。",
                "anchors": dict(seed_params),
            }
        ]
    return MOCK_STRATEGY_GROUPS_V2


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    seed_params = _normalize_seed_params_for_mock(config, param_space)
    reference_rows = _reference_rows_for_mock(limit=10)
    active_groups = _build_mock_groups_for_config(config, seed_params)
    batch_size = max(1, int(config.batch_size))
    strategy_group = active_groups[(max(1, int(current_round)) - 1) % len(active_groups)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(active_groups)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}:v6")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=seed_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        params = _apply_reference_memory_bias(
            params=params,
            param_space=param_space,
            reference_rows=reference_rows,
            candidate_index=candidate_index,
        )
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 第 {candidate_index + 1} 組",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                    "seed_source": config.seed_source,
                    "seed_label": config.seed_label,
                    "optimization_reference_count": len(reference_rows),
                },
            }
        )

    payload = {"candidates": candidates}
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(active_groups),
            "params_total_in_group": batch_size,
            "param_scope_text": _build_param_scope_text_v2(param_space, candidates),
            "anchor_text": _build_anchor_text_v2(strategy_group.get("anchors") or {}),
            "seed_source": config.seed_source,
            "seed_label": config.seed_label,
            "optimization_reference_count": len(reference_rows),
        },
    }


def _reference_rows_for_mock(limit: int = 10) -> list[dict[str, Any]]:
    rows = load_persistent_top10_rows(limit=limit)
    return [row for row in rows if isinstance(row, dict)]


def _apply_reference_memory_bias(
    *,
    params: dict[str, Any],
    param_space: dict[str, dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    candidate_index: int,
) -> dict[str, Any]:
    if not reference_rows or candidate_index <= 0:
        return params

    biased = dict(params)
    ref_row = reference_rows[(candidate_index - 1) % len(reference_rows)]
    focus_names = [name for name in KEY_SWEEP_PARAMS if name in biased and name in ref_row][: 2 + (candidate_index % 2)]

    for name in focus_names:
        spec = param_space.get(name)
        if spec is None:
            continue
        values = _spec_values(spec)
        if not values:
            continue

        current_index = _nearest_index(values, float(biased[name]))
        ref_index = _nearest_index(values, float(ref_row[name]))
        if current_index == ref_index:
            continue

        step_size = min(abs(ref_index - current_index), 1 + candidate_index // 3, 3)
        direction = 1 if ref_index > current_index else -1
        next_index = min(max(current_index + (direction * step_size), 0), len(values) - 1)
        biased[name] = values[next_index]

    return normalize_params_to_space(biased, param_space)


def _build_mock_groups_for_config(config: ResearchConfig, seed_params: dict[str, Any]) -> list[dict[str, Any]]:
    if str(config.exploration_mode or "seed_local") == "seed_local":
        return [
            {
                "code": str(config.seed_source or "seed_local"),
                "label": str(config.seed_label or "0313plus 原始參數延伸"),
                "summary": "先以你指定的起始參數為中心，小幅向外擴張測試，並把最佳化累積 Top10 當成次參考來源，不直接跳到遠離 0313plus 核心的參數區。",
                "anchors": dict(seed_params),
            }
        ]
    return MOCK_STRATEGY_GROUPS_V2


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    seed_params = _normalize_seed_params_for_mock(config, param_space)
    reference_rows = _reference_rows_for_mock(limit=10)
    active_groups = _build_mock_groups_for_config(config, seed_params)
    batch_size = max(1, int(config.batch_size))
    strategy_group = active_groups[(max(1, int(current_round)) - 1) % len(active_groups)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(active_groups)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}:v5")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=seed_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        params = _apply_reference_memory_bias(
            params=params,
            param_space=param_space,
            reference_rows=reference_rows,
            candidate_index=candidate_index,
        )
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 第 {candidate_index + 1} 組",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                    "seed_source": config.seed_source,
                    "seed_label": config.seed_label,
                    "optimization_reference_count": len(reference_rows),
                },
            }
        )

    payload = {"candidates": candidates}
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(active_groups),
            "params_total_in_group": batch_size,
            "param_scope_text": _build_param_scope_text_v2(param_space, candidates),
            "anchor_text": _build_anchor_text_v2(strategy_group.get("anchors") or {}),
            "seed_source": config.seed_source,
            "seed_label": config.seed_label,
            "optimization_reference_count": len(reference_rows),
        },
    }


def _normalize_seed_params_for_mock(config: ResearchConfig, param_space: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw_seed_params = config.seed_params or {}
    seed_params: dict[str, Any] = {}

    if isinstance(raw_seed_params, dict):
        seed_params = dict(raw_seed_params)
    else:
        for item in raw_seed_params if isinstance(raw_seed_params, (list, tuple)) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    seed_params[name] = item.get("default")
                continue
            name = str(getattr(item, "name", "")).strip()
            if name:
                seed_params[name] = getattr(item, "default", None)

    if not seed_params:
        seed_params = load_persistent_best_params()
    if not seed_params:
        seed_params = {name: spec.get("start") for name, spec in param_space.items()}
    return normalize_params_to_space(seed_params, param_space)


def _build_mock_groups_for_config(config: ResearchConfig, seed_params: dict[str, Any]) -> list[dict[str, Any]]:
    if str(config.exploration_mode or "seed_local") == "seed_local":
        return [
            {
                "code": str(config.seed_source or "seed_local"),
                "label": str(config.seed_label or "0313plus 原始參數延伸"),
                "summary": "先以你指定的起始參數為中心，小幅向外擴張測試，不直接跳到遠離 0313plus 核心的參數區。",
                "anchors": dict(seed_params),
            }
        ]
    return MOCK_STRATEGY_GROUPS_V2


def build_mock_candidate_batch(config: ResearchConfig, current_round: int = 1) -> dict[str, Any]:
    param_space = load_research_param_space(config.param_preset_path)
    if not param_space:
        raise RuntimeError("param space is empty for mock-local mode")

    seed_params = _normalize_seed_params_for_mock(config, param_space)
    active_groups = _build_mock_groups_for_config(config, seed_params)
    batch_size = max(1, int(config.batch_size))
    strategy_group = active_groups[(max(1, int(current_round)) - 1) % len(active_groups)]
    strategy_group_index = ((max(1, int(current_round)) - 1) % len(active_groups)) + 1
    rng = random.Random(f"{config.session_id}:{current_round}:{time.time_ns()}:{strategy_group['code']}:v4")

    candidates: list[dict[str, Any]] = []
    for candidate_index in range(batch_size):
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            params[name] = _pick_group_value(
                rng=rng,
                name=name,
                spec=spec,
                bootstrap_params=seed_params,
                anchors=strategy_group.get("anchors") or {},
                candidate_index=candidate_index,
            )
        params = normalize_params_to_space(params, param_space)
        candidates.append(
            {
                "parent_strategy_id": strategy_group["code"],
                "ai_summary": f"{strategy_group['label']} 第 {candidate_index + 1} 組",
                "params": params,
                "template_choices": {
                    "strategy_group_code": strategy_group["code"],
                    "strategy_group_label": strategy_group["label"],
                    "strategy_group_index": strategy_group_index,
                    "seed_source": config.seed_source,
                    "seed_label": config.seed_label,
                },
            }
        )

    payload = {"candidates": candidates}
    return {
        "payload": payload,
        "raw_text": json.dumps(payload, ensure_ascii=False, indent=2),
        "meta": {
            "strategy_group_code": strategy_group["code"],
            "strategy_group_label": strategy_group["label"],
            "strategy_group_summary": strategy_group["summary"],
            "strategy_group_index": strategy_group_index,
            "strategy_group_count": len(active_groups),
            "params_total_in_group": batch_size,
            "param_scope_text": _build_param_scope_text_v2(param_space, candidates),
            "anchor_text": _build_anchor_text_v2(strategy_group.get("anchors") or {}),
            "seed_source": config.seed_source,
            "seed_label": config.seed_label,
        },
    }
