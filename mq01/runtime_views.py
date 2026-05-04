from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st


def artifact_download_payload(artifact: dict[str, Any]) -> dict[str, Any]:
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


def render_action_bar(
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
            "輸出目前最佳 指標XS",
            data=export_payload.get("indicator_xs_bytes", b""),
            file_name=str(export_payload.get("indicator_xs_file_name") or "best_indicator.xs"),
            mime="text/plain",
            width="stretch",
            disabled=not bool(export_payload.get("indicator_xs_bytes")),
            key=f"mq01_download_indicator_xs_{key_suffix}",
        )
        action_cols[3].download_button(
            "輸出目前最佳 交易XS",
            data=export_payload.get("trade_xs_bytes", b""),
            file_name=str(export_payload.get("trade_xs_file_name") or "best_trade.xs"),
            mime="text/plain",
            width="stretch",
            disabled=not bool(export_payload.get("trade_xs_bytes")),
            key=f"mq01_download_trade_xs_{key_suffix}",
        )
        action_cols[4].download_button(
            "輸出目前最佳 逐筆TXT",
            data=export_payload.get("txt_bytes", b""),
            file_name=str(export_payload.get("txt_file_name") or "best_strategy.txt"),
            mime="text/plain",
            width="stretch",
            disabled=not bool(export_payload.get("txt_bytes")),
            key=f"mq01_download_txt_{key_suffix}",
        )
    return bool(run_clicked), bool(stop_clicked)


def render_saved_results() -> None:
    return None
