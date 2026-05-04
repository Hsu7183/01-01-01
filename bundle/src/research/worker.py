from __future__ import annotations

import json
import sys
from pathlib import Path

from .paths import research_db_path
from .research_loop import run_research_session
from .types import ResearchConfig


def load_config(path: str | Path) -> ResearchConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("research config JSON must be an object")
    return ResearchConfig(**payload)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit("Usage: python -m src.research.worker <config.json>")

    config = load_config(args[0])
    run_research_session(config, str(research_db_path()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
