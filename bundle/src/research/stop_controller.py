from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .paths import session_dir

# The browser keeps sending keepalive heartbeats while the app page is open.
# If the browser closes, the heartbeat will stop and the worker should end
# automatically after a short grace period.
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 2 * 60


def session_stop_flag_path(session_id: str) -> Path:
    return session_dir(session_id) / "stop.flag"


def session_status_path(session_id: str) -> Path:
    return session_dir(session_id) / "status.json"


def session_heartbeat_path(session_id: str) -> Path:
    return session_dir(session_id) / "heartbeat.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def touch_session_heartbeat(session_id: str, *, owner_pid: int | None = None, source: str = "ui") -> Path:
    heartbeat_path = session_heartbeat_path(session_id)
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_json(heartbeat_path)
    payload["session_id"] = session_id
    payload["source"] = source
    payload["owner_pid"] = int(owner_pid) if owner_pid else int(payload.get("owner_pid") or 0) or None
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    heartbeat_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return heartbeat_path


def read_session_heartbeat(session_id: str) -> dict:
    return _read_json(session_heartbeat_path(session_id))


def is_session_heartbeat_stale(session_id: str, timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS) -> bool:
    payload = read_session_heartbeat(session_id)
    updated_text = str(payload.get("updated_at") or "").strip()
    if not updated_text:
        status_payload = _read_json(session_status_path(session_id))
        status_updated_text = str(status_payload.get("updated_at") or "").strip()
        if not status_updated_text:
            return False
        try:
            status_updated_at = datetime.fromisoformat(status_updated_text)
        except Exception:
            return False
        return (datetime.now() - status_updated_at).total_seconds() > max(int(timeout_seconds), 1)
    try:
        updated_at = datetime.fromisoformat(updated_text)
    except Exception:
        return False
    age_seconds = (datetime.now() - updated_at).total_seconds()
    return age_seconds > max(int(timeout_seconds), 1)


def request_stop(session_id: str) -> Path:
    path = session_stop_flag_path(session_id)
    path.write_text("stop\n", encoding="utf-8")
    status_path = session_status_path(session_id)
    if status_path.exists():
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            current_status = str(payload.get("status") or "").upper()
            if current_status not in {"STOPPED", "FAILED", "COMPLETED"}:
                payload["status"] = "STOPPING"
                payload["current_action"] = "已收到停止請求，等待目前這一組回測結束後停止。"
                payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
                status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_stop(session_id: str) -> None:
    path = session_stop_flag_path(session_id)
    if path.exists():
        path.unlink()


def should_stop(session_id: str) -> bool:
    if session_stop_flag_path(session_id).exists():
        return True
    return is_session_heartbeat_stale(session_id, DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
