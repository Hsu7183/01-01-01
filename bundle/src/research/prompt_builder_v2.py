from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .memory_db import (
    get_module_learning_summary,
    get_param_learning_summary,
    get_recent_runs,
    get_session_summary,
    get_top_runs,
)
from .modular_0313plus import (
    build_0313plus_module_prompt_payload,
    is_modular_0313plus_enabled,
)
from .param_space import (
    load_optimization_reference_bundle,
    load_persistent_best_params,
    load_research_param_space,
)
from .types import ResearchConfig
from .xscript_policy import (
    XQ_XSCRIPT_POLICY_CHANGE_RULE,
    XQ_XSCRIPT_POLICY_REPO_DOC,
    XQ_XSCRIPT_POLICY_SOURCE_DOC,
    XQ_XSCRIPT_POLICY_VERSION,
)


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
    optimization_memory = load_optimization_reference_bundle(limit=10)
    module_learning = get_module_learning_summary(db_path, session_id=session_id, limit=6)
    param_learning = get_param_learning_summary(db_path, session_id=session_id, limit=10)
    return {
        "session_summary": session_summary,
        "recent_runs": recent_runs,
        "top_runs": top_runs,
        "recent_failures": summarize_recent_failures(recent_runs),
        "best_param_patterns": summarize_best_patterns(top_runs),
        "optimization_memory": optimization_memory,
        "module_learning": module_learning,
        "param_learning": param_learning,
    }


def _policy_payload() -> dict[str, Any]:
    return {
        "version": XQ_XSCRIPT_POLICY_VERSION,
        "source_doc": XQ_XSCRIPT_POLICY_SOURCE_DOC,
        "repo_doc": XQ_XSCRIPT_POLICY_REPO_DOC,
        "change_rule": XQ_XSCRIPT_POLICY_CHANGE_RULE,
        "hard_rules": [
            "策略限定為台指期、1 分 K、日當沖、不留倉、純 XScript/XS。",
            "所有交易判斷只能使用已完成且已定錨的資料。",
            "當根 Open 只能作為即時可得價格與執行價，不能先更新指標再回判同根。",
            "每根 K 棒只能執行一次交易動作；出場優先於進場；禁止同 Bar 反手；訊號不得累積到下一根。",
            "預設優先使用 Close[1] 與由 Close 計算後再取 [1] 的技術指標。",
            "未加 [1] 的浮動指標值不得用於當根交易判斷。",
            "所有 XS 腳本開頭都必須檢查分鐘線執行環境。",
            "ATR 必須逐筆 freeze；VWAP 必須手動累積、每日重置，交易判斷使用 vwap[1]。",
            "TXT 正式輸出必須使用單一完整字串、單參數 print、固定 14 碼時間戳、全檔單次 header。",
            "若要偏離通用指標算法或交易定義，必須視為策略特例並明確標註，不得默默修改底層規範。",
        ],
    }


def build_generation_prompt(config: ResearchConfig, context: dict[str, Any]) -> str:
    param_space = load_research_param_space(config.param_preset_path)
    bootstrap_params = load_persistent_best_params()
    selected_seed_params = config.seed_params or {}
    session_summary = context.get("session_summary") or {}
    best_run = session_summary.get("best_run") or {}
    recent_failures = context.get("recent_failures") or []
    best_param_patterns = context.get("best_param_patterns") or {}
    top_runs = context.get("top_runs") or []
    optimization_memory = context.get("optimization_memory") or {}
    module_learning = context.get("module_learning") or {}
    param_learning = context.get("param_learning") or {}

    payload = {
        "session_id": config.session_id,
        "market": "台指期",
        "bar_type": "1分K",
        "style": "日內當沖",
        "goal": "提升 total_return，同時控制 mdd_pct、loss_years、年報酬波動與交易品質。",
        "research_order": [
            "先選一個策略組合",
            "把該策略組合這一輪的候選參數完整測完",
            "再切換到下一個策略組合",
        ],
        "hard_filters": {
            "min_trades": config.min_trades,
            "min_total_return": config.min_total_return,
            "max_mdd_pct": config.max_mdd_pct,
        },
        "batch_size": config.batch_size,
        "seed_source": config.seed_source,
        "seed_label": config.seed_label,
        "selected_seed_params": selected_seed_params,
        "exploration_mode": config.exploration_mode,
        "allow_param_mutation": config.allow_param_mutation,
        "allow_template_mutation": config.allow_template_mutation,
        "param_space": param_space,
        "bootstrap_best_params": bootstrap_params,
        "best_run": best_run,
        "recent_failures": recent_failures,
        "best_param_patterns": best_param_patterns,
        "top_runs": top_runs[:5],
        "optimization_memory_best_params": optimization_memory.get("best_params") or bootstrap_params,
        "optimization_memory_top10": (optimization_memory.get("top10_rows") or [])[:10],
        "learned_module_blocks": module_learning.get("dimensions") or {},
        "learned_top_combos": (module_learning.get("top_combos") or [])[:6],
        "learned_param_zones": (param_learning.get("param_ranges") or [])[:10],
        "xscript_policy": _policy_payload(),
    }
    if is_modular_0313plus_enabled(config):
        payload["module_universe"] = build_0313plus_module_prompt_payload()

    return (
        "你是台指期 1 分 K 日內當沖策略研究助手。\n"
        "你後續提出的候選策略與參數，必須嚴格受 XScript 最高規範約束。\n"
        "你不能改寫最高規範；若需要例外，只能把它標成策略特例，不能默默修改通用規則。\n"
        "V2 研究階段必須遵守流程：先選一個策略組合，再把該組合的候選參數完整測完，再換下一個策略組合。\n"
        "請只輸出 JSON，格式必須是 "
        '{"candidates":[{"parent_strategy_id":"...","ai_summary":"...","params":{"Param":123},"template_choices":{}}]}\n'
        "每個 candidate 的 params 必須完整、可直接回測，不可省略。\n"
        "參數必須落在 param_space 允許範圍內。\n"
        "請避免重複 top_runs 已非常接近的組合，並優先提出風報比與穩定度可能更好的方案。\n"
        "以下是本輪上下文 JSON：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
