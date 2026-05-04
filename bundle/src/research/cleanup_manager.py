from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .param_space import load_persistent_top10_rows
from .paths import generated_strategy_dir, research_sessions_dir, run_history_dir


AUTO_CLEANUP_INTERVAL_SECONDS = 3600
_ACTIVE_STATUSES = {"RUNNING", "STOPPING"}
_SESSION_STALE_SECONDS = 20 * 60
_HOURLY_GENERATED_RETENTION_SECONDS = 6 * 3600
_HOURLY_RUN_DIR_RETENTION_SECONDS = 12 * 3600
_KEEP_RECENT_SESSION_DIRS = 2
_KEEP_GLOBAL_TOP_STRATEGIES = 20
_KEEP_SESSION_TOP_STRATEGIES = 20
_KEEP_ACTIVE_SESSION_RECENT_STRATEGIES = 80
_KEEP_INACTIVE_SESSION_RECENT_STRATEGIES = 20
_KEEP_LLM_CALLS_PER_SESSION = 5
_LOG_TAIL_BYTES = 512 * 1024
_DB_COMPACT_THRESHOLD_MB = 64.0
_RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_0313plus$")
_AUTO_CLEANUP_REPORT_PATH = run_history_dir() / "_auto_cleanup_last.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_active_status(status: object) -> bool:
    return str(status or "").strip().upper() in _ACTIVE_STATUSES


def _read_session_status_rows() -> list[dict[str, Any]]:
    now_ts = datetime.now().timestamp()
    rows: list[dict[str, Any]] = []
    for session_path in research_sessions_dir().iterdir():
        if not session_path.is_dir():
            continue
        payload = _read_json(session_path / "status.json")
        status = str(payload.get("status") or "").strip().upper()
        updated_ts = _parse_timestamp(payload.get("updated_at")) or _path_mtime(session_path / "status.json") or _path_mtime(session_path)
        rows.append(
            {
                "session_id": session_path.name,
                "session_path": session_path,
                "status": status,
                "updated_ts": updated_ts,
                "is_active": status in _ACTIVE_STATUSES and (now_ts - updated_ts) <= _SESSION_STALE_SECONDS,
                "best_strategy_id": str(payload.get("best_strategy_id") or "").strip() or None,
            }
        )
    rows.sort(key=lambda row: float(row.get("updated_ts") or 0.0), reverse=True)
    return rows


def _load_keep_rows() -> list[dict[str, Any]]:
    rows = load_persistent_top10_rows(limit=10)
    return [row for row in rows if isinstance(row, dict)]


def _run_dir_name(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).name
    except Exception:
        return None


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(db_path)}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _query_ids(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[str]:
    rows = conn.execute(sql, params).fetchall()
    values: list[str] = []
    for row in rows:
        value = row[0]
        if value:
            values.append(str(value))
    return values


def _build_keep_session_ids(
    conn: sqlite3.Connection | None,
    current_session_id: str | None,
    status_rows: list[dict[str, Any]],
    keep_rows: list[dict[str, Any]],
) -> set[str]:
    keep_session_ids: set[str] = set()
    if current_session_id:
        keep_session_ids.add(str(current_session_id))
    for row in status_rows[:_KEEP_RECENT_SESSION_DIRS]:
        keep_session_ids.add(str(row["session_id"]))
    for row in status_rows:
        if row.get("is_active"):
            keep_session_ids.add(str(row["session_id"]))
    for row in keep_rows:
        session_id = str(row.get("session_id") or "").strip()
        if session_id:
            keep_session_ids.add(session_id)
    if conn is not None:
        keep_session_ids.update(
            _query_ids(
                conn,
                "SELECT session_id FROM sessions ORDER BY created_at DESC LIMIT ?",
                (_KEEP_RECENT_SESSION_DIRS,),
            )
        )
    return keep_session_ids


def _build_keep_strategy_ids(
    conn: sqlite3.Connection | None,
    keep_session_ids: set[str],
    active_session_ids: set[str],
    status_rows: list[dict[str, Any]],
    keep_rows: list[dict[str, Any]],
) -> set[str]:
    keep_strategy_ids: set[str] = set()
    for row in keep_rows:
        strategy_id = str(row.get("strategy_id") or "").strip()
        if strategy_id:
            keep_strategy_ids.add(strategy_id)
    for row in status_rows:
        strategy_id = str(row.get("best_strategy_id") or "").strip()
        if strategy_id:
            keep_strategy_ids.add(strategy_id)
    if conn is None:
        return keep_strategy_ids

    keep_strategy_ids.update(
        _query_ids(
            conn,
            """
            SELECT strategy_id
            FROM runs
            ORDER BY composite_score DESC, mdd_pct ASC, total_return DESC
            LIMIT ?
            """,
            (_KEEP_GLOBAL_TOP_STRATEGIES,),
        )
    )
    for session_id in keep_session_ids:
        keep_strategy_ids.update(
            _query_ids(
                conn,
                """
                SELECT strategy_id
                FROM runs
                WHERE session_id = ?
                ORDER BY composite_score DESC, mdd_pct ASC, total_return DESC
                LIMIT ?
                """,
                (session_id, _KEEP_SESSION_TOP_STRATEGIES),
            )
        )
        recent_limit = (
            _KEEP_ACTIVE_SESSION_RECENT_STRATEGIES
            if session_id in active_session_ids
            else _KEEP_INACTIVE_SESSION_RECENT_STRATEGIES
        )
        keep_strategy_ids.update(
            _query_ids(
                conn,
                """
                SELECT strategy_id
                FROM runs
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, recent_limit),
            )
        )
    return keep_strategy_ids


def _build_keep_run_dirs(
    conn: sqlite3.Connection | None,
    keep_strategy_ids: set[str],
    keep_rows: list[dict[str, Any]],
) -> set[str]:
    keep_run_dirs: set[str] = set()
    for row in keep_rows:
        run_dir = _run_dir_name(row.get("source_run_dir"))
        if run_dir:
            keep_run_dirs.add(run_dir)
    if conn is None or not keep_strategy_ids:
        return keep_run_dirs

    placeholders = ",".join(["?"] * len(keep_strategy_ids))
    sql = f"SELECT params_json FROM strategies WHERE strategy_id IN ({placeholders})"
    for row in conn.execute(sql, tuple(sorted(keep_strategy_ids))).fetchall():
        payload = row[0]
        if not payload:
            continue
        try:
            params = json.loads(payload)
        except Exception:
            continue
        if not isinstance(params, dict):
            continue
        run_dir = _run_dir_name(params.get("source_run_dir"))
        if run_dir:
            keep_run_dirs.add(run_dir)
    return keep_run_dirs


def _remove_tree(path: Path) -> bool:
    if not path.exists():
        return False
    shutil.rmtree(path)
    return True


def _trim_file_tail(path: Path, max_bytes: int) -> bool:
    if not path.exists():
        return False
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if len(raw) <= max_bytes:
        return False
    tail = raw[-max_bytes:]
    newline_index = tail.find(b"\n")
    if newline_index >= 0:
        tail = tail[newline_index + 1 :]
    path.write_bytes(tail)
    return True


def _cleanup_generated_dirs(keep_strategy_ids: set[str], trigger: str, now_ts: float) -> int:
    removed = 0
    for strategy_path in generated_strategy_dir().iterdir():
        if not strategy_path.is_dir():
            continue
        if strategy_path.name in keep_strategy_ids:
            continue
        age_seconds = max(0.0, now_ts - _path_mtime(strategy_path))
        if trigger != "final" and age_seconds < _HOURLY_GENERATED_RETENTION_SECONDS:
            continue
        try:
            if _remove_tree(strategy_path):
                removed += 1
        except OSError:
            continue
    return removed


def _cleanup_run_history_dirs(keep_run_dirs: set[str], trigger: str, now_ts: float) -> int:
    removed = 0
    for child in run_history_dir().iterdir():
        if not child.is_dir():
            continue
        if child.name == "research_sessions":
            continue
        if not _RUN_DIR_PATTERN.match(child.name):
            continue
        if child.name in keep_run_dirs:
            continue
        age_seconds = max(0.0, now_ts - _path_mtime(child))
        if trigger != "final" and age_seconds < _HOURLY_RUN_DIR_RETENTION_SECONDS:
            continue
        try:
            if _remove_tree(child):
                removed += 1
        except OSError:
            continue
    return removed


def _cleanup_session_dirs(
    keep_session_ids: set[str],
    active_session_ids: set[str],
    status_rows: list[dict[str, Any]],
    trigger: str,
) -> tuple[int, int]:
    removed = 0
    trimmed_logs = 0
    for row in status_rows:
        session_id = str(row["session_id"])
        session_path = Path(row["session_path"])
        status = str(row.get("status") or "")
        log_path = session_path / "worker.log"

        if session_id in active_session_ids:
            continue

        if log_path.exists():
            try:
                if _trim_file_tail(log_path, _LOG_TAIL_BYTES):
                    trimmed_logs += 1
            except OSError:
                pass

        looks_ephemeral = session_id.startswith("smoke_") or session_id.startswith("__")
        can_remove = trigger == "final" and session_id not in keep_session_ids and status not in _ACTIVE_STATUSES
        if looks_ephemeral or can_remove:
            try:
                if _remove_tree(session_path):
                    removed += 1
            except OSError:
                continue
    return removed, trimmed_logs


def _compact_db(
    db_path: Path,
    keep_session_ids: set[str],
    keep_strategy_ids: set[str],
    active_session_ids: set[str],
    trigger: str,
) -> dict[str, Any]:
    result = {
        "db_compacted": False,
        "db_size_before_mb": round(db_path.stat().st_size / 1024 / 1024, 2) if db_path.exists() else 0.0,
        "db_size_after_mb": round(db_path.stat().st_size / 1024 / 1024, 2) if db_path.exists() else 0.0,
        "db_reason": "missing",
    }
    if not db_path.exists():
        return result
    if active_session_ids:
        result["db_reason"] = "active_session_present"
        return result
    if result["db_size_before_mb"] < _DB_COMPACT_THRESHOLD_MB:
        result["db_reason"] = "below_threshold"
        return result
    if not keep_session_ids:
        result["db_reason"] = "no_keep_sessions"
        return result

    try:
        with _connect(db_path) as conn:
            keep_session_ids = set(keep_session_ids)
            keep_strategy_ids = set(keep_strategy_ids)

            keep_session_ids.update(
                _query_ids(
                    conn,
                    "SELECT session_id FROM sessions ORDER BY created_at DESC LIMIT ?",
                    (_KEEP_RECENT_SESSION_DIRS,),
                )
            )
            keep_strategy_ids.update(
                _query_ids(
                    conn,
                    """
                    SELECT strategy_id
                    FROM runs
                    ORDER BY composite_score DESC, mdd_pct ASC, total_return DESC
                    LIMIT ?
                    """,
                    (_KEEP_GLOBAL_TOP_STRATEGIES,),
                )
            )
            for session_id in keep_session_ids:
                keep_strategy_ids.update(
                    _query_ids(
                        conn,
                        """
                        SELECT strategy_id
                        FROM runs
                        WHERE session_id = ?
                        ORDER BY composite_score DESC, mdd_pct ASC, total_return DESC
                        LIMIT ?
                        """,
                        (session_id, _KEEP_SESSION_TOP_STRATEGIES),
                    )
                )
                keep_strategy_ids.update(
                    _query_ids(
                        conn,
                        """
                        SELECT strategy_id
                        FROM runs
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (session_id, _KEEP_INACTIVE_SESSION_RECENT_STRATEGIES),
                    )
                )

            session_placeholders = ",".join(["?"] * len(keep_session_ids))
            strategy_placeholders = ",".join(["?"] * len(keep_strategy_ids)) if keep_strategy_ids else ""

            conn.execute(
                f"DELETE FROM llm_calls WHERE session_id NOT IN ({session_placeholders})",
                tuple(sorted(keep_session_ids)),
            )
            if keep_strategy_ids:
                conn.execute(
                    f"DELETE FROM runs WHERE strategy_id NOT IN ({strategy_placeholders})",
                    tuple(sorted(keep_strategy_ids)),
                )
                conn.execute(
                    f"DELETE FROM strategies WHERE strategy_id NOT IN ({strategy_placeholders})",
                    tuple(sorted(keep_strategy_ids)),
                )
            conn.execute(
                f"DELETE FROM sessions WHERE session_id NOT IN ({session_placeholders})",
                tuple(sorted(keep_session_ids)),
            )
            conn.commit()
            conn.execute("VACUUM")
            conn.commit()
        result["db_compacted"] = True
        result["db_reason"] = "compacted"
        result["db_size_after_mb"] = round(db_path.stat().st_size / 1024 / 1024, 2)
    except sqlite3.Error as exc:
        result["db_reason"] = f"sqlite_error:{exc}"
    except OSError as exc:
        result["db_reason"] = f"os_error:{exc}"
    return result


def run_auto_cleanup(
    db_path: str | Path,
    *,
    current_session_id: str | None = None,
    trigger: str = "hourly",
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = datetime.now()
    now_ts = now.timestamp()

    status_rows = _read_session_status_rows()
    keep_rows = _load_keep_rows()
    active_session_ids = {
        str(row["session_id"])
        for row in status_rows
        if row.get("is_active")
    }

    conn: sqlite3.Connection | None = None
    try:
        if db_path.exists():
            conn = _connect_readonly(db_path)
    except sqlite3.Error:
        conn = None

    try:
        try:
            keep_session_ids = _build_keep_session_ids(conn, current_session_id, status_rows, keep_rows)
            keep_strategy_ids = _build_keep_strategy_ids(conn, keep_session_ids, active_session_ids, status_rows, keep_rows)
            keep_run_dirs = _build_keep_run_dirs(conn, keep_strategy_ids, keep_rows)
        except sqlite3.Error:
            keep_session_ids = _build_keep_session_ids(None, current_session_id, status_rows, keep_rows)
            keep_strategy_ids = _build_keep_strategy_ids(None, keep_session_ids, active_session_ids, status_rows, keep_rows)
            keep_run_dirs = _build_keep_run_dirs(None, keep_strategy_ids, keep_rows)
    finally:
        if conn is not None:
            conn.close()

    removed_generated_dirs = _cleanup_generated_dirs(keep_strategy_ids, trigger, now_ts)
    removed_run_dirs = _cleanup_run_history_dirs(keep_run_dirs, trigger, now_ts)
    removed_session_dirs, trimmed_logs = _cleanup_session_dirs(
        keep_session_ids,
        active_session_ids,
        status_rows,
        trigger,
    )
    db_summary = _compact_db(
        db_path,
        keep_session_ids=keep_session_ids,
        keep_strategy_ids=keep_strategy_ids,
        active_session_ids=active_session_ids,
        trigger=trigger,
    )

    summary = {
        "saved_at": now.isoformat(timespec="seconds"),
        "trigger": trigger,
        "current_session_id": current_session_id,
        "active_session_ids": sorted(active_session_ids),
        "keep_session_ids": sorted(keep_session_ids),
        "keep_strategy_count": len(keep_strategy_ids),
        "keep_run_dir_count": len(keep_run_dirs),
        "removed_generated_dirs": removed_generated_dirs,
        "removed_run_dirs": removed_run_dirs,
        "removed_session_dirs": removed_session_dirs,
        "trimmed_logs": trimmed_logs,
    }
    summary.update(db_summary)
    _AUTO_CLEANUP_REPORT_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary
