from __future__ import annotations

import sys
import traceback
from typing import Any

import pandas as pd

from .bootstrap import bootstrap_source_root
from .job_store import read_job_request, stop_requested, write_job_state

sys.dont_write_bytecode = True
bootstrap_source_root()

from .services import build_current_best_snapshot, persist_best_run, run_optimizer


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def _df_rows(df: pd.DataFrame, *, limit: int) -> list[dict[str, Any]]:
    if df.empty:
        return []
    limited = df.head(limit).copy()
    return [{str(key): _json_safe(value) for key, value in row.items()} for row in limited.to_dict("records")]


def _append_narrative_line(narrative_lines: list[str], *, step_note: str, summary_lines: list[str]) -> list[str]:
    detail = " ".join(str(line).strip() for line in summary_lines if str(line).strip())
    text = f"{step_note}：{detail}" if detail else step_note
    text = text.strip()
    if not text:
        return narrative_lines
    if narrative_lines and narrative_lines[-1] == text:
        return narrative_lines
    return [*narrative_lines, text]


def _build_live_run_summary(
    *,
    request: dict[str, Any],
    done: int,
    total: int,
    passed: int,
    step_note: str,
    summary_lines: list[str],
    current_snapshot: dict[str, Any],
    elapsed_seconds: float,
    eta_seconds: float,
    fail_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime_settings = dict(request.get("runtime_settings") or {})
    best_params = current_snapshot.get("params") if isinstance(current_snapshot, dict) else {}
    best_params_summary = ""
    if isinstance(best_params, dict) and best_params:
        preview = list(best_params.items())[:5]
        best_params_summary = " / ".join(f"{key}={value}" for key, value in preview)
        if len(best_params) > len(preview):
            best_params_summary += " / ..."
    top_fail_reason = str(fail_rows[0].get("reason") or "") if fail_rows else ""
    primary_score = current_snapshot.get("robust_score", current_snapshot.get("composite_score"))
    return {
        "phase": "during",
        "mode_label": str(request.get("mode_label") or request.get("mode") or "智慧搜尋"),
        "development_period": f"{runtime_settings.get('development_start_date')} ~ {runtime_settings.get('development_end_date')}",
        "holdout_period": f"{runtime_settings.get('holdout_start_date')} ~ {runtime_settings.get('holdout_end_date')}",
        "tested_count": int(done),
        "estimated_count": int(total),
        "passed_count": int(passed),
        "pass_rate_pct": (float(passed) / float(total) * 100.0) if int(total) > 0 else 0.0,
        "elapsed_seconds": float(elapsed_seconds),
        "eta_seconds": float(eta_seconds),
        "current_step": str(step_note or ""),
        "current_best_score": primary_score,
        "current_best_return": current_snapshot.get("total_return"),
        "current_best_mdd": current_snapshot.get("mdd_pct"),
        "current_best_trades": current_snapshot.get("n_trades"),
        "current_best_params": best_params_summary,
        "top_fail_reason": top_fail_reason,
        "summary_lines": [str(line) for line in summary_lines[:5]],
    }


def _build_post_run_summary(
    *,
    request: dict[str, Any],
    status: str,
    done: int,
    total: int,
    passed: int,
    current_snapshot: dict[str, Any],
    fail_rows: list[dict[str, Any]],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    runtime_settings = dict(request.get("runtime_settings") or {})
    best_params = current_snapshot.get("params") if isinstance(current_snapshot, dict) else {}
    best_params_summary = ""
    if isinstance(best_params, dict) and best_params:
        preview = list(best_params.items())[:6]
        best_params_summary = " / ".join(f"{key}={value}" for key, value in preview)
        if len(best_params) > len(preview):
            best_params_summary += " / ..."
    top_fail_reason = str(fail_rows[0].get("reason") or "") if fail_rows else ""
    primary_score = current_snapshot.get("robust_score", current_snapshot.get("composite_score"))
    return {
        "phase": "after",
        "status": str(status or ""),
        "mode_label": str(request.get("mode_label") or request.get("mode") or "智慧搜尋"),
        "development_period": f"{runtime_settings.get('development_start_date')} ~ {runtime_settings.get('development_end_date')}",
        "holdout_period": f"{runtime_settings.get('holdout_start_date')} ~ {runtime_settings.get('holdout_end_date')}",
        "tested_count": int(done),
        "estimated_count": int(total),
        "passed_count": int(passed),
        "best_score": primary_score,
        "best_return": current_snapshot.get("total_return"),
        "best_mdd": current_snapshot.get("mdd_pct"),
        "best_trades": current_snapshot.get("n_trades"),
        "best_params": best_params_summary,
        "top_fail_reason": top_fail_reason,
        "artifact_summary_json": str(artifact.get("summary_json_path") or ""),
        "artifact_run_dir": str(artifact.get("run_dir") or artifact.get("export_dir") or ""),
    }


def _write_progress_state(
    *,
    job_id: str,
    status: str,
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
    elapsed_seconds: float = 0.0,
    compute_elapsed_seconds: float = 0.0,
    transition_elapsed_seconds: float = 0.0,
    eta_seconds: float = 0.0,
    artifact: dict[str, Any] | None = None,
    error_text: str = "",
    pre_run_summary: dict[str, Any] | None = None,
    live_run_summary: dict[str, Any] | None = None,
    post_run_summary: dict[str, Any] | None = None,
) -> None:
    top_rows_limit = 200 if isinstance(top_df, pd.DataFrame) and "mdd_pass_thresholds" in top_df.columns else 10
    write_job_state(
        job_id,
        {
            "status": status,
            "done": int(done),
            "total": int(total),
            "passed": int(passed),
            "step_note": str(step_note),
            "summary_lines": [str(line) for line in summary_lines],
            "current_snapshot": dict(current_snapshot),
            "top_rows": _df_rows(top_df, limit=top_rows_limit),
            "recent_rows": _df_rows(recent_df, limit=30),
            "fail_rows": [{str(key): _json_safe(value) for key, value in row.items()} for row in fail_rows],
            "narrative_lines": [str(line) for line in narrative_lines],
            "elapsed_seconds": float(elapsed_seconds),
            "compute_elapsed_seconds": float(compute_elapsed_seconds),
            "transition_elapsed_seconds": float(transition_elapsed_seconds),
            "eta_seconds": float(eta_seconds),
            "artifact": dict(artifact or {}),
            "error": error_text,
            "pre_run_summary": dict(pre_run_summary or {}),
            "live_run_summary": dict(live_run_summary or {}),
            "post_run_summary": dict(post_run_summary or {}),
        },
    )


def run_background_job(job_id: str) -> int:
    request = read_job_request(job_id)
    if not request:
        write_job_state(
            job_id,
            {
                "status": "error",
                "step_note": "缺少工作請求",
                "error": "missing request",
            },
        )
        return 1

    params_meta = list(request.get("params_meta") or [])
    param_names = [str(item["name"]) for item in params_meta if isinstance(item, dict) and "name" in item]
    runtime_settings = dict(request.get("runtime_settings") or {})
    hard_filters = dict(request.get("hard_filters") or {})
    pre_run_summary = dict(request.get("pre_run_summary") or {})

    final_top_df = pd.DataFrame()
    final_recent_df = pd.DataFrame()
    final_fail_rows: list[dict[str, Any]] = []
    current_snapshot: dict[str, Any] = {}
    narrative_lines = _append_narrative_line([], step_note="背景工作已啟動", summary_lines=["等待第一批最佳化結果。"])
    last_done = 0
    last_total = int(request.get("estimated_total") or 1)
    last_passed = 0
    last_elapsed = 0.0
    last_compute_elapsed = 0.0
    last_transition_elapsed = 0.0

    _write_progress_state(
        job_id=job_id,
        status="running",
        done=0,
        total=last_total,
        passed=0,
        step_note="背景工作準備中",
        summary_lines=["背景工作已啟動，等待第一批結果。"],
        current_snapshot={},
        top_df=final_top_df,
        recent_df=final_recent_df,
        fail_rows=final_fail_rows,
        narrative_lines=narrative_lines,
        pre_run_summary=pre_run_summary,
        live_run_summary=_build_live_run_summary(
            request=request,
            done=0,
            total=last_total,
            passed=0,
            step_note="開始準備研究工作",
            summary_lines=["已接收研究請求，正在初始化資料與工作程序。"],
            current_snapshot={},
            elapsed_seconds=0.0,
            eta_seconds=0.0,
            fail_rows=final_fail_rows,
        ),
    )

    stop_after_checkpoint = False
    try:
        for update in run_optimizer(
            mode=str(request.get("mode") or "smart"),
            ui_param_specs=list(request.get("ui_param_specs") or []),
            params_meta=params_meta,
            runtime_settings=runtime_settings,
            hard_filters=hard_filters,
            minute_path=str(request.get("minute_path") or ""),
            daily_path=str(request.get("daily_path") or ""),
            script_name=str(request.get("script_name") or "0313plus"),
        ):
            last_done = int(update.get("done") or 0)
            last_total = max(int(update.get("total") or 0), 1)
            last_passed = int(update.get("passed") or 0)
            last_elapsed = float(update.get("elapsed") or 0.0)
            last_compute_elapsed = float(update.get("compute_elapsed") or 0.0)
            last_transition_elapsed = float(update.get("transition_elapsed") or 0.0)

            top_df = update.get("top_df")
            if isinstance(top_df, pd.DataFrame) and not top_df.empty:
                final_top_df = top_df.copy()
            recent_df = update.get("recent_trials_df")
            if isinstance(recent_df, pd.DataFrame) and not recent_df.empty:
                final_recent_df = recent_df.copy()

            fail_counts = dict(update.get("fail_reason_counts") or {})
            if fail_counts:
                final_fail_rows = [
                    {"reason": key, "count": value}
                    for key, value in sorted(fail_counts.items(), key=lambda item: item[1], reverse=True)
                ]

            current_snapshot = build_current_best_snapshot(
                final_top_df,
                param_names,
                slip_per_side=runtime_settings.get("slip_per_side"),
            )
            step_note = str(update.get("step_note") or "背景工作執行中")
            summary_lines = [str(line) for line in (update.get("summary_lines") or [])]

            if update.get("row") is None:
                narrative_lines = _append_narrative_line(
                    narrative_lines,
                    step_note=step_note,
                    summary_lines=summary_lines,
                )

            _write_progress_state(
                job_id=job_id,
                status="running",
                done=last_done,
                total=last_total,
                passed=last_passed,
                step_note=step_note,
                summary_lines=summary_lines,
                current_snapshot=current_snapshot,
                top_df=final_top_df,
                recent_df=final_recent_df,
                fail_rows=final_fail_rows,
                narrative_lines=narrative_lines,
                elapsed_seconds=last_elapsed,
                compute_elapsed_seconds=last_compute_elapsed,
                transition_elapsed_seconds=last_transition_elapsed,
                eta_seconds=float(update.get("eta") or 0.0),
                pre_run_summary=pre_run_summary,
                live_run_summary=_build_live_run_summary(
                    request=request,
                    done=last_done,
                    total=last_total,
                    passed=last_passed,
                    step_note=step_note,
                    summary_lines=summary_lines,
                    current_snapshot=current_snapshot,
                    elapsed_seconds=last_elapsed,
                    eta_seconds=float(update.get("eta") or 0.0),
                    fail_rows=final_fail_rows,
                ),
            )

            if stop_requested(job_id):
                stop_after_checkpoint = True
                break

    except Exception as exc:
        traceback_text = traceback.format_exc()
        narrative_lines = _append_narrative_line(
            narrative_lines,
            step_note="背景工作發生錯誤",
            summary_lines=[str(exc)],
        )
        _write_progress_state(
            job_id=job_id,
            status="error",
            done=last_done,
            total=last_total,
            passed=last_passed,
            step_note="背景工作發生錯誤",
            summary_lines=[str(exc)],
            current_snapshot=current_snapshot,
            top_df=final_top_df,
            recent_df=final_recent_df,
            fail_rows=final_fail_rows,
            narrative_lines=narrative_lines,
            elapsed_seconds=last_elapsed,
            compute_elapsed_seconds=last_compute_elapsed,
            transition_elapsed_seconds=last_transition_elapsed,
            eta_seconds=0.0,
            error_text=str(exc),
            pre_run_summary=pre_run_summary,
            live_run_summary=_build_live_run_summary(
                request=request,
                done=last_done,
                total=last_total,
                passed=last_passed,
                step_note="研究執行錯誤",
                summary_lines=[str(exc)],
                current_snapshot=current_snapshot,
                elapsed_seconds=last_elapsed,
                eta_seconds=0.0,
                fail_rows=final_fail_rows,
            ),
            post_run_summary=_build_post_run_summary(
                request=request,
                status="error",
                done=last_done,
                total=last_total,
                passed=last_passed,
                current_snapshot=current_snapshot,
                fail_rows=final_fail_rows,
                artifact={},
            ),
        )
        write_job_state(job_id, {"error_traceback": traceback_text})
        return 1

    fail_reason_counts = {
        str(item["reason"]): int(item["count"])
        for item in final_fail_rows
        if isinstance(item, dict) and "reason" in item and "count" in item
    }
    artifact_payload: dict[str, Any] = {}
    if not final_top_df.empty:
        try:
            artifact_payload = persist_best_run(
                top_df=final_top_df,
                params_meta=params_meta,
                xs_path=str(request.get("xs_path") or ""),
                minute_path=str(request.get("minute_path") or ""),
                daily_path=str(request.get("daily_path") or ""),
                script_name=str(request.get("script_name") or "0313plus"),
                mode_label=str(request.get("mode_label") or request.get("mode") or "智慧搜尋"),
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
            artifact_payload = {"artifact_error": str(exc)}

    if stop_after_checkpoint:
        summary_lines = [
            "已依要求停止背景工作。",
            "目前最佳結果已整理完成，可使用上方按鈕下載 XS / TXT。",
        ]
        step_note = "背景工作已停止"
        if final_top_df.empty:
            summary_lines[1] = "目前尚無通過硬條件的最佳結果，因此沒有可存檔內容。"
        elif artifact_payload.get("historical_best_updated"):
            summary_lines.append("本輪結果已刷新歷史最佳總報酬，歷史最佳化已同步更新。")

        narrative_lines = _append_narrative_line(
            narrative_lines,
            step_note=step_note,
            summary_lines=summary_lines,
        )
        _write_progress_state(
            job_id=job_id,
            status="stopped",
            done=last_done,
            total=last_total,
            passed=last_passed,
            step_note=step_note,
            summary_lines=summary_lines,
            current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=runtime_settings.get("slip_per_side")),
            top_df=final_top_df,
            recent_df=final_recent_df,
            fail_rows=final_fail_rows,
            narrative_lines=narrative_lines,
            elapsed_seconds=last_elapsed,
            compute_elapsed_seconds=last_compute_elapsed,
            transition_elapsed_seconds=last_transition_elapsed,
            eta_seconds=0.0,
            artifact=artifact_payload,
            pre_run_summary=pre_run_summary,
            live_run_summary=_build_live_run_summary(
                request=request,
                done=last_done,
                total=last_total,
                passed=last_passed,
                step_note=step_note,
                summary_lines=summary_lines,
                current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=runtime_settings.get("slip_per_side")),
                elapsed_seconds=last_elapsed,
                eta_seconds=0.0,
                fail_rows=final_fail_rows,
            ),
            post_run_summary=_build_post_run_summary(
                request=request,
                status="stopped",
                done=last_done,
                total=last_total,
                passed=last_passed,
                current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=runtime_settings.get("slip_per_side")),
                fail_rows=final_fail_rows,
                artifact=artifact_payload,
            ),
        )
        return 0

    summary_lines = [
        "背景最佳化已完成。",
        "結果已保存完成，可直接下載最佳 XS / TXT。",
    ]
    if artifact_payload.get("historical_best_updated"):
        summary_lines.append("本輪結果已刷新歷史最佳總報酬，歷史最佳化已同步更新。")
    narrative_lines = _append_narrative_line(
        narrative_lines,
        step_note="背景最佳化已完成",
        summary_lines=summary_lines,
    )
    _write_progress_state(
        job_id=job_id,
        status="completed",
        done=last_done,
        total=last_total,
        passed=last_passed,
        step_note="背景最佳化已完成",
        summary_lines=summary_lines,
        current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=runtime_settings.get("slip_per_side")),
        top_df=final_top_df,
        recent_df=final_recent_df,
        fail_rows=final_fail_rows,
        narrative_lines=narrative_lines,
        elapsed_seconds=last_elapsed,
        compute_elapsed_seconds=last_compute_elapsed,
        transition_elapsed_seconds=last_transition_elapsed,
        eta_seconds=0.0,
        artifact=artifact_payload,
        pre_run_summary=pre_run_summary,
        live_run_summary=_build_live_run_summary(
            request=request,
            done=last_done,
            total=last_total,
            passed=last_passed,
            step_note="研究執行完成",
            summary_lines=summary_lines,
            current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=runtime_settings.get("slip_per_side")),
            elapsed_seconds=last_elapsed,
            eta_seconds=0.0,
            fail_rows=final_fail_rows,
        ),
        post_run_summary=_build_post_run_summary(
            request=request,
            status="completed",
            done=last_done,
            total=last_total,
            passed=last_passed,
            current_snapshot=build_current_best_snapshot(final_top_df, param_names, slip_per_side=runtime_settings.get("slip_per_side")),
            fail_rows=final_fail_rows,
            artifact=artifact_payload,
        ),
    )
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return 1
    return run_background_job(str(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
