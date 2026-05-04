from __future__ import annotations

import re


def _read_xq_lines(path: str) -> list[str]:
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            with open(path, "r", encoding=encoding, errors="strict") as f:
                return f.readlines()
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return []


TRADE_LINE_RE = re.compile(
    r"^(\d{14})\s+(\d+(?:\.\d+)?)\s+(新買|新賣|平買|平賣|強制平倉)\s*$"
)


def load_xq_trade_events(path: str):
    events = []
    for raw in _read_xq_lines(path):
        line = raw.strip()
        if not line:
            continue

        m = TRADE_LINE_RE.match(line)
        if not m:
            continue

        ts = m.group(1)
        px = float(m.group(2))
        action = m.group(3)

        events.append({
            "ts": ts,
            "price": px,
            "action": action,
        })
    return events


def parse_xq_header_params(path: str) -> dict[str, str]:
    lines = _read_xq_lines(path)
    first_line = lines[0].strip() if lines else ""
    if not first_line or "=" not in first_line:
        return {}
    matches = re.findall(r"([A-Za-z0-9_]+)=([^,]+)", first_line)
    return {key.strip(): value.strip() for key, value in matches}


def build_python_trade_events(trades):
    events = []
    for t in trades:
        events.append({
            "ts": f"{t.entry_date}{t.entry_time:06d}",
            "price": float(t.entry_price),
            "action": t.entry_action,
        })
        events.append({
            "ts": f"{t.exit_date}{t.exit_time:06d}",
            "price": float(t.exit_price),
            "action": t.exit_action,
        })
    return events


def compare_event_lists(xq_events, py_events, max_show=20):
    n = min(len(xq_events), len(py_events))

    mismatches = []
    for i in range(n):
        a = xq_events[i]
        b = py_events[i]

        if (
            a["ts"] != b["ts"]
            or a["action"] != b["action"]
            or abs(a["price"] - b["price"]) > 1e-9
        ):
            mismatches.append((i, a, b))
            if len(mismatches) >= max_show:
                break

    summary = {
        "xq_count": len(xq_events),
        "py_count": len(py_events),
        "same_prefix_count": 0,
        "first_mismatch": None,
    }

    if mismatches:
        first_idx, a, b = mismatches[0]
        summary["same_prefix_count"] = first_idx
        summary["first_mismatch"] = {
            "index": first_idx,
            "xq": a,
            "py": b,
        }
    else:
        summary["same_prefix_count"] = n

    return summary
