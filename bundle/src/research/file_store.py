from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .modular_0313plus import (
    ATR_FILTER_MODE_CHOICES,
    BIAS_MODE_CHOICES,
    ENTRY_MODE_CHOICES,
    KEY_SWEEP_PARAMS,
    MODULE_FAMILY_CODE,
    describe_0313plus_template_choices,
)
from .paths import research_sessions_dir, session_dir
from .types import BacktestMetrics, CandidateProposal, ResearchConfig


_MODULE_DIMENSION_KEYS = [
    "bias_mode",
    "entry_mode",
    "atr_filter_mode",
    "use_atr_stop",
    "use_atr_tp",
    "use_time_stop",
    "use_trail_exit",
]
_MODULE_DIMENSION_LABELS = {
    "bias_mode": "Bias 模組",
    "entry_mode": "Entry 模組",
    "atr_filter_mode": "ATR Filter",
    "use_atr_stop": "ATR 停損",
    "use_atr_tp": "ATR 停利",
    "use_time_stop": "Time Stop",
    "use_trail_exit": "Trail Exit",
}
_BOOL_CHOICE_LABELS = {
    "use_atr_stop": {0: "關閉 ATR 停損", 1: "啟用 ATR 停損"},
    "use_atr_tp": {0: "關閉 ATR 停利", 1: "啟用 ATR 停利"},
    "use_time_stop": {0: "關閉 Time Stop", 1: "啟用 Time Stop"},
    "use_trail_exit": {0: "關閉 Trail Exit", 1: "啟用 Trail Exit"},
}


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported json value: {type(value)!r}")


def _session_file(session_id: str) -> Path:
    return session_dir(session_id) / "session.json"


def _strategies_file(session_id: str) -> Path:
    return session_dir(session_id) / "strategies.jsonl"


def _runs_file(session_id: str) -> Path:
    return session_dir(session_id) / "runs.jsonl"


def _llm_calls_file(session_id: str) -> Path:
    return session_dir(session_id) / "llm_calls.jsonl"


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def init_store() -> None:
    research_sessions_dir()


def create_session(config: ResearchConfig) -> None:
    session_path = _session_file(config.session_id)
    stamp = _now_text()
    payload = {
        "session_id": config.session_id,
        "status": "CREATED",
        "model": config.model,
        "base_xs_path": config.base_xs_path,
        "minute_path": config.minute_path,
        "daily_path": config.daily_path,
        "txt_path": config.txt_path,
        "param_preset_path": config.param_preset_path,
        "allow_param_mutation": bool(config.allow_param_mutation),
        "allow_template_mutation": bool(config.allow_template_mutation),
        "batch_size": int(config.batch_size),
        "created_at": stamp,
        "updated_at": stamp,
        "stopped_at": None,
    }
    session_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def insert_strategy(
    *,
    session_id: str,
    strategy_id: str,
    proposal: CandidateProposal,
    xs_path: str,
    params_txt_path: str,
    xs_hash: str,
    strategy_signature: str,
    parent_strategy_id: str | None,
) -> None:
    rows = _read_jsonl(_strategies_file(session_id))
    version_no = len(rows) + 1
    _append_jsonl(
        _strategies_file(session_id),
        {
            "strategy_id": strategy_id,
            "parent_strategy_id": parent_strategy_id,
            "session_id": session_id,
            "version_no": version_no,
            "xs_path": xs_path,
            "params_txt_path": params_txt_path,
            "xs_hash": xs_hash,
            "strategy_signature": strategy_signature,
            "ai_summary": proposal.ai_summary,
            "template_choices_json": proposal.template_choices,
            "params_json": proposal.params,
            "created_at": _now_text(),
        },
    )


def insert_run(*, session_id: str, strategy_id: str, metrics: BacktestMetrics) -> None:
    _append_jsonl(
        _runs_file(session_id),
        {
            "run_id": uuid4().hex,
            "session_id": session_id,
            "strategy_id": strategy_id,
            "total_return": float(metrics.total_return),
            "mdd_pct": float(metrics.mdd_pct),
            "n_trades": int(metrics.n_trades),
            "year_avg_return": float(metrics.year_avg_return),
            "year_return_std": float(metrics.year_return_std),
            "loss_years": int(metrics.loss_years),
            "composite_score": float(metrics.composite_score),
            "fail_reason": metrics.fail_reason,
            "created_at": _now_text(),
        },
    )


def record_llm_call(
    *,
    session_id: str,
    model: str,
    prompt_text: str,
    response_text: str,
    candidate_count: int,
) -> None:
    _append_jsonl(
        _llm_calls_file(session_id),
        {
            "call_id": uuid4().hex,
            "session_id": session_id,
            "model": model,
            "prompt_text": prompt_text,
            "response_text": response_text,
            "candidate_count": int(candidate_count),
            "created_at": _now_text(),
        },
    )


def has_strategy_signature(strategy_signature: str) -> bool:
    for session_path in research_sessions_dir().iterdir():
        if not session_path.is_dir():
            continue
        for row in _read_jsonl(session_path / "strategies.jsonl"):
            if str(row.get("strategy_signature")) == str(strategy_signature):
                return True
    return False


def get_run_by_strategy_signature(strategy_signature: str) -> dict[str, Any] | None:
    target_signature = str(strategy_signature or "").strip()
    if not target_signature:
        return None

    for session_path in research_sessions_dir().iterdir():
        if not session_path.is_dir():
            continue
        session_id = session_path.name
        strategy_map = _strategy_map(session_id)
        matched_strategy = None
        for row in strategy_map.values():
            if str(row.get("strategy_signature") or "") == target_signature:
                matched_strategy = row
                break
        if not matched_strategy:
            continue

        matched_rows = [
            row
            for row in _read_jsonl(_runs_file(session_id))
            if str(row.get("strategy_id") or "") == str(matched_strategy.get("strategy_id") or "")
        ]
        if not matched_rows:
            return _attach_strategy_metadata({"session_id": session_id}, matched_strategy)

        best_row = sorted(
            matched_rows,
            key=lambda row: (
                -float(row.get("composite_score", -1e18)),
                float(row.get("mdd_pct", 1e18)),
                -float(row.get("total_return", -1e18)),
            ),
        )[0]
        merged = _attach_strategy_metadata({**best_row, "session_id": session_id}, matched_strategy)
        merged["xs_path"] = matched_strategy.get("xs_path")
        merged["params_txt_path"] = matched_strategy.get("params_txt_path")
        return merged
    return None


def update_session_status(session_id: str, status: str) -> None:
    path = _session_file(session_id)
    payload = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload["session_id"] = session_id
    payload["status"] = status
    payload["updated_at"] = _now_text()
    if status in {"STOPPED", "FAILED", "COMPLETED"}:
        payload["stopped_at"] = payload["updated_at"]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _strategy_map(session_id: str) -> dict[str, dict[str, Any]]:
    return {str(row.get("strategy_id")): row for row in _read_jsonl(_strategies_file(session_id))}


def _template_choices_payload(value: Any) -> dict[str, Any]:
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


def _params_payload(value: Any) -> dict[str, Any]:
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


def _choice_label(dimension_key: str, value: Any) -> str:
    text = str(value)
    if dimension_key == "bias_mode":
        for code, label in BIAS_MODE_CHOICES:
            if code == text:
                return label
    elif dimension_key == "entry_mode":
        for code, label in ENTRY_MODE_CHOICES:
            if code == text:
                return label
    elif dimension_key == "atr_filter_mode":
        for code, label in ATR_FILTER_MODE_CHOICES:
            if code == text:
                return label
    elif dimension_key in _BOOL_CHOICE_LABELS:
        try:
            return _BOOL_CHOICE_LABELS[dimension_key][int(value)]
        except Exception:
            return text
    return text


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    index = max(0.0, min(float(ratio), 1.0)) * float(len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return float(sorted_values[lower])
    weight = index - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def _best_tuple(row: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(row.get("composite_score") or -1e18),
        -float(row.get("mdd_pct") or 1e18),
        float(row.get("total_return") or -1e18),
    )


def _attach_strategy_metadata(row: dict[str, Any], strategy: dict[str, Any]) -> dict[str, Any]:
    template_choices = _template_choices_payload(strategy.get("template_choices_json"))
    return {
        **row,
        "params_json": json.dumps(strategy.get("params_json", {}), ensure_ascii=False),
        "ai_summary": strategy.get("ai_summary"),
        "xs_path": strategy.get("xs_path"),
        "params_txt_path": strategy.get("params_txt_path"),
        "template_choices_json": json.dumps(template_choices, ensure_ascii=False),
        "strategy_group_code": str(template_choices.get("strategy_group_code") or "ungrouped"),
        "strategy_group_label": str(template_choices.get("strategy_group_label") or "未分類策略家族"),
    }


def get_recent_runs(session_id: str, limit: int = 30) -> list[dict]:
    strategy_map = _strategy_map(session_id)
    rows = sorted(_read_jsonl(_runs_file(session_id)), key=lambda row: str(row.get("created_at", "")), reverse=True)
    merged: list[dict] = []
    for row in rows[: int(limit)]:
        strategy = strategy_map.get(str(row.get("strategy_id")), {})
        merged.append(_attach_strategy_metadata(row, strategy))
    return merged


def get_top_runs(session_id: str, limit: int = 10) -> list[dict]:
    strategy_map = _strategy_map(session_id)
    rows = sorted(
        _read_jsonl(_runs_file(session_id)),
        key=lambda row: (
            -float(row.get("composite_score", -1e18)),
            float(row.get("mdd_pct", 1e18)),
            -float(row.get("total_return", -1e18)),
        ),
    )
    merged: list[dict] = []
    for row in rows[: int(limit)]:
        strategy = strategy_map.get(str(row.get("strategy_id")), {})
        merged.append(_attach_strategy_metadata(row, strategy))
    return merged


def get_strategy_group_summary(session_id: str, limit: int = 8) -> list[dict]:
    strategy_map = _strategy_map(session_id)
    grouped: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(_runs_file(session_id)):
        strategy = strategy_map.get(str(row.get("strategy_id")), {})
        merged = _attach_strategy_metadata(row, strategy)
        group_code = str(merged.get("strategy_group_code") or "ungrouped")
        group_label = str(merged.get("strategy_group_label") or "未分類策略家族")
        bucket = grouped.setdefault(
            group_code,
            {
                "strategy_group_code": group_code,
                "strategy_group_label": group_label,
                "tested_params": 0,
                "valid_runs": 0,
                "best_strategy_id": None,
                "best_score": None,
                "best_total_return": None,
                "best_mdd_pct": None,
                "best_n_trades": None,
                "last_run_at": None,
            },
        )
        bucket["tested_params"] = int(bucket["tested_params"]) + 1
        if not merged.get("fail_reason"):
            bucket["valid_runs"] = int(bucket["valid_runs"]) + 1

        created_at = str(merged.get("created_at") or "")
        if created_at and (not bucket["last_run_at"] or created_at > str(bucket["last_run_at"])):
            bucket["last_run_at"] = created_at

        current_tuple = (
            float(merged.get("composite_score") or -1e18),
            -float(merged.get("mdd_pct") or 1e18),
            float(merged.get("total_return") or -1e18),
        )
        best_tuple = (
            float(bucket.get("best_score") or -1e18),
            -float(bucket.get("best_mdd_pct") or 1e18),
            float(bucket.get("best_total_return") or -1e18),
        )
        if current_tuple > best_tuple:
            bucket["best_strategy_id"] = merged.get("strategy_id")
            bucket["best_score"] = float(merged.get("composite_score") or 0.0)
            bucket["best_total_return"] = float(merged.get("total_return") or 0.0)
            bucket["best_mdd_pct"] = float(merged.get("mdd_pct") or 0.0)
            bucket["best_n_trades"] = int(merged.get("n_trades") or 0)

    rows = list(grouped.values())
    rows.sort(
        key=lambda row: (
            -float(row.get("best_score") or -1e18),
            float(row.get("best_mdd_pct") or 1e18),
            -float(row.get("best_total_return") or -1e18),
        )
    )
    return rows[: int(limit)]


def list_sessions(limit: int = 30) -> list[dict]:
    rows: list[dict] = []
    for path in research_sessions_dir().iterdir():
        if not path.is_dir():
            continue
        session_path = path / "session.json"
        if not session_path.exists():
            continue
        try:
            session_payload = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        run_rows = _read_jsonl(path / "runs.jsonl")
        best_score = None
        if run_rows:
            best_score = max(float(row.get("composite_score", -1e18)) for row in run_rows)
        rows.append(
            {
                "session_id": session_payload.get("session_id"),
                "status": session_payload.get("status"),
                "model": session_payload.get("model"),
                "base_xs_path": session_payload.get("base_xs_path"),
                "created_at": session_payload.get("created_at"),
                "updated_at": session_payload.get("updated_at"),
                "run_count": len(run_rows),
                "best_score": best_score,
            }
        )
    rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return rows[: int(limit)]


def get_session_summary(session_id: str) -> dict:
    session_payload = {}
    path = _session_file(session_id)
    if path.exists():
        try:
            session_payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            session_payload = {}
    strategy_map = _strategy_map(session_id)
    run_rows = _read_jsonl(_runs_file(session_id))
    best_run = None
    if run_rows:
        ranked = sorted(
            run_rows,
            key=lambda row: (
                -float(row.get("composite_score", -1e18)),
                float(row.get("mdd_pct", 1e18)),
                -float(row.get("total_return", -1e18)),
            ),
        )[0]
        strategy = strategy_map.get(str(ranked.get("strategy_id")), {})
        best_run = _attach_strategy_metadata(ranked, strategy)
    return {
        "session": session_payload or None,
        "run_count": len(run_rows),
        "best_run": best_run,
        "strategy_groups": get_strategy_group_summary(session_id, limit=8),
    }


def get_all_modular_runs() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in research_sessions_dir().iterdir():
        if not path.is_dir():
            continue
        session_id = path.name
        strategy_map = _strategy_map(session_id)
        for row in _read_jsonl(_runs_file(session_id)):
            strategy = strategy_map.get(str(row.get("strategy_id")), {})
            template_choices = _template_choices_payload(strategy.get("template_choices_json"))
            if str(template_choices.get("module_family") or "") != MODULE_FAMILY_CODE:
                continue
            rows.append(
                {
                    **row,
                    "session_id": session_id,
                    "params_json": json.dumps(strategy.get("params_json", {}), ensure_ascii=False),
                    "template_choices_json": json.dumps(template_choices, ensure_ascii=False),
                    "ai_summary": strategy.get("ai_summary"),
                }
            )
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def get_module_learning_summary(session_id: str | None = None, limit: int = 8) -> dict[str, Any]:
    rows = get_all_modular_runs()
    if not rows:
        return {
            "total_modular_runs": 0,
            "valid_modular_runs": 0,
            "dimensions": {},
            "top_combos": [],
        }

    dimension_buckets: dict[str, dict[str, dict[str, Any]]] = {
        key: {} for key in _MODULE_DIMENSION_KEYS
    }
    combo_buckets: dict[str, dict[str, Any]] = {}
    valid_modular_runs = 0

    for row in rows:
        template_choices = _template_choices_payload(row.get("template_choices_json"))
        params = _params_payload(row.get("params_json"))
        combo_signature = json.dumps({key: template_choices.get(key) for key in _MODULE_DIMENSION_KEYS}, ensure_ascii=False, sort_keys=True)
        combo_label = describe_0313plus_template_choices(template_choices)
        is_valid = not bool(row.get("fail_reason"))
        score_value = float(row.get("composite_score") or 0.0)
        if is_valid:
            valid_modular_runs += 1

        combo_bucket = combo_buckets.setdefault(
            combo_signature,
            {
                "combo_signature": combo_signature,
                "strategy_group_code": str(template_choices.get("strategy_group_code") or combo_signature),
                "label": combo_label,
                "template_choices": dict(template_choices),
                "tested_count": 0,
                "valid_runs": 0,
                "score_sum": 0.0,
                "best_score": None,
                "best_total_return": None,
                "best_mdd_pct": None,
                "best_params": {},
                "session_hits": 0,
            },
        )
        combo_bucket["tested_count"] = int(combo_bucket["tested_count"]) + 1
        if session_id and str(row.get("session_id") or "") == str(session_id):
            combo_bucket["session_hits"] = int(combo_bucket["session_hits"]) + 1
        if is_valid:
            combo_bucket["valid_runs"] = int(combo_bucket["valid_runs"]) + 1
            combo_bucket["score_sum"] = float(combo_bucket["score_sum"]) + score_value
        if combo_bucket["best_score"] is None or _best_tuple(row) > (
            float(combo_bucket["best_score"] or -1e18),
            -float(combo_bucket["best_mdd_pct"] or 1e18),
            float(combo_bucket["best_total_return"] or -1e18),
        ):
            combo_bucket["best_score"] = score_value
            combo_bucket["best_total_return"] = float(row.get("total_return") or 0.0)
            combo_bucket["best_mdd_pct"] = float(row.get("mdd_pct") or 0.0)
            combo_bucket["best_params"] = dict(params)

        for dimension_key in _MODULE_DIMENSION_KEYS:
            raw_value = template_choices.get(dimension_key)
            value_key = str(raw_value)
            bucket = dimension_buckets[dimension_key].setdefault(
                value_key,
                {
                    "dimension_key": dimension_key,
                    "dimension_label": _MODULE_DIMENSION_LABELS[dimension_key],
                    "code": raw_value,
                    "label": _choice_label(dimension_key, raw_value),
                    "tested_count": 0,
                    "valid_runs": 0,
                    "score_sum": 0.0,
                    "best_score": None,
                    "best_total_return": None,
                    "best_mdd_pct": None,
                    "session_hits": 0,
                },
            )
            bucket["tested_count"] = int(bucket["tested_count"]) + 1
            if session_id and str(row.get("session_id") or "") == str(session_id):
                bucket["session_hits"] = int(bucket["session_hits"]) + 1
            if is_valid:
                bucket["valid_runs"] = int(bucket["valid_runs"]) + 1
                bucket["score_sum"] = float(bucket["score_sum"]) + score_value
            if bucket["best_score"] is None or _best_tuple(row) > (
                float(bucket["best_score"] or -1e18),
                -float(bucket["best_mdd_pct"] or 1e18),
                float(bucket["best_total_return"] or -1e18),
            ):
                bucket["best_score"] = score_value
                bucket["best_total_return"] = float(row.get("total_return") or 0.0)
                bucket["best_mdd_pct"] = float(row.get("mdd_pct") or 0.0)

    dimensions: dict[str, list[dict[str, Any]]] = {}
    for dimension_key, bucket_map in dimension_buckets.items():
        dimension_rows: list[dict[str, Any]] = []
        for bucket in bucket_map.values():
            valid_runs = int(bucket["valid_runs"])
            avg_score = (float(bucket["score_sum"]) / valid_runs) if valid_runs else None
            learned_priority = (
                (float(bucket["best_score"] or 0.0) * 0.65)
                + (float(avg_score or 0.0) * 0.35)
                + min(valid_runs, 12) * 0.08
            )
            dimension_rows.append({**bucket, "avg_score": avg_score, "learned_priority": round(learned_priority, 6)})
        dimension_rows.sort(
            key=lambda row: (
                -float(row.get("learned_priority") or -1e18),
                -int(row.get("valid_runs") or 0),
                float(row.get("best_mdd_pct") or 1e18),
            )
        )
        dimensions[dimension_key] = dimension_rows[: int(limit)]

    top_combos: list[dict[str, Any]] = []
    for bucket in combo_buckets.values():
        valid_runs = int(bucket["valid_runs"])
        avg_score = (float(bucket["score_sum"]) / valid_runs) if valid_runs else None
        learned_priority = (
            (float(bucket["best_score"] or 0.0) * 0.7)
            + (float(avg_score or 0.0) * 0.3)
            + min(valid_runs, 12) * 0.1
        )
        top_combos.append({**bucket, "avg_score": avg_score, "learned_priority": round(learned_priority, 6)})
    top_combos.sort(
        key=lambda row: (
            -float(row.get("learned_priority") or -1e18),
            float(row.get("best_mdd_pct") or 1e18),
            -float(row.get("best_total_return") or -1e18),
        )
    )

    return {
        "total_modular_runs": len(rows),
        "valid_modular_runs": valid_modular_runs,
        "dimensions": dimensions,
        "top_combos": top_combos[: int(limit)],
    }


def get_param_learning_summary(session_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    del session_id
    rows = [
        row for row in get_all_modular_runs()
        if not row.get("fail_reason")
    ]
    if not rows:
        return {
            "total_valid_runs": 0,
            "focus_window": 0,
            "param_ranges": [],
        }

    rows.sort(key=_best_tuple, reverse=True)
    focus_window = max(8, min(len(rows), 36))
    focus_rows = rows[:focus_window]
    best_params = _params_payload(focus_rows[0].get("params_json"))

    param_ranges: list[dict[str, Any]] = []
    for name in KEY_SWEEP_PARAMS:
        all_values = sorted(
            value for value in (_to_float(_params_payload(row.get("params_json")).get(name)) for row in rows) if value is not None
        )
        focus_values = sorted(
            value for value in (_to_float(_params_payload(row.get("params_json")).get(name)) for row in focus_rows) if value is not None
        )
        if not all_values or not focus_values:
            continue
        preferred_low = _percentile(focus_values, 0.25)
        preferred_high = _percentile(focus_values, 0.75)
        if preferred_low is None or preferred_high is None:
            continue
        param_ranges.append(
            {
                "name": name,
                "tested_min": min(all_values),
                "tested_max": max(all_values),
                "preferred_low": preferred_low,
                "preferred_high": preferred_high,
                "best_value": _to_float(best_params.get(name)),
                "observations": len(all_values),
            }
        )

    param_ranges.sort(key=lambda row: (-int(row.get("observations") or 0), str(row.get("name") or "")))
    return {
        "total_valid_runs": len(rows),
        "focus_window": focus_window,
        "param_ranges": param_ranges[: int(limit)],
    }
