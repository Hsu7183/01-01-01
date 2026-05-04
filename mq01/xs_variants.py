from __future__ import annotations

import re
from typing import Any, Mapping


def format_param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def render_indicator_xs(base_xs_text: str, best_params: Mapping[str, Any]) -> str:
    rendered_lines: list[str] = []
    for line in base_xs_text.splitlines():
        updated_line = line
        for name, value in best_params.items():
            pattern = rf"^(\s*{re.escape(name)}\s*\()\s*([^,]+)(,.*)$"
            match = re.match(pattern, updated_line)
            if match is None:
                continue
            updated_line = f"{match.group(1)}{format_param_value(value)}{match.group(3)}"
            break
        rendered_lines.append(updated_line)
    return "\n".join(rendered_lines) + "\n"


_TRADE_SECTION = """//====================== C10.交易版執行 ======================
if isMinChart then begin
    if (sessOnManage = 1) and (CurrentBar > warmupBars) and (lastMarkBar = CurrentBar) then begin
        if ForceExitTrig or LongExitTrig or ShortExitTrig then begin
            if Position <> 0 then
                SetPosition(0, MARKET);
        end;
    end;

    if (sessOnEntry = 1) and (CurrentBar > warmupBars) and (lastMarkBar = CurrentBar) then begin
        if LongEntrySig then begin
            if Position <> 1 then
                SetPosition(1, MARKET);
        end
        else if ShortEntrySig then begin
            if Position <> -1 then
                SetPosition(-1, MARKET);
        end;
    end;
end;

//====================== C11.輸出（交易版不輸出） ======================
// // Plot / Print 交易版不使用
"""


_SECTION_HEADER_RE = re.compile(r"(?m)^//=+\s*C\d+\..*$")
_EXECUTABLE_OUTPUT_LINE_RE = re.compile(r"^\s*(?:print\s*\(|plot\d+\s*\()", re.IGNORECASE)


def _strip_output_sections(xs_prefix: str) -> str:
    matches = list(_SECTION_HEADER_RE.finditer(xs_prefix))
    if not matches:
        return xs_prefix

    kept_chunks: list[str] = []
    cursor = 0

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(xs_prefix)
        section = xs_prefix[start:end]

        kept_chunks.append(xs_prefix[cursor:start])
        if "print(file(" not in section.lower():
            kept_chunks.append(section)
        cursor = end

    kept_chunks.append(xs_prefix[cursor:])
    return "".join(kept_chunks)


def _strip_executable_output_lines(xs_prefix: str) -> str:
    trailing_newline = xs_prefix.endswith("\n")
    safe_lines: list[str] = []

    for line in xs_prefix.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            safe_lines.append(line)
            continue
        if _EXECUTABLE_OUTPUT_LINE_RE.match(line):
            safe_lines.append(re.sub(r"^(\s*)", r"\1// ", line, count=1))
            continue
        safe_lines.append(line)

    rendered = "\n".join(safe_lines)
    if trailing_newline:
        rendered += "\n"
    return rendered


def render_trade_xs(base_xs_text: str, best_params: Mapping[str, Any]) -> str:
    rendered = render_indicator_xs(base_xs_text, best_params)
    marker = "//====================== C10."
    if marker in rendered:
        rendered = rendered.split(marker, 1)[0]
    rendered = _strip_output_sections(rendered)
    rendered = _strip_executable_output_lines(rendered).rstrip() + "\n\n"
    return rendered + _TRADE_SECTION
