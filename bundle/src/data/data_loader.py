from __future__ import annotations

from pathlib import Path

from src.core.models import DailyBar, MinuteBar


_SUPPORTED_SUFFIXES = {".txt", ".csv"}


def _resolve_parts(path: str | Path) -> list[Path]:
    target = Path(path)
    if target.is_dir():
        parts = sorted(
            child for child in target.iterdir()
            if child.is_file() and child.suffix.lower() in _SUPPORTED_SUFFIXES
        )
        if not parts:
            raise FileNotFoundError(f"no market data files found in directory: {target}")
        return parts
    if target.is_file():
        return [target]
    raise FileNotFoundError(f"market data path not found: {target}")


def _clean_lines(path: str | Path) -> list[str]:
    lines: list[str] = []
    for part in _resolve_parts(path):
        text = part.read_text(encoding="utf-8", errors="ignore")
        lines.extend(line.strip() for line in text.splitlines() if line.strip())
    return lines


def load_m1(path: str | Path) -> list[MinuteBar]:
    rows: list[MinuteBar] = []

    for line in _clean_lines(path):
        parts = line.split()
        if len(parts) != 6:
            continue

        rows.append(
            MinuteBar(
                date=int(parts[0]),
                time=int(parts[1]),
                open=float(parts[2]),
                high=float(parts[3]),
                low=float(parts[4]),
                close=float(parts[5]),
            )
        )

    return rows


def load_d1(path: str | Path) -> list[DailyBar]:
    rows: list[DailyBar] = []

    for line in _clean_lines(path):
        parts = line.split()
        if len(parts) != 5:
            continue

        rows.append(
            DailyBar(
                date=int(parts[0]),
                open=float(parts[1]),
                high=float(parts[2]),
                low=float(parts[3]),
                close=float(parts[4]),
            )
        )

    return rows
