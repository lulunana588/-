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
RANGE_PATTERN = re.compile(r"^(\d{1,2}/\d{1,2})[-~](\d{1,2}/\d{1,2})$")


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


def parse_date_range_token(token: str):
    """回傳 (start_date, end_date) 或 None。只支援明確的 8/15-8/17 或 8/15~8/17 格式"""
    m = RANGE_PATTERN.match(token)
    if not m:
        return None
    start = parse_date_token(m.group(1))
    end = parse_date_token(m.group(2))
    if not start or not end or start > end:
        return None
    return start, end


def parse_input(raw_text: str):
    """
    回傳其中一種：
      {"type": "leave", "date": "2026-08-15", "person": "蕾蕾", "note": "半天" 或 None}
      {"type": "leave_range", "start_date": "2026-08-15", "end_date": "2026-08-17", "person": "蕾蕾", "note": None}
      {"type": "task", "date": "2026-08-15", "content": "交採購報表"}
      {"type": "unknown"}
    """
    text = raw_text.strip()
    if not text:
        return {"type": "unknown"}

    parts = text.split(None, 1)
    first_token = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    # 先檢查是否為日期區間（例如 8/15-8/17），只用於請假登記
    range_result = parse_date_range_token(first_token)
    if range_result:
        start_date, end_date = range_result
        if "請假" in rest:
            cleaned = rest.replace("請假", " ").strip()
            segs = cleaned.split(None, 1)
            person = segs[0] if segs else ""
            note = segs[1] if len(segs) > 1 else None
            if not person:
                return {"type": "unknown"}
            return {"type": "leave_range", "start_date": start_date, "end_date": end_date, "person": person, "note": note}
        return {"type": "unknown"}

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
