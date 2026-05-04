from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory_db import (
    create_session,
    get_run_by_strategy_signature,
    has_strategy_signature,
    init_memory_db,
    insert_run,
    insert_strategy,
    record_llm_call,
    update_session_status,
)
from .cleanup_manager import AUTO_CLEANUP_INTERVAL_SECONDS, run_auto_cleanup
from .error_utils import format_research_error
from .modular_0313plus import advance_modular_sweep_state, is_modular_0313plus_enabled
from .param_space import load_research_param_space
from .paths import PROJECT_ROOT, session_dir
from .prompt_builder_v2 import build_generation_prompt, build_search_context
from .proposal_schema import validate_candidate_batch
from .runtime_bridge import evaluate_candidate
from .stop_controller import clear_stop, session_status_path, should_stop
from .types import BacktestMetrics, CandidateProposal, ResearchConfig, ResearchStatus, StrategyArtifact
from .xs_generator import build_strategy_signature, write_strategy_artifacts


DISPLAY_PARAM_ORDER = [
    "DonLen",
    "ATRLen",
    "EntryBufferPts",
    "DonBufferPts",
    "ATRStopK",
    "ATRTakeProfitK",
    "TimeStopBars",
    "AnchorBackPct",
]


def _format_param_value(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _candidate_params_text(proposal: CandidateProposal) -> str:
    parts: list[str] = []
    for name in DISPLAY_PARAM_ORDER:
        if name in proposal.params:
            parts.append(f"{name}={_format_param_value(proposal.params[name])}")
    return "；".join(parts[:6])


def _write_status(status: ResearchStatus) -> None:
    path = session_status_path(status.session_id)
    path.write_text(json.dumps(asdict(status), ensure_ascii=False, indent=2), encoding="utf-8")


def _emit_status(
    *,
    session_id: str,
    status: str,
    current_round: int = 0,
    tested_count: int = 0,
    best_strategy_id: str | None = None,
    best_score: float | None = None,
    current_action: str | None = None,
    current_candidate_label: str | None = None,
    last_completed_strategy_id: str | None = None,
    current_strategy_group_code: str | None = None,
    current_strategy_group_label: str | None = None,
    current_strategy_group_summary: str | None = None,
    current_strategy_group_order_text: str | None = None,
    current_param_scope_text: str | None = None,
    current_candidate_params_text: str | None = None,
    current_candidate_index: int = 0,
    current_candidate_total: int = 0,
    strategy_groups_completed: int = 0,
    params_tested_in_group: int = 0,
    params_total_in_group: int = 0,
    current_phase: str | None = None,
    session_elapsed_seconds: int = 0,
    compute_elapsed_seconds: int = 0,
    wait_elapsed_seconds: int = 0,
    last_error: str | None = None,
) -> ResearchStatus:
    payload = ResearchStatus(
        session_id=session_id,
        status=status,
        current_round=current_round,
        tested_count=tested_count,
        best_strategy_id=best_strategy_id,
        best_score=best_score,
        current_action=current_action,
        current_candidate_label=current_candidate_label,
        last_completed_strategy_id=last_completed_strategy_id,
        current_strategy_group_code=current_strategy_group_code,
        current_strategy_group_label=current_strategy_group_label,
        current_strategy_group_summary=current_strategy_group_summary,
        current_strategy_group_order_text=current_strategy_group_order_text,
        current_param_scope_text=current_param_scope_text,
        current_candidate_params_text=current_candidate_params_text,
        current_candidate_index=current_candidate_index,
        current_candidate_total=current_candidate_total,
        strategy_groups_completed=strategy_groups_completed,
        params_tested_in_group=params_tested_in_group,
        params_total_in_group=params_total_in_group,
        current_phase=current_phase,
        session_elapsed_seconds=int(session_elapsed_seconds or 0),
        compute_elapsed_seconds=int(compute_elapsed_seconds or 0),
        wait_elapsed_seconds=int(wait_elapsed_seconds or 0),
        last_error=last_error,
    )
    _write_status(payload)

    log_parts = [
        payload.updated_at,
        f"status={payload.status}",
        f"round={payload.current_round}",
        f"tested={payload.tested_count}",
    ]
    if payload.current_action:
        log_parts.append(f"action={payload.current_action}")
    if payload.current_candidate_label:
        log_parts.append(f"candidate={payload.current_candidate_label}")
    if payload.best_strategy_id:
        log_parts.append(f"best={payload.best_strategy_id}")
    print(" | ".join(log_parts), flush=True)
    return payload


def _hash_file(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 64)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def persist_round_outputs(
    db_path: str,
    session_id: str,
    proposal: CandidateProposal,
    artifact: StrategyArtifact,
    metrics,
) -> None:
    insert_strategy(
        db_path=db_path,
        session_id=session_id,
        strategy_id=artifact.strategy_id,
        proposal=proposal,
        xs_path=artifact.xs_path,
        params_txt_path=artifact.params_txt_path,
        xs_hash=_hash_file(artifact.xs_path),
        strategy_signature=artifact.signature,
        parent_strategy_id=proposal.parent_strategy_id,
    )
    insert_run(db_path=db_path, session_id=session_id, strategy_id=artifact.strategy_id, metrics=metrics)


def run_research_session(config: ResearchConfig, db_path: str) -> None:
    init_memory_db(db_path)
    create_session(db_path, config)
    clear_stop(config.session_id)

    _emit_timed_status(
        session_id=config.session_id,
        status="RUNNING",
        current_action="研究工作已啟動，準備建立第一輪候選。",
    )
    update_session_status(db_path, config.session_id, "RUNNING")

    current_round = 0
    tested_count = 0
    best_strategy_id: str | None = None
    best_score: float | None = None
    last_completed_strategy_id: str | None = None
    max_fetch_attempts = 3
    strategy_groups_completed = 0
    current_strategy_group_code: str | None = None
    current_strategy_group_label: str | None = None
    current_strategy_group_summary: str | None = None
    current_strategy_group_order_text: str | None = None
    current_param_scope_text: str | None = None
    current_candidate_params_text: str | None = None
    current_candidate_index = 0
    current_candidate_total = 0
    params_tested_in_group = 0
    params_total_in_group = 0

    try:
        while not should_stop(config.session_id):
            if config.max_rounds is not None and current_round >= int(config.max_rounds):
                break

            current_round += 1
            _emit_timed_status(
                session_id=config.session_id,
                status="RUNNING",
                current_round=current_round,
                tested_count=tested_count,
                best_strategy_id=best_strategy_id,
                best_score=best_score,
                current_action=f"第 {current_round} 輪：整理歷史結果並建立 prompt。",
                last_completed_strategy_id=last_completed_strategy_id,
                current_strategy_group_code=current_strategy_group_code,
                current_strategy_group_label=current_strategy_group_label,
                strategy_groups_completed=strategy_groups_completed,
                params_tested_in_group=params_tested_in_group,
                params_total_in_group=params_total_in_group,
            )
            context = build_search_context(db_path, config.session_id)
            prompt_text = build_generation_prompt(config, context)
            evaluated_this_round = 0

            for _attempt in range(max_fetch_attempts):
                if should_stop(config.session_id):
                    break

                from .openai_client import request_candidate_batch

                _emit_timed_status(
                    session_id=config.session_id,
                    status="RUNNING",
                    current_round=current_round,
                    tested_count=tested_count,
                    best_strategy_id=best_strategy_id,
                    best_score=best_score,
                    current_action=f"第 {current_round} 輪：產生候選參數。",
                    last_completed_strategy_id=last_completed_strategy_id,
                    current_strategy_group_code=current_strategy_group_code,
                    current_strategy_group_label=current_strategy_group_label,
                    strategy_groups_completed=strategy_groups_completed,
                    params_tested_in_group=params_tested_in_group,
                    params_total_in_group=params_total_in_group,
                )
                response = request_candidate_batch(
                    config,
                    prompt_text,
                    current_round=current_round,
                    context=context,
                )
                payload = response["payload"]
                raw_text = response["raw_text"]
                response_meta = response.get("meta") or {}
                param_space = load_research_param_space(config.param_preset_path)
                proposals = validate_candidate_batch(payload, param_space=param_space)
                record_llm_call(
                    db_path=db_path,
                    session_id=config.session_id,
                    model=config.model,
                    prompt_text=prompt_text,
                    response_text=raw_text,
                    candidate_count=len(proposals),
                )

                if not proposals:
                    raise RuntimeError("LLM returned zero candidates")

                candidate_limit = min(len(proposals), int(config.batch_size))
                current_strategy_group_code = str(
                    response_meta.get("strategy_group_code")
                    or proposals[0].template_choices.get("strategy_group_code")
                    or f"round_{current_round}"
                )
                current_strategy_group_label = str(
                    response_meta.get("strategy_group_label")
                    or proposals[0].template_choices.get("strategy_group_label")
                    or f"第 {current_round} 組策略"
                )
                params_tested_in_group = 0
                params_total_in_group = int(response_meta.get("params_total_in_group") or candidate_limit)
                _emit_status(
                    session_id=config.session_id,
                    status="RUNNING",
                    current_round=current_round,
                    tested_count=tested_count,
                    best_strategy_id=best_strategy_id,
                    best_score=best_score,
                    current_action=f"第 {current_round} 輪：選定策略組合「{current_strategy_group_label}」，開始測參數。",
                    last_completed_strategy_id=last_completed_strategy_id,
                    current_strategy_group_code=current_strategy_group_code,
                    current_strategy_group_label=current_strategy_group_label,
                    strategy_groups_completed=strategy_groups_completed,
                    params_tested_in_group=params_tested_in_group,
                    params_total_in_group=params_total_in_group,
                )

                for proposal_index, proposal in enumerate(proposals[:candidate_limit], start=1):
                    if should_stop(config.session_id):
                        break

                    candidate_label = proposal.ai_summary or f"candidate {proposal_index}"
                    _emit_timed_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"第 {current_round} 輪：策略組合「{current_strategy_group_label}」正在測第 {proposal_index}/{candidate_limit} 組參數。",
                        current_candidate_label=candidate_label,
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_tested_in_group,
                        params_total_in_group=params_total_in_group,
                    )
                    artifact = write_strategy_artifacts(config.base_xs_path, proposal)
                    if has_strategy_signature(db_path, artifact.signature):
                        params_tested_in_group = proposal_index
                        _emit_status(
                            session_id=config.session_id,
                            status="RUNNING",
                            current_round=current_round,
                            tested_count=tested_count,
                            best_strategy_id=best_strategy_id,
                            best_score=best_score,
                            current_action=f"第 {current_round} 輪：策略組合「{current_strategy_group_label}」略過重複參數 {proposal_index}/{candidate_limit}。",
                            current_candidate_label=candidate_label,
                            last_completed_strategy_id=last_completed_strategy_id,
                            current_strategy_group_code=current_strategy_group_code,
                            current_strategy_group_label=current_strategy_group_label,
                            strategy_groups_completed=strategy_groups_completed,
                            params_tested_in_group=params_tested_in_group,
                            params_total_in_group=params_total_in_group,
                        )
                        continue

                    metrics = evaluate_candidate(config, proposal)
                    persist_round_outputs(db_path, config.session_id, proposal, artifact, metrics)

                    tested_count += 1
                    evaluated_this_round += 1
                    params_tested_in_group = proposal_index
                    if best_score is None or float(metrics.composite_score) > float(best_score):
                        best_score = float(metrics.composite_score)
                        best_strategy_id = artifact.strategy_id
                    last_completed_strategy_id = artifact.strategy_id

                    _emit_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"第 {current_round} 輪：策略組合「{current_strategy_group_label}」完成第 {proposal_index}/{candidate_limit} 組參數。",
                        current_candidate_label=candidate_label,
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_tested_in_group,
                        params_total_in_group=params_total_in_group,
                    )

                if evaluated_this_round > 0:
                    strategy_groups_completed += 1
                    _emit_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"第 {current_round} 輪：策略組合「{current_strategy_group_label}」已測完，準備切換下一組。",
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_total_in_group,
                        params_total_in_group=params_total_in_group,
                    )
                    break

        final_status = "STOPPED" if should_stop(config.session_id) else "COMPLETED"
        update_session_status(db_path, config.session_id, final_status)
        final_action = "研究已停止。" if final_status == "STOPPED" else "研究已完成。"
        _emit_status(
            session_id=config.session_id,
            status=final_status,
            current_round=current_round,
            tested_count=tested_count,
            best_strategy_id=best_strategy_id,
            best_score=best_score,
            current_action=final_action,
            last_completed_strategy_id=last_completed_strategy_id,
            current_strategy_group_code=current_strategy_group_code,
            current_strategy_group_label=current_strategy_group_label,
            strategy_groups_completed=strategy_groups_completed,
            params_tested_in_group=params_tested_in_group,
            params_total_in_group=params_total_in_group,
        )
    except Exception as exc:
        update_session_status(db_path, config.session_id, "FAILED")
        _emit_status(
            session_id=config.session_id,
            status="FAILED",
            current_round=current_round,
            tested_count=tested_count,
            best_strategy_id=best_strategy_id,
            best_score=best_score,
            current_action="研究失敗。",
            last_completed_strategy_id=last_completed_strategy_id,
            current_strategy_group_code=current_strategy_group_code,
            current_strategy_group_label=current_strategy_group_label,
            strategy_groups_completed=strategy_groups_completed,
            params_tested_in_group=params_tested_in_group,
            params_total_in_group=params_total_in_group,
            last_error=format_research_error(str(exc)),
        )
        raise


DISPLAY_PARAM_ORDER_V3 = [
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
    "UseAnchorExit",
    "AnchorBackPct",
]

PERSISTENT_TOP10_CSV = PROJECT_ROOT / "run_history" / "_persistent_top10_v3.csv"
PERSISTENT_TOP10_JSON = PROJECT_ROOT / "run_history" / "_persistent_top10_v3.json"
PERSISTENT_BEST_PARAMS_JSON = PROJECT_ROOT / "run_history" / "_persistent_best_params_v3.json"
PERSISTENT_BEST_PARAMS_TXT = PROJECT_ROOT / "run_history" / "_persistent_best_top1_v3.txt"
ROUND_SUMMARY_FILE = "round_summary.json"

_LEADERBOARD_META_FIELDS = {
    "saved_at",
    "session_id",
    "source_session_id",
    "source_saved_at",
    "source_run_dir",
    "strategy_id",
    "strategy_signature",
    "strategy_group_code",
    "strategy_group_label",
    "total_return",
    "mdd_pct",
    "n_trades",
    "year_avg_return",
    "year_return_std",
    "loss_years",
    "composite_score",
    "fail_reason",
    "xs_path",
    "params_txt_path",
    "params_json",
    "template_choices_json",
}


def _now_text_v3() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _format_param_value_v3(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _candidate_params_text_v3(proposal: CandidateProposal) -> str:
    ordered_names = DISPLAY_PARAM_ORDER_V3 + [name for name in proposal.params if name not in DISPLAY_PARAM_ORDER_V3]
    parts = [f"{name}={_format_param_value_v3(proposal.params[name])}" for name in ordered_names if name in proposal.params]
    return "; ".join(parts)


def _round_summary_path(session_id: str) -> Path:
    return session_dir(session_id) / ROUND_SUMMARY_FILE


def _write_round_records(session_id: str, rows: list[dict[str, Any]]) -> None:
    _round_summary_path(session_id).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _upsert_round_record(
    rows: list[dict[str, Any]],
    round_no: int,
    **updates: Any,
) -> dict[str, Any]:
    for row in rows:
        if int(row.get("round") or 0) == int(round_no):
            row.update(updates)
            row["updated_at"] = _now_text_v3()
            return row

    payload = {
        "round": int(round_no),
        "status": "PENDING",
        "strategy_group_code": None,
        "strategy_group_label": None,
        "strategy_group_summary": None,
        "strategy_group_order_text": None,
        "param_scope_text": None,
        "candidate_total": 0,
        "processed_count": 0,
        "evaluated_count": 0,
        "reused_count": 0,
        "tested_count_after_round": 0,
        "best_strategy_id": None,
        "best_total_return": None,
        "best_mdd_pct": None,
        "best_score": None,
        "detail_lines": [],
        "started_at": _now_text_v3(),
        "updated_at": _now_text_v3(),
        "completed_at": None,
    }
    payload.update(updates)
    rows.append(payload)
    rows.sort(key=lambda item: int(item.get("round") or 0))
    return payload


def _safe_float_v3(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _metric_sort_tuple_v3(row: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        _safe_float_v3(row.get("composite_score"), -1e18),
        -_safe_float_v3(row.get("mdd_pct"), 1e18),
        _safe_float_v3(row.get("total_return"), -1e18),
        _safe_float_v3(row.get("year_avg_return"), -1e18),
    )


def _row_unique_key_v3(row: dict[str, Any]) -> str:
    strategy_signature = str(row.get("strategy_signature") or "").strip()
    if strategy_signature:
        return strategy_signature
    params_payload = {
        key: value
        for key, value in row.items()
        if key not in _LEADERBOARD_META_FIELDS and value not in (None, "")
    }
    payload = {
        "strategy_group_code": row.get("strategy_group_code"),
        "params": params_payload,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _load_existing_top10_rows_v3() -> list[dict[str, Any]]:
    if PERSISTENT_TOP10_JSON.exists():
        try:
            payload = json.loads(PERSISTENT_TOP10_JSON.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        rows = payload.get("rows") or []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]

    if not PERSISTENT_TOP10_CSV.exists():
        return []

    try:
        with PERSISTENT_TOP10_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except Exception:
        return []


def _ordered_param_names_v3(rows: list[dict[str, Any]]) -> list[str]:
    discovered = {key for row in rows for key in row.keys() if key not in _LEADERBOARD_META_FIELDS}
    ordered = [name for name in DISPLAY_PARAM_ORDER_V3 if name in discovered]
    ordered.extend(sorted(name for name in discovered if name not in DISPLAY_PARAM_ORDER_V3))
    return ordered


def _write_top10_csv_v3(rows: list[dict[str, Any]]) -> None:
    fieldnames = list(_LEADERBOARD_META_FIELDS) + _ordered_param_names_v3(rows)
    with PERSISTENT_TOP10_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _best_params_from_row_v3(row: dict[str, Any], param_names: list[str]) -> dict[str, Any]:
    return {
        name: row[name]
        for name in param_names
        if name in row and row.get(name) not in (None, "")
    }


def _persist_best_params_v3(best_params: dict[str, Any]) -> None:
    payload = {
        "saved_at": _now_text_v3(),
        "params": best_params,
    }
    PERSISTENT_BEST_PARAMS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [f"{name}={_format_param_value_v3(value)}" for name, value in best_params.items()]
    PERSISTENT_BEST_PARAMS_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _leaderboard_row_v3(
    session_id: str,
    proposal: CandidateProposal,
    artifact: StrategyArtifact,
    metrics: BacktestMetrics,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "saved_at": _now_text_v3(),
        "session_id": session_id,
        "source_session_id": session_id,
        "source_saved_at": _now_text_v3(),
        "source_run_dir": artifact.out_dir,
        "strategy_id": artifact.strategy_id,
        "strategy_signature": artifact.signature,
        "strategy_group_code": proposal.template_choices.get("strategy_group_code"),
        "strategy_group_label": proposal.template_choices.get("strategy_group_label"),
        "total_return": float(metrics.total_return),
        "mdd_pct": float(metrics.mdd_pct),
        "n_trades": int(metrics.n_trades),
        "year_avg_return": float(metrics.year_avg_return),
        "year_return_std": float(metrics.year_return_std),
        "loss_years": int(metrics.loss_years),
        "composite_score": float(metrics.composite_score),
        "fail_reason": metrics.fail_reason or "",
        "xs_path": artifact.xs_path,
        "params_txt_path": artifact.params_txt_path,
        "params_json": json.dumps(proposal.params, ensure_ascii=False, sort_keys=True),
        "template_choices_json": json.dumps(proposal.template_choices, ensure_ascii=False, sort_keys=True),
    }
    for name, value in proposal.params.items():
        row[name] = value
    return row


def _update_persistent_top10_v3(
    session_id: str,
    proposal: CandidateProposal,
    artifact: StrategyArtifact,
    metrics: BacktestMetrics,
) -> None:
    PERSISTENT_TOP10_JSON.parent.mkdir(parents=True, exist_ok=True)
    rows = _load_existing_top10_rows_v3()
    rows.append(_leaderboard_row_v3(session_id, proposal, artifact, metrics))
    rows.sort(key=_metric_sort_tuple_v3, reverse=True)

    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in rows:
        unique_key = _row_unique_key_v3(row)
        if unique_key in seen_keys:
            continue
        seen_keys.add(unique_key)
        deduped.append(row)
        if len(deduped) >= 10:
            break

    param_names = _ordered_param_names_v3(deduped)
    best_params = _best_params_from_row_v3(deduped[0], param_names) if deduped else {}
    if best_params:
        _persist_best_params_v3(best_params)

    payload = {
        "saved_at": _now_text_v3(),
        "count": len(deduped),
        "best_params": best_params,
        "rows": deduped,
    }
    PERSISTENT_TOP10_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_top10_csv_v3(deduped)


def persist_round_outputs(
    db_path: str,
    session_id: str,
    proposal: CandidateProposal,
    artifact: StrategyArtifact,
    metrics: BacktestMetrics,
) -> None:
    insert_strategy(
        db_path=db_path,
        session_id=session_id,
        strategy_id=artifact.strategy_id,
        proposal=proposal,
        xs_path=artifact.xs_path,
        params_txt_path=artifact.params_txt_path,
        xs_hash=_hash_file(artifact.xs_path),
        strategy_signature=artifact.signature,
        parent_strategy_id=proposal.parent_strategy_id,
    )
    insert_run(db_path=db_path, session_id=session_id, strategy_id=artifact.strategy_id, metrics=metrics)
    _update_persistent_top10_v3(session_id, proposal, artifact, metrics)


def run_research_session(config: ResearchConfig, db_path: str) -> None:
    init_memory_db(db_path)
    create_session(db_path, config)
    clear_stop(config.session_id)

    round_records: list[dict[str, Any]] = []
    _write_round_records(config.session_id, round_records)

    current_round = 0
    tested_count = 0
    best_strategy_id: str | None = None
    best_score: float | None = None
    last_completed_strategy_id: str | None = None
    strategy_groups_completed = 0
    current_strategy_group_code: str | None = None
    current_strategy_group_label: str | None = None
    current_strategy_group_summary: str | None = None
    current_strategy_group_order_text: str | None = None
    current_param_scope_text: str | None = None
    current_candidate_params_text: str | None = None
    current_candidate_index = 0
    current_candidate_total = 0
    params_tested_in_group = 0
    params_total_in_group = 0
    max_fetch_attempts = 3
    last_cleanup_at = datetime.now()
    session_started_at = datetime.now()
    phase_started_at = session_started_at
    current_phase = "wait"
    compute_elapsed_seconds = 0.0
    wait_elapsed_seconds = 0.0

    def _maybe_run_auto_cleanup(*, force: bool = False) -> None:
        nonlocal last_cleanup_at
        now = datetime.now()
        if not force and (now - last_cleanup_at).total_seconds() < AUTO_CLEANUP_INTERVAL_SECONDS:
            return
        trigger = "final" if force else "hourly"
        try:
            summary = run_auto_cleanup(
                db_path,
                current_session_id=config.session_id,
                trigger=trigger,
            )
        except Exception as exc:
            print(f"[cleanup] trigger={trigger} failed: {exc}", flush=True)
        else:
            last_cleanup_at = now
            removed_total = (
                int(summary.get("removed_generated_dirs") or 0)
                + int(summary.get("removed_run_dirs") or 0)
                + int(summary.get("removed_session_dirs") or 0)
            )
            if removed_total or summary.get("db_compacted"):
                print(
                    "[cleanup] "
                    f"trigger={trigger} "
                    f"removed_generated={summary.get('removed_generated_dirs', 0)} "
                    f"removed_run_dirs={summary.get('removed_run_dirs', 0)} "
                    f"removed_sessions={summary.get('removed_session_dirs', 0)} "
                    f"db_compacted={summary.get('db_compacted', False)} "
                    f"db_reason={summary.get('db_reason', '-')}",
                    flush=True,
                )

    def _elapsed_snapshot(now: datetime | None = None) -> tuple[int, int, int]:
        snapshot_at = now or datetime.now()
        compute_seconds = float(compute_elapsed_seconds)
        wait_seconds = float(wait_elapsed_seconds)
        phase_delta = max(0.0, (snapshot_at - phase_started_at).total_seconds())
        if current_phase == "compute":
            compute_seconds += phase_delta
        else:
            wait_seconds += phase_delta
        session_seconds = max(0, int((snapshot_at - session_started_at).total_seconds()))
        return session_seconds, int(compute_seconds), int(wait_seconds)

    def _switch_phase(next_phase: str) -> None:
        nonlocal phase_started_at, current_phase, compute_elapsed_seconds, wait_elapsed_seconds
        now = datetime.now()
        phase_delta = max(0.0, (now - phase_started_at).total_seconds())
        if current_phase == "compute":
            compute_elapsed_seconds += phase_delta
        else:
            wait_elapsed_seconds += phase_delta
        current_phase = next_phase
        phase_started_at = now

    def _emit_timed_status(**kwargs: Any) -> ResearchStatus:
        session_elapsed, compute_elapsed, wait_elapsed = _elapsed_snapshot()
        return _emit_status(
            current_phase="計算中" if current_phase == "compute" else "等待中",
            session_elapsed_seconds=session_elapsed,
            compute_elapsed_seconds=compute_elapsed,
            wait_elapsed_seconds=wait_elapsed,
            **kwargs,
        )

    _emit_timed_status(
        session_id=config.session_id,
        status="RUNNING",
        current_action="研究工作已啟動，準備建立候選策略。",
    )
    update_session_status(db_path, config.session_id, "RUNNING")

    try:
        while not should_stop(config.session_id):
            if config.max_rounds is not None and current_round >= int(config.max_rounds):
                break
            _maybe_run_auto_cleanup()

            current_round += 1
            _emit_timed_status(
                session_id=config.session_id,
                status="RUNNING",
                current_round=current_round,
                tested_count=tested_count,
                best_strategy_id=best_strategy_id,
                best_score=best_score,
                current_action=f"第 {current_round} 輪：整理歷史結果並建立新的提示詞。",
                last_completed_strategy_id=last_completed_strategy_id,
                current_strategy_group_code=current_strategy_group_code,
                current_strategy_group_label=current_strategy_group_label,
                current_strategy_group_summary=current_strategy_group_summary,
                current_strategy_group_order_text=current_strategy_group_order_text,
                current_param_scope_text=current_param_scope_text,
                current_candidate_params_text=current_candidate_params_text,
                current_candidate_index=current_candidate_index,
                current_candidate_total=current_candidate_total,
                strategy_groups_completed=strategy_groups_completed,
                params_tested_in_group=params_tested_in_group,
                params_total_in_group=params_total_in_group,
            )

            context = build_search_context(db_path, config.session_id)
            prompt_text = build_generation_prompt(config, context)
            response: dict[str, Any] | None = None
            response_meta: dict[str, Any] = {}
            proposals: list[CandidateProposal] = []

            for attempt in range(1, max_fetch_attempts + 1):
                if should_stop(config.session_id):
                    break

                from .openai_client import request_candidate_batch

                _emit_timed_status(
                    session_id=config.session_id,
                    status="RUNNING",
                    current_round=current_round,
                    tested_count=tested_count,
                    best_strategy_id=best_strategy_id,
                    best_score=best_score,
                    current_action=f"第 {current_round} 輪：向模型請求候選策略，第 {attempt}/{max_fetch_attempts} 次。",
                    last_completed_strategy_id=last_completed_strategy_id,
                    current_strategy_group_code=current_strategy_group_code,
                    current_strategy_group_label=current_strategy_group_label,
                    current_strategy_group_summary=current_strategy_group_summary,
                    current_strategy_group_order_text=current_strategy_group_order_text,
                    current_param_scope_text=current_param_scope_text,
                    current_candidate_params_text=current_candidate_params_text,
                    current_candidate_index=current_candidate_index,
                    current_candidate_total=current_candidate_total,
                    strategy_groups_completed=strategy_groups_completed,
                    params_tested_in_group=params_tested_in_group,
                    params_total_in_group=params_total_in_group,
                )

                try:
                    response = request_candidate_batch(
                        config,
                        prompt_text,
                        current_round=current_round,
                        context=context,
                    )
                    payload = response["payload"]
                    raw_text = response["raw_text"]
                    response_meta = response.get("meta") or {}
                    param_space = load_research_param_space(config.param_preset_path)
                    proposals = validate_candidate_batch(payload, param_space=param_space)
                    record_llm_call(
                        db_path=db_path,
                        session_id=config.session_id,
                        model=config.model,
                        prompt_text=prompt_text,
                        response_text=raw_text,
                        candidate_count=len(proposals),
                    )
                    if not proposals:
                        raise RuntimeError("LLM returned zero candidates")
                    break
                except Exception as exc:
                    if attempt >= max_fetch_attempts:
                        raise
                    retry_reason = format_research_error(str(exc))
                    _emit_timed_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"第 {current_round} 輪：候選生成失敗，準備重試。原因：{retry_reason}",
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        current_strategy_group_summary=current_strategy_group_summary,
                        current_strategy_group_order_text=current_strategy_group_order_text,
                        current_param_scope_text=current_param_scope_text,
                        current_candidate_params_text=current_candidate_params_text,
                        current_candidate_index=current_candidate_index,
                        current_candidate_total=current_candidate_total,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_tested_in_group,
                        params_total_in_group=params_total_in_group,
                    )

            if should_stop(config.session_id):
                break
            if response is None or not proposals:
                raise RuntimeError("候選策略產生失敗，沒有可用提案。")

            candidate_limit = int(response_meta.get("candidate_limit") or len(proposals))
            candidate_limit = min(max(candidate_limit, 1), len(proposals))
            current_strategy_group_code = str(
                response_meta.get("strategy_group_code")
                or proposals[0].template_choices.get("strategy_group_code")
                or f"round_{current_round}"
            )
            current_strategy_group_label = str(
                response_meta.get("strategy_group_label")
                or proposals[0].template_choices.get("strategy_group_label")
                or f"第 {current_round} 輪策略家族"
            )
            group_index = int(response_meta.get("strategy_group_index") or current_round)
            group_count = int(response_meta.get("strategy_group_count") or 0)
            focus_group_index = int(response_meta.get("focus_param_group_index") or 0)
            focus_group_count = int(response_meta.get("focus_param_group_count") or 0)
            current_strategy_group_order_text = (
                f"策略家族 {group_index}/{group_count}｜變數組 {focus_group_index}/{focus_group_count}"
                if group_count > 0 and focus_group_count > 0
                else (f"第 {group_index} / {group_count} 個策略家族" if group_count > 0 else f"第 {current_round} 輪")
            )
            current_strategy_group_summary = str(response_meta.get("strategy_group_summary") or "").strip() or None
            current_param_scope_text = str(response_meta.get("param_scope_text") or "").strip() or None
            current_candidate_total = candidate_limit
            current_candidate_index = 0
            current_candidate_params_text = None
            params_tested_in_group = 0
            params_total_in_group = int(response_meta.get("params_total_in_group") or candidate_limit)
            detail_lines = [
                str(line).strip()
                for line in (response_meta.get("round_detail_lines") or [])
                if str(line).strip()
            ]
            if not detail_lines:
                detail_lines = [
                    f"策略家族：{current_strategy_group_label}",
                    f"掃描範圍：{current_param_scope_text or '-'}",
                ]

            _upsert_round_record(
                round_records,
                current_round,
                status="RUNNING",
                strategy_group_code=current_strategy_group_code,
                strategy_group_label=current_strategy_group_label,
                strategy_group_summary=current_strategy_group_summary,
                strategy_group_order_text=current_strategy_group_order_text,
                param_scope_text=current_param_scope_text,
                candidate_total=candidate_limit,
                processed_count=0,
                evaluated_count=0,
                reused_count=0,
                tested_count_after_round=tested_count,
                best_strategy_id=None,
                best_total_return=None,
                best_mdd_pct=None,
                best_score=None,
                detail_lines=detail_lines,
                started_at=_now_text_v3(),
                completed_at=None,
            )
            _write_round_records(config.session_id, round_records)

            evaluated_this_round = 0
            processed_this_round = 0
            reused_this_round = 0
            round_result_rows: list[dict[str, Any]] = []
            round_best_tuple: tuple[float, float, float] | None = None
            round_best_strategy_id: str | None = None
            round_best_total_return: float | None = None
            round_best_mdd_pct: float | None = None
            round_best_score: float | None = None

            for proposal_index, proposal in enumerate(proposals[:candidate_limit], start=1):
                if should_stop(config.session_id):
                    break

                candidate_label = proposal.ai_summary or f"candidate {proposal_index}"
                current_candidate_index = proposal_index
                current_candidate_params_text = _candidate_params_text_v3(proposal)
                processed_this_round = proposal_index

                _emit_timed_status(
                    session_id=config.session_id,
                    status="RUNNING",
                    current_round=current_round,
                    tested_count=tested_count,
                    best_strategy_id=best_strategy_id,
                    best_score=best_score,
                    current_action=f"第 {current_round} 輪：測試第 {proposal_index}/{candidate_limit} 組候選參數。",
                    current_candidate_label=candidate_label,
                    last_completed_strategy_id=last_completed_strategy_id,
                    current_strategy_group_code=current_strategy_group_code,
                    current_strategy_group_label=current_strategy_group_label,
                    current_strategy_group_summary=current_strategy_group_summary,
                    current_strategy_group_order_text=current_strategy_group_order_text,
                    current_param_scope_text=current_param_scope_text,
                    current_candidate_params_text=current_candidate_params_text,
                    current_candidate_index=current_candidate_index,
                    current_candidate_total=current_candidate_total,
                    strategy_groups_completed=strategy_groups_completed,
                    params_tested_in_group=processed_this_round,
                    params_total_in_group=params_total_in_group,
                )

                signature = build_strategy_signature(proposal)
                if has_strategy_signature(db_path, signature):
                    cached_metrics = _cached_metrics_from_row(get_run_by_strategy_signature(db_path, signature))
                    if cached_metrics is not None:
                        reused_this_round += 1
                        artifact = write_strategy_artifacts(config.base_xs_path, proposal, metrics=cached_metrics)
                        last_completed_strategy_id = artifact.strategy_id

                        if best_score is None or float(cached_metrics.composite_score) > float(best_score):
                            best_score = float(cached_metrics.composite_score)
                            best_strategy_id = artifact.strategy_id

                        candidate_tuple = (
                            float(cached_metrics.composite_score),
                            -float(cached_metrics.mdd_pct),
                            float(cached_metrics.total_return),
                        )
                        if round_best_tuple is None or candidate_tuple > round_best_tuple:
                            round_best_tuple = candidate_tuple
                            round_best_strategy_id = artifact.strategy_id
                            round_best_total_return = float(cached_metrics.total_return)
                            round_best_mdd_pct = float(cached_metrics.mdd_pct)
                            round_best_score = float(cached_metrics.composite_score)

                        round_result_rows.append(
                            {
                                "strategy_id": artifact.strategy_id,
                                "strategy_signature": artifact.signature,
                                "params": dict(proposal.params),
                                "template_choices": dict(proposal.template_choices),
                                "total_return": float(cached_metrics.total_return),
                                "mdd_pct": float(cached_metrics.mdd_pct),
                                "n_trades": int(cached_metrics.n_trades),
                                "composite_score": float(cached_metrics.composite_score),
                                "year_avg_return": float(cached_metrics.year_avg_return),
                                "year_return_std": float(cached_metrics.year_return_std),
                                "loss_years": int(cached_metrics.loss_years),
                                "fail_reason": cached_metrics.fail_reason or "",
                                "reused": True,
                            }
                        )
                    _upsert_round_record(
                        round_records,
                        current_round,
                        processed_count=processed_this_round,
                        evaluated_count=evaluated_this_round,
                        reused_count=reused_this_round,
                        tested_count_after_round=tested_count,
                        best_strategy_id=round_best_strategy_id,
                        best_total_return=round_best_total_return,
                        best_mdd_pct=round_best_mdd_pct,
                        best_score=round_best_score,
                    )
                    _write_round_records(config.session_id, round_records)
                    _emit_timed_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"第 {proposal_index}/{candidate_limit} 組參數已存在，沿用既有結果。",
                        current_candidate_label=candidate_label,
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        current_strategy_group_summary=current_strategy_group_summary,
                        current_strategy_group_order_text=current_strategy_group_order_text,
                        current_param_scope_text=current_param_scope_text,
                        current_candidate_params_text=current_candidate_params_text,
                        current_candidate_index=current_candidate_index,
                        current_candidate_total=current_candidate_total,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=processed_this_round,
                        params_total_in_group=params_total_in_group,
                    )
                    continue
                _switch_phase("compute")
                try:
                    metrics = evaluate_candidate(config, proposal)
                    artifact = write_strategy_artifacts(config.base_xs_path, proposal, metrics=metrics)
                    persist_round_outputs(db_path, config.session_id, proposal, artifact, metrics)
                finally:
                    _switch_phase("wait")

                tested_count += 1
                evaluated_this_round += 1
                last_completed_strategy_id = artifact.strategy_id

                if best_score is None or float(metrics.composite_score) > float(best_score):
                    best_score = float(metrics.composite_score)
                    best_strategy_id = artifact.strategy_id

                candidate_tuple = (
                    float(metrics.composite_score),
                    -float(metrics.mdd_pct),
                    float(metrics.total_return),
                )
                if round_best_tuple is None or candidate_tuple > round_best_tuple:
                    round_best_tuple = candidate_tuple
                    round_best_strategy_id = artifact.strategy_id
                    round_best_total_return = float(metrics.total_return)
                    round_best_mdd_pct = float(metrics.mdd_pct)
                    round_best_score = float(metrics.composite_score)

                round_result_rows.append(
                    {
                        "strategy_id": artifact.strategy_id,
                        "strategy_signature": artifact.signature,
                        "params": dict(proposal.params),
                        "template_choices": dict(proposal.template_choices),
                        "total_return": float(metrics.total_return),
                        "mdd_pct": float(metrics.mdd_pct),
                        "n_trades": int(metrics.n_trades),
                        "composite_score": float(metrics.composite_score),
                        "year_avg_return": float(metrics.year_avg_return),
                        "year_return_std": float(metrics.year_return_std),
                        "loss_years": int(metrics.loss_years),
                        "fail_reason": metrics.fail_reason or "",
                        "reused": False,
                    }
                )

                _upsert_round_record(
                    round_records,
                    current_round,
                    processed_count=processed_this_round,
                    evaluated_count=evaluated_this_round,
                    reused_count=reused_this_round,
                    tested_count_after_round=tested_count,
                    best_strategy_id=round_best_strategy_id,
                    best_total_return=round_best_total_return,
                    best_mdd_pct=round_best_mdd_pct,
                    best_score=round_best_score,
                )
                _write_round_records(config.session_id, round_records)

            round_status = "STOPPED" if should_stop(config.session_id) else "COMPLETED"
            latest_context = build_search_context(db_path, config.session_id)
            planner_transition: dict[str, Any] = {}
            if is_modular_0313plus_enabled(config):
                planner_transition = advance_modular_sweep_state(
                    config,
                    context=latest_context,
                    round_results=round_result_rows,
                )
            if round_status == "COMPLETED":
                strategy_groups_completed += 1

            summary_lines = list(detail_lines)
            summary_lines.append(
                f"本輪處理：{processed_this_round}/{candidate_limit}；新回測 {evaluated_this_round} 組；沿用既有結果 {reused_this_round} 組。"
            )
            if round_best_strategy_id:
                summary_lines.append(
                    "本輪最佳："
                    f"策略 {round_best_strategy_id}；總報酬 {round_best_total_return:.3f}%"
                    f"；MDD {round_best_mdd_pct:.3f}%；分數 {round_best_score:.3f}。"
                )
            if planner_transition.get("transition_text"):
                summary_lines.append(f"下一步：{planner_transition['transition_text']}")

            _upsert_round_record(
                round_records,
                current_round,
                status=round_status,
                processed_count=processed_this_round,
                evaluated_count=evaluated_this_round,
                reused_count=reused_this_round,
                tested_count_after_round=tested_count,
                best_strategy_id=round_best_strategy_id,
                best_total_return=round_best_total_return,
                best_mdd_pct=round_best_mdd_pct,
                best_score=round_best_score,
                detail_lines=summary_lines,
                completed_at=_now_text_v3(),
            )
            _write_round_records(config.session_id, round_records)

            params_tested_in_group = processed_this_round
            current_candidate_index = 0
            current_candidate_params_text = None

            if should_stop(config.session_id):
                break

            _emit_timed_status(
                session_id=config.session_id,
                status="RUNNING",
                current_round=current_round,
                tested_count=tested_count,
                best_strategy_id=best_strategy_id,
                best_score=best_score,
                current_action=str(planner_transition.get("transition_text") or f"第 {current_round} 輪完成，準備下一輪。"),
                last_completed_strategy_id=last_completed_strategy_id,
                current_strategy_group_code=current_strategy_group_code,
                current_strategy_group_label=current_strategy_group_label,
                current_strategy_group_summary=current_strategy_group_summary,
                current_strategy_group_order_text=current_strategy_group_order_text,
                current_param_scope_text=current_param_scope_text,
                current_candidate_params_text=current_candidate_params_text,
                current_candidate_index=current_candidate_index,
                current_candidate_total=current_candidate_total,
                strategy_groups_completed=strategy_groups_completed,
                params_tested_in_group=params_tested_in_group,
                params_total_in_group=params_total_in_group,
            )

        final_status = "STOPPED" if should_stop(config.session_id) else "COMPLETED"
        update_session_status(db_path, config.session_id, final_status)
        _emit_timed_status(
            session_id=config.session_id,
            status=final_status,
            current_round=current_round,
            tested_count=tested_count,
            best_strategy_id=best_strategy_id,
            best_score=best_score,
            current_action="研究已停止。" if final_status == "STOPPED" else "研究已完成。",
            last_completed_strategy_id=last_completed_strategy_id,
            current_strategy_group_code=current_strategy_group_code,
            current_strategy_group_label=current_strategy_group_label,
            current_strategy_group_summary=current_strategy_group_summary,
            current_strategy_group_order_text=current_strategy_group_order_text,
            current_param_scope_text=current_param_scope_text,
            current_candidate_params_text=current_candidate_params_text,
            current_candidate_index=current_candidate_index,
            current_candidate_total=current_candidate_total,
            strategy_groups_completed=strategy_groups_completed,
            params_tested_in_group=params_tested_in_group,
            params_total_in_group=params_total_in_group,
        )
    except Exception as exc:
        update_session_status(db_path, config.session_id, "FAILED")
        if current_round > 0:
            _upsert_round_record(
                round_records,
                current_round,
                status="FAILED",
                completed_at=_now_text_v3(),
            )
            _write_round_records(config.session_id, round_records)
        _emit_timed_status(
            session_id=config.session_id,
            status="FAILED",
            current_round=current_round,
            tested_count=tested_count,
            best_strategy_id=best_strategy_id,
            best_score=best_score,
            current_action="研究失敗。",
            last_completed_strategy_id=last_completed_strategy_id,
            current_strategy_group_code=current_strategy_group_code,
            current_strategy_group_label=current_strategy_group_label,
            current_strategy_group_summary=current_strategy_group_summary,
            current_strategy_group_order_text=current_strategy_group_order_text,
            current_param_scope_text=current_param_scope_text,
            current_candidate_params_text=current_candidate_params_text,
            current_candidate_index=current_candidate_index,
            current_candidate_total=current_candidate_total,
            strategy_groups_completed=strategy_groups_completed,
            params_tested_in_group=params_tested_in_group,
            params_total_in_group=params_total_in_group,
            last_error=format_research_error(str(exc)),
        )
        raise


run_research_session_v3 = run_research_session


def _format_param_value_v2(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _candidate_params_text_v2(proposal: CandidateProposal) -> str:
    parts: list[str] = []
    ordered_names = list(DISPLAY_PARAM_ORDER) + [name for name in proposal.params.keys() if name not in DISPLAY_PARAM_ORDER]
    for name in ordered_names:
        if name in proposal.params:
            parts.append(f"{name}={_format_param_value_v2(proposal.params[name])}")
    return "; ".join(parts)


def _cached_metrics_from_row(row: dict[str, Any] | None) -> BacktestMetrics | None:
    if not isinstance(row, dict):
        return None
    try:
        return BacktestMetrics(
            total_return=float(row.get("total_return") or 0.0),
            mdd_pct=float(row.get("mdd_pct") or 0.0),
            n_trades=int(row.get("n_trades") or 0),
            year_avg_return=float(row.get("year_avg_return") or 0.0),
            year_return_std=float(row.get("year_return_std") or 0.0),
            loss_years=int(row.get("loss_years") or 0),
            composite_score=float(row.get("composite_score") or 0.0),
            fail_reason=str(row.get("fail_reason") or "").strip() or None,
            passed_hard_filters=not bool(str(row.get("fail_reason") or "").strip()),
            trade_lines=list(row.get("trade_lines") or []),
        )
    except Exception:
        return None


def _emit_status(
    *,
    session_id: str,
    status: str,
    current_round: int = 0,
    tested_count: int = 0,
    best_strategy_id: str | None = None,
    best_score: float | None = None,
    current_action: str | None = None,
    current_candidate_label: str | None = None,
    last_completed_strategy_id: str | None = None,
    current_strategy_group_code: str | None = None,
    current_strategy_group_label: str | None = None,
    current_strategy_group_summary: str | None = None,
    current_strategy_group_order_text: str | None = None,
    current_param_scope_text: str | None = None,
    current_candidate_params_text: str | None = None,
    current_candidate_index: int = 0,
    current_candidate_total: int = 0,
    strategy_groups_completed: int = 0,
    params_tested_in_group: int = 0,
    params_total_in_group: int = 0,
    current_phase: str | None = None,
    session_elapsed_seconds: int = 0,
    compute_elapsed_seconds: int = 0,
    wait_elapsed_seconds: int = 0,
    last_error: str | None = None,
) -> ResearchStatus:
    payload = ResearchStatus(
        session_id=session_id,
        status=status,
        current_round=current_round,
        tested_count=tested_count,
        best_strategy_id=best_strategy_id,
        best_score=best_score,
        current_action=current_action,
        current_candidate_label=current_candidate_label,
        last_completed_strategy_id=last_completed_strategy_id,
        current_strategy_group_code=current_strategy_group_code,
        current_strategy_group_label=current_strategy_group_label,
        current_strategy_group_summary=current_strategy_group_summary,
        current_strategy_group_order_text=current_strategy_group_order_text,
        current_param_scope_text=current_param_scope_text,
        current_candidate_params_text=current_candidate_params_text,
        current_candidate_index=current_candidate_index,
        current_candidate_total=current_candidate_total,
        strategy_groups_completed=strategy_groups_completed,
        params_tested_in_group=params_tested_in_group,
        params_total_in_group=params_total_in_group,
        current_phase=current_phase,
        session_elapsed_seconds=int(session_elapsed_seconds or 0),
        compute_elapsed_seconds=int(compute_elapsed_seconds or 0),
        wait_elapsed_seconds=int(wait_elapsed_seconds or 0),
        last_error=last_error,
    )
    _write_status(payload)

    log_parts = [
        payload.updated_at,
        f"status={payload.status}",
        f"round={payload.current_round}",
        f"tested={payload.tested_count}",
    ]
    if payload.current_strategy_group_label:
        log_parts.append(f"group={payload.current_strategy_group_label}")
    if payload.current_action:
        log_parts.append(f"action={payload.current_action}")
    if payload.current_candidate_index and payload.current_candidate_total:
        log_parts.append(f"candidate={payload.current_candidate_index}/{payload.current_candidate_total}")
    if payload.best_strategy_id:
        log_parts.append(f"best={payload.best_strategy_id}")
    print(" | ".join(log_parts), flush=True)
    return payload


def run_research_session(config: ResearchConfig, db_path: str) -> None:
    init_memory_db(db_path)
    create_session(db_path, config)
    clear_stop(config.session_id)

    current_round = 0
    tested_count = 0
    best_strategy_id: str | None = None
    best_score: float | None = None
    last_completed_strategy_id: str | None = None
    strategy_groups_completed = 0
    current_strategy_group_code: str | None = None
    current_strategy_group_label: str | None = None
    current_strategy_group_summary: str | None = None
    current_strategy_group_order_text: str | None = None
    current_param_scope_text: str | None = None
    current_candidate_params_text: str | None = None
    current_candidate_index = 0
    current_candidate_total = 0
    params_tested_in_group = 0
    params_total_in_group = 0
    max_fetch_attempts = 3
    last_cleanup_at = datetime.now()

    def _maybe_run_auto_cleanup(*, force: bool = False) -> None:
        nonlocal last_cleanup_at
        now = datetime.now()
        if not force and (now - last_cleanup_at).total_seconds() < AUTO_CLEANUP_INTERVAL_SECONDS:
            return
        trigger = "final" if force else "hourly"
        try:
            summary = run_auto_cleanup(
                db_path,
                current_session_id=config.session_id,
                trigger=trigger,
            )
        except Exception as exc:
            print(f"[cleanup] trigger={trigger} failed: {exc}", flush=True)
        else:
            last_cleanup_at = now
            removed_total = (
                int(summary.get("removed_generated_dirs") or 0)
                + int(summary.get("removed_run_dirs") or 0)
                + int(summary.get("removed_session_dirs") or 0)
            )
            if removed_total or summary.get("db_compacted"):
                print(
                    "[cleanup] "
                    f"trigger={trigger} "
                    f"removed_generated={summary.get('removed_generated_dirs', 0)} "
                    f"removed_run_dirs={summary.get('removed_run_dirs', 0)} "
                    f"removed_sessions={summary.get('removed_session_dirs', 0)} "
                    f"db_compacted={summary.get('db_compacted', False)} "
                    f"db_reason={summary.get('db_reason', '-')}",
                    flush=True,
                )

    _emit_status(
        session_id=config.session_id,
        status="RUNNING",
        current_action="研究工作已啟動，準備建立第一輪策略家族候選。",
    )
    update_session_status(db_path, config.session_id, "RUNNING")

    try:
        while not should_stop(config.session_id):
            if config.max_rounds is not None and current_round >= int(config.max_rounds):
                break
            _maybe_run_auto_cleanup()

            current_round += 1
            _emit_status(
                session_id=config.session_id,
                status="RUNNING",
                current_round=current_round,
                tested_count=tested_count,
                best_strategy_id=best_strategy_id,
                best_score=best_score,
                current_action=f"第 {current_round} 輪開始：整理歷史結果並建立本輪策略家族候選。",
                last_completed_strategy_id=last_completed_strategy_id,
                current_strategy_group_code=current_strategy_group_code,
                current_strategy_group_label=current_strategy_group_label,
                current_strategy_group_summary=current_strategy_group_summary,
                current_strategy_group_order_text=current_strategy_group_order_text,
                current_param_scope_text=current_param_scope_text,
                current_candidate_params_text=current_candidate_params_text,
                current_candidate_index=current_candidate_index,
                current_candidate_total=current_candidate_total,
                strategy_groups_completed=strategy_groups_completed,
                params_tested_in_group=params_tested_in_group,
                params_total_in_group=params_total_in_group,
            )

            context = build_search_context(db_path, config.session_id)
            prompt_text = build_generation_prompt(config, context)
            evaluated_this_round = 0

            for _attempt in range(max_fetch_attempts):
                if should_stop(config.session_id):
                    break

                from .openai_client import request_candidate_batch

                _emit_status(
                    session_id=config.session_id,
                    status="RUNNING",
                    current_round=current_round,
                    tested_count=tested_count,
                    best_strategy_id=best_strategy_id,
                    best_score=best_score,
                    current_action=f"第 {current_round} 輪：產生策略家族與參數候選。",
                    last_completed_strategy_id=last_completed_strategy_id,
                    current_strategy_group_code=current_strategy_group_code,
                    current_strategy_group_label=current_strategy_group_label,
                    current_strategy_group_summary=current_strategy_group_summary,
                    current_strategy_group_order_text=current_strategy_group_order_text,
                    current_param_scope_text=current_param_scope_text,
                    current_candidate_params_text=current_candidate_params_text,
                    current_candidate_index=current_candidate_index,
                    current_candidate_total=current_candidate_total,
                    strategy_groups_completed=strategy_groups_completed,
                    params_tested_in_group=params_tested_in_group,
                    params_total_in_group=params_total_in_group,
                )

                response = request_candidate_batch(
                    config,
                    prompt_text,
                    current_round=current_round,
                    context=context,
                )
                payload = response["payload"]
                raw_text = response["raw_text"]
                response_meta = response.get("meta") or {}
                param_space = load_research_param_space(config.param_preset_path)
                proposals = validate_candidate_batch(payload, param_space=param_space)
                record_llm_call(
                    db_path=db_path,
                    session_id=config.session_id,
                    model=config.model,
                    prompt_text=prompt_text,
                    response_text=raw_text,
                    candidate_count=len(proposals),
                )

                if not proposals:
                    raise RuntimeError("LLM returned zero candidates")

                candidate_limit = min(len(proposals), int(config.batch_size))
                current_strategy_group_code = str(
                    response_meta.get("strategy_group_code")
                    or proposals[0].template_choices.get("strategy_group_code")
                    or f"round_{current_round}"
                )
                current_strategy_group_label = str(
                    response_meta.get("strategy_group_label")
                    or proposals[0].template_choices.get("strategy_group_label")
                    or f"第 {current_round} 輪策略家族"
                )
                group_index = int(response_meta.get("strategy_group_index") or 0)
                group_count = int(response_meta.get("strategy_group_count") or 0)
                current_strategy_group_order_text = (
                    f"第 {group_index}/{group_count} 個策略家族" if group_index and group_count else None
                )
                current_strategy_group_summary = str(response_meta.get("strategy_group_summary") or "").strip() or None
                current_param_scope_text = str(response_meta.get("param_scope_text") or "").strip() or None
                anchor_text = str(response_meta.get("anchor_text") or "").strip()
                if anchor_text:
                    if current_param_scope_text:
                        current_param_scope_text = f"核心值：{anchor_text} | 掃描：{current_param_scope_text}"
                    else:
                        current_param_scope_text = f"核心值：{anchor_text}"

                params_tested_in_group = 0
                params_total_in_group = int(response_meta.get("params_total_in_group") or candidate_limit)
                current_candidate_index = 0
                current_candidate_total = candidate_limit
                current_candidate_params_text = None

                _emit_status(
                    session_id=config.session_id,
                    status="RUNNING",
                    current_round=current_round,
                    tested_count=tested_count,
                    best_strategy_id=best_strategy_id,
                    best_score=best_score,
                    current_action=f"第 {current_round} 輪先測「{current_strategy_group_label}」，本輪共 {candidate_limit} 組參數。",
                    last_completed_strategy_id=last_completed_strategy_id,
                    current_strategy_group_code=current_strategy_group_code,
                    current_strategy_group_label=current_strategy_group_label,
                    current_strategy_group_summary=current_strategy_group_summary,
                    current_strategy_group_order_text=current_strategy_group_order_text,
                    current_param_scope_text=current_param_scope_text,
                    current_candidate_params_text=current_candidate_params_text,
                    current_candidate_index=current_candidate_index,
                    current_candidate_total=current_candidate_total,
                    strategy_groups_completed=strategy_groups_completed,
                    params_tested_in_group=params_tested_in_group,
                    params_total_in_group=params_total_in_group,
                )

                for proposal_index, proposal in enumerate(proposals[:candidate_limit], start=1):
                    _maybe_run_auto_cleanup()
                    if should_stop(config.session_id):
                        break

                    candidate_label = proposal.ai_summary or f"candidate {proposal_index}"
                    current_candidate_index = proposal_index
                    current_candidate_total = candidate_limit
                    current_candidate_params_text = _candidate_params_text_v2(proposal)

                    _emit_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"正在回測第 {proposal_index}/{candidate_limit} 組參數。",
                        current_candidate_label=candidate_label,
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        current_strategy_group_summary=current_strategy_group_summary,
                        current_strategy_group_order_text=current_strategy_group_order_text,
                        current_param_scope_text=current_param_scope_text,
                        current_candidate_params_text=current_candidate_params_text,
                        current_candidate_index=current_candidate_index,
                        current_candidate_total=current_candidate_total,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_tested_in_group,
                        params_total_in_group=params_total_in_group,
                    )

                    artifact = write_strategy_artifacts(config.base_xs_path, proposal)
                    if has_strategy_signature(db_path, artifact.signature):
                        params_tested_in_group = proposal_index
                        _emit_status(
                            session_id=config.session_id,
                            status="RUNNING",
                            current_round=current_round,
                            tested_count=tested_count,
                            best_strategy_id=best_strategy_id,
                            best_score=best_score,
                            current_action=f"第 {proposal_index}/{candidate_limit} 組參數已存在，跳過重複策略。",
                            current_candidate_label=candidate_label,
                            last_completed_strategy_id=last_completed_strategy_id,
                            current_strategy_group_code=current_strategy_group_code,
                            current_strategy_group_label=current_strategy_group_label,
                            current_strategy_group_summary=current_strategy_group_summary,
                            current_strategy_group_order_text=current_strategy_group_order_text,
                            current_param_scope_text=current_param_scope_text,
                            current_candidate_params_text=current_candidate_params_text,
                            current_candidate_index=current_candidate_index,
                            current_candidate_total=current_candidate_total,
                            strategy_groups_completed=strategy_groups_completed,
                            params_tested_in_group=params_tested_in_group,
                            params_total_in_group=params_total_in_group,
                        )
                        continue

                    metrics = evaluate_candidate(config, proposal)
                    persist_round_outputs(db_path, config.session_id, proposal, artifact, metrics)

                    tested_count += 1
                    evaluated_this_round += 1
                    params_tested_in_group = proposal_index
                    if best_score is None or float(metrics.composite_score) > float(best_score):
                        best_score = float(metrics.composite_score)
                        best_strategy_id = artifact.strategy_id
                    last_completed_strategy_id = artifact.strategy_id

                    _emit_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=(
                            f"第 {proposal_index}/{candidate_limit} 組參數完成。"
                            f" 報酬={metrics.total_return:.3f}% / MDD={metrics.mdd_pct:.3f}% / 策略ID={artifact.strategy_id}"
                        ),
                        current_candidate_label=candidate_label,
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        current_strategy_group_summary=current_strategy_group_summary,
                        current_strategy_group_order_text=current_strategy_group_order_text,
                        current_param_scope_text=current_param_scope_text,
                        current_candidate_params_text=current_candidate_params_text,
                        current_candidate_index=current_candidate_index,
                        current_candidate_total=current_candidate_total,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_tested_in_group,
                        params_total_in_group=params_total_in_group,
                    )

                if evaluated_this_round > 0:
                    strategy_groups_completed += 1
                    _emit_status(
                        session_id=config.session_id,
                        status="RUNNING",
                        current_round=current_round,
                        tested_count=tested_count,
                        best_strategy_id=best_strategy_id,
                        best_score=best_score,
                        current_action=f"「{current_strategy_group_label}」本輪參數掃描完成，準備切換下一個策略家族。",
                        last_completed_strategy_id=last_completed_strategy_id,
                        current_strategy_group_code=current_strategy_group_code,
                        current_strategy_group_label=current_strategy_group_label,
                        current_strategy_group_summary=current_strategy_group_summary,
                        current_strategy_group_order_text=current_strategy_group_order_text,
                        current_param_scope_text=current_param_scope_text,
                        current_candidate_params_text=current_candidate_params_text,
                        current_candidate_index=current_candidate_index,
                        current_candidate_total=current_candidate_total,
                        strategy_groups_completed=strategy_groups_completed,
                        params_tested_in_group=params_total_in_group,
                        params_total_in_group=params_total_in_group,
                    )
                    break

        final_status = "STOPPED" if should_stop(config.session_id) else "COMPLETED"
        update_session_status(db_path, config.session_id, final_status)
        final_action = "研究已停止。" if final_status == "STOPPED" else "研究已完成。"
        _emit_status(
            session_id=config.session_id,
            status=final_status,
            current_round=current_round,
            tested_count=tested_count,
            best_strategy_id=best_strategy_id,
            best_score=best_score,
            current_action=final_action,
            last_completed_strategy_id=last_completed_strategy_id,
            current_strategy_group_code=current_strategy_group_code,
            current_strategy_group_label=current_strategy_group_label,
            current_strategy_group_summary=current_strategy_group_summary,
            current_strategy_group_order_text=current_strategy_group_order_text,
            current_param_scope_text=current_param_scope_text,
            current_candidate_params_text=current_candidate_params_text,
            current_candidate_index=current_candidate_index,
            current_candidate_total=current_candidate_total,
            strategy_groups_completed=strategy_groups_completed,
            params_tested_in_group=params_tested_in_group,
            params_total_in_group=params_total_in_group,
        )
    except Exception as exc:
        update_session_status(db_path, config.session_id, "FAILED")
        _emit_status(
            session_id=config.session_id,
            status="FAILED",
            current_round=current_round,
            tested_count=tested_count,
            best_strategy_id=best_strategy_id,
            best_score=best_score,
            current_action="研究過程發生錯誤。",
            last_completed_strategy_id=last_completed_strategy_id,
            current_strategy_group_code=current_strategy_group_code,
            current_strategy_group_label=current_strategy_group_label,
            current_strategy_group_summary=current_strategy_group_summary,
            current_strategy_group_order_text=current_strategy_group_order_text,
            current_param_scope_text=current_param_scope_text,
            current_candidate_params_text=current_candidate_params_text,
            current_candidate_index=current_candidate_index,
            current_candidate_total=current_candidate_total,
            strategy_groups_completed=strategy_groups_completed,
            params_tested_in_group=params_tested_in_group,
            params_total_in_group=params_total_in_group,
            last_error=format_research_error(str(exc)),
        )
        raise
    finally:
        _maybe_run_auto_cleanup(force=True)


def run_research_session(config: ResearchConfig, db_path: str) -> None:
    return run_research_session_v3(config, db_path)
