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
RANGE_PATTERN = re.compile(r"^(\d{1,2}/\d{1,2})[-~～–—至到](\d{1,2}/\d{1,2})$")

# 支援的假別關鍵字，之後要加新假別直接加進這個清單就好
LEAVE_KEYWORDS = ["特休", "生理假", "病假", "事假", "補休", "喪假", "婚假", "產假", "陪產假", "請假"]

PERSON_SPLIT_PATTERN = re.compile(r"[、,，/]")

WEEKDAY_CHAR_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

TEMPLATE_WEEKLY_PATTERN = re.compile(r"^每[週周]([一二三四五六日])\s*(.+)$")
TEMPLATE_MONTHLY_DAY_PATTERN = re.compile(r"^每月(\d{1,2})[號号]?\s*(.+)$")
TEMPLATE_MONTHLY_LAST_PATTERN = re.compile(r"^每月(?:底|最後一天)\s*(.+)$")

# 例如「#12 改成交採購報表給財務」
EDIT_TASK_PATTERN = re.compile(r"^#(\d+)\s*改成\s*(.+)$")


def parse_edit_task(text: str):
    """回傳 (task_id, new_content) 或 None"""
    m = EDIT_TASK_PATTERN.match(text.strip())
    if not m:
        return None
    task_id = int(m.group(1))
    new_content = m.group(2).strip()
    if not new_content:
        return None
    return task_id, new_content


def split_persons(text: str):
    """把「蕾蕾、小菁」這種多人字串拆成清單"""
    return [p.strip() for p in PERSON_SPLIT_PATTERN.split(text) if p.strip()]


def find_leave_keyword(text: str):
    """在text中找出最早出現的假別關鍵字，回傳 (關鍵字, 起始位置) 或 None"""
    best_kw, best_idx = None, None
    for kw in LEAVE_KEYWORDS:
        idx = text.find(kw)
        if idx != -1 and (best_idx is None or idx < best_idx):
            best_kw, best_idx = kw, idx
    if best_kw is None:
        return None
    return best_kw, best_idx


def parse_template_input(text: str):
    """
    解析重複性任務範本，例如：
      "每週五 交週報" -> weekly, weekday=4(五)
      "每月5號 對帳"  -> monthly_day, day=5
      "每月底 自評"   -> monthly_last
    回傳 {"rule_type":..., "rule_value":..., "content":...} 或 None（不是範本語法）
    """
    text = text.strip()

    m = TEMPLATE_WEEKLY_PATTERN.match(text)
    if m:
        weekday_char, content = m.group(1), m.group(2).strip()
        if content:
            return {"rule_type": "weekly", "rule_value": WEEKDAY_CHAR_MAP[weekday_char], "content": content}
        return None

    m = TEMPLATE_MONTHLY_LAST_PATTERN.match(text)
    if m:
        content = m.group(1).strip()
        if content:
            return {"rule_type": "monthly_last", "rule_value": None, "content": content}
        return None

    m = TEMPLATE_MONTHLY_DAY_PATTERN.match(text)
    if m:
        day, content = int(m.group(1)), m.group(2).strip()
        if content and 1 <= day <= 31:
            return {"rule_type": "monthly_day", "rule_value": day, "content": content}
        return None

    return None


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

    # 先檢查是否為日期區間（例如 8/15-8/17），可用於請假登記或連續多天待辦
    range_result = parse_date_range_token(first_token)
    if range_result:
        start_date, end_date = range_result
        leave_kw = find_leave_keyword(rest)
        if leave_kw:
            kw, idx = leave_kw
            persons = split_persons(rest[:idx])
            note = rest[idx + len(kw):].strip() or None
            if not persons:
                return {"type": "unknown"}
            return {
                "type": "leave_range", "start_date": start_date, "end_date": end_date,
                "persons": persons, "leave_type": kw, "note": note,
            }
        if rest.strip():
            # 沒有假別關鍵字，視為連續多天的待辦事項（例如出差、駐點）
            return {"type": "task_range", "start_date": start_date, "end_date": end_date, "content": rest.strip()}
        return {"type": "unknown"}

    date_str = parse_date_token(first_token)
    if date_str is None:
        # 沒有辨識出日期，整句視為今天的待辦事項
        return {"type": "task", "date": _now().strftime("%Y-%m-%d"), "content": text}

    leave_kw = find_leave_keyword(rest)
    if leave_kw:
        kw, idx = leave_kw
        persons = split_persons(rest[:idx])
        note = rest[idx + len(kw):].strip() or None
        if not persons:
            return {"type": "unknown"}
        return {"type": "leave", "date": date_str, "persons": persons, "leave_type": kw, "note": note}

    if not rest.strip():
        return {"type": "unknown"}

    return {"type": "task", "date": date_str, "content": rest.strip()}
