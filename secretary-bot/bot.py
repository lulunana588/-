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
from datetime import datetime, timedelta, timezone

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import db
import parser
import card_renderer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("secretary-bot")

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

QUERY_BUTTON_TEXT = "查詢今日"
WEEK_BUTTON_TEXT = "查詢本週"
MONTH_BUTTON_TEXT = "查詢本月"
MANAGE_TASK_BUTTON_TEXT = "管理待辦事項"
MANAGE_LEAVE_BUTTON_TEXT = "管理請假登記"
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [QUERY_BUTTON_TEXT, WEEK_BUTTON_TEXT, MONTH_BUTTON_TEXT],
        [MANAGE_TASK_BUTTON_TEXT, MANAGE_LEAVE_BUTTON_TEXT],
    ],
    resize_keyboard=True,
)
WEEK_CARD_PATH = config.CARD_OUTPUT_PATH.replace("today_card.png", "week_card.png")
MONTH_CARD_PATH = config.CARD_OUTPUT_PATH.replace("today_card.png", "month_card.png")
MANAGE_TASK_LOOKAHEAD_DAYS = 30
MANAGE_LEAVE_LOOKAHEAD_DAYS = 60


def is_owner(update: Update) -> bool:
    return str(update.effective_user.id) == str(config.OWNER_USER_ID)


async def build_today_card_path(for_date: str = None) -> str:
    date_str = for_date or db.today_str()
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday_zh = WEEKDAY_ZH[dt.weekday()]

    leaves = db.get_leaves_for_date(date_str)
    tasks = db.get_tasks_for_date(date_str)
    overdue = db.get_overdue_tasks(date_str)
    for t in overdue:
        task_dt = datetime.strptime(t["task_date"], "%Y-%m-%d")
        t["overdue_days"] = (dt - task_dt).days

    img = card_renderer.render_today_card(date_str, weekday_zh, leaves, tasks, overdue)
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
        "・「8/15 蕾蕾 請假」→ 登記請假\n\n"
        "指令：\n"
        "/today 查看今天行事曆\n"
        "/week 查看本週行事曆\n"
        "/month 查看本月行事曆\n"
        "/done <編號> 標記完成\n"
        "/del <編號> 刪除事項\n"
        "/push <編號> <日期> 把逾期事項推到新日期\n\n"
        "下面也有「查詢今日」「查詢本週」「查詢本月」「管理待辦事項」「管理請假登記」按鈕，都不用打指令。",
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

def build_manage_task_keyboard():
    """列出所有未完成事項（含逾期），每項附「完成」「刪除」兩顆按鈕"""
    tasks = db.get_all_pending_tasks()
    rows = []
    for t in tasks:
        label = t["content"]
        if len(label) > 12:
            label = label[:12] + "…"
        date_disp = t["task_date"][5:]  # MM-DD
        rows.append([
            InlineKeyboardButton(f"{date_disp} {label}", callback_data="noop"),
            InlineKeyboardButton("完成", callback_data=f"mgdone|{t['id']}"),
            InlineKeyboardButton("刪除", callback_data=f"mgtdel|{t['id']}"),
        ])
    return tasks, InlineKeyboardMarkup(rows) if rows else None


async def send_manage_task_list(message_or_query):
    tasks, keyboard = build_manage_task_keyboard()
    text = "點「完成」或「刪除」管理事項：" if tasks else "目前沒有未完成的待辦事項"
    if hasattr(message_or_query, "edit_message_text"):
        # 來自 callback_query
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


# ───────────────── 管理請假（按鈕清單） ─────────────────

def build_manage_leave_keyboard():
    """列出最近7天起的所有請假紀錄，每項附「刪除」按鈕"""
    today_dt = datetime.strptime(db.today_str(), "%Y-%m-%d")
    start = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    leaves = db.get_leaves_from(start)
    rows = []
    for lv in leaves:
        date_disp = lv["leave_date"][5:]
        note = f"({lv['note']})" if lv.get("note") else ""
        rows.append([
            InlineKeyboardButton(f"{date_disp} {lv['person_name']}{note}", callback_data="noop"),
            InlineKeyboardButton("刪除", callback_data=f"mgldel|{lv['id']}"),
        ])
    return leaves, InlineKeyboardMarkup(rows) if rows else None


async def send_manage_leave_list(message_or_query):
    leaves, keyboard = build_manage_leave_keyboard()
    text = "點「刪除」取消請假登記：" if leaves else "目前沒有排定的請假紀錄"
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)


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
        lines.append(f"・#{lv['id']}  {lv['person_name']}{note}")
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


# ───────────────── 自然語言輸入 ─────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("這是Luna的專屬秘書Bot，暫不開放其他人登記事項喔🙏")
        return

    text = update.message.text.strip()

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

    result = parser.parse_input(text)

    if result["type"] == "task":
        task_id = db.add_task(result["date"], result["content"])
        await update.message.reply_text(
            f"已新增待辦 #{task_id}：{result['date']} {result['content']}",
            reply_markup=MAIN_KEYBOARD,
        )

    elif result["type"] == "leave":
        note_disp = f"（{result['note']}）" if result.get("note") else ""
        db.add_leave(result["date"], result["person"], result.get("note"))
        await update.message.reply_text(
            f"已登記請假：{result['date']} {result['person']} 請假{note_disp}",
            reply_markup=MAIN_KEYBOARD,
        )

    elif result["type"] == "leave_range":
        start = datetime.strptime(result["start_date"], "%Y-%m-%d")
        end = datetime.strptime(result["end_date"], "%Y-%m-%d")
        n_days = (end - start).days + 1
        d = start
        while d <= end:
            db.add_leave(d.strftime("%Y-%m-%d"), result["person"], result.get("note"))
            d += timedelta(days=1)
        note_disp = f"（{result['note']}）" if result.get("note") else ""
        await update.message.reply_text(
            f"已登記請假：{result['start_date']} ～ {result['end_date']}（共{n_days}天） {result['person']} 請假{note_disp}",
            reply_markup=MAIN_KEYBOARD,
        )

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

    # ── 管理待辦清單：完成／刪除 ──
    if action == "mgdone" and len(parts) >= 2:
        ok = db.mark_task_done(int(parts[1]))
        await query.answer("已完成" if ok else "找不到這個事項")
        await send_manage_task_list(query)
        return

    if action == "mgtdel" and len(parts) >= 2:
        ok = db.delete_task(int(parts[1]))
        await query.answer("已刪除" if ok else "找不到這個事項")
        await send_manage_task_list(query)
        return

    # ── 管理請假清單：刪除 ──
    if action == "mgldel" and len(parts) >= 2:
        ok = db.delete_leave(int(parts[1]))
        await query.answer("已刪除" if ok else "找不到這筆請假")
        await send_manage_leave_list(query)
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
    path = await build_today_card_path(date_str)
    tasks = db.get_tasks_for_date(date_str)
    keyboard = build_today_inline_keyboard(date_str, tasks)

    overdue = db.get_overdue_tasks(date_str)
    urgent_count = sum(1 for t in overdue if t.get("overdue_days", 0) >= 3)
    caption = "早安 Luna，今天的行事曆來了 ☀️"
    if urgent_count:
        caption += f"\n有 {urgent_count} 項事項已逾期3天以上，記得處理"

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


def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        scheduled_push,
        trigger=CronTrigger(day_of_week="mon-fri", hour=config.PUSH_HOUR, minute=config.PUSH_MINUTE),
        args=[app],
        misfire_grace_time=3600,  # 服務短暫離線也能在1小時內補跑排定的推播
    )
    scheduler.start()
    return scheduler


async def on_startup(app: Application):
    """服務啟動後執行一次：若今天工作日已過推播時間但還沒推播，立刻補推"""
    await catch_up_push_if_missed(app)


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
    app.add_handler(CommandHandler("delleave", cmd_delleave))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    setup_scheduler(app)

    logger.info("秘書Bot 啟動中…")
    app.run_polling()


if __name__ == "__main__":
    main()
