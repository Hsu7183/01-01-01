from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .bootstrap import resolve_source_root


def jobs_root() -> Path:
    root = resolve_source_root() / "run_history" / "mq01_jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_dir(job_id: str) -> Path:
    return jobs_root() / str(job_id)


def request_path(job_id: str) -> Path:
    return job_dir(job_id) / "request.json"


def state_path(job_id: str) -> Path:
    return job_dir(job_id) / "state.json"


def stop_flag_path(job_id: str) -> Path:
    return job_dir(job_id) / "stop.flag"


def heartbeat_path(job_id: str) -> Path:
    return job_dir(job_id) / "heartbeat.json"


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def create_job_request(payload: dict[str, Any]) -> str:
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    job_directory = job_dir(job_id)
    job_directory.mkdir(parents=True, exist_ok=True)
    _write_json(request_path(job_id), payload)
    pre_run_summary = payload.get("pre_run_summary") if isinstance(payload.get("pre_run_summary"), dict) else {}
    _write_json(
        state_path(job_id),
        {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now_text(),
            "updated_at": _now_text(),
            "done": 0,
            "total": 0,
            "passed": 0,
            "step_note": "排隊中",
            "summary_lines": ["已建立背景任務，等待啟動。"],
            "narrative_lines": ["背景任務已建立，準備啟動。"],
            "top_rows": [],
            "recent_rows": [],
            "fail_rows": [],
            "elapsed_seconds": 0.0,
            "compute_elapsed_seconds": 0.0,
            "transition_elapsed_seconds": 0.0,
            "eta_seconds": 0.0,
            "artifact": {},
            "pre_run_summary": pre_run_summary,
            "live_run_summary": {},
            "post_run_summary": {},
        },
    )
    return job_id


def read_job_request(job_id: str) -> dict[str, Any]:
    path = request_path(job_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_job_state(job_id: str) -> dict[str, Any]:
    path = state_path(job_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_job_state(job_id: str, payload: dict[str, Any]) -> None:
    existing = read_job_state(job_id)
    merged = dict(existing)
    merged.update(payload)
    merged["job_id"] = job_id
    merged["updated_at"] = _now_text()
    _write_json(state_path(job_id), merged)


def read_job_heartbeat(job_id: str) -> dict[str, Any]:
    path = heartbeat_path(job_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def touch_job_heartbeat(job_id: str, *, controller: str = "") -> None:
    _write_json(
        heartbeat_path(job_id),
        {
            "job_id": job_id,
            "controller": str(controller or ""),
            "updated_at": _now_text(),
        },
    )


def heartbeat_age_seconds(job_id: str) -> float | None:
    payload = read_job_heartbeat(job_id)
    updated_at = str(payload.get("updated_at") or "").strip()
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(updated_at)
    except Exception:
        return None
    return max((datetime.now() - updated).total_seconds(), 0.0)


def _terminate_pid(pid: int) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        pass

    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


def _state_age_seconds(state: dict[str, Any]) -> float | None:
    updated_at = str(state.get("updated_at") or state.get("created_at") or "").strip()
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(updated_at)
    except Exception:
        return None
    return max((datetime.now() - updated).total_seconds(), 0.0)


def list_job_runtime_records(*, stale_after_seconds: int = 45) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in sorted(jobs_root().iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        job_id = entry.name
        state = read_job_state(job_id)
        if not state:
            continue
        heartbeat_age = heartbeat_age_seconds(job_id)
        state_age = _state_age_seconds(state)
        status = str(state.get("status") or "")
        stale = False
        if not is_terminal_status(status):
            if heartbeat_age is not None:
                stale = heartbeat_age > float(stale_after_seconds)
            elif state_age is not None:
                stale = state_age > float(stale_after_seconds)
        records.append(
            {
                "job_id": job_id,
                "status": status,
                "pid": int(state.get("pid") or 0),
                "updated_at": str(state.get("updated_at") or ""),
                "heartbeat_age_seconds": heartbeat_age,
                "state_age_seconds": state_age,
                "stale": stale,
            }
        )
    return records


def cleanup_stale_jobs(*, stale_after_seconds: int = 45) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for record in list_job_runtime_records(stale_after_seconds=stale_after_seconds):
        if not record["stale"]:
            continue
        pid = int(record.get("pid") or 0)
        if pid > 0:
            _terminate_pid(pid)
        write_job_state(
            record["job_id"],
            {
                "status": "stopped",
                "step_note": "控制頁心跳逾時，已自動停止背景 worker",
                "summary_lines": [
                    "偵測到控制頁心跳逾時。",
                    "背景 Python worker 已自動清理，避免程序殘留持續吃資源。",
                ],
            },
        )
        cleaned.append(record)
    return cleaned


def force_stop_job(job_id: str, *, reason: str = "已手動強制停止背景 worker") -> None:
    state = read_job_state(job_id)
    pid = int(state.get("pid") or 0)
    if pid > 0:
        _terminate_pid(pid)
    stop_flag_path(job_id).write_text("stop\n", encoding="utf-8")
    write_job_state(
        job_id,
        {
            "status": "stopped",
            "step_note": reason,
            "summary_lines": [
                reason,
                "背景 Python worker 已被強制結束。",
            ],
        },
    )


def request_stop(job_id: str) -> None:
    stop_flag_path(job_id).write_text("stop\n", encoding="utf-8")
    write_job_state(
        job_id,
        {
            "status": "stopping",
            "step_note": "停止請求已送出",
            "summary_lines": ["背景任務收到停止請求，會在安全檢查點停止並存檔。"],
        },
    )


def stop_requested(job_id: str) -> bool:
    return stop_flag_path(job_id).exists()


def is_terminal_status(status: str) -> bool:
    return str(status) in {"completed", "stopped", "error"}


def launch_job_process(job_id: str, *, package_root: str) -> int:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stdout_path = job_dir(job_id) / "worker.stdout.log"
    stderr_path = job_dir(job_id) / "worker.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "mq01.background_worker", job_id],
        cwd=str(package_root),
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=creationflags,
        env=env,
    )
    write_job_state(
        job_id,
        {
            "status": "starting",
            "pid": int(process.pid),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        },
    )
    return int(process.pid)
