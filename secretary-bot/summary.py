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
        "你是Luna的貼心秘書，幫她寫一句簡短的" + ("明日預告" if is_tomorrow else "早安提醒") + "。",
        "用繁體中文，語氣自然親切像真人助理跟主管報告，不要用制式罐頭句，只寫一句話，不超過40字，不要加引號或表情符號：",
        f"待辦事項：{context.get('pending_count', 0)}項未完成",
        f"請假同仁：{('、'.join(leave_names)) if leave_names else '無'}",
        f"逾期事項：{context.get('overdue_count', 0)}項",
    ]
    if context.get("due_soon_count"):
        prompt_lines.append(f"即將到期（兩天內）：{context['due_soon_count']}項")
    if context.get("returning_names"):
        prompt_lines.append(f"今天銷假回來的同仁：{'、'.join(context['returning_names'])}")
    prompt = "\n".join(prompt_lines)

    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
                "temperature": 0.7,
            },
            timeout=8,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = text.strip('"').strip("「").strip("」").strip()
        if text:
            return text
    except Exception:
        logger.warning("Groq摘要產生失敗，改用固定模板", exc_info=True)

    return _fallback_summary(context)
