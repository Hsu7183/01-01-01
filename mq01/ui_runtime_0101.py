from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from .config import default_hard_filters, default_paths, default_runtime_settings
from .job_store import (
    cleanup_stale_jobs,
    create_job_request,
    force_stop_job,
    is_terminal_status,
    launch_job_process,
    list_job_runtime_records,
    read_job_state,
    request_stop,
    touch_job_heartbeat,
)
from .parameters import load_strategy_metadata
from .runtime_views import artifact_download_payload
from .services import (
    RESEARCH_PROFILE_TAG_0101,
    build_current_best_snapshot,
    collect_system_snapshot,
    estimate_run_count,
    grid_run_block_reason,
    load_historical_best_snapshot,
    load_latest_artifact_snapshot,
    resolve_0101_research_periods,
    resolve_effective_workers,
    run_0101_holdout_validation,
)
from .ui import (
    _apply_mode_enabled_defaults,
    _best_export_signature,
    _cached_best_export_payload,
    _current_ui_specs,
    _format_duration,
    _number_input,
    _remember_param_label_map,
    _saved_progress_state,
)


MODE_OPTIONS = {
    "smart": "智慧搜尋",
    "cycle": "循環搜尋",
}

TYPE_VALUE_LABELS = {
    "int": "整數",
    "float": "浮點",
}

STATUS_LABELS = {
    "queued": "排隊中",
    "running": "執行中",
    "stopping": "停止中",
    "stopped": "已停止",
    "completed": "已完成",
    "error": "錯誤",
    "idle": "待命",
}

RESULT_COLUMN_LABELS = {
    "mode": "模式",
    "cycle_no": "循環",
    "round_no": "輪次",
    "stage_name": "參數階段",
    "param_index": "參數索引",
    "param_total": "參數總數",
    "candidate_value": "候選值",
    "keep_count": "保留數",
    "status": "狀態",
    "reason": "原因",
    "n_trades": "交易筆數",
    "total_return": "總報酬率",
    "mdd_amount": "MDD 金額",
    "mdd_pct": "MDD",
    "year_avg_return": "年均報酬率",
    "year_return_std": "年度報酬標準差",
    "loss_years": "虧損年度數",
    "composite_score": "綜合分數",
    "robust_score": "穩健分數",
    "plateau_score": "平台分數",
    "worst_window_return": "最差年窗報酬",
    "slip_stress_score": "滑價壓測分數",
    "elapsed": "耗時",
    "eta": "預估剩餘",
    "saved_at": "儲存時間",
    "count": "次數",
}

HEARTBEAT_STALE_SECONDS = 45
PROFILE_SESSION_KEY = "mq0101_profile_tag"
HOLDOUT_RESULT_KEY = "mq0101_holdout_result"
HOLDOUT_SIGNATURE_KEY = "mq0101_holdout_signature"


def _reset_profile_state() -> None:
    for key in (
        "mq01_last_top_df",
        "mq01_last_recent_df",
        "mq01_last_fail_rows",
        "mq01_last_progress",
        "mq01_last_artifacts",
        "mq01_live_export_cache",
        HOLDOUT_RESULT_KEY,
        HOLDOUT_SIGNATURE_KEY,
    ):
        st.session_state.pop(key, None)


def _format_number(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return "--"
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if abs(numeric - round(numeric)) < 1e-9:
        return f"{int(round(numeric)):,}"
    return f"{numeric:,.{digits}f}"


def _format_percent(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return "--"
    try:
        return f"{float(value):.{digits}f}%"
    except Exception:
        return str(value)


def _status_label(status: str) -> str:
    text = str(status or "").strip()
    return STATUS_LABELS.get(text, text or "待命")


def _result_frame(payload: Any) -> pd.DataFrame:
    if isinstance(payload, pd.DataFrame):
        return payload.copy()
    return pd.DataFrame()


def _rows_frame(rows: Any) -> pd.DataFrame:
    if isinstance(rows, list):
        return pd.DataFrame(rows)
    return pd.DataFrame()


def _clean_param_label(label: str) -> str:
    text = re.sub(r"^\d+\.", "", str(label or "")).strip()
    return text or str(label or "")


def _format_param_summary(snapshot: dict[str, Any], limit: int = 4) -> str:
    params = snapshot.get("params") if isinstance(snapshot, dict) else {}
    if not isinstance(params, dict) or not params:
        return "--"
    items = list(params.items())[: max(1, int(limit))]
    text = " / ".join(f"{name}={_format_number(value, 4)}" for name, value in items)
    if len(params) > len(items):
        text += " / ..."
    return text


def _count_spec_values(spec: dict[str, Any]) -> int:
    try:
        if not bool(spec.get("enabled")):
            return 0
        start = float(spec.get("start"))
        stop = float(spec.get("stop"))
        step = float(spec.get("step"))
        if step <= 0 or stop < start:
            return 0
        if str(spec.get("type") or "float").lower() == "int":
            return int((int(round(stop)) - int(round(start))) // max(int(round(step)), 1)) + 1
        return max(int(round((stop - start) / step)) + 1, 1)
    except Exception:
        return 0


def _full_grid_count(ui_specs: list[dict[str, Any]]) -> int:
    total = 1
    enabled = 0
    for spec in ui_specs:
        count = _count_spec_values(spec)
        if count <= 0:
            continue
        enabled += 1
        total *= count
    return total if enabled > 0 else 0


def _build_pre_run_summary(
    *,
    mode_label: str,
    ui_specs: list[dict[str, Any]],
    estimated_total: int,
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    research_periods: dict[str, Any],
) -> dict[str, Any]:
    enabled_specs = [spec for spec in ui_specs if bool(spec.get("enabled"))]
    return {
        "phase": "before",
        "mode_label": str(mode_label or "智慧搜尋"),
        "development_period": str(research_periods.get("development_label") or "--"),
        "holdout_period": str(research_periods.get("holdout_label") or "--"),
        "enabled_param_count": len(enabled_specs),
        "estimated_candidates": int(max(estimated_total, 0)),
        "full_grid_candidates": int(max(_full_grid_count(ui_specs), 0)),
        "cpu_limit_pct": int(runtime_settings.get("cpu_limit_pct") or 0),
        "memory_limit_pct": int(runtime_settings.get("memory_limit_pct") or 0),
        "requested_workers": int(runtime_settings.get("requested_workers") or 0),
        "effective_workers": int(runtime_settings.get("max_workers") or 0),
        "slip_per_side": float(runtime_settings.get("slip_per_side") or 0.0),
        "top_n": int(runtime_settings.get("top_n") or 0),
        "seed_keep_count": int(runtime_settings.get("seed_keep_count") or 0),
        "min_trades": int(hard_filters.get("min_trades") or 0),
        "min_total_return": float(hard_filters.get("min_total_return") or 0.0),
        "max_mdd_pct": float(hard_filters.get("max_mdd_pct") or 0.0),
    }


def _summary_lines_from_dict(phase: str, summary: dict[str, Any]) -> list[str]:
    if not isinstance(summary, dict) or not summary:
        if phase == "before":
            return ["尚未開始執行，這裡會先顯示研究期間、候選組數、硬過濾與資源限制。"]
        if phase == "during":
            return ["尚未進入執行中階段，開始後這裡會顯示目前做到哪一輪、目前最佳與主要淘汰原因。"]
        return ["尚未產生執行後摘要，完成或停止後這裡會顯示結論、第一名參數與下一步建議。"]

    if phase == "before":
        return [
            f"研究模式：{summary.get('mode_label', '--')}；開發區 {summary.get('development_period', '--')}；最近 1 年驗收 {summary.get('holdout_period', '--')}。",
            f"啟用參數 {int(summary.get('enabled_param_count') or 0):,} 個；智慧搜尋候選組約 {int(summary.get('estimated_candidates') or 0):,} 組；若全排列約 {int(summary.get('full_grid_candidates') or 0):,} 組。",
            f"資源限制：CPU {int(summary.get('cpu_limit_pct') or 0)}% / 記憶體 {int(summary.get('memory_limit_pct') or 0)}% / workers {int(summary.get('effective_workers') or 0)}。",
            f"硬過濾：最少交易 {int(summary.get('min_trades') or 0)}、最低總報酬 {float(summary.get('min_total_return') or 0.0):.2f}%、最大 MDD {float(summary.get('max_mdd_pct') or 0.0):.2f}%。",
        ]
    if phase == "during":
        return [
            f"目前進度：已測 {int(summary.get('tested_count') or 0):,} / {int(summary.get('estimated_count') or 0):,}，通過 {int(summary.get('passed_count') or 0):,}，通過率 {float(summary.get('pass_rate_pct') or 0.0):.2f}%。",
            f"目前步驟：{summary.get('current_step', '--')}；已耗時 {_format_duration(summary.get('elapsed_seconds') or 0.0)}；預估剩餘 {_format_duration(summary.get('eta_seconds') or 0.0)}。",
            f"目前最佳：分數 {_format_number(summary.get('current_best_score'))} / 報酬 {_format_percent(summary.get('current_best_return'))} / MDD {_format_percent(summary.get('current_best_mdd'))} / 交易 {_format_number(summary.get('current_best_trades'), 0)}。",
            f"目前最佳參數：{summary.get('current_best_params') or '--'}。",
            f"目前最常淘汰原因：{summary.get('top_fail_reason') or '--'}。",
        ]
    return [
        f"執行結果：{summary.get('status', '--')}；共測 {int(summary.get('tested_count') or 0):,} / {int(summary.get('estimated_count') or 0):,}，通過 {int(summary.get('passed_count') or 0):,}。",
        f"第一名結果：分數 {_format_number(summary.get('best_score'))} / 報酬 {_format_percent(summary.get('best_return'))} / MDD {_format_percent(summary.get('best_mdd'))} / 交易 {_format_number(summary.get('best_trades'), 0)}。",
        f"第一名參數：{summary.get('best_params') or '--'}。",
        f"主要淘汰原因：{summary.get('top_fail_reason') or '--'}。",
        f"研究區間：開發區 {summary.get('development_period', '--')}；最近 1 年驗收 {summary.get('holdout_period', '--')}。",
    ]


def _render_phase_summaries(*, draft_pre_summary: dict[str, Any], view: dict[str, Any]) -> None:
    pre_summary = view.get("pre_run_summary") if isinstance(view.get("pre_run_summary"), dict) and view.get("pre_run_summary") else draft_pre_summary
    live_summary = view.get("live_run_summary") if isinstance(view.get("live_run_summary"), dict) else {}
    post_summary = view.get("post_run_summary") if isinstance(view.get("post_run_summary"), dict) else {}

    st.markdown("## 執行摘要")
    cols = st.columns(3)
    with cols[0]:
        _render_bullet_block("執行前摘要", _summary_lines_from_dict("before", pre_summary))
    with cols[1]:
        _render_bullet_block("執行中摘要", _summary_lines_from_dict("during", live_summary))
    with cols[2]:
        _render_bullet_block("執行後摘要", _summary_lines_from_dict("after", post_summary))


def _holdout_signature(current_snapshot: dict[str, Any], research_periods: dict[str, Any]) -> str:
    params = current_snapshot.get("params") if isinstance(current_snapshot, dict) else {}
    if not isinstance(params, dict) or not params:
        return ""
    payload = {
        "params": params,
        "holdout_start_date": research_periods.get("holdout_start_date"),
        "holdout_end_date": research_periods.get("holdout_end_date"),
        "research_profile_tag": research_periods.get("research_profile_tag"),
        "holdout_validation_version": 2,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rename_columns(df: pd.DataFrame, param_label_map: dict[str, str]) -> pd.DataFrame:
    if df.empty:
        return df
    rename_map: dict[str, str] = {}
    for column in df.columns:
        column_text = str(column)
        if column_text in RESULT_COLUMN_LABELS:
            rename_map[column_text] = RESULT_COLUMN_LABELS[column_text]
        elif column_text in param_label_map:
            rename_map[column_text] = param_label_map[column_text]
        elif column_text.startswith("year_return_"):
            year = column_text.replace("year_return_", "")
            rename_map[column_text] = f"{year} 年報酬率"
    return df.rename(columns=rename_map)


def _render_dataframe(df: pd.DataFrame, *, param_label_map: dict[str, str], height: int = 320) -> None:
    if df.empty:
        st.info("目前沒有可顯示的資料。")
        return
    st.dataframe(_rename_columns(df, param_label_map), width="stretch", hide_index=True, height=height)


def _render_bullet_block(title: str, lines: list[str]) -> None:
    st.markdown(f"#### {title}")
    if not lines:
        st.caption("目前沒有可顯示的摘要。")
        return
    st.markdown("\n".join(f"- {line}" for line in lines))


def _build_live_view_state(
    *,
    params_meta: list[dict[str, Any]],
    slip_per_side: float,
    xs_path: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    active_job_id: str,
    research_profile_tag: str,
) -> dict[str, Any]:
    param_names = [str(item["name"]) for item in params_meta]
    saved_progress = _saved_progress_state()
    saved_top_df = _result_frame(st.session_state.get("mq01_last_top_df"))
    saved_recent_df = _result_frame(st.session_state.get("mq01_last_recent_df"))
    saved_fail_rows = list(st.session_state.get("mq01_last_fail_rows", []))

    active_job_state = read_job_state(active_job_id) if active_job_id else {}
    active_status = str(active_job_state.get("status") or saved_progress.get("status") or "")
    running_now = bool(active_job_id) and not is_terminal_status(active_status)
    has_active_job_state = bool(active_job_state)

    if isinstance(active_job_state.get("artifact"), dict):
        st.session_state["mq01_last_artifacts"] = dict(active_job_state["artifact"])

    current_top_df = _rows_frame(active_job_state.get("top_rows")) if has_active_job_state else saved_top_df
    current_recent_df = _rows_frame(active_job_state.get("recent_rows")) if has_active_job_state else saved_recent_df
    current_fail_rows = list(active_job_state.get("fail_rows") or ([] if has_active_job_state else saved_fail_rows))

    current_snapshot = active_job_state.get("current_snapshot")
    if not isinstance(current_snapshot, dict) and not has_active_job_state:
        current_snapshot = dict(saved_progress.get("current_snapshot") or {})
    elif not isinstance(current_snapshot, dict):
        current_snapshot = {}
    if not current_snapshot and not current_top_df.empty:
        current_snapshot = build_current_best_snapshot(current_top_df, param_names, slip_per_side=slip_per_side)

    if not current_top_df.empty:
        st.session_state["mq01_last_top_df"] = current_top_df.copy()
    if not current_recent_df.empty:
        st.session_state["mq01_last_recent_df"] = current_recent_df.copy()
    st.session_state["mq01_last_fail_rows"] = list(current_fail_rows)

    progress_source = active_job_state if has_active_job_state else saved_progress
    done = int(progress_source.get("done") or 0)
    total = max(int(progress_source.get("total") or 0), 1)
    passed = int(progress_source.get("passed") or 0)
    step_note = str(progress_source.get("step_note") or "等待開始")
    summary_lines = [str(line) for line in (progress_source.get("summary_lines") or [])]
    narrative_lines = [str(line) for line in (progress_source.get("narrative_lines") or [])]
    elapsed_seconds = float(progress_source.get("elapsed_seconds") or 0.0)
    compute_elapsed_seconds = float(progress_source.get("compute_elapsed_seconds") or 0.0)
    transition_elapsed_seconds = float(progress_source.get("transition_elapsed_seconds") or 0.0)
    eta_seconds = float(progress_source.get("eta_seconds") or 0.0)

    artifact_snapshot = st.session_state.get("mq01_last_artifacts")
    active_artifact = active_job_state.get("artifact") if isinstance(active_job_state.get("artifact"), dict) else {}
    export_payload = artifact_download_payload(active_artifact)

    live_signature = _best_export_signature(top_df=current_top_df, params_meta=params_meta, slip_per_side=slip_per_side)
    live_export_cache = st.session_state.get("mq01_live_export_cache")
    cache_signature = str(live_export_cache.get("signature") or "") if isinstance(live_export_cache, dict) else ""
    if not running_now and live_signature:
        export_payload = _cached_best_export_payload(
            top_df=current_top_df,
            params_meta=params_meta,
            xs_path=xs_path,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            slip_per_side=slip_per_side,
        ) or export_payload
    elif live_signature and cache_signature == live_signature and isinstance(live_export_cache, dict):
        export_payload = live_export_cache

    if not export_payload:
        if running_now:
            artifact_snapshot = {}
        elif not isinstance(artifact_snapshot, dict) or not artifact_snapshot:
            artifact_snapshot = load_latest_artifact_snapshot(research_profile_tag=research_profile_tag)
        if artifact_snapshot:
            export_payload = artifact_download_payload(artifact_snapshot)

    return {
        "active_job_state": active_job_state,
        "active_status": active_status,
        "running_now": running_now,
        "current_top_df": current_top_df,
        "current_recent_df": current_recent_df,
        "current_fail_rows": current_fail_rows,
        "current_snapshot": current_snapshot,
        "done": done,
        "total": total,
        "passed": passed,
        "step_note": step_note,
        "summary_lines": summary_lines,
        "narrative_lines": narrative_lines,
        "elapsed_seconds": elapsed_seconds,
        "compute_elapsed_seconds": compute_elapsed_seconds,
        "transition_elapsed_seconds": transition_elapsed_seconds,
        "eta_seconds": eta_seconds,
        "export_payload": export_payload,
        "pre_run_summary": active_job_state.get("pre_run_summary") or {},
        "live_run_summary": active_job_state.get("live_run_summary") or {},
        "post_run_summary": active_job_state.get("post_run_summary") or {},
    }


def _controller_token() -> str:
    token = str(st.session_state.get("mq0101_controller_token") or "").strip()
    if not token:
        token = f"mq0101_{uuid.uuid4().hex[:10]}"
        st.session_state["mq0101_controller_token"] = token
    return token


def _format_seconds_value(value: Any) -> str:
    if value in (None, ""):
        return "--"
    try:
        return f"{float(value):.1f}s"
    except Exception:
        return str(value)


def _job_status_label(status: str) -> str:
    mapping = {
        "running": "執行中",
        "completed": "已完成",
        "stopped": "已停止",
        "failed": "失敗",
        "pending": "等待中",
        "queued": "排隊中",
    }
    text = str(status or "").strip().lower()
    return mapping.get(text, str(status or "--"))


def _render_worker_monitor(*, active_job_id: str, active_status: str) -> None:
    st.subheader("背景程序監控")
    st.caption(f"控制頁心跳只用來標記是否離線；超過 {HEARTBEAT_STALE_SECONDS} 秒只會列為殘留背景程序，不再自動停工。")

    cleaned_records = list(st.session_state.get("mq0101_last_cleaned_workers") or [])
    if cleaned_records:
        cleaned_ids = ", ".join(str(item.get("job_id") or "--") for item in cleaned_records[:3])
        suffix = "..." if len(cleaned_records) > 3 else ""
        st.success(f"已清理 {len(cleaned_records)} 個殘留背景程序：{cleaned_ids}{suffix}")
        st.session_state["mq0101_last_cleaned_workers"] = []

    force_stopped_ids = list(st.session_state.get("mq0101_last_force_stopped_ids") or [])
    if force_stopped_ids:
        text = ", ".join(force_stopped_ids[:3])
        suffix = "..." if len(force_stopped_ids) > 3 else ""
        st.warning(f"已強制停止 {len(force_stopped_ids)} 個背景程序：{text}{suffix}")
        st.session_state["mq0101_last_force_stopped_ids"] = []

    records = list_job_runtime_records(stale_after_seconds=HEARTBEAT_STALE_SECONDS)
    active_records = [record for record in records if not is_terminal_status(str(record.get("status") or ""))]
    stale_records = [record for record in records if bool(record.get("stale"))]

    metric_cols = st.columns(2)
    metric_cols[0].metric("活躍背景程序", f"{len(active_records):,}")
    metric_cols[1].metric("殘留背景程序", f"{len(stale_records):,}")

    stop_current = st.button(
        "強制停止目前任務",
        width="stretch",
        disabled=not (active_job_id and not is_terminal_status(active_status)),
        key="mq0101_force_stop_current_job",
    )
    cleanup_clicked = st.button(
        "清理殘留背景程序",
        width="stretch",
        key="mq0101_cleanup_stale_jobs",
    )
    stop_all = st.button(
        "強制停止全部活躍背景程序",
        width="stretch",
        disabled=not bool(active_records),
        key="mq0101_force_stop_all_jobs",
    )

    if stop_current and active_job_id:
        force_stop_job(active_job_id, reason="使用者在 01-01 介面強制停止目前工作")
        st.session_state["mq0101_last_force_stopped_ids"] = [active_job_id]
        st.rerun()

    if cleanup_clicked:
        cleaned_records = cleanup_stale_jobs(stale_after_seconds=HEARTBEAT_STALE_SECONDS)
        st.session_state["mq0101_last_cleaned_workers"] = cleaned_records
        st.rerun()

    if stop_all and active_records:
        stopped_ids: list[str] = []
        for record in active_records:
            job_id = str(record.get("job_id") or "").strip()
            if not job_id:
                continue
            force_stop_job(job_id, reason="使用者在 01-01 介面強制停止全部活躍背景程序")
            stopped_ids.append(job_id)
        st.session_state["mq0101_last_force_stopped_ids"] = stopped_ids
        st.rerun()

    if records:
        monitor_df = pd.DataFrame(
            [
                {
                    "工作代號": str(record.get("job_id") or ""),
                    "狀態": _job_status_label(str(record.get("status") or "")),
                    "程序 ID": int(record.get("pid") or 0),
                    "心跳延遲": _format_seconds_value(record.get("heartbeat_age_seconds")),
                    "狀態檔延遲": _format_seconds_value(record.get("state_age_seconds")),
                    "殘留": "是" if record.get("stale") else "否",
                }
                for record in records[:12]
            ]
        )
        st.dataframe(monitor_df, width="stretch", hide_index=True, height=240)
    else:
        st.caption("目前沒有 job runtime 記錄。")


def _render_action_bar(*, run_disabled: bool, stop_enabled: bool, export_payload: dict[str, Any], key_suffix: str) -> tuple[bool, bool]:
    action_cols = st.columns([1.05, 1.05, 1.0, 1.0, 1.0])
    run_clicked = action_cols[0].button(
        "開始執行",
        type="primary",
        width="stretch",
        disabled=run_disabled,
        key=f"mq0101_run_{key_suffix}",
    )
    stop_clicked = action_cols[1].button(
        "停止",
        width="stretch",
        disabled=not stop_enabled,
        key=f"mq0101_stop_{key_suffix}",
    )
    action_cols[2].download_button(
        "下載指標 XS",
        data=export_payload.get("indicator_xs_bytes", b""),
        file_name=str(export_payload.get("indicator_xs_file_name") or "best_indicator.xs"),
        mime="text/plain",
        width="stretch",
        disabled=not bool(export_payload.get("indicator_xs_bytes")),
        key=f"mq0101_download_indicator_{key_suffix}",
    )
    action_cols[3].download_button(
        "下載交易 XS",
        data=export_payload.get("trade_xs_bytes", b""),
        file_name=str(export_payload.get("trade_xs_file_name") or "best_trade.xs"),
        mime="text/plain",
        width="stretch",
        disabled=not bool(export_payload.get("trade_xs_bytes")),
        key=f"mq0101_download_trade_{key_suffix}",
    )
    action_cols[4].download_button(
        "下載逐筆 TXT",
        data=export_payload.get("txt_bytes", b""),
        file_name=str(export_payload.get("txt_file_name") or "best_strategy.txt"),
        mime="text/plain",
        width="stretch",
        disabled=not bool(export_payload.get("txt_bytes")),
        key=f"mq0101_download_txt_{key_suffix}",
    )
    return bool(run_clicked), bool(stop_clicked)


def _render_status_notices(view: dict[str, Any]) -> None:
    status = str(view["active_status"] or "")
    if status == "stopping":
        st.warning("背景工作正在停止，會在安全檢查點停下來。")
    elif status == "stopped":
        st.success("背景工作已停止。")
    elif status == "completed":
        st.success("背景工作已完成。")
    elif status == "error":
        st.error(str(view["active_job_state"].get("error") or "背景工作執行失敗。"))

    if view["summary_lines"] and status in {"stopped", "completed", "error"}:
        st.info("\n".join(view["summary_lines"]))


def _render_progress(view: dict[str, Any]) -> None:
    if not (view["running_now"] or view["done"] > 0):
        return
    st.progress(min(view["done"] / view["total"], 1.0), text=f"已完成 {view['done']:,}/{view['total']:,}")
    cols = st.columns(6)
    cols[0].metric("已完成", f"{view['done']:,}")
    cols[1].metric("通過硬條件", f"{view['passed']:,}")
    cols[2].metric("總耗時", _format_duration(view["elapsed_seconds"]))
    cols[3].metric("計算耗時", _format_duration(view["compute_elapsed_seconds"]))
    cols[4].metric("轉場耗時", _format_duration(view["transition_elapsed_seconds"]))
    cols[5].metric("剩餘組數", f"{max(view['total'] - view['done'], 0):,}")
    st.caption(f"目前狀態：{_status_label(view['active_status'])} | 預估剩餘：{_format_duration(view['eta_seconds'])}")
    st.caption(f"目前步驟：{view['step_note']}")


def _trim_year_columns(df: pd.DataFrame, *, max_year: int | None) -> pd.DataFrame:
    if df.empty or max_year is None:
        return df
    keep_columns: list[str] = []
    for column in df.columns:
        match = re.fullmatch(r"year_return_(\d{4})", str(column))
        if match and int(match.group(1)) > int(max_year):
            continue
        keep_columns.append(str(column))
    return df.loc[:, keep_columns].copy()


def _development_year_rows(current_snapshot: dict[str, Any], *, research_periods: dict[str, Any]) -> pd.DataFrame:
    payload = current_snapshot.get("year_returns") if isinstance(current_snapshot, dict) else {}
    if not isinstance(payload, dict) or not payload:
        return pd.DataFrame()
    try:
        development_end_date = int(research_periods.get("development_end_date") or 0)
        development_end_year = int(str(development_end_date)[:4])
        end_month = int(str(development_end_date)[4:6])
        end_day = int(str(development_end_date)[6:8])
    except Exception:
        development_end_year = None
        end_month = 12
        end_day = 31

    rows: list[dict[str, Any]] = []
    for year_text, value in sorted(payload.items(), key=lambda item: item[0]):
        try:
            year = int(str(year_text))
        except Exception:
            continue
        if development_end_year is not None and year > development_end_year:
            continue
        label = str(year)
        if development_end_year is not None and year == development_end_year and not (end_month == 12 and end_day == 31):
            label = f"{year}（截至 {end_month:02d}-{end_day:02d}）"
        rows.append({"年度": label, "報酬率": f"{float(value):.2f}%"})
    return pd.DataFrame(rows)


def _development_fail_reason_lines(fail_rows: list[dict[str, Any]]) -> list[str]:
    if not fail_rows:
        return ["目前還沒有淘汰原因統計。"]
    top_rows = fail_rows[:3]
    return [f"{row.get('reason', '--')}：{int(row.get('count') or 0):,} 次" for row in top_rows]


def _holdout_failure_labels(holdout_result: dict[str, Any]) -> list[str]:
    if not holdout_result:
        return []
    labels: list[str] = []
    if float(holdout_result.get("trade_count") or 0) <= 0:
        labels.append("no_trades")
    if float(holdout_result.get("net_profit") or 0) <= 0:
        labels.append("oos_net_profit_non_positive")
    if float(holdout_result.get("profit_factor") or 0) < 1.0:
        labels.append("profit_factor_below_1")
    slippage_rows = list(holdout_result.get("slippage_results") or [])
    slip2 = next((row for row in slippage_rows if abs(float(row.get("slip_per_side") or 0.0) - 2.0) < 1e-9), None)
    if slip2 and float(slip2.get("net_profit") or 0.0) <= 0:
        labels.append("fails_at_2pt_slippage")
    return labels


def _build_development_summary_lines(
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    fail_rows: list[dict[str, Any]],
    *,
    research_periods: dict[str, Any],
) -> list[str]:
    development_label = str(research_periods.get("development_label") or "--")
    if not current_snapshot:
        return [
            f"開發區固定使用 {development_label}，目前尚未產生第一名候選。",
            "請先完成一輪最佳化，系統才會顯示最佳候選摘要與最近 1 年驗收入口。",
        ]

    current_primary_score = current_snapshot.get("robust_score", current_snapshot.get("composite_score"))
    historical_primary_score = historical_snapshot.get("robust_score", historical_snapshot.get("composite_score"))
    lines = [
        f"開發區固定使用 {development_label}，只用這段資料做最佳化與排名。",
        (
            f"目前第一名候選的開發區穩健分數為 {_format_number(current_primary_score)}，"
            f"總報酬 {_format_percent(current_snapshot.get('total_return'))}，"
            f"MDD {_format_percent(current_snapshot.get('mdd_pct'))}，"
            f"交易 {_format_number(current_snapshot.get('n_trades'), 0)} 筆。"
        ),
        f"目前最佳參數摘要：{_format_param_summary(current_snapshot, limit=5)}。",
    ]
    if historical_snapshot:
        lines.append(
            f"歷史最佳穩健分數為 {_format_number(historical_primary_score)}，可用來對照本輪是否接近歷史高點。"
        )
    if fail_rows:
        top_fail = fail_rows[0]
        lines.append(f"目前最常見淘汰原因是 {top_fail.get('reason', '--')}，累計 {int(top_fail.get('count') or 0):,} 次。")
    return lines


def _build_execution_summary_lines(view: dict[str, Any], *, research_periods: dict[str, Any]) -> list[str]:
    lines = [
        f"研究設定固定為：開發區 {research_periods.get('development_label', '--')}，最近 1 年驗收 {research_periods.get('holdout_label', '--')}。",
        f"目前狀態：{_status_label(str(view.get('active_status') or 'idle'))}。",
        f"已完成 {int(view.get('done') or 0):,} / {int(view.get('total') or 0):,} 組，通過硬條件 {int(view.get('passed') or 0):,} 組。",
        f"目前步驟：{str(view.get('step_note') or '--')}",
    ]
    if view.get("summary_lines"):
        lines.extend(str(line) for line in list(view["summary_lines"])[:3])
    return lines


def _build_wf_summary_lines(research_periods: dict[str, Any]) -> list[str]:
    return [
        f"Walk-Forward 正式研究區只應限制在開發區 {research_periods.get('development_label', '--')} 內。",
        "主規格固定為 Train 36 個月 / Test 6 個月 / Step 3 個月。",
        "最近 1 年驗收只做最終測試，不參與選型，也不回頭影響開發區排名。",
    ]


def _build_annual_summary_lines(current_snapshot: dict[str, Any], *, research_periods: dict[str, Any]) -> list[str]:
    year_rows = _development_year_rows(current_snapshot, research_periods=research_periods)
    if year_rows.empty:
        return [
            f"年度拆解應只顯示開發區 {research_periods.get('development_label', '--')} 內的年度結果。",
            "目前尚未產生年度拆解摘要。",
        ]
    positive_count = 0
    for value in year_rows["報酬率"]:
        try:
            numeric = float(str(value).replace("%", ""))
        except Exception:
            numeric = 0.0
        if numeric > 0:
            positive_count += 1
    return [
        f"年度拆解只顯示開發區結果，不會把最近 1 年驗收混進來。",
        f"目前開發區年度摘要共 {len(year_rows):,} 段，其中正報酬年度 {positive_count:,} 段。",
        "這一區不再顯示 2026 這種不應混入開發區的年度欄位。",
    ]


def _build_regime_summary_lines() -> list[str]:
    return [
        "Regime 分桶畫面位置已保留在開發區摘要內。",
        "01-01 的 Regime backend 目前尚未接入正式分桶結果，所以這裡先不顯示錯誤數據。",
        "下一步應接上方向 × 波動的 9 桶結果，再把優勢桶與危險桶放進來。",
    ]


def _build_holdout_summary_lines(holdout_result: dict[str, Any], *, research_periods: dict[str, Any]) -> list[str]:
    holdout_label = str(research_periods.get("holdout_label") or "--")
    if not holdout_result:
        return [
            f"最近 1 年驗收固定使用 {holdout_label}，這段資料不參與最佳化，只做最終測試。",
            "請先跑出開發區第一名，再按按鈕測試最近 1 年。",
        ]

    lines = [
        f"最近 1 年驗收固定使用 {holdout_label}，Final Verdict 為 {str(holdout_result.get('final_verdict') or '--')}。",
        (
            f"2 點滑價下淨利 {_format_number(holdout_result.get('net_profit'))}，"
            f"PF {_format_number(holdout_result.get('profit_factor'))}，"
            f"MDD {_format_percent(holdout_result.get('mdd_pct'))}，"
            f"交易 {_format_number(holdout_result.get('trade_count'), 0)} 筆。"
        ),
    ]
    labels = _holdout_failure_labels(holdout_result)
    if labels:
        lines.append(f"失敗原因標籤：{', '.join(labels)}。")
    slippage_rows = list(holdout_result.get("slippage_results") or [])
    if slippage_rows:
        slippage_text = " / ".join(
            f"{_format_number(row.get('slip_per_side'), 1)} 點：{_format_number(row.get('net_profit'))}"
            for row in slippage_rows
        )
        lines.append(f"滑價敏感度：{slippage_text}。")
    return lines


def _render_overview_cards(
    *,
    mode: str,
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    system_snapshot: dict[str, Any],
    research_periods: dict[str, Any],
    holdout_result: dict[str, Any],
) -> None:
    current_primary_score = current_snapshot.get("robust_score", current_snapshot.get("composite_score"))
    historical_primary_score = historical_snapshot.get("robust_score", historical_snapshot.get("composite_score"))
    row1 = st.columns(6)
    row1[0].metric("開發區", str(research_periods.get("development_label") or "--"))
    row1[1].metric("最近 1 年驗收", str(research_periods.get("holdout_label") or "--"))
    row1[2].metric("最佳參數組", _format_param_summary(current_snapshot, limit=1))
    row1[3].metric("開發區穩健分數", _format_number(current_primary_score))
    row1[4].metric("開發區總報酬", _format_percent(current_snapshot.get("total_return")))
    row1[5].metric("最近 1 年驗收", str(holdout_result.get("final_verdict") or "待測試"))

    row2 = st.columns(6)
    row2[0].metric("開發區交易筆數", _format_number(current_snapshot.get("n_trades"), 0))
    row2[1].metric("開發區年均報酬", _format_percent(current_snapshot.get("year_avg_return")))
    row2[2].metric("歷史最佳穩健分數", _format_number(historical_primary_score))
    row2[3].metric("單邊滑價", f"{_format_number(current_snapshot.get('slip_per_side') or 2.0, 1)} 點")
    row2[4].metric("搜尋模式", MODE_OPTIONS.get(mode, mode))
    row2[5].metric(
        "最近 1 年 PF / 淨利",
        (
            f"{_format_number(holdout_result.get('profit_factor'))} / {_format_number(holdout_result.get('net_profit'))}"
            if holdout_result
            else "--"
        ),
    )

    st.caption(
        f"CPU 上限 {system_snapshot.get('cpu_limit_pct', '--')}% | 記憶體上限 {system_snapshot.get('memory_limit_pct', '--')}% | "
        f"Workers {system_snapshot.get('configured_workers', '--')} | 可用核心 {system_snapshot.get('effective_cpu_count', '--')}"
    )


def _render_development_section(
    view: dict[str, Any],
    historical_snapshot: dict[str, Any],
    param_label_map: dict[str, str],
    *,
    research_periods: dict[str, Any],
) -> None:
    st.markdown("## 開發區最佳化結果")
    st.caption(f"開發區固定使用 {research_periods.get('development_label', '--')}。這段資料才參與最佳化與排名。")

    summary_cols = st.columns(2)
    with summary_cols[0]:
        _render_bullet_block(
            "最佳候選詳細摘要",
            _build_development_summary_lines(
                view["current_snapshot"],
                historical_snapshot,
                view["current_fail_rows"],
                research_periods=research_periods,
            ),
        )
    with summary_cols[1]:
        _render_bullet_block(
            "研究執行摘要",
            _build_execution_summary_lines(view, research_periods=research_periods),
        )

    detail_cols = st.columns(3)
    with detail_cols[0]:
        _render_bullet_block("Walk-Forward 摘要", _build_wf_summary_lines(research_periods))
    with detail_cols[1]:
        _render_bullet_block("年度拆解摘要", _build_annual_summary_lines(view["current_snapshot"], research_periods=research_periods))
    with detail_cols[2]:
        _render_bullet_block("Regime 分桶摘要", _build_regime_summary_lines())

    development_end_year = None
    try:
        development_end_year = int(str(research_periods.get("development_end_date") or "0")[:4])
    except Exception:
        development_end_year = None

    display_top_df = _trim_year_columns(view["current_top_df"], max_year=development_end_year)
    display_recent_df = _trim_year_columns(view["current_recent_df"], max_year=development_end_year)

    if display_top_df.empty:
        st.info("目前尚未產生開發區最佳結果表。")
    else:
        st.markdown("### 開發區候選排名")
        _render_dataframe(display_top_df, param_label_map=param_label_map, height=360)

    annual_df = _development_year_rows(view["current_snapshot"], research_periods=research_periods)
    if not annual_df.empty:
        st.markdown("### 年度表")
        st.dataframe(annual_df, width="stretch", hide_index=True, height=220)

    if not display_recent_df.empty:
        st.markdown("### 最近測試明細")
        _render_dataframe(display_recent_df, param_label_map=param_label_map, height=280)

    if view["current_fail_rows"]:
        st.markdown("### 淘汰原因統計")
        fail_df = pd.DataFrame(view["current_fail_rows"]).rename(columns={"reason": "原因", "count": "次數"})
        st.dataframe(fail_df, width="stretch", hide_index=True, height=220)
        _render_bullet_block("主要淘汰原因", _development_fail_reason_lines(view["current_fail_rows"]))


def _render_holdout_section(
    *,
    holdout_result: dict[str, Any],
    research_periods: dict[str, Any],
    current_snapshot: dict[str, Any],
    running_now: bool,
) -> bool:
    st.markdown("## 最近 1 年最終驗收")
    st.caption(
        f"最近 1 年驗收固定使用 {research_periods.get('holdout_label', '--')}。這段資料不參與最佳化，只做最終測試。"
    )

    params = current_snapshot.get("params") if isinstance(current_snapshot, dict) else {}
    can_test = isinstance(params, dict) and bool(params) and not running_now
    button_label = (
        f"重新測試最近 1 年（{research_periods.get('holdout_label', '--')}）"
        if holdout_result
        else f"開始測試最近 1 年（{research_periods.get('holdout_label', '--')}）"
    )
    test_clicked = st.button(
        button_label,
        type="primary",
        width="stretch",
        disabled=not can_test,
        key="mq0101_holdout_test_button",
    )
    if not params:
        st.caption("請先跑出開發區第一名候選，再測試最近 1 年。")
    elif running_now:
        st.caption("最佳化進行中，請等開發區結果穩定後再測試最近 1 年。")

    cards = st.columns(8)
    cards[0].metric("淨利", _format_number(holdout_result.get("net_profit")))
    cards[1].metric("MDD", _format_percent(holdout_result.get("mdd_pct")))
    cards[2].metric("PF", _format_number(holdout_result.get("profit_factor")))
    cards[3].metric("勝率", _format_percent(holdout_result.get("win_rate_pct")))
    cards[4].metric("平均每筆", _format_number(holdout_result.get("avg_net_profit")))
    cards[5].metric("交易筆數", _format_number(holdout_result.get("trade_count"), 0))

    slip_rows = {int(round(float(row.get("slip_per_side") or 0.0))): row for row in list(holdout_result.get("slippage_results") or [])}
    cards[6].metric(
        "2 / 3 / 4 點",
        (
            " / ".join(
                "PASS" if float(slip_rows.get(slip, {}).get("net_profit") or 0.0) > 0 else "FAIL"
                for slip in (2, 3, 4)
            )
            if slip_rows
            else "--"
        ),
    )
    cards[7].metric("Final Verdict", str(holdout_result.get("final_verdict") or "待測試"))

    _render_bullet_block(
        "最近 1 年驗收摘要",
        _build_holdout_summary_lines(holdout_result, research_periods=research_periods),
    )

    failure_labels = _holdout_failure_labels(holdout_result)
    if failure_labels:
        st.caption("失敗原因標籤：" + " / ".join(failure_labels))

    detail_cols = st.columns(3)
    with detail_cols[0]:
        monthly_rows = list(holdout_result.get("monthly_returns") or [])
        if monthly_rows:
            monthly_df = pd.DataFrame(monthly_rows).rename(columns={"period": "月份", "pnl": "淨利", "return": "報酬率"})
            monthly_df["報酬率"] = monthly_df["報酬率"].map(lambda value: f"{float(value) * 100.0:.2f}%")
            st.markdown("#### 月度表")
            st.dataframe(monthly_df, width="stretch", hide_index=True, height=240)
        else:
            _render_bullet_block("月度表", ["待測試後顯示最近 1 年月度分布。"])

    with detail_cols[1]:
        st.markdown("#### Regime 表")
        st.caption("01-01 的 Regime 驗收 backend 尚未接入，這裡暫不顯示錯誤資料。")
        st.markdown("- 後續會補上方向 × 波動的 9 桶結果。")
        st.markdown("- 補上後才會顯示優勢 regime / 危險 regime。")

    with detail_cols[2]:
        slippage_rows = list(holdout_result.get("slippage_results") or [])
        if slippage_rows:
            slippage_df = pd.DataFrame(slippage_rows).rename(
                columns={
                    "slip_per_side": "單邊滑價",
                    "net_profit": "淨利",
                    "trade_count": "交易筆數",
                    "win_rate_pct": "勝率",
                    "avg_net_profit": "平均每筆",
                    "profit_factor": "PF",
                    "total_return_pct": "報酬率",
                    "mdd_pct": "MDD",
                }
            )
            for percent_col in ("勝率", "報酬率", "MDD"):
                slippage_df[percent_col] = slippage_df[percent_col].map(lambda value: f"{float(value):.2f}%")
            st.markdown("#### 滑價敏感度表")
            st.dataframe(slippage_df, width="stretch", hide_index=True, height=240)
        else:
            _render_bullet_block("滑價敏感度表", ["待測試後顯示 2 / 3 / 4 點滑價的存活與退化情況。"])

    return bool(test_clicked)


def _render_raw_section(view: dict[str, Any], *, research_profile_tag: str) -> None:
    st.markdown("## 原始明細 / 匯出資訊")
    with st.expander("查看原始執行摘要"):
        if view["narrative_lines"]:
            st.markdown("\n".join(f"- {line}" for line in view["narrative_lines"]))
        else:
            st.caption("目前沒有可顯示的原始摘要。")

    artifact = st.session_state.get("mq01_last_artifacts")
    if not isinstance(artifact, dict) or not artifact:
        artifact = load_latest_artifact_snapshot(research_profile_tag=research_profile_tag)
    if artifact:
        with st.expander("查看最新匯出資訊"):
            st.json(artifact)


def render_app() -> None:
    if st.session_state.get(PROFILE_SESSION_KEY) != RESEARCH_PROFILE_TAG_0101:
        _reset_profile_state()
        st.session_state[PROFILE_SESSION_KEY] = RESEARCH_PROFILE_TAG_0101

    st.set_page_config(page_title="MQQuant 01-01", layout="wide")
    st.title("MQQuant 01-01 - 固定策略參數最佳化")
    st.caption("介面操作比照 01，但結果改成兩段式：先顯示開發區最佳化結果，再看最近 1 年最終驗收。")

    path_defaults = default_paths()
    runtime_defaults = default_runtime_settings()
    hard_filter_defaults = default_hard_filters()

    with st.sidebar:
        st.subheader("資料路徑")
        xs_path = st.text_input("XS 路徑", value=path_defaults.xs_path)
        minute_path = st.text_input("M1 路徑", value=path_defaults.minute_path)
        daily_path = st.text_input("D1 路徑", value=path_defaults.daily_path)
        preset_path = st.text_input("參數範圍 preset", value=path_defaults.param_preset_path)

        st.subheader("搜尋模式")
        mode = st.selectbox(
            "模式",
            options=list(MODE_OPTIONS.keys()),
            format_func=lambda key: MODE_OPTIONS[key],
            index=0,
        )

        st.subheader("執行設定")
        capital = int(st.number_input("本金", value=int(runtime_defaults["capital"]), step=100_000, format="%d"))
        slip_per_side = float(st.number_input("單邊滑價", value=float(runtime_defaults["slip_per_side"]), min_value=0.0, step=0.1, format="%.2f"))
        requested_workers = int(st.number_input("最大 workers", value=int(runtime_defaults["max_workers"]), min_value=1, step=1, format="%d"))
        cpu_limit_pct = int(st.number_input("CPU 使用率上限(%)", value=int(runtime_defaults["cpu_limit_pct"]), min_value=1, max_value=100, step=5, format="%d"))
        memory_limit_pct = int(st.number_input("記憶體使用率上限(%)", value=int(runtime_defaults["memory_limit_pct"]), min_value=1, max_value=100, step=5, format="%d"))
        effective_workers = resolve_effective_workers(requested_workers=requested_workers, cpu_limit_pct=cpu_limit_pct)
        top_n = int(st.number_input("保留前幾名", value=int(runtime_defaults["top_n"]), min_value=1, step=1, format="%d"))
        seed_keep_count = int(st.number_input("每個參數保留前幾名", value=int(runtime_defaults["seed_keep_count"]), min_value=1, step=1, format="%d"))
        st.caption(f"CPU 上限 {cpu_limit_pct}% | 記憶體上限 {memory_limit_pct}% | 可用 workers {effective_workers}")

        st.subheader("硬性過濾")
        min_trades = int(st.number_input("最少交易筆數", value=int(hard_filter_defaults["min_trades"]), min_value=0, step=10, format="%d"))
        min_total_return = float(st.number_input("最低總報酬(%)", value=float(hard_filter_defaults["min_total_return"]), step=1.0, format="%.2f"))
        max_mdd_pct = float(st.number_input("最大 MDD(%)", value=float(hard_filter_defaults["max_mdd_pct"]), step=1.0, format="%.2f"))

    path_errors = [path for path in (xs_path, minute_path, daily_path, preset_path) if not Path(path).exists()]
    if path_errors:
        st.error("以下路徑不存在，請先確認：\n" + "\n".join(path_errors))
        return

    try:
        script_name, params_meta, default_specs = load_strategy_metadata(xs_path, preset_path)
    except Exception as exc:
        st.exception(exc)
        return

    _remember_param_label_map(params_meta)
    _apply_mode_enabled_defaults(mode, default_specs)
    param_label_map = {str(item["name"]): _clean_param_label(str(item.get("label") or item["name"])) for item in params_meta}

    research_periods = resolve_0101_research_periods(minute_path=minute_path, daily_path=daily_path)
    if not research_periods:
        st.error("無法解析 01-01 的研究期間，請先確認 M1 / D1 資料是否完整。")
        return

    historical_snapshot = load_historical_best_snapshot(
        [str(item["name"]) for item in params_meta],
        slip_per_side=slip_per_side,
        research_profile_tag=RESEARCH_PROFILE_TAG_0101,
    )

    config_hidden = bool(st.session_state.get("mq01_hide_config", False))
    toggle_label = "展開參數設定" if config_hidden else "收起參數設定"
    if st.button(toggle_label, width="content", key="mq0101_toggle_config"):
        st.session_state["mq01_hide_config"] = not config_hidden
        st.rerun()

    if not config_hidden:
        st.subheader("參數設定")
        st.caption(f"目前策略為 `{script_name}`，介面維持和 01 相同的參數設定方式。")
        header_cols = st.columns([1.0, 1.8, 1.4, 1.4, 1.2, 0.8])
        header_cols[0].markdown("**啟用**")
        header_cols[1].markdown("**參數**")
        header_cols[2].markdown("**起點**")
        header_cols[3].markdown("**終點**")
        header_cols[4].markdown("**步長**")
        header_cols[5].markdown("**型別**")

        for spec in default_specs:
            name = str(spec["name"])
            value_type = str(spec["type"])
            row_cols = st.columns([1.0, 1.8, 1.4, 1.4, 1.2, 0.8])
            row_cols[0].checkbox("啟用", value=bool(spec["enabled"]), key=f"mq01_enabled_{name}", label_visibility="collapsed")
            row_cols[1].markdown(f"**{name}**  \n{_clean_param_label(str(spec['label']))}")
            with row_cols[2]:
                _number_input(label="起點", key=f"mq01_start_{name}", value=spec["start"], value_type=value_type, step=spec["step"])
            with row_cols[3]:
                _number_input(label="終點", key=f"mq01_stop_{name}", value=spec["stop"], value_type=value_type, step=spec["step"])
            with row_cols[4]:
                _number_input(
                    label="步長",
                    key=f"mq01_step_{name}",
                    value=spec["step"],
                    value_type=value_type,
                    step=spec["step"] if value_type == "float" else 1,
                )
            row_cols[5].write(TYPE_VALUE_LABELS.get(value_type, value_type))

    ui_specs = _current_ui_specs(default_specs)
    try:
        estimated_total = estimate_run_count(mode, ui_specs, params_meta, seed_keep_count=seed_keep_count)
    except Exception as exc:
        estimated_total = 0
        if not config_hidden:
            st.warning(f"參數設定無法估算總組數：{exc}")
    else:
        if not config_hidden:
            st.info(f"預估本輪會測試 **{estimated_total:,}** 組。")

    run_block_reason = grid_run_block_reason(mode, estimated_total)
    if run_block_reason and not config_hidden:
        st.error(run_block_reason)

    runtime_settings = {
        "cpu_limit_pct": cpu_limit_pct,
        "memory_limit_pct": memory_limit_pct,
        "capital": capital,
        "slip_per_side": slip_per_side,
        "requested_workers": requested_workers,
        "effective_workers": effective_workers,
        "max_workers": effective_workers,
        "top_n": top_n,
        "seed_keep_count": seed_keep_count,
        "research_profile_tag": RESEARCH_PROFILE_TAG_0101,
        "development_start_date": int(research_periods["development_start_date"]),
        "development_end_date": int(research_periods["development_end_date"]),
        "holdout_start_date": int(research_periods["holdout_start_date"]),
        "holdout_end_date": int(research_periods["holdout_end_date"]),
    }
    hard_filters = {
        "min_trades": min_trades,
        "min_total_return": min_total_return,
        "max_mdd_pct": max_mdd_pct,
    }
    draft_pre_summary = _build_pre_run_summary(
        mode_label=MODE_OPTIONS[mode],
        ui_specs=ui_specs,
        estimated_total=estimated_total,
        runtime_settings=runtime_settings,
        hard_filters=hard_filters,
        research_periods=research_periods,
    )
    package_root = Path(__file__).resolve().parent.parent

    active_job_id = str(st.session_state.get("mq01_active_job_id") or "").strip()
    controller_token = _controller_token()
    if active_job_id:
        touch_job_heartbeat(active_job_id, controller=controller_token)

    view = _build_live_view_state(
        params_meta=params_meta,
        slip_per_side=slip_per_side,
        xs_path=xs_path,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        active_job_id=active_job_id,
        research_profile_tag=RESEARCH_PROFILE_TAG_0101,
    )

    current_holdout_signature = _holdout_signature(view["current_snapshot"], research_periods)
    saved_holdout_signature = str(st.session_state.get(HOLDOUT_SIGNATURE_KEY) or "")
    if current_holdout_signature != saved_holdout_signature:
        st.session_state[HOLDOUT_SIGNATURE_KEY] = current_holdout_signature
        st.session_state[HOLDOUT_RESULT_KEY] = {}
    holdout_result = dict(st.session_state.get(HOLDOUT_RESULT_KEY) or {})

    with st.sidebar:
        st.divider()
        _render_worker_monitor(active_job_id=active_job_id, active_status=str(view["active_status"] or ""))

    run_clicked, stop_clicked = _render_action_bar(
        run_disabled=bool(run_block_reason) or view["running_now"],
        stop_enabled=view["running_now"],
        export_payload=view["export_payload"],
        key_suffix=active_job_id or "idle",
    )

    if run_clicked:
        job_id = create_job_request(
            {
                "mode": mode,
                "mode_label": MODE_OPTIONS[mode],
                "xs_path": xs_path,
                "minute_path": minute_path,
                "daily_path": daily_path,
                "script_name": script_name,
                "ui_param_specs": ui_specs,
                "params_meta": params_meta,
                "runtime_settings": runtime_settings,
                "hard_filters": hard_filters,
                "estimated_total": estimated_total,
                "pre_run_summary": draft_pre_summary,
            }
        )
        touch_job_heartbeat(job_id, controller=controller_token)
        launch_job_process(job_id, package_root=str(package_root))
        st.session_state["mq01_active_job_id"] = job_id
        st.session_state["mq01_hide_config"] = True
        st.session_state["mq01_live_export_cache"] = {}
        st.session_state[HOLDOUT_SIGNATURE_KEY] = ""
        st.session_state[HOLDOUT_RESULT_KEY] = {}
        st.rerun()

    if stop_clicked and active_job_id:
        request_stop(active_job_id)
        st.rerun()

    def _render_main_content(current_view: dict[str, Any], current_holdout_result: dict[str, Any], *, running_now: bool) -> None:
        _render_phase_summaries(draft_pre_summary=draft_pre_summary, view=current_view)
        _render_progress(current_view)
        _render_overview_cards(
            mode=mode,
            current_snapshot=current_view["current_snapshot"],
            historical_snapshot=historical_snapshot,
            system_snapshot=collect_system_snapshot(
                max_workers=effective_workers,
                requested_workers=requested_workers,
                cpu_limit_pct=cpu_limit_pct,
                memory_limit_pct=memory_limit_pct,
            ),
            research_periods=research_periods,
            holdout_result=current_holdout_result,
        )
        _render_development_section(
            current_view,
            historical_snapshot,
            param_label_map,
            research_periods=research_periods,
        )
        holdout_test_clicked = _render_holdout_section(
            holdout_result=current_holdout_result,
            research_periods=research_periods,
            current_snapshot=current_view["current_snapshot"],
            running_now=running_now,
        )
        if holdout_test_clicked and isinstance(current_view["current_snapshot"], dict) and isinstance(current_view["current_snapshot"].get("params"), dict):
            with st.spinner(f"開始測試最近 1 年（{research_periods.get('holdout_label', '--')}）..."):
                result = run_0101_holdout_validation(
                    params=dict(current_view["current_snapshot"]["params"]),
                    minute_path=minute_path,
                    daily_path=daily_path,
                    script_name=script_name,
                    capital=capital,
                )
            st.session_state[HOLDOUT_SIGNATURE_KEY] = _holdout_signature(current_view["current_snapshot"], research_periods)
            st.session_state[HOLDOUT_RESULT_KEY] = result
            st.rerun()
        _render_raw_section(current_view, research_profile_tag=RESEARCH_PROFILE_TAG_0101)

    if view["running_now"]:

        @st.fragment(run_every=4)
        def _render_live_header() -> None:
            current_active_job_id = str(st.session_state.get("mq01_active_job_id") or "").strip()
            if current_active_job_id:
                touch_job_heartbeat(current_active_job_id, controller=controller_token)
            refreshed_view = _build_live_view_state(
                params_meta=params_meta,
                slip_per_side=slip_per_side,
                xs_path=xs_path,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                active_job_id=current_active_job_id,
                research_profile_tag=RESEARCH_PROFILE_TAG_0101,
            )
            if not refreshed_view["running_now"]:
                st.rerun()
                return

            live_holdout_signature = _holdout_signature(refreshed_view["current_snapshot"], research_periods)
            if live_holdout_signature != str(st.session_state.get(HOLDOUT_SIGNATURE_KEY) or ""):
                st.session_state[HOLDOUT_SIGNATURE_KEY] = live_holdout_signature
                st.session_state[HOLDOUT_RESULT_KEY] = {}
            live_holdout_result = dict(st.session_state.get(HOLDOUT_RESULT_KEY) or {})
            _render_main_content(refreshed_view, live_holdout_result, running_now=True)

        _render_live_header()
        return

    _render_status_notices(view)
    _render_main_content(view, holdout_result, running_now=False)
