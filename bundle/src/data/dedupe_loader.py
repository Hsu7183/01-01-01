from __future__ import annotations


def dedupe(rows):
    if not rows:
        return []

    out = [rows[0]]

    for r in rows[1:]:
        if r.key != out[-1].key:
            out.append(r)

    return out