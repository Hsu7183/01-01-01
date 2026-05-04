from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .secret_config import resolve_openai_api_key


SUPPORTED_RESEARCH_RUNTIMES = {"0313plus", "0313plus_modular"}


def _check_path(label: str, path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    return {
        "label": label,
        "ok": bool(path_text) and path.exists(),
        "detail": path_text or "-",
    }


def collect_research_preflight(
    *,
    model: str | None = None,
    script_name: str,
    xs_path: str,
    minute_path: str,
    daily_path: str,
    param_preset_path: str | None = None,
    api_key_override: str | None = None,
) -> dict[str, Any]:
    use_mock_model = str(model or "").strip().lower().startswith("mock")
    normalized_script_name = str(script_name or "").strip().lower()
    checks: list[dict[str, Any]] = [
        {
            "label": "Strategy Runtime",
            "ok": normalized_script_name in SUPPORTED_RESEARCH_RUNTIMES,
            "detail": f"current={script_name}",
        },
        _check_path("XS Path", xs_path),
        _check_path("M1 Path", minute_path),
        _check_path("D1 Path", daily_path),
        _check_path("Param Preset", param_preset_path or ""),
        {
            "label": "OpenAI SDK",
            "ok": True if use_mock_model else importlib.util.find_spec("openai") is not None,
            "detail": "mock-local skips this check" if use_mock_model else "python package `openai`",
        },
        {
            "label": "OPENAI_API_KEY",
            "ok": True if use_mock_model else bool(resolve_openai_api_key(api_key_override)),
            "detail": "mock-local skips this check" if use_mock_model else "environment variable, Streamlit secrets, or GUI input",
        },
    ]

    issues: list[str] = []
    if normalized_script_name not in SUPPORTED_RESEARCH_RUNTIMES:
        issues.append("目前 AI 研究模式只接到 `0313plus` 的 Python 回測鏡像。")
    for item in checks:
        if item["ok"]:
            continue
        label = str(item["label"])
        if label == "XS Path":
            issues.append(f"XS 路徑不存在：{xs_path or '-'}")
        elif label == "M1 Path":
            issues.append(f"M1 路徑不存在：{minute_path or '-'}")
        elif label == "D1 Path":
            issues.append(f"D1 路徑不存在：{daily_path or '-'}")
        elif label == "Param Preset":
            issues.append(f"參數 preset 路徑不存在：{param_preset_path or '-'}")
        elif label == "OpenAI SDK":
            issues.append("尚未安裝 `openai` 套件。")
        elif label == "OPENAI_API_KEY":
            issues.append("尚未設定 `OPENAI_API_KEY`。")

    return {
        "checks": checks,
        "issues": issues,
        "can_start": not issues,
        "install_hint": "py -m pip install openai",
    }
