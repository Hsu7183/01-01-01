from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from .config import default_hard_filters, default_paths, default_runtime_settings
from .job_store import (
    create_job_request,
    is_terminal_status,
    launch_job_process,
    read_job_state,
    request_stop,
)
from .parameters import load_strategy_metadata
from .services import (
    build_best_export_payload,
    build_current_best_snapshot,
    collect_system_snapshot,
    estimate_run_count,
    grid_run_block_reason,
    load_historical_best_snapshot,
    load_latest_artifact_snapshot,
    persist_best_run,
    resolve_effective_workers,
    run_optimizer,
)


MODE_OPTIONS = {
    "smart": "智慧搜尋",
    "cycle": "單參數循環",
}

TYPE_VALUE_LABELS = {
    "int": "整數",
    "float": "浮點",
}

PARAM_SHORT_LABEL_OVERRIDES = {
    "DonLen": "Don長度",
    "ATRLen": "ATR長度",
    "EMAWarmBars": "EMA回推",
    "EntryBufferPts": "突破緩衝",
    "DonBufferPts": "Don緩衝",
    "MinATRD": "最小ATR",
    "ATRStopK": "ATR停損",
    "ATRTakeProfitK": "ATR停利",
    "MaxEntriesPerDay": "日進場數",
    "TimeStopBars": "時間停損",
    "MinRunPctAnchor": "停損啟動",
    "TrailStartPctAnchor": "回吐啟動",
    "TrailGivePctAnchor": "允許回吐",
    "UseAnchorExit": "08:48出場",
    "AnchorBackPct": "失敗出場",
}


def _format_number(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "--"
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if abs(numeric - round(numeric)) < 1e-9:
        return str(int(round(numeric)))
    return f"{numeric:.{digits}f}"


def _format_percent(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "--"
    try:
        return f"{float(value):.{digits}f}%"
    except Exception:
        return str(value)


def _format_duration(seconds: Any) -> str:
    if seconds is None or seconds == "":
        return "--"
    try:
        total_seconds = max(float(seconds), 0.0)
    except Exception:
        return str(seconds)
    if total_seconds >= 3600:
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        secs = int(total_seconds % 60)
        return f"{hours}時 {minutes}分 {secs}秒"
    if total_seconds >= 60:
        minutes = int(total_seconds // 60)
        secs = int(total_seconds % 60)
        return f"{minutes}分 {secs}秒"
    return f"{total_seconds:.1f}秒"


def _format_saved_at(value: Any) -> str:
    if value is None or value == "":
        return "--"
    text = str(value).strip().replace("T", " ")
    return text[:19]


def _format_param_text(params: dict[str, Any], limit: int = 6) -> str:
    if not params:
        return "參數：--"
    items = list(params.items())[: max(1, int(limit))]
    pairs = [f"{name}={_format_number(value, digits=4)}" for name, value in items]
    return "參數：" + " / ".join(pairs)


def _short_param_label(name: str, label: str) -> str:
    if name in PARAM_SHORT_LABEL_OVERRIDES:
        return PARAM_SHORT_LABEL_OVERRIDES[name]
    text = re.sub(r"^\d+\.", "", str(label or name)).strip()
    text = text.replace("(%)", "").replace("(1=是,0=否)", "").strip()
    if len(text) > 10:
        text = text[:10]
    return text or name


TABLE_COLUMN_LABELS = {
    "mode": "模式",
    "cycle_no": "第幾輪",
    "round_no": "第幾階段",
    "stage_name": "目前參數",
    "param_index": "參數序號",
    "param_total": "參數總數",
    "candidate_value": "候選值",
    "keep_count": "保留前幾名",
    "status": "結果",
    "reason": "判定原因",
    "n_trades": "交易數",
    "total_return": "總報酬(%)",
    "mdd_amount": "MDD金額",
    "mdd_pct": "MDD(%)",
    "year_avg_return": "平均年報酬(%)",
    "year_return_std": "年報酬波動",
    "loss_years": "虧損年份數",
    "composite_score": "綜合分數",
    "elapsed": "耗時",
    "eta": "預估剩餘",
    "saved_at": "保存時間",
    "count": "次數",
}

MODE_VALUE_LABELS = {
    "smart": "智慧搜尋",
    "cycle": "單參數循環",
    "batch": "單參數循環",
}


def _year_return_items(snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    payload = snapshot.get("year_returns") if isinstance(snapshot, dict) else {}
    if not isinstance(payload, dict):
        return []
    items = [(str(year), value) for year, value in payload.items() if value not in (None, "")]
    return sorted(items, key=lambda item: item[0])


def _remember_param_label_map(params_meta: list[dict[str, Any]]) -> None:
    st.session_state["mq01_param_label_map"] = {
        str(item["name"]): _short_param_label(str(item["name"]), str(item.get("label") or item["name"]))
        for item in params_meta
    }


def _localize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    localized = df.copy()
    if "mode" in localized.columns:
        localized["mode"] = localized["mode"].map(lambda value: MODE_VALUE_LABELS.get(str(value), str(value)))
    rename_map: dict[str, str] = {}
    param_label_map = st.session_state.get("mq01_param_label_map")
    if not isinstance(param_label_map, dict):
        param_label_map = {}
    for column in localized.columns:
        if column in TABLE_COLUMN_LABELS:
            rename_map[column] = TABLE_COLUMN_LABELS[column]
        elif column in param_label_map:
            rename_map[column] = str(param_label_map[column])
        elif str(column).startswith("year_return_"):
            year = str(column).replace("year_return_", "")
            rename_map[column] = f"{year}年報酬率(%)"
    return localized.rename(columns=rename_map)


def _build_column_config(df: pd.DataFrame) -> dict[str, Any] | None:
    column_factory = getattr(getattr(st, "column_config", None), "Column", None)
    if column_factory is None or df.empty:
        return None
    config: dict[str, Any] = {}
    for column in df.columns:
        label = str(column)
        width = "small"
        if len(label) >= 12 or label in {"目前參數", "判定原因", "每年報酬率"}:
            width = "medium"
        config[column] = column_factory(label, width=width)
    return config


def _render_compact_dataframe(df: pd.DataFrame, *, height: int = 320) -> None:
    localized = _localize_dataframe(df)
    st.dataframe(
        localized,
        width="stretch",
        hide_index=True,
        height=height,
        column_config=_build_column_config(localized),
    )


def _artifact_download_payload(artifact: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(artifact, dict) or not artifact:
        return {}
    indicator_xs_path = str(artifact.get("best_indicator_xs_path") or artifact.get("best_xs_path") or "").strip()
    trade_xs_path = str(artifact.get("best_trade_xs_path") or "").strip()
    best_txt_path = str(artifact.get("best_txt_path") or "").strip()
    payload: dict[str, Any] = {}
    if indicator_xs_path and Path(indicator_xs_path).exists():
        payload["indicator_xs_bytes"] = Path(indicator_xs_path).read_bytes()
        payload["indicator_xs_file_name"] = Path(indicator_xs_path).name
    if trade_xs_path and Path(trade_xs_path).exists():
        payload["trade_xs_bytes"] = Path(trade_xs_path).read_bytes()
        payload["trade_xs_file_name"] = Path(trade_xs_path).name
    if best_txt_path and Path(best_txt_path).exists():
        payload["txt_bytes"] = Path(best_txt_path).read_bytes()
        payload["txt_file_name"] = Path(best_txt_path).name
    return payload


def _best_export_signature(
    *,
    top_df: pd.DataFrame,
    params_meta: list[dict[str, Any]],
    slip_per_side: float,
) -> str:
    if top_df.empty:
        return ""
    best_row = top_df.iloc[0].to_dict()
    signature_source = {
        "params": {
            str(meta["name"]): best_row.get(str(meta["name"]))
            for meta in params_meta
            if str(meta["name"]) in best_row
        },
        "slip_per_side": round(float(slip_per_side), 4),
    }
    return hashlib.sha1(
        json.dumps(signature_source, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _cached_best_export_payload(
    *,
    top_df: pd.DataFrame,
    params_meta: list[dict[str, Any]],
    xs_path: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    slip_per_side: float,
) -> dict[str, Any]:
    if top_df.empty:
        return {}

    signature = _best_export_signature(top_df=top_df, params_meta=params_meta, slip_per_side=slip_per_side)
    if not signature:
        return {}
    cache = st.session_state.get("mq01_live_export_cache")
    if isinstance(cache, dict) and cache.get("signature") == signature:
        return cache

    payload = build_best_export_payload(
        top_df=top_df,
        params_meta=params_meta,
        xs_path=xs_path,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        slip_per_side=slip_per_side,
    )
    if payload:
        st.session_state["mq01_live_export_cache"] = payload
    return payload


def _render_monitor_card(
    placeholder,
    *,
    system_snapshot: dict[str, Any],
    done: int,
    total: int,
    passed: int,
) -> None:
    with placeholder.container():
        st.subheader("監控")
        metric_cols = st.columns(2)
        metric_cols[0].metric("CPU 使用率", _format_percent(system_snapshot.get("cpu_pct"), digits=1))
        metric_cols[1].metric("記憶體使用率", _format_percent(system_snapshot.get("memory_pct"), digits=1))
        detail_cols = st.columns(2)
        configured_workers = int(system_snapshot.get("configured_workers") or 0)
        requested_workers = int(system_snapshot.get("requested_workers") or configured_workers or 0)
        worker_metric = _format_number(configured_workers, digits=0)
        if requested_workers > 0 and configured_workers > 0 and requested_workers != configured_workers:
            worker_metric = f"{configured_workers}/{requested_workers}"
        detail_cols[0].metric("工作程序", worker_metric)
        effective_cpu_count = int(system_snapshot.get("effective_cpu_count") or system_snapshot.get("logical_cpu_count") or 0)
        logical_cpu_count = int(system_snapshot.get("logical_cpu_count") or effective_cpu_count or 0)
        logical_metric = _format_number(effective_cpu_count, digits=0)
        if logical_cpu_count > 0 and effective_cpu_count > 0 and logical_cpu_count != effective_cpu_count:
            logical_metric = f"{effective_cpu_count}/{logical_cpu_count}"
        detail_cols[1].metric("邏輯核心", logical_metric)
        detail_bits: list[str] = []
        cpu_limit_pct = system_snapshot.get("cpu_limit_pct")
        if cpu_limit_pct not in (None, ""):
            detail_bits.append(f"CPU 上限 {int(cpu_limit_pct)}%")
        memory_limit_pct = system_snapshot.get("memory_limit_pct")
        if memory_limit_pct not in (None, ""):
            detail_bits.append(f"記憶體上限 {int(memory_limit_pct)}%")
        if system_snapshot.get("memory_used_gb") is not None and system_snapshot.get("memory_total_gb") is not None:
            detail_bits.append(
                f"記憶體 {_format_number(system_snapshot.get('memory_used_gb'), digits=1)}/{_format_number(system_snapshot.get('memory_total_gb'), digits=1)} GB"
            )
        if system_snapshot.get("worker_load_pct") is not None:
            detail_bits.append(f"工作程序/核心比 {_format_percent(system_snapshot.get('worker_load_pct'), digits=1)}")
        detail_bits.append(f"進度 {done:,}/{total:,}")
        detail_bits.append(f"通過 {passed:,}")
        st.caption(" | ".join(detail_bits))


def _render_best_card(
    placeholder,
    *,
    title: str,
    snapshot: dict[str, Any],
    empty_text: str,
) -> None:
    with placeholder.container():
        st.subheader(title)
        if not snapshot:
            st.caption(empty_text)
            return
        metric_cols = st.columns(2)
        metric_cols[0].metric("總報酬", _format_percent(snapshot.get("total_return")))
        metric_cols[1].metric("MDD", _format_percent(snapshot.get("mdd_pct")))
        detail_cols = st.columns(2)
        detail_cols[0].metric("交易數", _format_number(snapshot.get("n_trades"), digits=0))
        detail_cols[1].metric("分數", _format_number(snapshot.get("composite_score")))

        meta_bits: list[str] = []
        if snapshot.get("saved_at"):
            meta_bits.append(f"時間 {_format_saved_at(snapshot.get('saved_at'))}")
        if snapshot.get("optimization_mode"):
            meta_bits.append(str(snapshot.get("optimization_mode")))
        if snapshot.get("slip_per_side") is not None:
            meta_bits.append(f"slip {float(snapshot.get('slip_per_side')):.2f}")
        if snapshot.get("tested_count"):
            total_count = snapshot.get("total_count")
            if total_count:
                meta_bits.append(f"已測 {int(snapshot['tested_count']):,}/{int(total_count):,}")
            else:
                meta_bits.append(f"已測 {int(snapshot['tested_count']):,}")
        if snapshot.get("elapsed_seconds") is not None:
            meta_bits.append(f"耗時 {_format_duration(snapshot.get('elapsed_seconds'))}")
        if meta_bits:
            st.caption(" | ".join(meta_bits))

        st.caption(_format_param_text(snapshot.get("params") or {}))
        year_items = _year_return_items(snapshot)
        if year_items:
            st.caption("每年報酬率")
            for idx in range(0, len(year_items), 2):
                row_items = year_items[idx : idx + 2]
                year_cols = st.columns(2)
                for col, (year, value) in zip(year_cols, row_items):
                    year_text = str(year)
                    label = f"{year_text}年" if year_text.isdigit() else ("年報酬波動" if year_text.lower() == "std" else year_text)
                    try:
                        numeric_value = float(value)
                    except Exception:
                        color = "#D1D5DB"
                    else:
                        if year_text.isdigit():
                            if numeric_value > 0:
                                color = "#EF4444"
                            elif numeric_value < 0:
                                color = "#22C55E"
                            else:
                                color = "#D1D5DB"
                        else:
                            color = "#D1D5DB"
                    col.markdown(
                        (
                            "<div style='font-size:0.82rem; margin-bottom:0.25rem;'>"
                            f"{label}：<span style='color:{color};'>{_format_percent(value)}</span>"
                            "</div>"
                        ),
                        unsafe_allow_html=True,
                    )


def _render_summary_card(
    placeholder,
    *,
    mode_label: str,
    estimated_total: int,
    step_note: str,
    summary_lines: list[str],
    done: int,
    total: int,
    passed: int,
    fail_rows: list[dict[str, Any]],
) -> None:
    with placeholder.container():
        st.subheader("摘要")
        metric_cols = st.columns(2)
        metric_cols[0].metric("模式", mode_label)
        pass_rate = (passed / done * 100.0) if done > 0 else None
        metric_cols[1].metric("通過率", _format_percent(pass_rate, digits=1))

        progress_bits = [f"目前 {done:,}/{total:,}"]
        if estimated_total > 0:
            progress_bits.append(f"預估 {estimated_total:,}")
        st.caption(" | ".join(progress_bits))

        if step_note:
            st.write(step_note)
        for line in summary_lines[:5]:
            st.caption(str(line))
        if fail_rows:
            top_fail = fail_rows[0]
            st.caption(f"主要失敗：{top_fail['reason']} x{top_fail['count']}")


def _render_action_bar(
    placeholder,
    *,
    run_disabled: bool,
    stop_enabled: bool,
    export_payload: dict[str, Any],
    key_suffix: str,
) -> tuple[bool, bool]:
    with placeholder.container():
        action_cols = st.columns([1.05, 1.05, 1.0, 1.0, 1.0])
        run_clicked = action_cols[0].button(
            "開始最佳化",
            type="primary",
            width="stretch",
            disabled=run_disabled,
            key=f"mq01_run_button_{key_suffix}",
        )
        stop_clicked = action_cols[1].button(
            "停止存檔",
            width="stretch",
            disabled=not stop_enabled,
            key=f"mq01_stop_button_{key_suffix}",
        )
        action_cols[2].download_button(
            "輸出目前最佳 XS",
            data=export_payload.get("xs_bytes", b""),
            file_name=str(export_payload.get("xs_file_name") or "best_strategy.xs"),
            mime="text/plain",
            width="stretch",
            disabled=not bool(export_payload.get("xs_bytes")),
            key=f"mq01_download_xs_{key_suffix}",
        )
        action_cols[3].download_button(
            "輸出目前最佳 TXT",
            data=export_payload.get("txt_bytes", b""),
            file_name=str(export_payload.get("txt_file_name") or "best_strategy.txt"),
            mime="text/plain",
            width="stretch",
            disabled=not bool(export_payload.get("txt_bytes")),
            key=f"mq01_download_txt_{key_suffix}",
        )
    return bool(run_clicked), bool(stop_clicked)


def _number_input(
    *,
    label: str,
    key: str,
    value: int | float,
    value_type: str,
    step: int | float,
) -> int | float:
    if value_type == "int":
        return int(
            st.number_input(
                label,
                key=key,
                value=int(value),
                step=max(1, int(step)),
                format="%d",
                label_visibility="collapsed",
            )
        )
    return float(
        st.number_input(
            label,
            key=key,
            value=float(value),
            step=max(float(step), 0.01),
            format="%.4f",
            label_visibility="collapsed",
        )
    )


def _current_ui_specs(default_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for spec in default_specs:
        name = str(spec["name"])
        value_type = str(spec["type"])
        enabled = bool(st.session_state.get(f"mq01_enabled_{name}", spec["enabled"]))
        start_value = st.session_state.get(f"mq01_start_{name}", spec["start"])
        stop_value = st.session_state.get(f"mq01_stop_{name}", spec["stop"])
        step_value = st.session_state.get(f"mq01_step_{name}", spec["step"])
        specs.append(
            {
                **spec,
                "enabled": enabled,
                "start": int(start_value) if value_type == "int" else float(start_value),
                "stop": int(stop_value) if value_type == "int" else float(stop_value),
                "step": int(step_value) if value_type == "int" else float(step_value),
            }
        )
    return specs


def _apply_mode_enabled_defaults(mode: str, default_specs: list[dict[str, Any]]) -> None:
    state_key = "mq01_enabled_defaults_mode"
    if st.session_state.get(state_key) == mode:
        return
    enable_all = mode == "smart"
    for spec in default_specs:
        st.session_state[f"mq01_enabled_{spec['name']}"] = enable_all
    st.session_state[state_key] = mode


def _result_frame(payload: Any) -> pd.DataFrame:
    if isinstance(payload, pd.DataFrame):
        return payload
    return pd.DataFrame()


def _rows_frame(rows: Any) -> pd.DataFrame:
    if isinstance(rows, list):
        return pd.DataFrame(rows)
    return pd.DataFrame()


def _render_saved_results() -> None:
    artifact = st.session_state.get("mq01_last_artifacts")
    if not isinstance(artifact, dict) or not artifact:
        artifact = load_latest_artifact_snapshot()
    if isinstance(artifact, dict) and artifact:
        st.subheader("最佳輸出檔案")
        saved_at = artifact.get("saved_at")
        if saved_at:
            st.caption(f"最近輸出時間：{_format_saved_at(saved_at)}")
        export_dir = str(artifact.get("export_dir") or "").strip()
        best_xs_path = str(artifact.get("best_xs_path") or "").strip()
        best_txt_path = str(artifact.get("best_txt_path") or "").strip()
        if export_dir:
            st.code(export_dir, language="text")
        for label, path_text in [
            ("最佳 XS", best_xs_path),
            ("最佳 TXT", best_txt_path),
        ]:
            if not path_text:
                continue
            st.caption(f"{label}：{path_text}")
        download_cols = st.columns(2)
        if best_xs_path and Path(best_xs_path).exists():
            download_cols[0].download_button(
                "下載最佳 XS",
                data=Path(best_xs_path).read_bytes(),
                file_name=Path(best_xs_path).name,
                mime="text/plain",
                width="stretch",
            )
        if best_txt_path and Path(best_txt_path).exists():
            download_cols[1].download_button(
                "下載最佳 TXT",
                data=Path(best_txt_path).read_bytes(),
                file_name=Path(best_txt_path).name,
                mime="text/plain",
                width="stretch",
            )

    top_df = st.session_state.get("mq01_last_top_df")
    recent_df = st.session_state.get("mq01_last_recent_df")
    fail_rows = st.session_state.get("mq01_last_fail_rows", [])
    if isinstance(top_df, pd.DataFrame) and not top_df.empty:
        st.subheader("上次結果")
        _render_compact_dataframe(top_df, height=320)
    if isinstance(recent_df, pd.DataFrame) and not recent_df.empty:
        st.subheader("最近測試")
        _render_compact_dataframe(recent_df, height=320)
    if fail_rows:
        st.subheader("失敗原因統計")
        _render_compact_dataframe(pd.DataFrame(fail_rows), height=220)


def _saved_progress_state() -> dict[str, Any]:
    progress = st.session_state.get("mq01_last_progress")
    return dict(progress) if isinstance(progress, dict) else {}


def _save_run_checkpoint(
    *,
    done: int,
    total: int,
    passed: int,
    step_note: str,
    summary_lines: list[str],
    current_snapshot: dict[str, Any],
    top_df: pd.DataFrame,
    recent_df: pd.DataFrame,
    fail_rows: list[dict[str, Any]],
    narrative_lines: list[str],
    status: str,
) -> None:
    if not top_df.empty:
        st.session_state["mq01_last_top_df"] = top_df.copy()
    if not recent_df.empty:
        st.session_state["mq01_last_recent_df"] = recent_df.copy()
    st.session_state["mq01_last_fail_rows"] = list(fail_rows)
    st.session_state["mq01_last_progress"] = {
        "done": int(done),
        "total": int(total),
        "passed": int(passed),
        "step_note": str(step_note),
        "summary_lines": [str(line) for line in summary_lines],
        "current_snapshot": dict(current_snapshot),
        "narrative_lines": [str(line) for line in narrative_lines],
        "status": str(status),
    }


def _append_narrative_line(narrative_lines: list[str], *, step_note: str, summary_lines: list[str]) -> list[str]:
    detail = " ".join(str(line).strip() for line in summary_lines if str(line).strip())
    text = f"{step_note}：{detail}" if detail else step_note
    text = text.strip()
    if not text:
        return narrative_lines
    if narrative_lines and narrative_lines[-1] == text:
        return narrative_lines
    return [*narrative_lines, text]


def _render_narrative_box(placeholder, narrative_lines: list[str]) -> None:
    if not narrative_lines:
        placeholder.empty()
        return
    with placeholder.container():
        st.subheader("詳細摘要")
        st.code("\n".join(str(line) for line in narrative_lines), language="text", height=320, wrap_lines=True)


def render_app() -> None:
    st.set_page_config(page_title="MQQuant 01", layout="wide")
    st.title("MQQuant 01 - 固定策略參數最佳化")
    st.caption("這支程式只做固定策略參數搜尋，直接接 `src/optimize/gui_backend.py`，不碰 `gui_app.py`。")

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
        slip_per_side = float(
            st.number_input(
                "????",
                value=float(runtime_defaults["slip_per_side"]),
                min_value=0.0,
                step=0.1,
                format="%.2f",
            )
        )
        requested_workers = int(
            st.number_input("最大 workers", value=int(runtime_defaults["max_workers"]), min_value=1, step=1, format="%d")
        )
        cpu_limit_pct = int(
            st.number_input("CPU 使用率上限(%)", value=int(runtime_defaults["cpu_limit_pct"]), min_value=1, max_value=100, step=5, format="%d")
        )
        memory_limit_pct = int(
            st.number_input(
                "記憶體使用率上限(%)",
                value=int(runtime_defaults["memory_limit_pct"]),
                min_value=1,
                max_value=100,
                step=5,
                format="%d",
            )
        )
        effective_workers = resolve_effective_workers(
            requested_workers=requested_workers,
            cpu_limit_pct=cpu_limit_pct,
        )
        effective_cpu_count = max(1, int((os.cpu_count() or 1) * cpu_limit_pct / 100))
        st.caption(
            f"CPU 上限 {cpu_limit_pct}%｜記憶體上限 {memory_limit_pct}%｜可用核心 {effective_cpu_count}｜實際工作程序 {effective_workers}"
        )
        top_n = int(st.number_input("保留前幾名", value=int(runtime_defaults["top_n"]), min_value=1, step=1, format="%d"))
        seed_keep_count = int(
            st.number_input("每個參數保留前幾名", value=int(runtime_defaults["seed_keep_count"]), min_value=1, step=1, format="%d")
        )
        st.caption("兩種模式都會逐參數掃描；差別只在預設勾選方式。")

        st.subheader("硬條件")
        min_trades = int(
            st.number_input("最少交易數", value=int(hard_filter_defaults["min_trades"]), min_value=0, step=10, format="%d")
        )
        min_total_return = float(st.number_input("最低總報酬(%)", value=float(hard_filter_defaults["min_total_return"]), step=1.0))
        max_mdd_pct = float(st.number_input("最高 MDD(%)", value=float(hard_filter_defaults["max_mdd_pct"]), step=1.0))

    path_errors = [path for path in (xs_path, minute_path, daily_path, preset_path) if not Path(path).exists()]
    if path_errors:
        st.error("以下路徑不存在，請先修正：\n" + "\n".join(path_errors))
        _render_saved_results()
        return

    try:
        script_name, params_meta, default_specs = load_strategy_metadata(xs_path, preset_path)
    except Exception as exc:
        st.exception(exc)
        _render_saved_results()
        return
    _remember_param_label_map(params_meta)
    _apply_mode_enabled_defaults(mode, default_specs)
    param_names = [str(item["name"]) for item in params_meta]
    historical_snapshot = load_historical_best_snapshot(param_names, slip_per_side=slip_per_side)
    saved_top_df = _result_frame(st.session_state.get("mq01_last_top_df"))
    saved_fail_rows = list(st.session_state.get("mq01_last_fail_rows", []))
    saved_progress = _saved_progress_state()
    saved_done = int(saved_progress.get("done") or 0)
    saved_total = int(saved_progress.get("total") or 0)
    saved_passed = int(saved_progress.get("passed") or 0)
    saved_status = str(saved_progress.get("status") or "")
    saved_step_note = str(saved_progress.get("step_note") or "")
    saved_summary_lines = [str(line) for line in (saved_progress.get("summary_lines") or [])]
    narrative_lines = [str(line) for line in (saved_progress.get("narrative_lines") or [])]
    saved_current_snapshot = saved_progress.get("current_snapshot")
    if not isinstance(saved_current_snapshot, dict):
        saved_current_snapshot = {}

    config_placeholder = st.empty()
    config_hidden = bool(st.session_state.get("mq01_hide_config", False))
    if config_hidden:
        if st.button("顯示參數設定", width="content"):
            st.session_state["mq01_hide_config"] = False
            st.rerun()

    with config_placeholder.container():
        if not config_hidden:
            st.subheader("策略與參數")
            if mode == "cycle":
                st.caption(f"目前來源策略：`{script_name}`。單參數循環預設全部不勾，請手動挑要掃描的參數。")
            else:
                st.caption(f"目前來源策略：`{script_name}`。智慧搜尋預設全部勾選，但跑法仍是逐參數循環。")

            header_cols = st.columns([1.0, 1.8, 1.4, 1.4, 1.2, 0.8])
            header_cols[0].markdown("**啟用**")
            header_cols[1].markdown("**參數**")
            header_cols[2].markdown("**起點**")
            header_cols[3].markdown("**終點**")
            header_cols[4].markdown("**步距**")
            header_cols[5].markdown("**型別**")

            for spec in default_specs:
                name = str(spec["name"])
                value_type = str(spec["type"])
                row_cols = st.columns([1.0, 1.8, 1.4, 1.4, 1.2, 0.8])
                row_cols[0].checkbox(
                    "啟用",
                    value=bool(spec["enabled"]),
                    key=f"mq01_enabled_{name}",
                    label_visibility="collapsed",
                )
                row_cols[1].markdown(f"**{name}**  \n{spec['label']}")
                with row_cols[2]:
                    _number_input(
                        label="起點",
                        key=f"mq01_start_{name}",
                        value=spec["start"],
                        value_type=value_type,
                        step=spec["step"],
                    )
                with row_cols[3]:
                    _number_input(
                        label="終點",
                        key=f"mq01_stop_{name}",
                        value=spec["stop"],
                        value_type=value_type,
                        step=spec["step"],
                    )
                with row_cols[4]:
                    _number_input(
                        label="步距",
                        key=f"mq01_step_{name}",
                        value=spec["step"],
                        value_type=value_type,
                        step=spec["step"] if value_type == "float" else 1,
                    )
                row_cols[5].write(TYPE_VALUE_LABELS.get(value_type, value_type))

    ui_specs = _current_ui_specs(default_specs)

    try:
        estimated_total = estimate_run_count(
            mode,
            ui_specs,
            params_meta,
            seed_keep_count=seed_keep_count,
        )
    except Exception as exc:
        estimated_total = 0
        if not config_hidden:
            st.warning(f"目前參數設定還不能估算搜尋量：{exc}")
    else:
        if not config_hidden:
            st.info(f"每一輪至少會跑 **{estimated_total:,}** 組；會持續循環到各參數保留名單穩定為止。")
    run_block_reason = grid_run_block_reason(mode, estimated_total)
    if run_block_reason and not config_hidden:
        st.error(run_block_reason)
    initial_done = saved_done if saved_done > 0 else 0
    initial_total = max(saved_total or estimated_total or 0, 1)
    initial_passed = saved_passed if saved_done > 0 else 0
    initial_step_note = saved_step_note or "按下「開始最佳化」後，這裡會即時更新目前步驟。"
    initial_summary_lines = saved_summary_lines
    if saved_status == "running" and saved_done > 0:
        initial_summary_lines = ["偵測到上次執行中斷，以下是最後一次成功刷新時的畫面資料。"] + initial_summary_lines[:2]

    artifact_snapshot = st.session_state.get("mq01_last_artifacts")
    if not isinstance(artifact_snapshot, dict) or not artifact_snapshot:
        artifact_snapshot = load_latest_artifact_snapshot()
    action_export_payload = _cached_best_export_payload(
        top_df=saved_top_df,
        params_meta=params_meta,
        xs_path=xs_path,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        slip_per_side=slip_per_side,
    )
    if not action_export_payload:
        action_export_payload = _artifact_download_payload(artifact_snapshot)

    action_placeholder = st.empty()
    run_button = _render_action_bar(
        action_placeholder,
        run_disabled=bool(run_block_reason),
        stop_enabled=False,
        export_payload=action_export_payload,
        key_suffix="idle",
    )

    dashboard_cols = st.columns(4)
    monitor_placeholder = dashboard_cols[0].empty()
    history_placeholder = dashboard_cols[1].empty()
    current_placeholder = dashboard_cols[2].empty()
    digest_placeholder = dashboard_cols[3].empty()
    narrative_placeholder = st.empty()
    _render_monitor_card(
        monitor_placeholder,
        system_snapshot=collect_system_snapshot(
            max_workers=effective_workers,
            requested_workers=requested_workers,
            cpu_limit_pct=cpu_limit_pct,
            memory_limit_pct=memory_limit_pct,
        ),
        done=initial_done,
        total=initial_total,
        passed=initial_passed,
    )
    _render_best_card(
        history_placeholder,
        title="歷史最佳化",
        snapshot=historical_snapshot,
        empty_text="尚未找到歷史最佳化記錄。",
    )
    _render_best_card(
        current_placeholder,
        title="目前最佳化",
        snapshot=saved_current_snapshot,
        empty_text="尚未開始本輪最佳化。",
    )
    _render_summary_card(
        digest_placeholder,
        mode_label=MODE_OPTIONS[mode],
        estimated_total=estimated_total,
        step_note=initial_step_note,
        summary_lines=initial_summary_lines,
        done=initial_done,
        total=initial_total,
        passed=initial_passed,
        fail_rows=saved_fail_rows,
    )
    _render_narrative_box(narrative_placeholder, narrative_lines)

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
    }
    hard_filters = {
        "min_trades": min_trades,
        "min_total_return": min_total_return,
        "max_mdd_pct": max_mdd_pct,
    }

    if run_button:
        st.session_state["mq01_hide_config"] = True
        config_placeholder.empty()
        action_placeholder.empty()
        _render_action_bar(
            action_placeholder,
            run_disabled=True,
            stop_enabled=True,
            export_payload=action_export_payload,
            key_suffix="running",
        )
        progress_box = st.empty()
        summary_box = st.empty()
        metric_cols = [col.empty() for col in st.columns(5)]
        top_title = st.empty()
        top_table = st.empty()
        recent_title = st.empty()
        recent_table = st.empty()
        fail_title = st.empty()
        fail_table = st.empty()

        final_top_df = pd.DataFrame()
        final_recent_df = pd.DataFrame()
        final_fail_rows: list[dict[str, Any]] = []
        last_done = 0
        last_total = max(estimated_total, 1)
        last_passed = 0
        last_elapsed = 0.0
        last_compute_elapsed = 0.0
        last_transition_elapsed = 0.0
        current_snapshot: dict[str, Any] = {}
        narrative_lines = _append_narrative_line([], step_note="最佳化準備中", summary_lines=["已開始執行，等待第一批結果回傳。"])
        _render_narrative_box(narrative_placeholder, narrative_lines)
        _save_run_checkpoint(
            done=0,
            total=last_total,
            passed=0,
            step_note="最佳化準備中",
            summary_lines=["已開始執行，等待第一批結果回傳。"],
            current_snapshot={},
            top_df=final_top_df,
            recent_df=final_recent_df,
            fail_rows=final_fail_rows,
            narrative_lines=narrative_lines,
            status="running",
        )

        try:
            for update in run_optimizer(
                mode=mode,
                ui_param_specs=ui_specs,
                params_meta=params_meta,
                runtime_settings=runtime_settings,
                hard_filters=hard_filters,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
            ):
                done = int(update.get("done") or 0)
                total = max(int(update.get("total") or 0), 1)
                passed = int(update.get("passed") or 0)
                last_done = done
                last_total = total
                last_passed = passed
                elapsed = float(update.get("elapsed") or 0.0)
                compute_elapsed = float(update.get("compute_elapsed") or 0.0)
                transition_elapsed = float(update.get("transition_elapsed") or 0.0)
                last_elapsed = elapsed
                last_compute_elapsed = compute_elapsed
                last_transition_elapsed = transition_elapsed

                top_df = _result_frame(update.get("top_df"))
                recent_df = _result_frame(update.get("recent_trials_df"))
                fail_counts = dict(update.get("fail_reason_counts") or {})
                if not top_df.empty:
                    final_top_df = top_df
                if not recent_df.empty:
                    final_recent_df = recent_df
                if fail_counts:
                    final_fail_rows = [
                        {"reason": key, "count": value}
                        for key, value in sorted(fail_counts.items(), key=lambda item: item[1], reverse=True)
                    ]

                current_snapshot = build_current_best_snapshot(final_top_df, param_names, slip_per_side=slip_per_side)
                _render_monitor_card(
                    monitor_placeholder,
                    system_snapshot=collect_system_snapshot(
                        max_workers=effective_workers,
                        requested_workers=requested_workers,
                        cpu_limit_pct=cpu_limit_pct,
                        memory_limit_pct=memory_limit_pct,
                    ),
                    done=done,
                    total=total,
                    passed=passed,
                )
                _render_best_card(
                    history_placeholder,
                    title="歷史最佳化",
                    snapshot=historical_snapshot,
                    empty_text="尚未找到歷史最佳化記錄。",
                )
                _render_best_card(
                    current_placeholder,
                    title="目前最佳化",
                    snapshot=current_snapshot,
                    empty_text="尚未有通過硬條件的目前最佳結果。",
                )

                progress_box.progress(min(done / total, 1.0), text=f"已完成 {done:,} / {total:,}")
                metric_cols[0].metric("已完成", f"{done:,}")
                metric_cols[1].metric("通過硬條件", f"{passed:,}")
                metric_cols[2].metric("總耗時", _format_duration(elapsed))
                metric_cols[3].metric("計算耗時", _format_duration(compute_elapsed))
                metric_cols[4].metric("切換耗時", _format_duration(transition_elapsed))

                summary_lines = update.get("summary_lines") or []
                summary_text = "\n".join(str(line) for line in summary_lines) if summary_lines else str(update.get("step_note") or "最佳化中")
                summary_box.info(summary_text)
                if update.get("row") is None:
                    narrative_lines = _append_narrative_line(
                        narrative_lines,
                        step_note=str(update.get("step_note") or "最佳化中"),
                        summary_lines=[str(line) for line in summary_lines],
                    )
                    _render_narrative_box(narrative_placeholder, narrative_lines)
                _render_summary_card(
                    digest_placeholder,
                    mode_label=MODE_OPTIONS[mode],
                    estimated_total=estimated_total,
                    step_note=str(update.get("step_note") or "最佳化中"),
                    summary_lines=[str(line) for line in summary_lines],
                    done=done,
                    total=total,
                    passed=passed,
                    fail_rows=final_fail_rows,
                )
                _save_run_checkpoint(
                    done=done,
                    total=total,
                    passed=passed,
                    step_note=str(update.get("step_note") or "最佳化中"),
                    summary_lines=[str(line) for line in summary_lines],
                    current_snapshot=current_snapshot,
                    top_df=final_top_df,
                    recent_df=final_recent_df,
                    fail_rows=final_fail_rows,
                    narrative_lines=narrative_lines,
                    status="running",
                )

                if not final_top_df.empty:
                    top_title.subheader("目前 Top 結果")
                    with top_table.container():
                        _render_compact_dataframe(final_top_df, height=360)
                if not final_recent_df.empty:
                    recent_title.subheader("最近試算")
                    with recent_table.container():
                        _render_compact_dataframe(final_recent_df, height=360)
                if final_fail_rows:
                    fail_title.subheader("失敗原因統計")
                    with fail_table.container():
                        _render_compact_dataframe(pd.DataFrame(final_fail_rows), height=220)
        except Exception as exc:
            narrative_lines = _append_narrative_line(
                narrative_lines,
                step_note="本輪最佳化已中斷。",
                summary_lines=[str(exc)],
            )
            _render_narrative_box(narrative_placeholder, narrative_lines)
            _save_run_checkpoint(
                done=last_done,
                total=last_total,
                passed=last_passed,
                step_note="本輪最佳化已中斷。",
                summary_lines=[str(exc)],
                current_snapshot=current_snapshot,
                top_df=final_top_df,
                recent_df=final_recent_df,
                fail_rows=final_fail_rows,
                narrative_lines=narrative_lines,
                status="interrupted",
            )
            st.exception(exc)
        else:
            fail_reason_counts = {str(item["reason"]): int(item["count"]) for item in final_fail_rows if "reason" in item and "count" in item}
            artifact_payload: dict[str, Any] = {}
            artifact_exception: Exception | None = None
            try:
                artifact_payload = persist_best_run(
                    top_df=final_top_df,
                    params_meta=params_meta,
                    xs_path=xs_path,
                    minute_path=minute_path,
                    daily_path=daily_path,
                    script_name=script_name,
                    mode_label=MODE_OPTIONS[mode],
                    runtime_settings=runtime_settings,
                    hard_filters=hard_filters,
                    tested_count=last_done,
                    total_count=last_total,
                    elapsed_seconds=last_elapsed,
                    compute_elapsed_seconds=last_compute_elapsed,
                    transition_elapsed_seconds=last_transition_elapsed,
                    fail_reason_counts=fail_reason_counts,
                )
            except Exception as exc:
                artifact_exception = exc
            if artifact_payload:
                st.session_state["mq01_last_artifacts"] = artifact_payload
                historical_snapshot = load_historical_best_snapshot(param_names, slip_per_side=slip_per_side)
                _render_best_card(
                    history_placeholder,
                    title="歷史最佳化",
                    snapshot=historical_snapshot,
                    empty_text="尚未找到歷史最佳化記錄。",
                )
                st.success("最佳化已完成，並已輸出最佳 XS / TXT。")
                if artifact_payload.get("artifact_error"):
                    st.warning(f"交易明細匯出時發生問題：{artifact_payload['artifact_error']}")
            else:
                if final_top_df.empty:
                    st.warning("最佳化已完成，但沒有通過硬條件的結果，因此沒有輸出最佳 XS / TXT。")
                elif artifact_exception is not None:
                    st.warning(f"最佳化已完成，但輸出最佳 XS / TXT 時失敗：{artifact_exception}")
                else:
                    st.warning("最佳化已完成，但最佳結果檔案尚未成功輸出。")
            _render_summary_card(
                digest_placeholder,
                mode_label=MODE_OPTIONS[mode],
                estimated_total=estimated_total,
                step_note="本輪最佳化已完成。",
                summary_lines=["結果已保存在本頁下方，重點輸出為最佳 XS / TXT。"],
                done=last_done,
                total=last_total,
                passed=last_passed,
                fail_rows=final_fail_rows,
            )
            narrative_lines = _append_narrative_line(
                narrative_lines,
                step_note="本輪最佳化已完成。",
                summary_lines=["結果已保存在本頁下方，重點輸出為最佳 XS / TXT。"],
            )
            _render_narrative_box(narrative_placeholder, narrative_lines)
            _save_run_checkpoint(
                done=last_done,
                total=last_total,
                passed=last_passed,
                step_note="本輪最佳化已完成。",
                summary_lines=["結果已保存在本頁下方，重點輸出為最佳 XS / TXT。"],
                current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=slip_per_side),
                top_df=final_top_df,
                recent_df=final_recent_df,
                fail_rows=final_fail_rows,
                narrative_lines=narrative_lines,
                status="completed",
            )
        refreshed_export_payload = _cached_best_export_payload(
            top_df=final_top_df if not final_top_df.empty else saved_top_df,
            params_meta=params_meta,
            xs_path=xs_path,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            slip_per_side=slip_per_side,
        )
        if not refreshed_export_payload:
            latest_artifact_snapshot = st.session_state.get("mq01_last_artifacts")
            if not isinstance(latest_artifact_snapshot, dict) or not latest_artifact_snapshot:
                latest_artifact_snapshot = load_latest_artifact_snapshot()
            refreshed_export_payload = _artifact_download_payload(latest_artifact_snapshot)
        action_placeholder.empty()
        _render_action_bar(
            action_placeholder,
            run_disabled=bool(run_block_reason),
            stop_enabled=False,
            export_payload=refreshed_export_payload,
            key_suffix="after_run",
        )

    _render_saved_results()
