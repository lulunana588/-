"""
自然語言摘要產生器
優先呼叫Groq LLM生成有溫度的秘書提醒句，跟你其他bot共用同一把Groq API金鑰。
若沒設定金鑰、或API呼叫失敗（逾時、額度用完等），自動退回固定模板，
確保推播不會因為AI呼叫失敗而整個中斷——這是文字問候語，不是關鍵資料，
所以就算AI掛了也要有東西可以送出去。
"""
import os
import logging

import requests

logger = logging.getLogger("secretary-bot")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _fallback_summary(context: dict) -> str:
    """Groq無法使用時的固定模板，資訊量跟AI版本一致，只是語氣比較樣板"""
    pending = context.get("pending_count", 0)
    leave_names = context.get("leave_names", [])
    overdue_count = context.get("overdue_count", 0)
    is_tomorrow = context.get("is_tomorrow", False)

    greeting = "明天預告" if is_tomorrow else "早安 Luna"

    segments = []
    if pending:
        segments.append(f"{pending}項待辦")
    if leave_names:
        segments.append("、".join(leave_names))
    if overdue_count:
        segments.append(f"{overdue_count}項逾期未處理")

    if segments:
        body = "，".join(segments)
    else:
        body = "沒有安排，很輕鬆" if is_tomorrow else "今天很輕鬆，沒有待辦事項"

    return f"{greeting}，{body}"


def generate_summary(context: dict) -> str:
    """
    context 需要包含：
      pending_count(int), leave_names(list[str]), overdue_count(int),
      urgent_overdue_count(int), due_soon_count(int), returning_names(list[str]),
      is_tomorrow(bool)
    回傳一句適合當推播開頭的中文句子。
    """
    if not GROQ_API_KEY:
        return _fallback_summary(context)

    is_tomorrow = context.get("is_tomorrow", False)
    leave_names = context.get("leave_names", [])

    prompt_lines = [
        "你是Luna的貼心秘書，" + ("要跟她說明天的行事曆已經整理好了" if is_tomorrow else "要跟她說早安、今天的行事曆已經整理好了") + "。",
        "用繁體中文寫一句簡短、溫暖、自然的開場白，最多20字，不要加引號或表情符號。",
        "非常重要：這句話裡絕對不能出現任何具體數字、人名、任務內容、會議、日期、地點等細節——"
        "這些事實之後會由另一段文字列出，你完全不知道細節是什麼，只需要負責語氣溫暖自然的開場白就好，不要自己猜測或編造細節。",
        "參考風格（不要照抄，自己換句話說一次就好）："
        + ("「明天的安排我整理好了，辛苦妳了」" if is_tomorrow else "「早安，今天的行事曆來囉，一起加油」"),
    ]
    prompt = "\n".join(prompt_lines)

    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 40,
                "temperature": 0.4,
            },
            timeout=8,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = text.strip('"').strip("「").strip("」").strip()
        # 安全檢查：AI被明確要求不要提具體數字/細節，若出現數字代表可能還是講了不該講的內容，直接捨棄改用模板
        has_digit = any(ch.isdigit() for ch in text)
        too_long = len(text) > 30
        if text and not has_digit and not too_long:
            return text
    except Exception:
        logger.warning("Groq摘要產生失敗，改用固定模板", exc_info=True)

    return _fallback_summary(context)
