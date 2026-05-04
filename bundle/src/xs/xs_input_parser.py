from __future__ import annotations

import re
from pathlib import Path

from src.core.models import ParamSpec, XSParseResult


SCRIPT_NAME_RE = re.compile(
    r"ScriptName\s*:\s*([A-Za-z0-9_\-\+]+)",
    re.IGNORECASE
)

INPUT_BLOCK_RE = re.compile(
    r"input\s*:\s*(.*?);",
    re.IGNORECASE | re.DOTALL
)

PARAM_RE = re.compile(
    r"""
    ([A-Za-z_][A-Za-z0-9_]*)      
    \s*
    \(
    \s*
    ([^,]+?)                      
    \s*,\s*
    "([^"]*)"                     
    \s*
    \)
    """,
    re.VERBOSE | re.DOTALL
)


def _parse_default_value(raw: str):
    s = raw.strip()

    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)

    if re.fullmatch(r"[+-]?\d+\.\d+", s):
        return float(s)

    return s


def parse_xs_text(text: str) -> XSParseResult:
    script_name = None
    m = SCRIPT_NAME_RE.search(text)
    if m:
        script_name = m.group(1).strip()

    params: list[ParamSpec] = []
    m2 = INPUT_BLOCK_RE.search(text)
    if m2:
        block = m2.group(1)
        for pm in PARAM_RE.finditer(block):
            name = pm.group(1).strip()
            default_raw = pm.group(2).strip()
            label = pm.group(3).strip()
            default = _parse_default_value(default_raw)
            params.append(ParamSpec(name=name, default=default, label=label))

    return XSParseResult(
        script_name=script_name,
        params=params,
        raw_text=text,
    )


def parse_xs_file(path: str | Path) -> XSParseResult:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return parse_xs_text(text)