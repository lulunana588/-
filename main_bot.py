# main_bot.py
# Luna 行政小幫手 - 桶裝水登記 + 款項追蹤（直接寫入 Google Sheets）
#
# 執行方式： python3 main_bot.py
# 需要先設定好 .env（Bot Token、試算表ID等）與 service_account.json

import logging
import re
import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import config
import sheets_service as sheets

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_DISPLAY_NAME = "Luna-資深行政專員-TW"

# ---------- Conversation 狀態 ----------
(
    MAIN_MENU,
    WATER_LOCATION,
    WATER_ACTION,
    WATER_QTY,
    PAY_MENU,
    PAY_ADD_NAME,
    PAY_ADD_AMOUNT,
    PAY_ADD_DATE,
    PAY_ADD_PROGRESS,
    PAY_UPDATE_SEARCH,
    PAY_UPDATE_PICK,
    PAY_UPDATE_NOTE,
) = range(12)


def _authorized(update: Update) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in config.ALLOWED_USER_IDS


async def _reject(update: Update):
    await update.effective_message.reply_text("⛔ 您沒有使用這個機器人的權限。")


# =========================================================
# 群組 @提及 觸發（不用 /start，在群裡 @機器人 就能直接處理）
# =========================================================

_ACTION_IN_WORDS = ("入庫", "入", "補貨", "進貨", "送達")
_ACTION_OUT_WORDS = ("出庫", "出", "領用", "領取", "扣")


def _strip_mention(text: str, bot_username: str) -> str:
    return re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()


def _parse_quick_water_command(remainder: str, locations: list):
    """
    嘗試解析一行快速指令，支援兩種寫法：
      1) 帶正負號：「共享服務中心(忠孝) +10」「忠孝辦 -5」
      2) 帶動作字：「忠孝 出庫 5」「松山辦 入庫20」
    比對成功回傳 (location_dict, delta)；比對不到回傳 None（會改成打開選單）。
    """
    signed_match = re.search(r"([+\-－＋])\s*(\d+)", remainder)
    if signed_match:
        qty = int(signed_match.group(2))
        if qty <= 0:
            return None
        delta_sign = -1 if signed_match.group(1) in "-－" else 1
    else:
        qty_match = re.search(r"(\d+)", remainder)
        if not qty_match:
            return None
        qty = int(qty_match.group(1))
        if qty <= 0:
            return None

        if any(w in remainder for w in _ACTION_OUT_WORDS) and not any(
            w in remainder for w in _ACTION_IN_WORDS
        ):
            delta_sign = -1
        elif any(w in remainder for w in _ACTION_IN_WORDS):
            delta_sign = 1
        else:
            return None  # 看不出來是入庫還出庫，不要亂猜，交給選單流程

    # 把數字、正負號、動作字、標點、括號都拿掉，剩下的當作「地點關鍵字」，
    # 中文通常不會特地打空格分詞，所以用子字串比對而不是切詞比對
    core = remainder
    for w in (*_ACTION_IN_WORDS, *_ACTION_OUT_WORDS):
        core = core.replace(w, "")
    core = re.sub(r"[\d\s　、，,+\-－＋()（）]+", "", core)

    best_len = 0
    candidates = []
    for loc in locations:
        match_len = 0
        for start in range(len(core)):
            for end in range(len(core), start + 1, -1):
                piece = core[start:end]
                if len(piece) >= 2 and piece in loc["location"]:
                    match_len = max(match_len, len(piece))
                    break
        if match_len >= 2:
            if match_len > best_len:
                best_len = match_len
                candidates = [loc]
            elif match_len == best_len:
                candidates.append(loc)

    if not candidates:
        return None
    if len(candidates) > 1:
        # 多個地點都符合關鍵字時，再用供應商名稱（華生/水寶貝）進一步篩選
        refined = [c for c in candidates if c["supplier"] and c["supplier"] in remainder]
        if len(refined) == 1:
            candidates = refined
        else:
            return None  # 仍有歧義，交給選單流程讓使用者自己選

    return candidates[0], qty * delta_sign


async def mention_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群組訊息中 @機器人 時觸發：沒有附加文字就開選單；附加『地點 入庫/出庫 數量』就直接處理桶裝水登記。"""
    if not _authorized(update):
        await _reject(update)
        return ConversationHandler.END

    bot_username = context.bot.username
    raw_text = update.effective_message.text or ""
    remainder = _strip_mention(raw_text, bot_username)

    if not remainder:
        return await start(update, context)

    try:
        locations = sheets.list_water_locations()
    except Exception:
        logger.exception("快速指令讀取桶裝水表失敗，改開選單")
        return await start(update, context)

    parsed = _parse_quick_water_command(remainder, locations)
    if not parsed:
        await update.effective_message.reply_text(
            f"{BOT_DISPLAY_NAME}\n沒看懂「{remainder}」這個指令，幫您打開選單操作："
        )
        return await start(update, context)

    loc, delta = parsed
    action_label = "入庫" if delta > 0 else "出庫"

    processing_msg = await update.effective_message.reply_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：{action_label}　📅 日期：{sheets.today_str()}\n"
        f"⏳ 處理中，請稍候..."
    )
    try:
        result = sheets.record_water_transaction(loc, delta)
    except Exception as e:
        logger.exception("快速指令更新桶裝水庫存失敗")
        await processing_msg.edit_text(f"❌ 更新失敗：{e}")
        return ConversationHandler.END

    await processing_msg.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：{action_label}　📅 日期：{result['date']}\n"
        f"✅ {result['location']} → {result['old_stock']}桶 → {result['new_stock']}桶\n"
        f"狀態：{result['status']}\n"
        f"📋 已同步登記到「{result.get('detail_tab', result['location'])}」分頁（剩餘 {result.get('detail_balance', result['new_stock'])}桶）\n"
        f"已更新"
    )
    return ConversationHandler.END


# =========================================================
# 主選單
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _reject(update)
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🪣 桶裝水登記", callback_data="menu_water")],
        [InlineKeyboardButton("💰 款項追蹤", callback_data="menu_payment")],
    ]
    await update.effective_message.reply_text(
        f"{BOT_DISPLAY_NAME}\n請選擇要操作的項目：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return MAIN_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("已取消，輸入 /start 可重新開始。")
    return ConversationHandler.END


async def main_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "menu_water":
        return await water_show_locations(update, context)
    elif query.data == "menu_payment":
        keyboard = [
            [InlineKeyboardButton("📝 新增款項", callback_data="pay_add")],
            [InlineKeyboardButton("🔄 更新付款狀態", callback_data="pay_update")],
        ]
        await query.edit_message_text(
            f"{BOT_DISPLAY_NAME}\n💰 款項追蹤 - 請選擇：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return PAY_MENU


# =========================================================
# 桶裝水登記
# =========================================================

async def water_show_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        locations = sheets.list_water_locations()
    except Exception as e:
        logger.exception("讀取桶裝水表失敗")
        await query.edit_message_text(f"❌ 讀取試算表失敗：{e}")
        return ConversationHandler.END

    context.user_data["water_locations"] = {
        str(loc["row"]): loc for loc in locations
    }

    keyboard = []
    for loc in locations:
        label = f"{loc['location']}（{loc['stock']}桶 {loc['status']}）"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"waterloc_{loc['row']}")]
        )
    keyboard.append([InlineKeyboardButton("⬅️ 返回主選單", callback_data="back_main")])

    await query.edit_message_text(
        f"{BOT_DISPLAY_NAME}\n🪣 桶裝水登記 - 請選擇地點：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WATER_LOCATION


async def water_pick_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        return await start(update, context)

    row_key = query.data.replace("waterloc_", "")
    loc = context.user_data["water_locations"].get(row_key)
    if not loc:
        await query.edit_message_text("❌ 找不到這個地點資料，請重新 /start")
        return ConversationHandler.END

    context.user_data["water_target"] = loc

    keyboard = [
        [
            InlineKeyboardButton("➕ 入庫", callback_data="waterop_in"),
            InlineKeyboardButton("➖ 出庫", callback_data="waterop_out"),
        ],
        [InlineKeyboardButton("⬅️ 返回地點列表", callback_data="back_water_list")],
    ]
    await query.edit_message_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"📍 {loc['location']}\n"
        f"目前庫存：{loc['stock']} 桶（{loc['status']}）\n\n"
        f"請選擇動作：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WATER_ACTION


async def water_pick_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_water_list":
        return await water_show_locations(update, context)

    op = "in" if query.data == "waterop_in" else "out"
    context.user_data["water_op"] = op
    action_label = "入庫" if op == "in" else "出庫"

    await query.edit_message_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"📍 {context.user_data['water_target']['location']}\n"
        f"動作：{action_label}\n\n"
        f"請直接輸入數量（桶）："
    )
    return WATER_QTY


async def water_receive_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ 請輸入大於 0 的整數桶數，例如：25")
        return WATER_QTY

    qty = int(text)
    op = context.user_data["water_op"]
    loc = context.user_data["water_target"]
    delta = qty if op == "in" else -qty
    action_label = "入庫" if op == "in" else "出庫"

    processing_msg = await update.message.reply_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：{action_label}　📅 日期：{sheets.today_str()}\n"
        f"⏳ 處理中，請稍候..."
    )

    try:
        result = sheets.record_water_transaction(loc, delta)
    except Exception as e:
        logger.exception("更新桶裝水庫存失敗")
        await processing_msg.edit_text(f"❌ 更新失敗：{e}")
        return ConversationHandler.END

    await processing_msg.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：{action_label}　📅 日期：{result['date']}\n"
        f"✅ {result['location']} → {result['old_stock']}桶 → {result['new_stock']}桶\n"
        f"狀態：{result['status']}\n"
        f"📋 已同步登記到「{result.get('detail_tab', result['location'])}」分頁（剩餘 {result.get('detail_balance', result['new_stock'])}桶）\n"
        f"已更新"
    )
    context.user_data.clear()
    await update.message.reply_text("輸入 /start 可繼續操作。")
    return ConversationHandler.END


# =========================================================
# 款項追蹤 - 新增
# =========================================================

async def pay_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        return await start(update, context)
    elif query.data == "pay_add":
        await query.edit_message_text(
            f"{BOT_DISPLAY_NAME}\n📝 新增款項\n\n請輸入款項名稱："
        )
        return PAY_ADD_NAME
    elif query.data == "pay_update":
        await query.edit_message_text(
            f"{BOT_DISPLAY_NAME}\n🔄 更新付款狀態\n\n"
            f"請輸入要更新的「編號」或「款項名稱關鍵字」："
        )
        return PAY_UPDATE_SEARCH


async def pay_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pay_name"] = update.message.text.strip()
    await update.message.reply_text("請輸入金額（純數字，例如：1260）：")
    return PAY_ADD_AMOUNT


async def pay_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = update.message.text.strip().replace(",", "")
    if not amount.isdigit():
        await update.message.reply_text("⚠️ 請輸入純數字金額，例如：1260")
        return PAY_ADD_AMOUNT
    context.user_data["pay_amount"] = amount

    keyboard = [
        [InlineKeyboardButton(f"📅 今天（{sheets.today_str()}）", callback_data="date_today")]
    ]
    await update.message.reply_text(
        "請選擇送件日期，或直接輸入日期（格式 2026-07-02）：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PAY_ADD_DATE


async def pay_add_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pay_date"] = sheets.today_str()
    return await pay_ask_progress(update, context, via_query=True)


async def pay_add_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.datetime.strptime(text, "%Y-%m-%d")
        context.user_data["pay_date"] = d.strftime("%Y/%m/%d")
    except ValueError:
        await update.message.reply_text("⚠️ 日期格式錯誤，請用 2026-07-02 這種格式，或按上方「今天」按鈕")
        return PAY_ADD_DATE
    return await pay_ask_progress(update, context, via_query=False)


async def pay_ask_progress(update: Update, context: ContextTypes.DEFAULT_TYPE, via_query: bool):
    keyboard = [
        [InlineKeyboardButton(opt, callback_data=f"prog_{i}")]
        for i, opt in enumerate(config.PAYMENT_PROGRESS_OPTIONS)
    ]
    text = "請選擇目前進度："
    if via_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return PAY_ADD_PROGRESS


async def pay_add_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.replace("prog_", ""))
    progress = config.PAYMENT_PROGRESS_OPTIONS[idx]

    await query.edit_message_text(
        f"{BOT_DISPLAY_NAME}\n📝 新增款項\n⏳ 處理中，請稍候..."
    )

    try:
        result = sheets.add_payment_record(
            name=context.user_data["pay_name"],
            amount=context.user_data["pay_amount"],
            submit_date=context.user_data["pay_date"],
            progress=progress,
        )
    except Exception as e:
        logger.exception("新增款項失敗")
        await query.edit_message_text(f"❌ 新增失敗：{e}")
        return ConversationHandler.END

    await query.edit_message_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：新增款項　📅 日期：{result['submit_date']}\n"
        f"✅ 編號 {result['id']} → {result['name']}（NT${result['amount']}）\n"
        f"進度：{result['progress']}　付款狀態：待付\n"
        f"已更新"
    )
    context.user_data.clear()
    await query.message.reply_text("輸入 /start 可繼續操作。")
    return ConversationHandler.END


# =========================================================
# 款項追蹤 - 更新付款狀態
# =========================================================

async def pay_update_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    try:
        matches = sheets.find_payment_records(query_text)
    except Exception as e:
        logger.exception("搜尋款項失敗")
        await update.message.reply_text(f"❌ 搜尋失敗：{e}")
        return ConversationHandler.END

    if not matches:
        await update.message.reply_text("找不到符合的款項，請重新輸入編號或關鍵字（或 /cancel 取消）：")
        return PAY_UPDATE_SEARCH

    context.user_data["pay_matches"] = {str(m["row"]): m["values"] for m in matches}

    keyboard = []
    for m in matches:
        v = m["values"]
        pay_status = v[5] if len(v) > 5 else ""
        label = f"#{v[0]} {v[2]}（{pay_status}）"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"payrow_{m['row']}")])

    await update.message.reply_text(
        "請選擇要更新的款項：", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PAY_UPDATE_PICK


async def pay_update_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    row_key = query.data.replace("payrow_", "")
    values = context.user_data["pay_matches"].get(row_key)
    if not values:
        await query.edit_message_text("❌ 資料異常，請重新 /start")
        return ConversationHandler.END

    context.user_data["pay_target_row"] = int(row_key)
    context.user_data["pay_target_values"] = values

    pay_status = values[5] if len(values) > 5 else ""
    if pay_status == "已付":
        await query.edit_message_text(
            f"#{values[0]} {values[2]} 目前狀態已經是「已付」了，不需要重複更新。\n"
            f"輸入 /start 可返回主選單。"
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"#{values[0]} {values[2]}（NT${values[3]}）\n\n"
        f"確定要標記為「已付」嗎？若要附加備註，請直接輸入文字；\n"
        f"不需要備註請輸入「略過」。"
    )
    return PAY_UPDATE_NOTE


async def pay_update_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    note = None if note in ("略過", "skip", "") else note

    row = context.user_data["pay_target_row"]
    processing = await update.message.reply_text(
        f"{BOT_DISPLAY_NAME}\n🔄 動作：更新付款狀態\n⏳ 處理中，請稍候..."
    )

    try:
        result = sheets.mark_payment_paid(row, note=note)
    except Exception as e:
        logger.exception("更新付款狀態失敗")
        await processing.edit_text(f"❌ 更新失敗：{e}")
        return ConversationHandler.END

    await processing.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：更新付款狀態　📅 日期：{result['paid_date']}\n"
        f"✅ 編號 {result['id']} → {result['name']}（NT${result['amount']}）\n"
        f"付款狀態：待付 → 已付\n"
        f"已更新"
    )
    context.user_data.clear()
    await update.message.reply_text("輸入 /start 可繼續操作。")
    return ConversationHandler.END


# =========================================================
# 組裝 Application
# =========================================================

def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    mention_filter = filters.Entity(MessageEntity.MENTION) & filters.TEXT

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(mention_filter, mention_entry),
        ],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_router)],
            WATER_LOCATION: [CallbackQueryHandler(water_pick_location)],
            WATER_ACTION: [CallbackQueryHandler(water_pick_action)],
            WATER_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_receive_qty)],
            PAY_MENU: [CallbackQueryHandler(pay_menu_router)],
            PAY_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_add_name)],
            PAY_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_add_amount)],
            PAY_ADD_DATE: [
                CallbackQueryHandler(pay_add_date_button, pattern="^date_today$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pay_add_date_text),
            ],
            PAY_ADD_PROGRESS: [CallbackQueryHandler(pay_add_progress, pattern="^prog_")],
            PAY_UPDATE_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_update_search)],
            PAY_UPDATE_PICK: [CallbackQueryHandler(pay_update_pick, pattern="^payrow_")],
            PAY_UPDATE_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_update_note)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            MessageHandler(mention_filter, mention_entry),
        ],
        per_message=False,
    )

    app.add_handler(conv)
    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Luna 行政小幫手機器人啟動中...")
    application.run_polling()
