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

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
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


def is_owner(update: Update) -> bool:
    return str(update.effective_user.id) == str(config.OWNER_USER_ID)


async def build_today_card_path(for_date: str = None) -> str:
    date_str = for_date or db.today_str()
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday_zh = WEEKDAY_ZH[dt.weekday()]

    leaves = db.get_leaves_for_date(date_str)
    tasks = db.get_tasks_for_date(date_str)
    overdue = db.get_overdue_tasks(date_str)

    img = card_renderer.render_today_card(date_str, weekday_zh, leaves, tasks, overdue)
    card_renderer.save_card(img, config.CARD_OUTPUT_PATH)
    return config.CARD_OUTPUT_PATH


# ───────────────── Commands ─────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "秘書Bot上線了。\n\n"
        "直接傳文字就能記事：\n"
        "・「8/15 交採購報表」→ 新增待辦\n"
        "・「8/15 蕾蕾 請假」→ 登記請假\n\n"
        "指令：\n"
        "/today 查看今天行事曆\n"
        "/done <編號> 標記完成\n"
        "/del <編號> 刪除事項\n"
        "/push <編號> <日期> 把逾期事項推到新日期"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = await build_today_card_path()
    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f)


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


# ───────────────── 自然語言輸入 ─────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("這是Luna的專屬秘書Bot，暫不開放其他人登記事項喔🙏")
        return

    result = parser.parse_input(update.message.text)

    if result["type"] == "task":
        task_id = db.add_task(result["date"], result["content"])
        await update.message.reply_text(f"已新增待辦 #{task_id}：{result['date']} {result['content']}")

    elif result["type"] == "leave":
        note_disp = f"（{result['note']}）" if result.get("note") else ""
        db.add_leave(result["date"], result["person"], result.get("note"))
        await update.message.reply_text(f"已登記請假：{result['date']} {result['person']} 請假{note_disp}")

    else:
        await update.message.reply_text(
            "看不太懂這句話🤔 可以用：\n"
            "「8/15 交採購報表」（待辦）\n"
            "「8/15 蕾蕾 請假」（請假）"
        )


# ───────────────── 每日推播 ─────────────────

async def scheduled_push(app: Application):
    now = datetime.now(TW_TZ)
    if now.weekday() not in config.PUSH_WEEKDAYS:
        return
    path = await build_today_card_path()
    with open(path, "rb") as f:
        await app.bot.send_photo(chat_id=config.TELEGRAM_CHAT_ID, photo=f, caption="早安 Luna，今天的行事曆來了 ☀️")
    logger.info("已推播今日行事曆")


def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        scheduled_push,
        trigger=CronTrigger(day_of_week="mon-fri", hour=config.PUSH_HOUR, minute=config.PUSH_MINUTE),
        args=[app],
    )
    scheduler.start()
    return scheduler


def main():
    db.init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    setup_scheduler(app)

    logger.info("秘書Bot 啟動中…")
    app.run_polling()


if __name__ == "__main__":
    main()
