from __future__ import annotations

from pathlib import Path


XQ_XSCRIPT_POLICY_VERSION = "2026-03-17"
XQ_XSCRIPT_POLICY_SOURCE_DOC = (
    r"C:\Users\User\Documents\台指期 1 分 K 日當沖 XScript 程式撰寫最高規範（新聊天專用最終版20260317）.docx"
)
XQ_XSCRIPT_POLICY_REPO_DOC = str(
    Path(__file__).resolve().parents[2] / "docs" / "xscript_policy_v20260317.md"
)
XQ_XSCRIPT_POLICY_CHANGE_RULE = (
    "此規範為專案內建最高法源；除非使用者明確提供新版或同意修訂，否則不得自行更改。"
)

XQ_XSCRIPT_POLICY_SUMMARY = """
台指期 1 分 K 日當沖策略一律以純 XScript / XS 撰寫，並符合 XQ / XS 可直接使用格式。
所有正式交易判斷只能使用已完成、已定錨資料，並在當根 Open 首次出現瞬間完成判斷與執行。
當根 Open 只能作為即時可得價格與執行價；不得先用當根 Open 更新指標，再回頭判斷同根 Open。
每根 K 棒只能執行一次交易動作；出場優先於進場；禁止同 Bar 反手；訊號不保留、不遞延。
預設判斷資料優先使用 Close[1] 與由 Close 計算後再取 [1] 的技術指標；未加 [1] 的浮動指標值不得直接用於當根交易判斷。
所有 XS 程式都必須檢查分鐘線執行環境：if barfreq <> "Min" then raiseRunTimeError("本腳本僅支援分鐘線");
固定模組架構為 C1 參數區、C2 基礎資料與指標、C3 進場、C4 出場、C5 狀態更新、C6 輸出。
狀態機固定順序是先出場再進場；持倉相關狀態在平倉、強平或跨日時都必須完整歸零。
ATR 必須逐筆 freeze 為 entryATR；VWAP 必須手動累積、每日重置，且交易判斷使用 vwap[1]。
TXT 正式輸出必須使用單一完整字串、單參數 print、固定 14 碼時間戳、全檔單次 header，且不得有多餘空白。
舊策略 1001plus+、1001、0807 只可作為參考，不得凌駕本規範；如衝突一律以本規範為準。
""".strip()


def get_policy_prompt_block() -> str:
    return (
        f"XScript 最高規範版本：{XQ_XSCRIPT_POLICY_VERSION}\n"
        f"來源：{XQ_XSCRIPT_POLICY_SOURCE_DOC}\n"
        f"內建摘要：{XQ_XSCRIPT_POLICY_REPO_DOC}\n"
        f"變更規則：{XQ_XSCRIPT_POLICY_CHANGE_RULE}\n"
        "必須遵守的摘要如下：\n"
        f"{XQ_XSCRIPT_POLICY_SUMMARY}"
    )


def get_policy_reference_text() -> str:
    return (
        f"PolicyVersion={XQ_XSCRIPT_POLICY_VERSION}\n"
        f"PolicySourceDoc={XQ_XSCRIPT_POLICY_SOURCE_DOC}\n"
        f"PolicyRepoDoc={XQ_XSCRIPT_POLICY_REPO_DOC}\n"
        f"ChangeRule={XQ_XSCRIPT_POLICY_CHANGE_RULE}\n"
        "\n"
        f"{XQ_XSCRIPT_POLICY_SUMMARY}\n"
    )
