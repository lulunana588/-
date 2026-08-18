"""
秘書Bot 主程式
指令：
  /today            查看今天的行事曆（隨時可查，不用等10點）
  /done <id>        標記事項完成
  /del <id>         刪除事項
  /push <id> <日期>  把逾期事項手動推到新日期（例如 /push 12 8/16）
  直接傳文字：
    "8/15 交採購報表"     → 新增待辦事項
    "8/15 蕾蕾 請假"      → 登記請假
    "交採購報表"（無日期） → 視為今天的待辦事項

只有 Luna（OWNER_USER_ID）能寫入資料，其他人傳訊息會被禮貌拒絕。
"""
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import db
import parser
import card_renderer
import backup
import summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("secretary-bot")

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

# 例如「蕾蕾這個月請了幾天假」「蕾蕾請了幾天假？」
LEAVE_QUERY_PATTERN = re.compile(r"^(.+?)(這個月|本月)?請了?幾天假[？?]?$")

QUERY_BUTTON_TEXT = "查詢今日"
WEEK_BUTTON_TEXT = "查詢本週"
MONTH_BUTTON_TEXT = "查詢本月"
MANAGE_TASK_BUTTON_TEXT = "管理待辦事項"
MANAGE_LEAVE_BUTTON_TEXT = "管理請假登記"
MANAGE_TEMPLATE_BUTTON_TEXT = "管理重複任務"
MANAGE_WAITING_BUTTON_TEXT = "追蹤等待中"
MANAGE_REMINDER_BUTTON_TEXT = "時間提醒清單"
STATS_BUTTON_TEXT = "本月統計"
WEEK_STATS_BUTTON_TEXT = "本週統計"
HEALTH_BUTTON_TEXT = "服務健康自檢"
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [QUERY_BUTTON_TEXT, WEEK_BUTTON_TEXT, MONTH_BUTTON_TEXT],
        [MANAGE_TASK_BUTTON_TEXT, MANAGE_LEAVE_BUTTON_TEXT],
        [MANAGE_WAITING_BUTTON_TEXT, MANAGE_REMINDER_BUTTON_TEXT],
        [MANAGE_TEMPLATE_BUTTON_TEXT, WEEK_STATS_BUTTON_TEXT, STATS_BUTTON_TEXT],
        [HEALTH_BUTTON_TEXT],
    ],
    resize_keyboard=True,
)
WEEK_CARD_PATH = config.CARD_OUTPUT_PATH.replace("today_card.png", "week_card.png")
MONTH_CARD_PATH = config.CARD_OUTPUT_PATH.replace("today_card.png", "month_card.png")
MANAGE_TASK_LOOKAHEAD_DAYS = 30
MANAGE_LEAVE_LOOKAHEAD_DAYS = 60
MANAGE_TASK_PAGE_SIZE = 8
MANAGE_LEAVE_PAGE_SIZE = 8
MANAGE_WAITING_PAGE_SIZE = 8
MANAGE_REMINDER_PAGE_SIZE = 8

# 例如「搜尋 採購」「找待辦 採購」
SEARCH_PATTERN = re.compile(r"^(?:搜尋|搜索|找待辦)\s*(.+)$")


def is_owner(update: Update) -> bool:
    return str(update.effective_user.id) == str(config.OWNER_USER_ID)


async def build_today_card_path(for_date: str = None) -> str:
    date_str = for_date or db.today_str()
    if date_str == db.today_str():
        ensure_daily_generation()
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday_zh = WEEKDAY_ZH[dt.weekday()]

    leaves = db.get_leaves_for_date(date_str)
    tasks = db.get_tasks_for_date(date_str)
    overdue = db.get_overdue_tasks(date_str)
    for t in overdue:
        task_dt = datetime.strptime(t["task_date"], "%Y-%m-%d")
        t["overdue_days"] = (dt - task_dt).days

    due_soon = db.get_due_soon_tasks(date_str, days_ahead=2)
    for t in due_soon:
        task_dt = datetime.strptime(t["task_date"], "%Y-%m-%d")
        t["days_left"] = (task_dt - dt).days

    img = card_renderer.render_today_card(date_str, weekday_zh, leaves, tasks, overdue, due_soon)
    card_renderer.save_card(img, config.CARD_OUTPUT_PATH)
    return config.CARD_OUTPUT_PATH


def build_today_inline_keyboard(date_str: str, tasks: list):
    """今日卡片附帶的快速完成按鈕：每個未完成事項一顆，加一顆全部完成"""
    pending = [t for t in tasks if t["status"] == "pending"]
    if not pending:
        return None
    rows = []
    for t in pending:
        label = t["content"]
        if len(label) > 16:
            label = label[:16] + "…"
        rows.append([InlineKeyboardButton(f"完成 #{t['id']} {label}", callback_data=f"done|{t['id']}|{date_str}")])
    if len(pending) > 1:
        rows.append([InlineKeyboardButton("全部完成", callback_data=f"doneall|{date_str}")])
    return InlineKeyboardMarkup(rows)


# ───────────────── Commands ─────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "秘書Bot上線了。\n\n"
        "直接傳文字就能記事：\n"
        "・「8/15 交採購報表」→ 新增待辦\n"
        "・「8/15 蕾蕾 特休」「8/15 蕾蕾、小菁 事假」→ 登記請假（支援多假別、多人）\n"
        "・「每週五 交週報」「每月底 自評」→ 建立重複任務\n"
        "・「蕾蕾這個月請了幾天假」→ 查詢請假天數\n"
        "・「#12 改成交採購報表給財務」→ 修改已建立的事項內容\n"
        "・「#12 刪除」「刪除 #12」→ 刪除事項；「#12 完成」「完成 #12」→ 標記完成\n"
        "・「找待辦 採購」「搜尋 採購」→ 依關鍵字搜尋所有待辦（含已完成）\n"
        "・「等 廠商 回覆報價單」「等 主管簽核採購單」→ 追蹤等別人回覆/處理的事，"
        "跟一般待辦分開列，對方回覆了再回來標完成\n"
        "・「下午2點 提醒我打電話給廠商」「明天上午9點 提醒我交報告」→ 精確時間點提醒，"
        "時間一到會主動傳訊息，中文時間一定要帶上午/下午等字樣（或用14:30這種格式），"
        "不然會被當成一般待辦\n\n"
        "帳單追收提醒也會自動整合進來（跟你VPS上的bill_reminder.py共用同一份規則，"
        "不用另外設定，該追的帳單到了會自動出現在當天的待辦裡）。\n\n"
        "指令：\n"
        "/today 查看今天行事曆\n"
        "/week 查看本週行事曆\n"
        "/month 查看本月行事曆\n"
        "/done <編號> 標記完成\n"
        "/del <編號> 刪除事項\n"
        "/push <編號> <日期> 把逾期事項推到新日期\n"
        "/find <關鍵字> 搜尋待辦\n\n"
        "下面按鈕也都能用，不用打指令：\n"
        "查詢今日／查詢本週／查詢本月／管理待辦事項／管理請假登記／追蹤等待中／時間提醒清單／管理重複任務／本週統計／本月統計",
        reply_markup=MAIN_KEYBOARD,
    )


async def send_today_card(update: Update):
    date_str = db.today_str()
    path = await build_today_card_path(date_str)
    tasks = db.get_tasks_for_date(date_str)
    keyboard = build_today_inline_keyboard(date_str, tasks)
    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f, reply_markup=keyboard)


async def build_week_card_path() -> str:
    ensure_daily_generation()
    now = datetime.now(TW_TZ)
    monday = now - timedelta(days=now.weekday())
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        days.append({
            "date": date_str,
            "weekday_zh": WEEKDAY_ZH[d.weekday()],
            "leaves": db.get_leaves_for_date(date_str),
            "tasks": db.get_tasks_for_date(date_str),
        })
    week_start = days[0]["date"]
    week_end = days[-1]["date"]
    img = card_renderer.render_week_card(week_start, week_end, days)
    card_renderer.save_card(img, WEEK_CARD_PATH)
    return WEEK_CARD_PATH


async def send_week_card(update: Update):
    path = await build_week_card_path()
    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f, reply_markup=MAIN_KEYBOARD)


async def build_month_card_path() -> str:
    ensure_daily_generation()
    now = datetime.now(TW_TZ)
    year, month = now.year, now.month

    first_day = datetime(year, month, 1)
    if month == 12:
        next_month_first = datetime(year + 1, 1, 1)
    else:
        next_month_first = datetime(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)

    start_str = first_day.strftime("%Y-%m-%d")
    end_str = last_day.strftime("%Y-%m-%d")

    leaves = db.get_leaves_for_range(start_str, end_str)
    tasks = db.get_tasks_for_range(start_str, end_str)

    weekday_zh_map = {}
    d = first_day
    while d <= last_day:
        weekday_zh_map[d.strftime("%Y-%m-%d")] = WEEKDAY_ZH[d.weekday()]
        d += timedelta(days=1)

    img = card_renderer.render_month_card(year, month, leaves, tasks, weekday_zh_map)
    card_renderer.save_card(img, MONTH_CARD_PATH)
    return MONTH_CARD_PATH


async def send_month_card(update: Update):
    path = await build_month_card_path()
    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f, reply_markup=MAIN_KEYBOARD)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_today_card(update)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_week_card(update)


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_month_card(update)


# ───────────────── 管理待辦（按鈕清單） ─────────────────

def build_manage_task_keyboard(page: int = 0):
    """列出未完成事項（含逾期），每項附「完成」「刪除」兩顆按鈕。
    未完成事項數量沒有上限，長期累積可能撐爆Telegram單則訊息的按鈕數量，
    所以這裡固定每頁只顯示MANAGE_TASK_PAGE_SIZE項，附上下頁按鈕翻頁。"""
    all_tasks = db.get_all_pending_tasks()
    total = len(all_tasks)
    total_pages = max(1, (total + MANAGE_TASK_PAGE_SIZE - 1) // MANAGE_TASK_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * MANAGE_TASK_PAGE_SIZE
    tasks = all_tasks[start:start + MANAGE_TASK_PAGE_SIZE]

    rows = []
    for t in tasks:
        label = t["content"]
        if len(label) > 22:
            label = label[:22] + "…"
        date_disp = t["task_date"][5:]  # MM-DD
        rows.append([InlineKeyboardButton(f"{date_disp}　{label}", callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✓ 完成", callback_data=f"mgdone|{t['id']}|{page}"),
            InlineKeyboardButton("✕ 刪除", callback_data=f"mgtdel|{t['id']}|{page}"),
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ 上一頁", callback_data=f"mgtpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"第{page + 1}/{total_pages}頁", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("下一頁 ›", callback_data=f"mgtpage|{page + 1}"))
        rows.append(nav)

    return all_tasks, InlineKeyboardMarkup(rows) if rows else None, total


async def send_manage_task_list(message_or_query, page: int = 0):
    all_tasks, keyboard, total = build_manage_task_keyboard(page)
    text = f"點「完成」或「刪除」管理事項（共{total}項）：" if all_tasks else "目前沒有未完成的待辦事項"
    if hasattr(message_or_query, "edit_message_text"):
        # 來自 callback_query
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


# ───────────────── 管理請假（按鈕清單） ─────────────────

def build_manage_leave_keyboard(page: int = 0):
    """列出最近7天起的所有請假紀錄，每項附「刪除」按鈕。
    跟待辦清單一樣沒有筆數上限，用同樣的分頁方式避免清單無限長。"""
    today_dt = datetime.strptime(db.today_str(), "%Y-%m-%d")
    start = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    all_leaves = db.get_leaves_from(start)
    total = len(all_leaves)
    total_pages = max(1, (total + MANAGE_LEAVE_PAGE_SIZE - 1) // MANAGE_LEAVE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * MANAGE_LEAVE_PAGE_SIZE
    leaves = all_leaves[start_idx:start_idx + MANAGE_LEAVE_PAGE_SIZE]

    rows = []
    for lv in leaves:
        date_disp = lv["leave_date"][5:]
        note = f"({lv['note']})" if lv.get("note") else ""
        rows.append([InlineKeyboardButton(f"{date_disp}　{lv['person_name']}{lv.get('leave_type') or '請假'}{note}", callback_data="noop")])
        rows.append([InlineKeyboardButton("✕ 刪除這筆請假", callback_data=f"mgldel|{lv['id']}|{page}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ 上一頁", callback_data=f"mglpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"第{page + 1}/{total_pages}頁", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("下一頁 ›", callback_data=f"mglpage|{page + 1}"))
        rows.append(nav)

    return all_leaves, InlineKeyboardMarkup(rows) if rows else None, total


async def send_manage_leave_list(message_or_query, page: int = 0):
    all_leaves, keyboard, total = build_manage_leave_keyboard(page)
    text = f"點「刪除」取消請假登記（共{total}筆）：" if all_leaves else "目前沒有排定的請假紀錄"
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


# ───────────────── 追蹤等待中（等別人回覆/處理，跟一般待辦分開列） ─────────────────

def build_manage_waiting_keyboard(page: int = 0):
    """列出所有「等待中」事項（等對方回覆/處理，不是自己要做的事），
    每項顯示已經等了幾天，附「完成」（對方回覆/處理好了）「刪除」（不用追了）兩顆按鈕。"""
    today_dt = datetime.strptime(db.today_str(), "%Y-%m-%d")
    all_waiting = db.get_all_waiting_tasks()
    total = len(all_waiting)
    total_pages = max(1, (total + MANAGE_WAITING_PAGE_SIZE - 1) // MANAGE_WAITING_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * MANAGE_WAITING_PAGE_SIZE
    items = all_waiting[start_idx:start_idx + MANAGE_WAITING_PAGE_SIZE]

    rows = []
    for t in items:
        task_dt = datetime.strptime(t["task_date"], "%Y-%m-%d")
        waited_days = max(0, (today_dt - task_dt).days)
        label = t["content"]
        if len(label) > 20:
            label = label[:20] + "…"
        days_disp = f"已等{waited_days}天" if waited_days > 0 else "今天開始等"
        rows.append([InlineKeyboardButton(f"{days_disp}　{label}", callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✓ 完成（有回覆了）", callback_data=f"mgwdone|{t['id']}|{page}"),
            InlineKeyboardButton("✕ 不用追了", callback_data=f"mgwdel|{t['id']}|{page}"),
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ 上一頁", callback_data=f"mgwpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"第{page + 1}/{total_pages}頁", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("下一頁 ›", callback_data=f"mgwpage|{page + 1}"))
        rows.append(nav)

    return all_waiting, InlineKeyboardMarkup(rows) if rows else None, total


async def send_manage_waiting_list(message_or_query, page: int = 0):
    all_waiting, keyboard, total = build_manage_waiting_keyboard(page)
    text = (
        f"以下是還在等別人回覆/處理的事（共{total}件），對方回應了就點「完成」：" if all_waiting
        else "目前沒有在追蹤的等待事項\n\n可以直接傳「等 廠商 回覆報價單」「等 主管簽核採購單」來新增"
    )
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


# ───────────────── 精確時間點提醒（鬧鐘式，到時間主動推播） ─────────────────

def build_manage_reminder_keyboard(page: int = 0):
    """列出所有還沒推播的時間提醒，附「取消」按鈕"""
    all_reminders = db.get_upcoming_reminders()
    total = len(all_reminders)
    total_pages = max(1, (total + MANAGE_REMINDER_PAGE_SIZE - 1) // MANAGE_REMINDER_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * MANAGE_REMINDER_PAGE_SIZE
    items = all_reminders[start_idx:start_idx + MANAGE_REMINDER_PAGE_SIZE]

    rows = []
    for r in items:
        date_disp = r["remind_date"][5:]
        label = r["content"]
        if len(label) > 18:
            label = label[:18] + "…"
        rows.append([InlineKeyboardButton(f"{date_disp} {r['remind_time']}　{label}", callback_data="noop")])
        rows.append([InlineKeyboardButton("✕ 取消這個提醒", callback_data=f"mgrdel|{r['id']}|{page}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ 上一頁", callback_data=f"mgrpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"第{page + 1}/{total_pages}頁", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("下一頁 ›", callback_data=f"mgrpage|{page + 1}"))
        rows.append(nav)

    return all_reminders, InlineKeyboardMarkup(rows) if rows else None, total


async def send_manage_reminder_list(message_or_query, page: int = 0):
    all_reminders, keyboard, total = build_manage_reminder_keyboard(page)
    text = (
        f"以下是還沒到時間的提醒（共{total}件），不需要了可以點「取消」：" if all_reminders
        else "目前沒有排定的時間提醒\n\n可以直接傳「下午2點 提醒我打電話給廠商」"
        "「明天上午9點 提醒我交報告」來新增，時間到了會主動推播"
    )
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


# ───────────────── 重複性任務模板 ─────────────────

def describe_template_rule(rule_type: str, rule_value) -> str:
    if rule_type == "weekly":
        return f"每週{WEEKDAY_ZH[rule_value]}"
    if rule_type == "monthly_day":
        return f"每月{rule_value}號"
    if rule_type == "monthly_last":
        return "每月底"
    return "未知規則"


def generate_tasks_from_templates():
    """檢查所有範本，若今天符合規則且今天還沒產生過，就自動新增一筆待辦事項。
    若這次的日期剛好被設定「跳過」（skip_date），就不產生待辦，但一樣標記今天已處理過，
    並清掉skip_date，避免影響到下一次的產生。"""
    today_str = db.today_str()
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    is_last_day_of_month = (today_dt + timedelta(days=1)).month != today_dt.month

    for tpl in db.get_all_templates():
        if tpl.get("last_generated_date") == today_str:
            continue  # 今天已經產生過，避免重複

        matched = False
        if tpl["rule_type"] == "weekly" and today_dt.weekday() == tpl["rule_value"]:
            matched = True
        elif tpl["rule_type"] == "monthly_day" and today_dt.day == tpl["rule_value"]:
            matched = True
        elif tpl["rule_type"] == "monthly_last" and is_last_day_of_month:
            matched = True

        if matched:
            if tpl.get("skip_date") == today_str:
                db.mark_template_generated(tpl["id"], today_str)
                db.set_template_skip(tpl["id"], None)
                logger.info(f"範本 #{tpl['id']} 今天設定了跳過，不產生待辦")
            else:
                db.add_task(today_str, tpl["content"])
                db.mark_template_generated(tpl["id"], today_str)
                logger.info(f"範本 #{tpl['id']} 自動產生今日待辦：{tpl['content']}")


def compute_next_occurrence(rule_type: str, rule_value, start_dt: datetime):
    """從start_dt（含）開始往後找，回傳符合範本規則的下一個日期（datetime）。
    最多找400天避免規則異常時無窮迴圈，正常情況一定能在一個月內找到。"""
    d = start_dt
    for _ in range(400):
        is_last_day = (d + timedelta(days=1)).month != d.month
        if rule_type == "weekly" and d.weekday() == rule_value:
            return d
        if rule_type == "monthly_day" and d.day == rule_value:
            return d
        if rule_type == "monthly_last" and is_last_day:
            return d
        d += timedelta(days=1)
    return None


BILL_REMINDER_SCRIPT_DIR = "/root/bill-reminder"
BILL_REMINDER_LOOKAHEAD_DAYS = 45


def generate_tasks_from_bill_reminder():
    """
    直接呼叫bill_reminder.py裡的get_bills()函式（不重新實作邏輯，也不複製規則清單），
    往後掃描45天內該追的帳單，自動新增對應待辦事項。
    往後掃描是為了讓「查詢本週」「查詢本月」也能提前看到帳單提醒，不用等到當天才出現；
    用內容比對避免同一筆帳單被重複建立。
    這樣以後不管bill_reminder.py的規則或邏輯怎麼調整，秘書Bot都會自動跟著同步。
    """
    today_str = db.today_str()
    if db.get_meta("last_bill_generate_date") == today_str:
        return  # 今天已經掃描過，避免重複

    try:
        if BILL_REMINDER_SCRIPT_DIR not in sys.path:
            sys.path.insert(0, BILL_REMINDER_SCRIPT_DIR)
        import bill_reminder
    except Exception:
        logger.warning("讀取bill_reminder.py規則失敗，略過帳單提醒整合", exc_info=True)
        return

    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    for i in range(BILL_REMINDER_LOOKAHEAD_DAYS):
        d = today_dt + timedelta(days=i)
        bills = bill_reminder.get_bills(d)
        if not bills:
            continue

        date_str = d.strftime("%Y-%m-%d")
        existing_contents = {t["content"] for t in db.get_tasks_for_date(date_str)}
        for place, items in bills.items():
            content = f"帳單提醒：{place}－{'/'.join(items)}"
            if content not in existing_contents:
                db.add_task(date_str, content)
                logger.info(f"帳單規則自動產生待辦（{date_str}）：{content}")

    db.set_meta("last_bill_generate_date", today_str)


def ensure_daily_generation():
    """統一入口：把範本任務、帳單提醒都跑一次自動產生檢查。
    每個函式內部都有「今天是否已產生過」的guard，重複呼叫不會重複建立資料，
    所以可以放心在多個查看入口（今日/本週/本月/服務啟動）都呼叫這個函式，
    確保不管使用者先點哪個按鈕，資料都已經是最新的。"""
    generate_tasks_from_templates()
    generate_tasks_from_bill_reminder()


def build_manage_template_keyboard():
    templates = db.get_all_templates()
    rows = []
    for tpl in templates:
        rule_disp = describe_template_rule(tpl["rule_type"], tpl["rule_value"])
        label = tpl["content"]
        if len(label) > 18:
            label = label[:18] + "…"
        skip_disp = f"（{tpl['skip_date'][5:]}跳過）" if tpl.get("skip_date") else ""
        rows.append([InlineKeyboardButton(f"{rule_disp}　{label}{skip_disp}", callback_data="noop")])
        if tpl.get("skip_date"):
            rows.append([
                InlineKeyboardButton("取消跳過", callback_data=f"mgtplunskip|{tpl['id']}"),
                InlineKeyboardButton("✕ 刪除這個範本", callback_data=f"mgtpldel|{tpl['id']}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton("跳過下一次", callback_data=f"mgtplskip|{tpl['id']}"),
                InlineKeyboardButton("✕ 刪除這個範本", callback_data=f"mgtpldel|{tpl['id']}"),
            ])
    return templates, InlineKeyboardMarkup(rows) if rows else None


async def send_manage_template_list(message_or_query):
    templates, keyboard = build_manage_template_keyboard()
    text = "點「刪除」取消重複任務：" if templates else "目前沒有設定重複性任務\n\n可以直接傳「每週五 交週報」「每月5號 對帳」「每月底 自評」來新增"
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


# ───────────────── 月度統計 ─────────────────

async def send_month_stats(message):
    now = datetime.now(TW_TZ)
    year, month = now.year, now.month
    first_day = datetime(year, month, 1)
    next_month_first = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    start_str = first_day.strftime("%Y-%m-%d")
    end_str = last_day.strftime("%Y-%m-%d")

    tasks = db.get_tasks_for_range(start_str, end_str)
    total_tasks = len(tasks)
    done_tasks = sum(1 for t in tasks if t["status"] == "done")
    pending_tasks = total_tasks - done_tasks
    rate = round(done_tasks / total_tasks * 100) if total_tasks else 0

    leaves = db.get_leaves_for_range(start_str, end_str)
    leave_by_person = {}
    for lv in leaves:
        leave_by_person[lv["person_name"]] = leave_by_person.get(lv["person_name"], 0) + 1
    leave_lines = [f"・{name}：{days}天" for name, days in sorted(leave_by_person.items(), key=lambda x: -x[1])]

    lines = [
        f"{year}年{month}月 統計",
        f"待辦事項共 {total_tasks} 項，完成 {done_tasks} 項，完成率 {rate}%",
        f"未完成 {pending_tasks} 項",
        "",
        f"請假紀錄共 {len(leaves)} 筆：",
    ]
    lines.extend(leave_lines if leave_lines else ["・本月無請假紀錄"])

    await message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def send_week_stats(message):
    now = datetime.now(TW_TZ)
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    start_str = monday.strftime("%Y-%m-%d")
    end_str = sunday.strftime("%Y-%m-%d")

    tasks = db.get_tasks_for_range(start_str, end_str)
    total_tasks = len(tasks)
    done_tasks = sum(1 for t in tasks if t["status"] == "done")
    pending_tasks = total_tasks - done_tasks
    rate = round(done_tasks / total_tasks * 100) if total_tasks else 0

    leaves = db.get_leaves_for_range(start_str, end_str)
    leave_by_person = {}
    for lv in leaves:
        leave_by_person[lv["person_name"]] = leave_by_person.get(lv["person_name"], 0) + 1
    leave_lines = [f"・{name}：{days}天" for name, days in sorted(leave_by_person.items(), key=lambda x: -x[1])]

    lines = [
        f"本週（{start_str[5:]} ～ {end_str[5:]}）統計",
        f"待辦事項共 {total_tasks} 項，完成 {done_tasks} 項，完成率 {rate}%",
        f"未完成 {pending_tasks} 項",
        "",
        f"請假紀錄共 {len(leaves)} 筆：",
    ]
    lines.extend(leave_lines if leave_lines else ["・本週無請假紀錄"])

    await message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


# ───────────────── 關鍵字搜尋待辦 ─────────────────

async def send_task_search_results(message, keyword: str):
    """依內容關鍵字搜尋所有待辦事項（含已完成），方便找回忘記是哪天登記的事項"""
    results = db.search_tasks(keyword)
    if not results:
        await message.reply_text(f"沒有找到內容包含「{keyword}」的待辦事項", reply_markup=MAIN_KEYBOARD)
        return

    lines = [f"搜尋「{keyword}」，共{len(results)}筆（最新在前）："]
    for t in results:
        status_disp = "✓已完成" if t["status"] == "done" else "・待處理"
        lines.append(f"{status_disp} #{t['id']} {t['task_date']}　{t['content']}")
    if len(results) >= 30:
        lines.append("\n（僅顯示最近30筆，若要縮小範圍請用更精確的關鍵字）")
    await message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


# ───────────────── 按人名查詢請假 ─────────────────

async def send_person_leave_query(message, person: str, scope_month: bool):
    if scope_month:
        now = datetime.now(TW_TZ)
        first_day = datetime(now.year, now.month, 1)
        next_month_first = datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)
        last_day = next_month_first - timedelta(days=1)
        leaves = db.get_leaves_by_person(person, first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d"))
        scope_disp = f"{now.year}年{now.month}月"
    else:
        leaves = db.get_leaves_by_person(person)
        scope_disp = "累計"

    if not leaves:
        await message.reply_text(f"{scope_disp}沒有找到「{person}」的請假紀錄", reply_markup=MAIN_KEYBOARD)
        return

    lines = [f"{scope_disp}「{person}」請假紀錄（共{len(leaves)}天）："]
    for lv in leaves:
        note = f"（{lv['note']}）" if lv.get("note") else ""
        lines.append(f"・{lv['leave_date']}　{lv.get('leave_type') or '請假'}{note}")
    await message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/done 事項編號（例如 /done 12）")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("編號要是數字喔")
        return
    ok = db.mark_task_done(task_id)
    await update.message.reply_text(f"#{task_id} 已標記完成 ✅" if ok else f"找不到 #{task_id}（或已經是完成狀態）")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/del 事項編號（例如 /del 12）")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("編號要是數字喔")
        return
    ok = db.delete_task(task_id)
    await update.message.reply_text(f"#{task_id} 已刪除" if ok else f"找不到 #{task_id}")


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法：/push 事項編號 日期（例如 /push 12 8/16）")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("編號要是數字喔")
        return
    new_date = parser.parse_date_token(context.args[1])
    if not new_date:
        await update.message.reply_text("日期看不懂，請用「8/16」或「明天」這種格式")
        return
    ok = db.push_task_to_date(task_id, new_date)
    await update.message.reply_text(f"#{task_id} 已推到 {new_date}" if ok else f"找不到 #{task_id}")


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手動觸發一次備份，方便測試"""
    if not is_owner(update):
        return
    await update.message.reply_text("備份中，請稍等…")
    try:
        file_id = backup.backup_database()
        await update.message.reply_text(f"備份完成 ✓（Drive檔案id：{file_id}）")
    except Exception as e:
        logger.error("手動備份失敗", exc_info=True)
        await update.message.reply_text(f"備份失敗：{e}")


def _format_meta_time(iso_str: str) -> str:
    if not iso_str:
        return "尚無紀錄"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


# 排程任務名稱 -> 換算成台灣時間後，下次執行「應該」是幾點幾分。
# 這張表就是為了讓健康自檢能主動比對「排程算出來的時間」跟「原本設定的時間」對不對得上，
# 這正是之前「早上10點推播其實在晚上6點才觸發」那個時區bug的檢查方式——
# 早該有這個自我檢查，才不用等到漏推播才發現，之後如果再發生類似狀況，一查/health就能看到。
EXPECTED_JOB_TAIPEI_TIME = {
    "早上10點今日推播": (config.PUSH_HOUR, config.PUSH_MINUTE),
    "下班前明日預告": (config.EVENING_PUSH_HOUR, config.EVENING_PUSH_MINUTE),
    "週日資料庫備份": (22, 0),
}


def _check_daily_push_anomaly(now: datetime) -> str:
    """檢查今天的推播有沒有正常發生，回傳異常訊息，沒異常回傳空字串"""
    if now.weekday() not in config.PUSH_WEEKDAYS:
        return ""  # 週末本來就不推播，不算異常
    scheduled_time = now.replace(hour=config.PUSH_HOUR, minute=config.PUSH_MINUTE, second=0, microsecond=0)
    if now < scheduled_time:
        return ""  # 今天還沒到推播時間，先不判斷
    if db.get_meta("last_push_date") != db.today_str():
        return "今天已經過了推播時間，但「上次今日推播」還不是今天，今天的推播可能沒有成功，麻煩檢查一下"
    return ""


def _check_backup_anomaly(now: datetime) -> str:
    """檢查資料庫備份是不是太久沒成功（原本每週日一次），回傳異常訊息，沒異常回傳空字串"""
    last_backup = db.get_meta("last_backup_at")
    if not last_backup:
        return "目前沒有任何成功備份的紀錄，建議傳 /backup 手動測試一次，確認金鑰跟權限正常"
    try:
        last_backup_dt = datetime.fromisoformat(last_backup)
    except Exception:
        return ""
    if last_backup_dt.tzinfo is None:
        last_backup_dt = last_backup_dt.replace(tzinfo=TW_TZ)
    days_since = (now - last_backup_dt).days
    if days_since > 8:
        return f"距離上次成功備份已經 {days_since} 天，超過原本每週一次的頻率，中間可能備份失敗過，建議傳 /backup 手動測試一次"
    return ""


def _check_job_schedule_anomalies(scheduler) -> list:
    """比對排程任務換算成台灣時間的下次執行時間，是否真的對到原本設定的小時分鐘"""
    warnings = []
    for job in scheduler.get_jobs():
        expected = EXPECTED_JOB_TAIPEI_TIME.get(job.name)
        if not expected or not job.next_run_time:
            continue
        expected_hour, expected_minute = expected
        actual = job.next_run_time.astimezone(TW_TZ)
        if (actual.hour, actual.minute) != (expected_hour, expected_minute):
            warnings.append(
                f"排程「{job.name}」換算成台灣時間的下次執行是{actual.strftime('%H:%M')}，"
                f"跟設定的{expected_hour:02d}:{expected_minute:02d}對不上，可能時區設定又跑掉了"
            )
    return warnings


# 同一台VPS上，其他跟secretary-bot無關、各自獨立運作的排程腳本。
# 2026-08-18那次gmail-bot的Google授權悄悄失效、卻整整好幾天沒人發現，
# 就是因為這些腳本本來沒有任何人在主動盯著——所以在這裡把它們的log也一併看一下，
# 只要「太久沒更新」或「內容裡出現錯誤字樣」，就在健康自檢裡主動示警，
# 不用再靠「剛好想到才發現」。
# max_silent_hours 是抓「正常間隔 + 一些緩衝」估出來的，不是精確值，夠用來抓「明顯停擺」就好。
SIBLING_SCRIPTS = {
    "gmail-bot（Gmail掃描，平日一天2次）": {
        "log": "/root/gmail-bot/scanner.log",
        "max_silent_hours": 80,
    },
    "diary-bot（每小時健康檢查）": {
        "log": "/root/diary-bot/health_check.log",
        "max_silent_hours": 3,
        # 2026-08-18實測發現：這支腳本設計成「平常安靜、只有偵測到問題才會出聲」，
        # 靠syslog確認過cron真的每小時準時觸發，但即使正常執行，log也完全不會有任何內容，
        # 所以「太久沒更新」這個判斷方式對它不適用，會一直誤報，只保留內容關鍵字掃描就好。
        "skip_staleness_check": True,
    },
    "diary-bot（平日每日提醒）": {
        "log": "/root/diary-bot/reminder.log",
        "max_silent_hours": 90,
    },
    "diary-bot（每週備份）": {
        "log": "/root/diary-bot/backup_to_drive.log",
        "max_silent_hours": 24 * 9,
    },
    "bill-reminder（每日帳單提醒）": {
        "log": "/root/bill-reminder/log.txt",
        "max_silent_hours": 30,
    },
}

_LOG_ERROR_KEYWORDS = ("Traceback", "Error", "錯誤", "Exception", "failed")


def _check_sibling_scripts_anomalies(now: datetime) -> list:
    """檢查上面那些「跟secretary-bot無關、但住在同一台VPS上」的獨立腳本，
    用log檔案的更新時間、以及內容裡有沒有錯誤字樣，做best-effort的掃描。
    這不是100%準確的判斷（例如log裡剛好提到"error"這個字但其實沒出錯），
    但至少能在「完全沒人發現」跟「至少收到一次提醒」之間，往前跨一步。"""
    warnings = []
    for name, cfg in SIBLING_SCRIPTS.items():
        log_path = cfg["log"]
        if not os.path.exists(log_path):
            continue  # 這台機器上找不到這個log，可能腳本已經不在了，不算異常
        try:
            if not cfg.get("skip_staleness_check"):
                mtime = datetime.fromtimestamp(os.path.getmtime(log_path), tz=TW_TZ)
                silent_hours = (now - mtime).total_seconds() / 3600
                if silent_hours > cfg["max_silent_hours"]:
                    warnings.append(
                        f"「{name}」的紀錄檔已經 {int(silent_hours)} 小時沒有更新，"
                        f"可能排程沒有正常執行，建議登入VPS看一下 {log_path}"
                    )
                    continue  # 已經示警過一次，不用再往下查內容
            with open(log_path, "r", errors="ignore") as f:
                f.seek(max(0, os.path.getsize(log_path) - 4000))
                tail = f.read()
            tail_lower = tail.lower()
            if any(kw.lower() in tail_lower for kw in _LOG_ERROR_KEYWORDS):
                warnings.append(
                    f"「{name}」的紀錄檔最近的內容裡出現疑似錯誤字樣，建議登入VPS看一下 {log_path} 確認"
                )
        except Exception:
            continue  # 讀檔本身出問題就跳過，不要讓健康自檢因此掛掉
    return warnings


async def send_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TW_TZ)
    lines = ["服務健康自檢"]
    lines.append(f"Bot帳號：@{context.bot.username}　運作中 ✓")

    anomalies = []

    push_anomaly = _check_daily_push_anomaly(now)
    if push_anomaly:
        anomalies.append(push_anomaly)
    last_push = db.get_meta("last_push_date")
    lines.append(f"上次今日推播：{last_push or '尚無紀錄'}" + ("　❗" if push_anomaly else ""))

    backup_anomaly = _check_backup_anomaly(now)
    if backup_anomaly:
        anomalies.append(backup_anomaly)
    last_backup = db.get_meta("last_backup_at")
    lines.append(f"上次資料庫備份：{_format_meta_time(last_backup)}" + ("　❗" if backup_anomaly else ""))

    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        jobs = scheduler.get_jobs()
        lines.append(f"\n排程任務（共{len(jobs)}個，全部運作中，時間都已換算成台灣時間）：")
        for job in jobs:
            next_run = job.next_run_time.astimezone(TW_TZ).strftime("%m-%d %H:%M") if job.next_run_time else "未知"
            lines.append(f"・{job.name}　下次執行：{next_run}")
        anomalies.extend(_check_job_schedule_anomalies(scheduler))
    else:
        lines.append("\n排程器狀態：讀取不到，可能需要重啟服務確認")

    try:
        pending_count = len(db.get_all_pending_tasks())
        lines.append(f"\n資料庫讀取：正常 ✓（目前未完成待辦 {pending_count} 項）")
    except Exception as e:
        lines.append(f"\n資料庫讀取：異常 ✗（{e}）")

    groq_status = "已設定" if summary.GROQ_API_KEY else "未設定（會自動降級用固定模板，不影響運作）"
    lines.append(f"Groq AI摘要：{groq_status}")

    checked_siblings = [name for name, cfg in SIBLING_SCRIPTS.items() if os.path.exists(cfg["log"])]
    if checked_siblings:
        lines.append(f"\n同機其他腳本監控（共{len(checked_siblings)}個）：")
        for name in checked_siblings:
            lines.append(f"・{name}")
        anomalies.extend(_check_sibling_scripts_anomalies(now))

    if anomalies:
        lines.append("\n⚠️ 偵測到可能的異常：")
        for a in anomalies:
            lines.append(f"・{a}")
    else:
        lines.append("\n沒有偵測到異常 ✓")

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await send_health_check(update, context)


async def cmd_leaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查詢某天的請假清單（含id，方便配合/delleave刪除）"""
    if context.args:
        date_str = parser.parse_date_token(context.args[0])
        if not date_str:
            await update.message.reply_text("日期看不懂，請用「8/16」或「今天」這種格式")
            return
    else:
        date_str = db.today_str()

    leaves = db.get_leaves_for_date(date_str)
    if not leaves:
        await update.message.reply_text(f"{date_str} 沒有請假紀錄")
        return

    lines = [f"{date_str} 請假名單："]
    for lv in leaves:
        note = f"（{lv['note']}）" if lv.get("note") else ""
        lines.append(f"・#{lv['id']}  {lv['person_name']}{lv.get('leave_type') or '請假'}{note}")
    lines.append("\n要刪除用 /delleave 編號")
    await update.message.reply_text("\n".join(lines))


async def cmd_delleave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/delleave 請假編號（用 /leaves 查編號）")
        return
    try:
        leave_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("編號要是數字喔")
        return
    ok = db.delete_leave(leave_id)
    await update.message.reply_text(f"請假紀錄 #{leave_id} 已刪除" if ok else f"找不到 #{leave_id}")


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/find 關鍵字（例如 /find 採購）")
        return
    keyword = " ".join(context.args)
    await send_task_search_results(update.message, keyword)


# ───────────────── 自然語言輸入 ─────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("這是Luna的專屬秘書Bot，暫不開放其他人登記事項喔🙏")
        return

    text = update.message.text.strip()

    # 編輯已建立的事項：例如「#12 改成交採購報表給財務」（改內容）或「#12 改成 8/20」（改日期）
    edit_result = parser.parse_edit_task(text)
    if edit_result:
        task_id = edit_result["task_id"]
        old_task = db.get_task_by_id(task_id)
        old_date = old_task["task_date"] if old_task else None
        if edit_result["field"] == "date":
            new_date = edit_result["value"]
            ok = db.push_task_to_date(task_id, new_date)
            reply = f"#{task_id} 日期已改成：{new_date}" if ok else f"找不到 #{task_id}"
        else:
            new_content = edit_result["value"]
            ok = db.update_task_content(task_id, new_content)
            reply = f"#{task_id} 內容已改成：{new_content}" if ok else f"找不到 #{task_id}"
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
        # 改之前是今天的事項、或改之後變成今天的事項，都要重新出一張今日卡片，
        # 這樣「把8/20的事項改成今天」跟「把今天的事項改到別天」都能立刻反映在卡片上。
        if ok:
            today = db.today_str()
            new_task = db.get_task_by_id(task_id)
            new_date = new_task["task_date"] if new_task else None
            if today in (old_date, new_date):
                await send_today_card(update)
        return

    # 直接用文字刪除/完成事項：例如「#38 刪除」「刪除 #38」「#38 完成」「完成 #38」，
    # 不用再特地打/del或/done指令、也不用進管理清單找按鈕
    task_action = parser.parse_task_action(text)
    if task_action:
        task_id = task_action["task_id"]
        task = db.get_task_by_id(task_id)
        if task_action["action"] == "delete":
            ok = db.delete_task(task_id)
            reply = f"#{task_id} 已刪除" if ok else f"找不到 #{task_id}"
        else:
            ok = db.mark_task_done(task_id)
            reply = f"#{task_id} 已標記完成 ✅" if ok else f"找不到 #{task_id}（或已經是完成狀態）"
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
        if ok and task and task["task_date"] == db.today_str():
            await send_today_card(update)
        return

    # 用關鍵字比對而非完全相等，避免emoji編碼差異導致按鈕誤判成待辦事項
    if "查詢" in text and "本月" in text:
        await send_month_card(update)
        return
    if "查詢" in text and "本週" in text:
        await send_week_card(update)
        return
    if "查詢" in text and "今日" in text:
        await send_today_card(update)
        return

    if "管理" in text and "待辦" in text:
        await send_manage_task_list(update.message)
        return

    if "管理" in text and "請假" in text:
        await send_manage_leave_list(update.message)
        return

    if "管理" in text and ("重複" in text or "範本" in text):
        await send_manage_template_list(update.message)
        return

    if "追蹤" in text and "等待" in text:
        await send_manage_waiting_list(update.message)
        return

    if "提醒" in text and "清單" in text:
        await send_manage_reminder_list(update.message)
        return

    if "本週統計" in text or ("統計" in text and "本週" in text):
        await send_week_stats(update.message)
        return

    if "本月統計" in text or ("統計" in text and "本月" in text):
        await send_month_stats(update.message)
        return

    if "健康" in text and ("自檢" in text or "檢查" in text):
        await send_health_check(update, context)
        return

    # 關鍵字搜尋待辦：例如「找待辦 採購」「搜尋 採購」
    search_match = SEARCH_PATTERN.match(text)
    if search_match:
        keyword = search_match.group(1).strip()
        if keyword:
            await send_task_search_results(update.message, keyword)
            return

    # 按人名查詢請假紀錄：例如「蕾蕾這個月請了幾天假」「蕾蕾請了幾天假」
    leave_query_match = LEAVE_QUERY_PATTERN.match(text)
    if leave_query_match:
        person = leave_query_match.group(1).strip()
        scope_month = bool(leave_query_match.group(2))
        if person:
            await send_person_leave_query(update.message, person, scope_month)
            return

    # 重複性任務範本：例如「每週五 交週報」「每月5號 對帳」「每月底 自評」
    template_result = parser.parse_template_input(text)
    if template_result:
        tid = db.add_template(template_result["rule_type"], template_result["rule_value"], template_result["content"])
        rule_disp = describe_template_rule(template_result["rule_type"], template_result["rule_value"])
        await update.message.reply_text(
            f"已建立重複任務 #{tid}：{rule_disp} {template_result['content']}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # 精確時間點提醒：例如「下午2點 提醒我打電話給廠商」「明天上午9點 提醒我交報告」
    reminder_result = parser.parse_reminder_input(text)
    if reminder_result:
        rid = db.add_reminder(reminder_result["date"], reminder_result["time"], reminder_result["content"])
        date_disp = "今天" if reminder_result["date"] == db.today_str() else reminder_result["date"]
        await update.message.reply_text(
            f"已設定提醒 #{rid}：{date_disp} {reminder_result['time']}－{reminder_result['content']}\n"
            "時間到了會主動傳訊息提醒妳",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # 等待中事項（等別人回覆/處理）：例如「等 廠商 回覆報價單」「等 主管簽核採購單」
    waiting_result = parser.parse_waiting_input(text)
    if waiting_result:
        display_content = (
            f"{waiting_result['waiting_on']}－{waiting_result['content']}"
            if waiting_result["waiting_on"] else waiting_result["content"]
        )
        task_id = db.add_task(waiting_result["date"], display_content, status="waiting")
        await update.message.reply_text(
            f"已加入等待中追蹤 #{task_id}：{display_content}\n對方回覆/處理好了記得回來標完成",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    result = parser.parse_input(text)

    if result["type"] == "task":
        task_id = db.add_task(result["date"], result["content"])
        await update.message.reply_text(
            f"已新增待辦 #{task_id}：{result['date']} {result['content']}",
            reply_markup=MAIN_KEYBOARD,
        )
        if result["date"] == db.today_str():
            await send_today_card(update)

    elif result["type"] == "leave":
        note_disp = f"（{result['note']}）" if result.get("note") else ""
        leave_type = result.get("leave_type", "請假")
        persons = result["persons"]
        for person in persons:
            db.add_leave(result["date"], person, result.get("note"), leave_type)
        persons_disp = "、".join(persons)
        await update.message.reply_text(
            f"已登記{leave_type}：{result['date']} {persons_disp}{note_disp}",
            reply_markup=MAIN_KEYBOARD,
        )
        if result["date"] == db.today_str():
            await send_today_card(update)

    elif result["type"] == "leave_range":
        start = datetime.strptime(result["start_date"], "%Y-%m-%d")
        end = datetime.strptime(result["end_date"], "%Y-%m-%d")
        n_days = (end - start).days + 1
        leave_type = result.get("leave_type", "請假")
        persons = result["persons"]
        for person in persons:
            d = start
            while d <= end:
                db.add_leave(d.strftime("%Y-%m-%d"), person, result.get("note"), leave_type)
                d += timedelta(days=1)
        note_disp = f"（{result['note']}）" if result.get("note") else ""
        persons_disp = "、".join(persons)
        await update.message.reply_text(
            f"已登記{leave_type}：{result['start_date']} ～ {result['end_date']}（共{n_days}天） {persons_disp}{note_disp}",
            reply_markup=MAIN_KEYBOARD,
        )
        if result["start_date"] <= db.today_str() <= result["end_date"]:
            await send_today_card(update)

    elif result["type"] == "task_range":
        start = datetime.strptime(result["start_date"], "%Y-%m-%d")
        end = datetime.strptime(result["end_date"], "%Y-%m-%d")
        n_days = (end - start).days + 1
        d = start
        while d <= end:
            db.add_task(d.strftime("%Y-%m-%d"), result["content"])
            d += timedelta(days=1)
        await update.message.reply_text(
            f"已新增待辦：{result['start_date']} ～ {result['end_date']}（共{n_days}天） {result['content']}",
            reply_markup=MAIN_KEYBOARD,
        )
        if result["start_date"] <= db.today_str() <= result["end_date"]:
            await send_today_card(update)

    else:
        await update.message.reply_text(
            "看不太懂這句話🤔 可以用：\n"
            "「8/15 交採購報表」（待辦）\n"
            "「8/15 蕾蕾 請假」（請假）",
            reply_markup=MAIN_KEYBOARD,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if str(query.from_user.id) != str(config.OWNER_USER_ID):
        await query.answer("這不是你的秘書Bot喔", show_alert=True)
        return

    parts = (query.data or "").split("|")
    action = parts[0] if parts else ""

    if action == "noop":
        await query.answer()
        return

    # ── 管理待辦清單：完成／刪除／翻頁 ──
    if action == "mgdone" and len(parts) >= 2:
        ok = db.mark_task_done(int(parts[1]))
        page = int(parts[2]) if len(parts) >= 3 else 0
        await query.answer("已完成" if ok else "找不到這個事項")
        await send_manage_task_list(query, page)
        return

    if action == "mgtdel" and len(parts) >= 2:
        ok = db.delete_task(int(parts[1]))
        page = int(parts[2]) if len(parts) >= 3 else 0
        await query.answer("已刪除" if ok else "找不到這個事項")
        await send_manage_task_list(query, page)
        return

    if action == "mgtpage" and len(parts) >= 2:
        await query.answer()
        await send_manage_task_list(query, int(parts[1]))
        return

    # ── 管理請假清單：刪除／翻頁 ──
    if action == "mgldel" and len(parts) >= 2:
        ok = db.delete_leave(int(parts[1]))
        page = int(parts[2]) if len(parts) >= 3 else 0
        await query.answer("已刪除" if ok else "找不到這筆請假")
        await send_manage_leave_list(query, page)
        return

    if action == "mglpage" and len(parts) >= 2:
        await query.answer()
        await send_manage_leave_list(query, int(parts[1]))
        return

    # ── 管理重複任務清單：刪除／跳過下一次／取消跳過 ──
    if action == "mgtpldel" and len(parts) >= 2:
        ok = db.delete_template(int(parts[1]))
        await query.answer("已刪除" if ok else "找不到這個範本")
        await send_manage_template_list(query)
        return

    if action == "mgtplskip" and len(parts) >= 2:
        tpl_id = int(parts[1])
        tpl = db.get_template_by_id(tpl_id)
        if not tpl:
            await query.answer("找不到這個範本")
            await send_manage_template_list(query)
            return
        today_str = db.today_str()
        today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        start_dt = today_dt if tpl.get("last_generated_date") != today_str else today_dt + timedelta(days=1)
        next_dt = compute_next_occurrence(tpl["rule_type"], tpl["rule_value"], start_dt)
        if next_dt:
            db.set_template_skip(tpl_id, next_dt.strftime("%Y-%m-%d"))
            await query.answer(f"已設定跳過 {next_dt.strftime('%m/%d')}")
        else:
            await query.answer("找不到下一次產生日期，設定失敗")
        await send_manage_template_list(query)
        return

    if action == "mgtplunskip" and len(parts) >= 2:
        db.set_template_skip(int(parts[1]), None)
        await query.answer("已取消跳過")
        await send_manage_template_list(query)
        return

    # ── 追蹤等待中清單：完成／不用追了／翻頁 ──
    if action == "mgwdone" and len(parts) >= 2:
        ok = db.mark_task_done(int(parts[1]))
        page = int(parts[2]) if len(parts) >= 3 else 0
        await query.answer("已完成" if ok else "找不到這個事項")
        await send_manage_waiting_list(query, page)
        return

    if action == "mgwdel" and len(parts) >= 2:
        ok = db.delete_task(int(parts[1]))
        page = int(parts[2]) if len(parts) >= 3 else 0
        await query.answer("已刪除" if ok else "找不到這個事項")
        await send_manage_waiting_list(query, page)
        return

    if action == "mgwpage" and len(parts) >= 2:
        await query.answer()
        await send_manage_waiting_list(query, int(parts[1]))
        return

    # ── 時間提醒清單：取消／翻頁 ──
    if action == "mgrdel" and len(parts) >= 2:
        ok = db.delete_reminder(int(parts[1]))
        page = int(parts[2]) if len(parts) >= 3 else 0
        await query.answer("已取消" if ok else "找不到這個提醒")
        await send_manage_reminder_list(query, page)
        return

    if action == "mgrpage" and len(parts) >= 2:
        await query.answer()
        await send_manage_reminder_list(query, int(parts[1]))
        return

    # ── 下班前推播：把今天未完成事項全部推到明天 ──
    if action == "pushall" and len(parts) >= 3:
        from_date, to_date = parts[1], parts[2]
        count = db.bulk_push_pending_tasks(from_date, to_date)
        await query.answer(f"已推 {count} 項到明天" if count else "沒有可推的事項")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # ── 今日卡片上的快速完成按鈕 ──
    if action == "done" and len(parts) >= 3:
        task_id, date_str = int(parts[1]), parts[2]
        ok = db.mark_task_done(task_id)
        await query.answer("已完成" if ok else "找不到這個事項")
    elif action == "doneall" and len(parts) >= 2:
        date_str = parts[1]
        for t in db.get_tasks_for_date(date_str):
            if t["status"] == "pending":
                db.mark_task_done(t["id"])
        await query.answer("全部標記完成")
    else:
        await query.answer()
        return

    # 重新產圖並更新按鈕，讓卡片跟按鈕都反映最新狀態
    path = await build_today_card_path(date_str)
    tasks = db.get_tasks_for_date(date_str)
    keyboard = build_today_inline_keyboard(date_str, tasks)
    try:
        with open(path, "rb") as f:
            await query.edit_message_media(media=InputMediaPhoto(f), reply_markup=keyboard)
    except Exception:
        logger.warning("edit_message_media失敗，改用純更新按鈕", exc_info=True)
        await query.edit_message_reply_markup(reply_markup=keyboard)


# ───────────────── 每日推播 ─────────────────

async def do_daily_push(app: Application):
    """實際執行今日推播的核心邏輯，供排程與補推播共用"""
    date_str = db.today_str()
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    path = await build_today_card_path(date_str)
    tasks = db.get_tasks_for_date(date_str)
    keyboard = build_today_inline_keyboard(date_str, tasks)

    leaves = db.get_leaves_for_date(date_str)
    overdue = db.get_overdue_tasks(date_str)
    urgent_count = sum(1 for t in overdue if t.get("overdue_days", 0) >= 3)
    due_soon = db.get_due_soon_tasks(date_str, days_ahead=2)
    pending_count = sum(1 for t in tasks if t["status"] == "pending")
    waiting_count = len(db.get_all_waiting_tasks())

    # 銷假回崗提醒：昨天有請假、但今天沒有請假紀錄的人，視為今天回崗
    yesterday_str = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_leaves = db.get_leaves_for_date(yesterday_str)
    today_leave_names = {lv["person_name"] for lv in leaves}
    returning_names = sorted({lv["person_name"] for lv in yesterday_leaves if lv["person_name"] not in today_leave_names})

    context = {
        "pending_count": pending_count,
        "leave_names": [f"{lv['person_name']}{lv.get('leave_type') or '請假'}" for lv in leaves],
        "overdue_count": len(overdue),
        "urgent_overdue_count": urgent_count,
        "due_soon_count": len(due_soon),
        "returning_names": returning_names,
        "is_tomorrow": False,
    }
    headline = summary.generate_summary(context)

    caption_lines = [headline]
    if returning_names:
        caption_lines.append(f"{'、'.join(returning_names)}今天銷假回來了，記得跟他對接一下工作進度")
    if due_soon:
        caption_lines.append(f"有 {len(due_soon)} 項事項兩天內即將到期")
    if urgent_count:
        caption_lines.append(f"有 {urgent_count} 項事項已逾期3天以上，記得處理")
    if waiting_count:
        caption_lines.append(f"目前有 {waiting_count} 件事還在等對方回覆／處理，要不要順便催一下")
    caption = "\n".join(caption_lines)

    with open(path, "rb") as f:
        await app.bot.send_photo(
            chat_id=config.TELEGRAM_CHAT_ID, photo=f,
            caption=caption,
            reply_markup=keyboard,
        )
    db.set_meta("last_push_date", date_str)
    logger.info(f"已推播今日行事曆（{date_str}）")


async def scheduled_push(app: Application):
    now = datetime.now(TW_TZ)
    if now.weekday() not in config.PUSH_WEEKDAYS:
        return
    await do_daily_push(app)


async def catch_up_push_if_missed(app: Application):
    """服務啟動時檢查：若今天是工作日、已過推播時間、但今天還沒推播過，立刻補推"""
    now = datetime.now(TW_TZ)
    if now.weekday() not in config.PUSH_WEEKDAYS:
        return
    scheduled_time = now.replace(hour=config.PUSH_HOUR, minute=config.PUSH_MINUTE, second=0, microsecond=0)
    if now < scheduled_time:
        return
    today_str = db.today_str()
    if db.get_meta("last_push_date") == today_str:
        return
    logger.info("偵測到今天尚未推播，補推播中…")
    await do_daily_push(app)


async def evening_push(app: Application):
    """下班前推播：今日未完成事項確認 + 明日行程預告"""
    now = datetime.now(TW_TZ)
    if now.weekday() not in config.PUSH_WEEKDAYS:
        return

    today_str = db.today_str()
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    tomorrow_dt = today_dt + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    today_tasks = db.get_tasks_for_date(today_str)
    today_pending = [t for t in today_tasks if t["status"] == "pending"]
    today_done = [t for t in today_tasks if t["status"] == "done"]

    tomorrow_leaves = db.get_leaves_for_date(tomorrow_str)
    tomorrow_tasks = db.get_tasks_for_date(tomorrow_str)
    tomorrow_pending = [t for t in tomorrow_tasks if t["status"] == "pending"]
    tomorrow_leave_names = [f"{lv['person_name']}{lv.get('leave_type') or '請假'}" for lv in tomorrow_leaves]

    context = {
        "pending_count": len(tomorrow_pending),
        "leave_names": tomorrow_leave_names,
        "overdue_count": 0,
        "urgent_overdue_count": 0,
        "due_soon_count": 0,
        "returning_names": [],
        "is_tomorrow": True,
    }
    headline = summary.generate_summary(context)

    lines = [headline]

    if today_done:
        lines.append(f"\n今天完成摘要（可直接貼日報）：")
        lines.append(f"今天共完成 {len(today_done)} 項：")
        for i, t in enumerate(today_done, 1):
            lines.append(f"{i}. {t['content']}")

    if today_pending:
        lines.append(f"\n今天還有 {len(today_pending)} 項未完成：")
        for t in today_pending:
            lines.append(f"・#{t['id']} {t['content']}")

    if tomorrow_leave_names:
        lines.append(f"\n明天請假：{'、'.join(tomorrow_leave_names)}")

    if tomorrow_pending:
        lines.append("\n明天待辦：")
        for t in tomorrow_pending:
            lines.append(f"・{t['content']}")

    keyboard = None
    if today_pending:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("全部推到明天", callback_data=f"pushall|{today_str}|{tomorrow_str}")
        ]])

    await app.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        reply_markup=keyboard,
    )
    logger.info("已推播下班前提醒")


async def scheduled_backup(app: Application):
    """每週日晚上22:00把資料庫備份到Google Drive"""
    try:
        file_id = backup.backup_database()
        logger.info(f"資料庫備份完成，Drive檔案id：{file_id}")
    except Exception:
        logger.error("資料庫備份失敗", exc_info=True)
        try:
            await app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text="資料庫備份失敗，麻煩檢查一下VPS上的服務帳號金鑰或Drive資料夾權限",
            )
        except Exception:
            pass


async def check_due_reminders(app: Application):
    """每分鐘檢查一次有沒有到時間的精確提醒，到了就主動推播並標記已送出。
    用get_due_reminders抓「時間<=現在」而不是「時間==現在」，
    這樣萬一服務曾經短暫離線、錯過了確切那一分鐘，重新上線後還是會補推，不會憑空消失。"""
    now = datetime.now(TW_TZ)
    now_date = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")
    due = db.get_due_reminders(now_date, now_time)
    for r in due:
        try:
            await app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=f"⏰ 時間到了提醒：{r['content']}",
            )
            db.mark_reminder_sent(r["id"])
            logger.info(f"已推播時間提醒 #{r['id']}：{r['content']}")
        except Exception:
            logger.error(f"時間提醒 #{r['id']} 推播失敗", exc_info=True)


def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
    # 重要：CronTrigger物件如果不明確帶timezone參數，不會自動套用上面AsyncIOScheduler
    # 設定的"Asia/Taipei"，而是退回VPS系統本身的時區（這台VPS是UTC）。
    # 這曾經導致早上10點推播實際上在UTC 10點（=台灣晚上6點）才觸發，晚了8小時還沒人發現。
    # 三個CronTrigger都要明確帶timezone="Asia/Taipei"才會在正確的台灣時間點觸發。
    scheduler.add_job(
        scheduled_push,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=config.PUSH_HOUR, minute=config.PUSH_MINUTE,
            timezone="Asia/Taipei",
        ),
        args=[app],
        misfire_grace_time=3600,  # 服務短暫離線也能在1小時內補跑排定的推播
        name="早上10點今日推播",
    )
    scheduler.add_job(
        evening_push,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=config.EVENING_PUSH_HOUR, minute=config.EVENING_PUSH_MINUTE,
            timezone="Asia/Taipei",
        ),
        args=[app],
        misfire_grace_time=3600,
        name="下班前明日預告",
    )
    scheduler.add_job(
        scheduled_backup,
        trigger=CronTrigger(day_of_week="sun", hour=22, minute=0, timezone="Asia/Taipei"),
        args=[app],
        misfire_grace_time=3600 * 6,
        name="週日資料庫備份",
    )
    scheduler.add_job(
        check_due_reminders,
        trigger=IntervalTrigger(minutes=1),
        args=[app],
        misfire_grace_time=120,
        name="時間提醒每分鐘檢查",
    )
    scheduler.start()
    return scheduler


async def on_startup(app: Application):
    """服務啟動後執行：先確保範本/帳單提醒都已產生，檢查是否需要補推播，
    也順便檢查一次有沒有服務離線期間就已經到時間、還沒推播的時間提醒"""
    ensure_daily_generation()
    await catch_up_push_if_missed(app)
    await check_due_reminders(app)


def main():
    db.init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("leaves", cmd_leaves))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("delleave", cmd_delleave))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    scheduler = setup_scheduler(app)
    app.bot_data["scheduler"] = scheduler

    logger.info("秘書Bot 啟動中…")
    app.run_polling()


if __name__ == "__main__":
    main()
