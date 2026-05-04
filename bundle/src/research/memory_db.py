from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import file_store
from .modular_0313plus import (
    ATR_FILTER_MODE_CHOICES,
    BIAS_MODE_CHOICES,
    ENTRY_MODE_CHOICES,
    KEY_SWEEP_PARAMS,
    MODULE_FAMILY_CODE,
    describe_0313plus_template_choices,
)
from .types import BacktestMetrics, CandidateProposal, ResearchConfig


_SQLITE_USABLE_CACHE: dict[str, bool] = {}
_MODULE_DIMENSION_KEYS = [
    "bias_mode",
    "entry_mode",
    "atr_filter_mode",
    "use_atr_stop",
    "use_atr_tp",
    "use_time_stop",
    "use_trail_exit",
]
_MODULE_DIMENSION_LABELS = {
    "bias_mode": "Bias 模組",
    "entry_mode": "Entry 模組",
    "atr_filter_mode": "ATR Filter",
    "use_atr_stop": "ATR 停損",
    "use_atr_tp": "ATR 停利",
    "use_time_stop": "Time Stop",
    "use_trail_exit": "Trail Exit",
}
_BOOL_CHOICE_LABELS = {
    "use_atr_stop": {0: "關閉 ATR 停損", 1: "啟用 ATR 停損"},
    "use_atr_tp": {0: "關閉 ATR 停利", 1: "啟用 ATR 停利"},
    "use_time_stop": {0: "關閉 Time Stop", 1: "啟用 Time Stop"},
    "use_trail_exit": {0: "關閉 Trail Exit", 1: "啟用 Trail Exit"},
}


def _utc_now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    return db


def _json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _sqlite_usable(db_path: str | Path) -> bool:
    cache_key = str(db_path)
    if cache_key in _SQLITE_USABLE_CACHE:
        return _SQLITE_USABLE_CACHE[cache_key]
    probe_path = Path(db_path).with_name("__sqlite_probe__.db")
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if probe_path.exists():
            probe_path.unlink()
        with closing(sqlite3.connect(str(probe_path))) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
            conn.execute("INSERT INTO t (x) VALUES (1)")
            conn.commit()
        if probe_path.exists():
            probe_path.unlink()
        _SQLITE_USABLE_CACHE[cache_key] = True
        return True
    except (sqlite3.Error, OSError):
        try:
            if probe_path.exists():
                probe_path.unlink()
        except OSError:
            pass
        _SQLITE_USABLE_CACHE[cache_key] = False
        return False


def init_memory_db(db_path: str | Path) -> None:
    if not _sqlite_usable(db_path):
        file_store.init_store()
        return
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                model TEXT NOT NULL,
                base_xs_path TEXT NOT NULL,
                minute_path TEXT NOT NULL,
                daily_path TEXT NOT NULL,
                param_preset_path TEXT,
                txt_path TEXT,
                allow_param_mutation INTEGER NOT NULL,
                allow_template_mutation INTEGER NOT NULL,
                batch_size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                stopped_at TEXT
            );

            CREATE TABLE IF NOT EXISTS strategies (
                strategy_id TEXT PRIMARY KEY,
                parent_strategy_id TEXT,
                session_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                xs_path TEXT NOT NULL,
                params_txt_path TEXT NOT NULL,
                xs_hash TEXT NOT NULL,
                strategy_signature TEXT NOT NULL UNIQUE,
                ai_summary TEXT,
                template_choices_json TEXT NOT NULL,
                params_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                total_return REAL NOT NULL,
                mdd_pct REAL NOT NULL,
                n_trades INTEGER NOT NULL,
                year_avg_return REAL NOT NULL,
                year_return_std REAL NOT NULL,
                loss_years INTEGER NOT NULL,
                composite_score REAL NOT NULL,
                fail_reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                response_text TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runs_session_score
            ON runs(session_id, composite_score DESC, mdd_pct ASC, total_return DESC);
            """
        )
        existing_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "param_preset_path" not in existing_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN param_preset_path TEXT")
        conn.commit()


def create_session(db_path: str | Path, config: ResearchConfig) -> None:
    if not _sqlite_usable(db_path):
        file_store.create_session(config)
        return
    stamp = _utc_now_text()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                session_id, status, model, base_xs_path, minute_path, daily_path, param_preset_path, txt_path,
                allow_param_mutation, allow_template_mutation, batch_size,
                created_at, updated_at, stopped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config.session_id,
                "CREATED",
                config.model,
                config.base_xs_path,
                config.minute_path,
                config.daily_path,
                config.param_preset_path,
                config.txt_path,
                int(config.allow_param_mutation),
                int(config.allow_template_mutation),
                int(config.batch_size),
                stamp,
                stamp,
                None,
            ),
        )
        conn.commit()


def insert_strategy(
    db_path: str | Path,
    session_id: str,
    strategy_id: str,
    proposal: CandidateProposal,
    xs_path: str,
    params_txt_path: str,
    xs_hash: str,
    strategy_signature: str,
    parent_strategy_id: str | None,
) -> None:
    if not _sqlite_usable(db_path):
        file_store.insert_strategy(
            session_id=session_id,
            strategy_id=strategy_id,
            proposal=proposal,
            xs_path=xs_path,
            params_txt_path=params_txt_path,
            xs_hash=xs_hash,
            strategy_signature=strategy_signature,
            parent_strategy_id=parent_strategy_id,
        )
        return
    stamp = _utc_now_text()
    with closing(_connect(db_path)) as conn:
        version_no = conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0] + 1
        conn.execute(
            """
            INSERT INTO strategies (
                strategy_id, parent_strategy_id, session_id, version_no,
                xs_path, params_txt_path, xs_hash, strategy_signature,
                ai_summary, template_choices_json, params_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                parent_strategy_id,
                session_id,
                version_no,
                xs_path,
                params_txt_path,
                xs_hash,
                strategy_signature,
                proposal.ai_summary,
                _json_text(proposal.template_choices),
                _json_text(proposal.params),
                stamp,
            ),
        )
        conn.commit()


def insert_run(db_path: str | Path, session_id: str, strategy_id: str, metrics: BacktestMetrics) -> None:
    if not _sqlite_usable(db_path):
        file_store.insert_run(session_id=session_id, strategy_id=strategy_id, metrics=metrics)
        return
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, session_id, strategy_id, total_return, mdd_pct, n_trades,
                year_avg_return, year_return_std, loss_years, composite_score,
                fail_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                session_id,
                strategy_id,
                float(metrics.total_return),
                float(metrics.mdd_pct),
                int(metrics.n_trades),
                float(metrics.year_avg_return),
                float(metrics.year_return_std),
                int(metrics.loss_years),
                float(metrics.composite_score),
                metrics.fail_reason,
                _utc_now_text(),
            ),
        )
        conn.commit()


def record_llm_call(
    db_path: str | Path,
    session_id: str,
    model: str,
    prompt_text: str,
    response_text: str,
    candidate_count: int,
) -> None:
    if not _sqlite_usable(db_path):
        file_store.record_llm_call(
            session_id=session_id,
            model=model,
            prompt_text=prompt_text,
            response_text=response_text,
            candidate_count=candidate_count,
        )
        return
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO llm_calls (
                call_id, session_id, model, prompt_text, response_text, candidate_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                session_id,
                model,
                prompt_text,
                response_text,
                int(candidate_count),
                _utc_now_text(),
            ),
        )
        conn.commit()


def has_strategy_signature(db_path: str | Path, strategy_signature: str) -> bool:
    if not _sqlite_usable(db_path):
        return file_store.has_strategy_signature(strategy_signature)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM strategies WHERE strategy_signature = ? LIMIT 1",
            (strategy_signature,),
        ).fetchone()
    return row is not None


def get_run_by_strategy_signature(db_path: str | Path, strategy_signature: str) -> dict[str, Any] | None:
    if not _sqlite_usable(db_path):
        return file_store.get_run_by_strategy_signature(strategy_signature)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT
                r.run_id,
                r.session_id,
                r.strategy_id,
                r.total_return,
                r.mdd_pct,
                r.n_trades,
                r.year_avg_return,
                r.year_return_std,
                r.loss_years,
                r.composite_score,
                r.fail_reason,
                r.created_at,
                s.params_json,
                s.ai_summary,
                s.template_choices_json,
                s.xs_path,
                s.params_txt_path,
                s.strategy_signature
            FROM strategies s
            LEFT JOIN runs r ON r.strategy_id = s.strategy_id
            WHERE s.strategy_signature = ?
            ORDER BY r.composite_score DESC, r.mdd_pct ASC, r.total_return DESC
            LIMIT 1
            """,
            (strategy_signature,),
        ).fetchone()
    if row is None:
        return None
    return _attach_strategy_metadata(dict(row))


def update_session_status(db_path: str | Path, session_id: str, status: str) -> None:
    if not _sqlite_usable(db_path):
        file_store.update_session_status(session_id, status)
        return
    stamp = _utc_now_text()
    stopped_at = stamp if status in {"STOPPED", "FAILED", "COMPLETED"} else None
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE sessions
            SET status = ?, updated_at = ?, stopped_at = COALESCE(?, stopped_at)
            WHERE session_id = ?
            """,
            (status, stamp, stopped_at, session_id),
        )
        conn.commit()


def _load_template_choices(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_params_payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _choice_label(dimension_key: str, value: Any) -> str:
    text = str(value)
    if dimension_key == "bias_mode":
        for code, label in BIAS_MODE_CHOICES:
            if code == text:
                return label
    elif dimension_key == "entry_mode":
        for code, label in ENTRY_MODE_CHOICES:
            if code == text:
                return label
    elif dimension_key == "atr_filter_mode":
        for code, label in ATR_FILTER_MODE_CHOICES:
            if code == text:
                return label
    elif dimension_key in _BOOL_CHOICE_LABELS:
        try:
            return _BOOL_CHOICE_LABELS[dimension_key][int(value)]
        except Exception:
            return text
    return text


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    index = max(0.0, min(float(ratio), 1.0)) * float(len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return float(sorted_values[lower])
    weight = index - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def _normalize_modular_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        template_choices = _load_template_choices(row.get("template_choices_json"))
        if str(template_choices.get("module_family") or "") != MODULE_FAMILY_CODE:
            continue
        params = _load_params_payload(row.get("params_json"))
        normalized.append(
            {
                **row,
                "template_choices": template_choices,
                "params": params,
                "combo_signature": _json_text({key: template_choices.get(key) for key in _MODULE_DIMENSION_KEYS}),
                "combo_label": describe_0313plus_template_choices(template_choices),
            }
        )
    return normalized


def _best_tuple(row: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(row.get("composite_score") or -1e18),
        -float(row.get("mdd_pct") or 1e18),
        float(row.get("total_return") or -1e18),
    )


def _fetch_all_modular_rows(db_path: str | Path) -> list[dict[str, Any]]:
    if not _sqlite_usable(db_path):
        return file_store.get_all_modular_runs()
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                r.session_id,
                r.run_id,
                r.strategy_id,
                r.total_return,
                r.mdd_pct,
                r.n_trades,
                r.year_avg_return,
                r.year_return_std,
                r.loss_years,
                r.composite_score,
                r.fail_reason,
                r.created_at,
                s.params_json,
                s.ai_summary,
                s.template_choices_json
            FROM runs r
            JOIN strategies s ON s.strategy_id = r.strategy_id
            ORDER BY r.created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_module_learning_summary(
    db_path: str | Path,
    session_id: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    rows = _normalize_modular_rows(_fetch_all_modular_rows(db_path))
    if not rows:
        return {
            "total_modular_runs": 0,
            "valid_modular_runs": 0,
            "dimensions": {},
            "top_combos": [],
        }

    dimension_buckets: dict[str, dict[str, dict[str, Any]]] = {
        key: {} for key in _MODULE_DIMENSION_KEYS
    }
    combo_buckets: dict[str, dict[str, Any]] = {}
    valid_modular_runs = 0

    for row in rows:
        template_choices = row["template_choices"]
        params = row["params"]
        combo_signature = str(row["combo_signature"])
        combo_label = str(row["combo_label"])
        is_valid = not bool(row.get("fail_reason"))
        score_value = float(row.get("composite_score") or 0.0)
        if is_valid:
            valid_modular_runs += 1

        combo_bucket = combo_buckets.setdefault(
            combo_signature,
            {
                "combo_signature": combo_signature,
                "strategy_group_code": str(template_choices.get("strategy_group_code") or combo_signature),
                "label": combo_label,
                "template_choices": dict(template_choices),
                "tested_count": 0,
                "valid_runs": 0,
                "score_sum": 0.0,
                "best_score": None,
                "best_total_return": None,
                "best_mdd_pct": None,
                "best_params": {},
                "session_hits": 0,
            },
        )
        combo_bucket["tested_count"] = int(combo_bucket["tested_count"]) + 1
        if session_id and str(row.get("session_id") or "") == str(session_id):
            combo_bucket["session_hits"] = int(combo_bucket["session_hits"]) + 1
        if is_valid:
            combo_bucket["valid_runs"] = int(combo_bucket["valid_runs"]) + 1
            combo_bucket["score_sum"] = float(combo_bucket["score_sum"]) + score_value
        if combo_bucket["best_score"] is None or _best_tuple(row) > (
            float(combo_bucket["best_score"] or -1e18),
            -float(combo_bucket["best_mdd_pct"] or 1e18),
            float(combo_bucket["best_total_return"] or -1e18),
        ):
            combo_bucket["best_score"] = score_value
            combo_bucket["best_total_return"] = float(row.get("total_return") or 0.0)
            combo_bucket["best_mdd_pct"] = float(row.get("mdd_pct") or 0.0)
            combo_bucket["best_params"] = dict(params)

        for dimension_key in _MODULE_DIMENSION_KEYS:
            raw_value = template_choices.get(dimension_key)
            value_key = str(raw_value)
            bucket = dimension_buckets[dimension_key].setdefault(
                value_key,
                {
                    "dimension_key": dimension_key,
                    "dimension_label": _MODULE_DIMENSION_LABELS[dimension_key],
                    "code": raw_value,
                    "label": _choice_label(dimension_key, raw_value),
                    "tested_count": 0,
                    "valid_runs": 0,
                    "score_sum": 0.0,
                    "best_score": None,
                    "best_total_return": None,
                    "best_mdd_pct": None,
                    "session_hits": 0,
                },
            )
            bucket["tested_count"] = int(bucket["tested_count"]) + 1
            if session_id and str(row.get("session_id") or "") == str(session_id):
                bucket["session_hits"] = int(bucket["session_hits"]) + 1
            if is_valid:
                bucket["valid_runs"] = int(bucket["valid_runs"]) + 1
                bucket["score_sum"] = float(bucket["score_sum"]) + score_value
            if bucket["best_score"] is None or _best_tuple(row) > (
                float(bucket["best_score"] or -1e18),
                -float(bucket["best_mdd_pct"] or 1e18),
                float(bucket["best_total_return"] or -1e18),
            ):
                bucket["best_score"] = score_value
                bucket["best_total_return"] = float(row.get("total_return") or 0.0)
                bucket["best_mdd_pct"] = float(row.get("mdd_pct") or 0.0)

    dimensions: dict[str, list[dict[str, Any]]] = {}
    for dimension_key, bucket_map in dimension_buckets.items():
        rows_for_dimension: list[dict[str, Any]] = []
        for bucket in bucket_map.values():
            valid_runs = int(bucket["valid_runs"])
            avg_score = (float(bucket["score_sum"]) / valid_runs) if valid_runs else None
            learned_priority = (
                (float(bucket["best_score"] or 0.0) * 0.65)
                + (float(avg_score or 0.0) * 0.35)
                + min(valid_runs, 12) * 0.08
            )
            rows_for_dimension.append(
                {
                    **bucket,
                    "avg_score": avg_score,
                    "learned_priority": round(learned_priority, 6),
                }
            )
        rows_for_dimension.sort(
            key=lambda row: (
                -float(row.get("learned_priority") or -1e18),
                -int(row.get("valid_runs") or 0),
                float(row.get("best_mdd_pct") or 1e18),
            )
        )
        dimensions[dimension_key] = rows_for_dimension[: int(limit)]

    top_combos: list[dict[str, Any]] = []
    for bucket in combo_buckets.values():
        valid_runs = int(bucket["valid_runs"])
        avg_score = (float(bucket["score_sum"]) / valid_runs) if valid_runs else None
        learned_priority = (
            (float(bucket["best_score"] or 0.0) * 0.7)
            + (float(avg_score or 0.0) * 0.3)
            + min(valid_runs, 12) * 0.1
        )
        top_combos.append(
            {
                **bucket,
                "avg_score": avg_score,
                "learned_priority": round(learned_priority, 6),
            }
        )
    top_combos.sort(
        key=lambda row: (
            -float(row.get("learned_priority") or -1e18),
            float(row.get("best_mdd_pct") or 1e18),
            -float(row.get("best_total_return") or -1e18),
        )
    )

    return {
        "total_modular_runs": len(rows),
        "valid_modular_runs": valid_modular_runs,
        "dimensions": dimensions,
        "top_combos": top_combos[: int(limit)],
    }


def get_param_learning_summary(
    db_path: str | Path,
    session_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    del session_id  # currently aggregated across all persisted modular research
    rows = _normalize_modular_rows(_fetch_all_modular_rows(db_path))
    valid_rows = [row for row in rows if not row.get("fail_reason")]
    if not valid_rows:
        return {
            "total_valid_runs": 0,
            "focus_window": 0,
            "param_ranges": [],
        }

    valid_rows.sort(key=_best_tuple, reverse=True)
    focus_window = max(8, min(len(valid_rows), 36))
    focus_rows = valid_rows[:focus_window]
    best_row = focus_rows[0]

    param_ranges: list[dict[str, Any]] = []
    for name in KEY_SWEEP_PARAMS:
        all_values = sorted(
            value for value in (_to_float(row["params"].get(name)) for row in valid_rows) if value is not None
        )
        focus_values = sorted(
            value for value in (_to_float(row["params"].get(name)) for row in focus_rows) if value is not None
        )
        if not all_values or not focus_values:
            continue
        best_value = _to_float(best_row["params"].get(name))
        preferred_low = _percentile(focus_values, 0.25)
        preferred_high = _percentile(focus_values, 0.75)
        if preferred_low is None or preferred_high is None:
            continue
        param_ranges.append(
            {
                "name": name,
                "tested_min": min(all_values),
                "tested_max": max(all_values),
                "preferred_low": preferred_low,
                "preferred_high": preferred_high,
                "best_value": best_value,
                "observations": len(all_values),
            }
        )

    param_ranges.sort(key=lambda row: (-int(row.get("observations") or 0), str(row.get("name") or "")))
    return {
        "total_valid_runs": len(valid_rows),
        "focus_window": focus_window,
        "param_ranges": param_ranges[: int(limit)],
    }


def _attach_strategy_metadata(row: dict[str, object]) -> dict[str, object]:
    payload = dict(row)
    template_choices = _load_template_choices(payload.get("template_choices_json"))
    payload["template_choices_json"] = json.dumps(template_choices, ensure_ascii=False)
    payload["strategy_group_code"] = str(
        template_choices.get("strategy_group_code") or payload.get("strategy_group_code") or "ungrouped"
    )
    payload["strategy_group_label"] = str(
        template_choices.get("strategy_group_label") or payload.get("strategy_group_label") or "未分類策略家族"
    )
    return payload


def get_recent_runs(db_path: str | Path, session_id: str, limit: int = 30) -> list[dict]:
    if not _sqlite_usable(db_path):
        return file_store.get_recent_runs(session_id, limit=limit)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                r.run_id,
                r.strategy_id,
                r.total_return,
                r.mdd_pct,
                r.n_trades,
                r.year_avg_return,
                r.year_return_std,
                r.loss_years,
                r.composite_score,
                r.fail_reason,
                r.created_at,
                s.params_json,
                s.ai_summary,
                s.template_choices_json
            FROM runs r
            JOIN strategies s ON s.strategy_id = r.strategy_id
            WHERE r.session_id = ?
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (session_id, int(limit)),
        ).fetchall()
    return [_attach_strategy_metadata(dict(row)) for row in rows]


def get_top_runs(db_path: str | Path, session_id: str, limit: int = 10) -> list[dict]:
    if not _sqlite_usable(db_path):
        return file_store.get_top_runs(session_id, limit=limit)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                r.run_id,
                r.strategy_id,
                r.total_return,
                r.mdd_pct,
                r.n_trades,
                r.year_avg_return,
                r.year_return_std,
                r.loss_years,
                r.composite_score,
                r.fail_reason,
                r.created_at,
                s.params_json,
                s.ai_summary,
                s.template_choices_json,
                s.xs_path,
                s.params_txt_path
            FROM runs r
            JOIN strategies s ON s.strategy_id = r.strategy_id
            WHERE r.session_id = ?
            ORDER BY r.composite_score DESC, r.mdd_pct ASC, r.total_return DESC
            LIMIT ?
            """,
            (session_id, int(limit)),
        ).fetchall()
    return [_attach_strategy_metadata(dict(row)) for row in rows]


def get_strategy_group_summary(db_path: str | Path, session_id: str, limit: int = 8) -> list[dict]:
    if not _sqlite_usable(db_path):
        return file_store.get_strategy_group_summary(session_id, limit=limit)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                r.strategy_id,
                r.total_return,
                r.mdd_pct,
                r.n_trades,
                r.composite_score,
                r.fail_reason,
                r.created_at,
                s.template_choices_json
            FROM runs r
            JOIN strategies s ON s.strategy_id = r.strategy_id
            WHERE r.session_id = ?
            """,
            (session_id,),
        ).fetchall()

    grouped: dict[str, dict[str, object]] = {}
    for raw_row in rows:
        row = _attach_strategy_metadata(dict(raw_row))
        group_code = str(row.get("strategy_group_code") or "ungrouped")
        group_label = str(row.get("strategy_group_label") or "未分類策略家族")
        bucket = grouped.setdefault(
            group_code,
            {
                "strategy_group_code": group_code,
                "strategy_group_label": group_label,
                "tested_params": 0,
                "valid_runs": 0,
                "best_strategy_id": None,
                "best_score": None,
                "best_total_return": None,
                "best_mdd_pct": None,
                "best_n_trades": None,
                "last_run_at": None,
            },
        )
        bucket["tested_params"] = int(bucket["tested_params"]) + 1
        if not row.get("fail_reason"):
            bucket["valid_runs"] = int(bucket["valid_runs"]) + 1
        created_at = str(row.get("created_at") or "")
        if created_at and (not bucket["last_run_at"] or created_at > str(bucket["last_run_at"])):
            bucket["last_run_at"] = created_at

        current_score = float(row.get("composite_score") or -1e18)
        best_score = bucket.get("best_score")
        should_replace = best_score is None or (
            current_score,
            -float(row.get("mdd_pct") or 1e18),
            float(row.get("total_return") or -1e18),
        ) > (
            float(best_score),
            -float(bucket.get("best_mdd_pct") or 1e18),
            float(bucket.get("best_total_return") or -1e18),
        )
        if should_replace:
            bucket["best_strategy_id"] = row.get("strategy_id")
            bucket["best_score"] = float(row.get("composite_score") or 0.0)
            bucket["best_total_return"] = float(row.get("total_return") or 0.0)
            bucket["best_mdd_pct"] = float(row.get("mdd_pct") or 0.0)
            bucket["best_n_trades"] = int(row.get("n_trades") or 0)

    summary_rows = list(grouped.values())
    summary_rows.sort(
        key=lambda row: (
            -float(row.get("best_score") or -1e18),
            float(row.get("best_mdd_pct") or 1e18),
            -float(row.get("best_total_return") or -1e18),
        )
    )
    return summary_rows[: int(limit)]


def list_sessions(db_path: str | Path, limit: int = 30) -> list[dict]:
    if not _sqlite_usable(db_path):
        return file_store.list_sessions(limit=limit)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                s.session_id,
                s.status,
                s.model,
                s.base_xs_path,
                s.created_at,
                s.updated_at,
                COUNT(r.run_id) AS run_count,
                MAX(r.composite_score) AS best_score
            FROM sessions s
            LEFT JOIN runs r ON r.session_id = s.session_id
            GROUP BY
                s.session_id, s.status, s.model, s.base_xs_path, s.created_at, s.updated_at
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_session_summary(db_path: str | Path, session_id: str) -> dict:
    if not _sqlite_usable(db_path):
        return file_store.get_session_summary(session_id)
    with closing(_connect(db_path)) as conn:
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        run_count = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        best_row = conn.execute(
            """
            SELECT
                r.strategy_id,
                r.composite_score,
                r.total_return,
                r.mdd_pct,
                r.n_trades,
                s.template_choices_json
            FROM runs r
            JOIN strategies s ON s.strategy_id = r.strategy_id
            WHERE r.session_id = ?
            ORDER BY composite_score DESC, mdd_pct ASC, total_return DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    group_summary = get_strategy_group_summary(db_path, session_id, limit=8)
    return {
        "session": None if session_row is None else dict(session_row),
        "run_count": int(run_count),
        "best_run": None if best_row is None else _attach_strategy_metadata(dict(best_row)),
        "strategy_groups": group_summary,
    }
