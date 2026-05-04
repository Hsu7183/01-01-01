from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_HISTORY_DIR = PROJECT_ROOT / "run_history"
RESEARCH_SESSIONS_DIR = RUN_HISTORY_DIR / "research_sessions"
GENERATED_STRATEGY_DIR = PROJECT_ROOT / "strategy" / "generated"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_history_dir() -> Path:
    return ensure_dir(RUN_HISTORY_DIR)


def research_sessions_dir() -> Path:
    return ensure_dir(RESEARCH_SESSIONS_DIR)


def generated_strategy_dir() -> Path:
    return ensure_dir(GENERATED_STRATEGY_DIR)


def session_dir(session_id: str) -> Path:
    return ensure_dir(research_sessions_dir() / session_id)


def research_db_path() -> Path:
    return run_history_dir() / "research_memory.db"
