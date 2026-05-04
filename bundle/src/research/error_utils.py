from __future__ import annotations


def classify_research_error(error_text: str | None) -> str:
    text = str(error_text or "").lower()
    if "insufficient_quota" in text or ("429" in text and "quota" in text):
        return "insufficient_quota"
    if "rate_limit" in text or ("429" in text and "rate limit" in text):
        return "rate_limit"
    if "api key" in text and ("not configured" in text or "not set" in text):
        return "missing_api_key"
    if "param space is empty" in text:
        return "missing_param_space"
    return "generic"


def format_research_error(error_text: str | None) -> str:
    text = str(error_text or "").strip()
    kind = classify_research_error(text)

    if kind == "insufficient_quota":
        return (
            "OpenAI API 回傳 429 insufficient_quota。"
            "這通常表示 API 帳號目前沒有可用額度、尚未啟用 billing，"
            "或已達到 project / account 的支出上限。"
            "這不是程式邏輯錯誤。"
        )
    if kind == "rate_limit":
        return (
            "OpenAI API 回傳 429 rate limit。"
            "代表目前請求頻率超過限制，稍後重試、降低併發，"
            "或改用較小模型通常會改善。"
        )
    if kind == "missing_api_key":
        return "目前沒有可用的 OpenAI API key。請在 GUI 輸入，或放進 `.streamlit/secrets.toml`。"
    if kind == "missing_param_space":
        return "AI 研究模式找不到可用的參數 preset，因此無法產生候選參數。"
    return text or "未知錯誤"
