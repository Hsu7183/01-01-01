from __future__ import annotations

import os
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_ROOT = PACKAGE_ROOT / "bundle"


def _looks_like_source_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "src").is_dir()
        and (path / "strategy").is_dir()
        and (path / "run_history").is_dir()
    )


def resolve_source_root() -> Path:
    override = os.getenv("MQQUANT_SOURCE_ROOT", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(DEFAULT_BUNDLE_ROOT)
    for candidate in candidates:
        if _looks_like_source_root(candidate):
            return candidate
    if override:
        return Path(override).expanduser()
    return DEFAULT_BUNDLE_ROOT


def bootstrap_source_root() -> Path:
    source_root = resolve_source_root()
    source_root_str = str(source_root)
    if source_root_str not in sys.path:
        sys.path.insert(0, source_root_str)
    return source_root
