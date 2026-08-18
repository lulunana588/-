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

# 直接用文字刪除/完成事項，兩種語序都支援，因為使用者自然會打出任何一種：
# 「#38 刪除」「#38 完成」（編號在前，跟編輯語法同一個習慣）
# 「刪除 #38」「完成 #38」（動作在前，跟打指令的直覺一樣）
TASK_ACTION_ID_FIRST_PATTERN = re.compile(r"^#(\d+)\s*(刪除|完成)$")
TASK_ACTION_WORD_FIRST_PATTERN = re.compile(r"^(刪除|完成)\s*#(\d+)$")
TASK_ACTION_MAP = {"刪除": "delete", "完成": "done"}

# 例如「等 廠商 回覆報價單」「等 主管簽核採購單」「8/15 等 廠商 回覆報價單」
WAITING_PATTERN = re.compile(r"^(?:(\d{1,2}[/\-]\d{1,2})\s+)?等\s+(.+)$")

# 精確時間點提醒：先抓「...提醒[我]內容」，日期/時間都在「提醒」前面
REMINDER_SPLIT_PATTERN = re.compile(r"^(.+?)提醒(?:我)?\s*(.+)$")
REMINDER_DATE_PREFIX_PATTERN = re.compile(r"^(今天|明天|後天|\d{1,2}[/\-]\d{1,2})\s*")

# 「14:30」這種冒號格式，當成24小時制字面值（不用判斷上下午）
COLON_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")

# 中文時間一定要帶上午/早上/凌晨/中午/下午/晚上/半夜其中一個，避免「9點」這種模糊講法被誤判
CN_TIME_PATTERN = re.compile(
    r"^(上午|早上|凌晨|半夜|中午|下午|晚上)\s*(\d{1,2})(?:點|時)?(?:(\d{1,2})分|(半))?$"
)


def _parse_time_token(text: str):
    """把時間文字轉成「HH:MM」字串，看不懂就回傳None（包含沒有上下午標示的模糊時間）"""
    text = text.strip()
    if not text:
        return None

    m = COLON_TIME_PATTERN.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    m = CN_TIME_PATTERN.match(text)
    if not m:
        return None
    ampm_word, hour_str, minute_str, half = m.group(1), m.group(2), m.group(3), m.group(4)
    hour = int(hour_str)
    if not (1 <= hour <= 12):
        return None
    minute = int(minute_str) if minute_str else (30 if half else 0)
    if minute > 59:
        return None

    if ampm_word in ("上午", "早上", "凌晨", "半夜"):
        if hour == 12:
            hour = 0
    elif ampm_word == "中午":
        hour = 12
    elif ampm_word in ("下午", "晚上"):
        if hour != 12:
            hour += 12
    return f"{hour:02d}:{minute:02d}"


def parse_reminder_input(text: str):
    """
    解析精確時間點的提醒（不是待辦，是到時間會主動推播的鬧鐘式提醒），例如：
      "下午2點 提醒我打電話給廠商"    -> 今天14:00
      "下午2點半 提醒我打電話"        -> 今天14:30
      "明天上午9點 提醒我交報告"      -> 明天09:00
      "8/15 14:30 提醒我開會"         -> 2026-08-15 14:30
    一定要能辨識出明確時間（中文時間必須帶上午/下午等字樣，或用HH:MM冒號格式）才算數，
    看不懂時間就回傳None（不是提醒語法，交給後面的一般待辦/請假解析繼續處理，
    這樣「提醒我打電話」這種沒帶時間的話還是會正常變成今天的待辦，不會憑空消失）。
    回傳 {"date":..., "time": "HH:MM", "content": str} 或 None
    """
    m = REMINDER_SPLIT_PATTERN.match(text.strip())
    if not m:
        return None
    prefix, content = m.group(1).strip(), m.group(2).strip()
    if not content:
        return None

    date_str = _now().strftime("%Y-%m-%d")
    date_m = REMINDER_DATE_PREFIX_PATTERN.match(prefix)
    if date_m:
        parsed_date = parse_date_token(date_m.group(1))
        if not parsed_date:
            return None
        date_str = parsed_date
        prefix = prefix[date_m.end():].strip()

    time_str = _parse_time_token(prefix)
    if not time_str:
        return None

    return {"date": date_str, "time": time_str, "content": content}


def parse_waiting_input(text: str):
    """
    解析「等待中」事項（等別人回覆/處理，不是自己要做的事），例如：
      "等 廠商 回覆報價單"        -> 從今天開始等，對象「廠商」，事項「回覆報價單」
      "等 主管簽核採購單"         -> 從今天開始等，沒拆出明確對象，事項就是整句
      "8/15 等 廠商 回覆報價單"   -> 從8/15開始等
    回傳 {"date":..., "waiting_on": str或None, "content": str} 或 None（不是等待語法）
    """
    text = text.strip()
    m = WAITING_PATTERN.match(text)
    if not m:
        return None

    date_token, rest = m.group(1), m.group(2).strip()
    if not rest:
        return None

    if date_token:
        date_str = parse_date_token(date_token)
        if not date_str:
            return None
    else:
        date_str = _now().strftime("%Y-%m-%d")

    parts = rest.split(None, 1)
    if len(parts) == 2:
        waiting_on, content = parts[0], parts[1]
    else:
        waiting_on, content = None, rest

    return {"date": date_str, "waiting_on": waiting_on, "content": content}


def parse_edit_task(text: str):
    """
    解析「#id 改成 X」，回傳其中一種：
      {"task_id": 38, "field": "date", "value": "2026-08-20"}   -> X整段就是看得懂的日期，視為改期
      {"task_id": 38, "field": "content", "value": "交報告初稿"} -> 其他情況，視為改內容（原本就有的行為）
      None -> 完全沒對到格式

    只有「改成」後面整段（去掉頭尾空白後）剛好就是一個完整的日期詞（8/20、明天、今天、後天）時，
    才會判斷成改期；只要後面還多了別的字，就照舊當成改內容，避免「#38 改成 8/20 記得帶資料」
    這種混合寫法被誤判成只改日期、把後面的內容整段吃掉。
    """
    m = EDIT_TASK_PATTERN.match(text.strip())
    if not m:
        return None
    task_id = int(m.group(1))
    new_value = m.group(2).strip()
    if not new_value:
        return None
    # 注意：parse_date_token內部用的是match()（只比對開頭），
    # 這裡刻意改用fullmatch確認「整段」都是日期格式，
    # 避免「改成8/20記得帶資料」這種沒有空白分隔的混合寫法被誤判成只改日期。
    if new_value in ("今天", "今日", "明天", "明日", "後天") or DATE_PATTERN.fullmatch(new_value):
        date_str = parse_date_token(new_value)
        if date_str:
            return {"task_id": task_id, "field": "date", "value": date_str}
    return {"task_id": task_id, "field": "content", "value": new_value}


def parse_task_action(text: str):
    """
    解析用文字直接刪除/完成事項的指令，例如：
      "#38 刪除"／"刪除 #38"  -> {"action": "delete", "task_id": 38}
      "#38 完成"／"完成 #38"  -> {"action": "done", "task_id": 38}
    回傳 {"action":..., "task_id": int} 或 None（不是這個語法）
    """
    text = text.strip()

    m = TASK_ACTION_ID_FIRST_PATTERN.match(text)
    if m:
        return {"action": TASK_ACTION_MAP[m.group(2)], "task_id": int(m.group(1))}

    m = TASK_ACTION_WORD_FIRST_PATTERN.match(text)
    if m:
        return {"action": TASK_ACTION_MAP[m.group(1)], "task_id": int(m.group(2))}

    return None


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
