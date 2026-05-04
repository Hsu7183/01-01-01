from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .paths import PROJECT_ROOT


STREAMLIT_SECRETS_PATH = PROJECT_ROOT / ".streamlit" / "secrets.toml"


def _load_streamlit_secrets() -> dict:
    if not STREAMLIT_SECRETS_PATH.exists():
        return {}
    try:
        with STREAMLIT_SECRETS_PATH.open("rb") as fh:
            payload = tomllib.load(fh)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_openai_api_key(api_key_override: str | None = None) -> str:
    override = str(api_key_override or "").strip()
    if override:
        return override

    env_key = os.getenv("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key

    secrets_payload = _load_streamlit_secrets()
    root_key = str(secrets_payload.get("OPENAI_API_KEY", "") or "").strip()
    if root_key:
        return root_key

    openai_section = secrets_payload.get("openai") or {}
    if isinstance(openai_section, dict):
        section_key = str(openai_section.get("api_key", "") or "").strip()
        if section_key:
            return section_key

    return ""


def has_streamlit_openai_secret() -> bool:
    return bool(resolve_openai_api_key())
