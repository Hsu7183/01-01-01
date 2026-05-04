from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .memory_db import get_recent_runs, get_session_summary, get_top_runs
from .param_space import load_persistent_best_params, load_research_param_space
from .types import ResearchConfig


def _parse_params_text(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def summarize_recent_failures(recent_runs: list[dict]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in recent_runs:
        fail_reason = str(row.get("fail_reason") or "").strip()
        if fail_reason:
            counter[fail_reason] += 1
    return [{"reason": reason, "count": count} for reason, count in counter.most_common(5)]


def summarize_best_patterns(top_runs: list[dict]) -> dict[str, list[Any]]:
    patterns: dict[str, list[Any]] = {}
    for row in top_runs:
        params = _parse_params_text(row.get("params_json"))
        for name, value in params.items():
            patterns.setdefault(str(name), []).append(value)
    return patterns


def build_search_context(db_path: str, session_id: str) -> dict[str, Any]:
    recent_runs = get_recent_runs(db_path, session_id, limit=20)
    top_runs = get_top_runs(db_path, session_id, limit=10)
    session_summary = get_session_summary(db_path, session_id)
    return {
        "session_summary": session_summary,
        "recent_runs": recent_runs,
        "top_runs": top_runs,
        "recent_failures": summarize_recent_failures(recent_runs),
        "best_param_patterns": summarize_best_patterns(top_runs),
    }


def build_generation_prompt(config: ResearchConfig, context: dict[str, Any]) -> str:
    param_space = load_research_param_space(config.param_preset_path)
    bootstrap_params = load_persistent_best_params()
    session_summary = context.get("session_summary") or {}
    best_run = session_summary.get("best_run") or {}
    recent_failures = context.get("recent_failures") or []
    best_param_patterns = context.get("best_param_patterns") or {}
    top_runs = context.get("top_runs") or []

    payload = {
        "session_id": config.session_id,
        "market": "台指期",
        "bar_type": "1分K",
        "style": "日內當沖",
        "goal": "提高 total_return，同時控制 mdd_pct、loss_years 與波動。",
        "hard_filters": {
            "min_trades": config.min_trades,
            "min_total_return": config.min_total_return,
            "max_mdd_pct": config.max_mdd_pct,
        },
        "batch_size": config.batch_size,
        "allow_param_mutation": config.allow_param_mutation,
        "allow_template_mutation": config.allow_template_mutation,
        "param_space": param_space,
        "bootstrap_best_params": bootstrap_params,
        "best_run": best_run,
        "recent_failures": recent_failures,
        "best_param_patterns": best_param_patterns,
        "top_runs": top_runs[:5],
    }

    return (
        "你是台指期 1 分 K 日內當沖策略研究助手。\n"
        "請根據目前歷史結果，提出下一批值得測試的候選策略。\n"
        "V2-A 階段以調整參數為主；除非特別允許，不要虛構新的 XS 語法區塊。\n"
        "請只輸出 JSON，格式必須是 "
        '{"candidates":[{"parent_strategy_id":"...","ai_summary":"...","params":{"Param":123},"template_choices":{}}]}\n'
        "每個 candidate 的 params 必須完整、可直接回測，不要省略。\n"
        "參數必須落在 param_space 允許範圍內；若 bootstrap_best_params 超出原始 preset，表示目前可接受更寬的實際範圍。\n"
        "請避免重複 top_runs 已經非常接近的組合，優先提出風報比與穩定度可能改善的方案。\n"
        "以下是目前上下文 JSON：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
