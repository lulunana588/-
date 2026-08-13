"""
解析使用者輸入：
- 請假登記： "8/15 蕾蕾 請假"、"8/15 蕾蕾請假"、"8/15 蕾蕾 請假 半天"
- 待辦事項： "8/15 交採購報表"、"今天 交採購報表"、"明天 盤點SIM卡"
"""
import re
from datetime import datetime, timedelta, timezone
import config

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))

DATE_PATTERN = re.compile(r"^(\d{1,2})[/\-](\d{1,2})")


def _now():
    return datetime.now(TW_TZ)


def parse_date_token(token: str, text_after: str = ""):
    """回傳 YYYY-MM-DD 或 None。支援 8/15、8-15、今天、明天、後天"""
    now = _now()
    if token in ("今天", "今日"):
        return now.strftime("%Y-%m-%d")
    if token in ("明天", "明日"):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if token in ("後天",):
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")

    m = DATE_PATTERN.match(token)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            dt = datetime(year, month, day)
        except ValueError:
            return None
        # 若日期已經過去超過60天，視為指的是明年（跨年情境）
        if (now.replace(tzinfo=None) - dt).days > 60:
            dt = datetime(year + 1, month, day)
        return dt.strftime("%Y-%m-%d")
    return None


def parse_input(raw_text: str):
    """
    回傳其中一種：
      {"type": "leave", "date": "2026-08-15", "person": "蕾蕾", "note": "半天" 或 None}
      {"type": "task", "date": "2026-08-15", "content": "交採購報表"}
      {"type": "unknown"}
    """
    text = raw_text.strip()
    if not text:
        return {"type": "unknown"}

    parts = text.split(None, 1)
    first_token = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    date_str = parse_date_token(first_token)
    if date_str is None:
        # 沒有辨識出日期，整句視為今天的待辦事項
        return {"type": "task", "date": _now().strftime("%Y-%m-%d"), "content": text}

    if "請假" in rest:
        # 例如："蕾蕾 請假" 或 "蕾蕾請假 半天"
        cleaned = rest.replace("請假", " ").strip()
        segs = cleaned.split(None, 1)
        person = segs[0] if segs else ""
        note = segs[1] if len(segs) > 1 else None
        if not person:
            return {"type": "unknown"}
        return {"type": "leave", "date": date_str, "person": person, "note": note}

    if not rest.strip():
        return {"type": "unknown"}

    return {"type": "task", "date": date_str, "content": rest.strip()}
