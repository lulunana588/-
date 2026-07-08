# main_bot.py
# Luna 行政小幫手 - 桶裝水登記 + 款項追蹤（直接寫入 Google Sheets）
#
# 執行方式： python3 main_bot.py
# 需要先設定好 .env（Bot Token、試算表ID等）與 service_account.json

import logging
import re
import datetime
from zoneinfo import ZoneInfo

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

_WATER_KEYWORD = "桶裝水"
_WATER_OUT_WORD = "送水"  # 送水 = 扣桶（出庫）
_WATER_IN_WORD = "儲值"   # 儲值 = 補桶（入庫）


def _operator_name(update: Update) -> str:
    """從 Telegram 訊息取得操作人顯示名稱，優先用全名，沒有就用帳號"""
    user = update.effective_user
    if not user:
        return "未知"
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def _strip_mention(text: str, bot_username: str) -> str:
    # 拿掉訊息裡「所有」@提及（包含操作人自己被標註在同一則訊息裡的情況），
    # 不能只拿掉 bot 自己的 @提及——否則像「@lulunana588 運營中心南京辦 送水10桶」
    # 這種訊息，帳號名稱裡的數字（588）會被後面的桶數解析誤抓走。
    # （2026/07/08 修正：這是造成「扣除588桶」異常紀錄的根本原因）
    return re.sub(r"@\w+", "", text).strip()


def _parse_quick_water_command(remainder: str, locations: list):
    """
    嘗試解析一行快速指令，格式固定為：
      「地點 送水 5 桶」或「地點 儲值 100 桶」→ 都代表補貨，入庫+5 / +100
    「桶裝水」三個字可加可不加，但「送水」/「儲值」兩個關鍵字至少要出現一個，
    避免隨口打幾個字就誤觸發，比對不到就回傳 None（改成打開選單）。
    """
    has_out = _WATER_OUT_WORD in remainder
    has_in = _WATER_IN_WORD in remainder
    if has_out == has_in:
        return None  # 兩個字都有，或兩個字都沒有 -> 語意不明確，不要亂猜

    qty_match = re.search(r"(\d+)", remainder)
    if not qty_match:
        return None
    qty = int(qty_match.group(1))
    if qty <= 0:
        return None

    delta_sign = -1 if has_out else 1

    # 把關鍵字、桶、數字、標點都拿掉，剩下的當作「地點關鍵字」
    core = remainder
    for w in (_WATER_KEYWORD, _WATER_OUT_WORD, _WATER_IN_WORD, "桶"):
        core = core.replace(w, "")
    core = re.sub(r"[\d\s　、，,()（）]+", "", core)

    # 先查使用者自訂的別名對照表（config.WATER_LOCATION_ALIASES），
    # 查到就直接用，不用再跑模糊比對；查不到才照原本的關鍵字子字串比對邏輯
    for alias, target_text in config.WATER_LOCATION_ALIASES.items():
        if alias and alias in core:
            for loc in locations:
                if target_text in loc["location"]:
                    return loc, qty * delta_sign

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


_PAYMENT_FIELD_ALIASES = {
    "款項名稱": "name",
    "名稱": "name",
    "項目": "name",
    "金額": "amount",
    "金额": "amount",
    "進度": "progress",
    "付款狀態": "status",
    "狀態": "status",
    "送件日期": "submit_date",
    "日期": "submit_date",
    "實付日期": "paid_date",
    "備註": "note",
    "备注": "note",
}

_PAYMENT_NAME_LINE_RE = re.compile(r"^.{0,4}?(款項名稱|名稱|項目)\s*[:：]", re.MULTILINE)


def _clean_field_key(raw_key: str) -> str:
    """把欄位名稱前面可能出現的圖釘/emoji等非文字符號去掉，只留中文字/英數字"""
    return re.sub(r"^[^\w\u4e00-\u9fff]+", "", raw_key).strip()


def _split_payment_blocks(remainder: str):
    """
    把一則訊息切成多筆款項的區塊，每次看到新的一行「款項名稱:」（或「名稱:」「項目:」，
    前面可以有 📌 之類的符號/emoji）就當作下一筆的開始，不用特地空行分隔。
    只有一筆時原封不動回傳單一區塊。
    """
    matches = list(_PAYMENT_NAME_LINE_RE.finditer(remainder))
    if len(matches) <= 1:
        return [remainder]

    blocks = []
    for i, m in enumerate(matches):
        start = m.start(1)  # 從關鍵字本身開始切，前面不相關的符號留給前一筆（不影響解析）
        end = matches[i + 1].start(1) if i + 1 < len(matches) else len(remainder)
        blocks.append(remainder[start:end].strip())
    return blocks


def _parse_quick_payment_command(remainder: str):
    """
    嘗試解析多行「款項名稱: xxx / 金額: xxx / 進度: xxx / 付款狀態: xxx」格式的快速新增指令。
    「款項名稱」跟「金額」為必填，其餘可省略。解析不到必填欄位就回傳 None（改開選單）。
    """
    fields = {}
    for line in remainder.splitlines():
        m = re.match(r"\s*(.{1,12}?)[:：]\s*(.+?)\s*$", line)
        if not m:
            continue
        raw_key, value = _clean_field_key(m.group(1)), m.group(2).strip()
        key = _PAYMENT_FIELD_ALIASES.get(raw_key)
        if key and value:
            fields[key] = value

    if "name" not in fields or "amount" not in fields:
        return None

    amount = fields["amount"].replace(",", "").replace("NT$", "").replace("$", "").strip()
    if not amount.isdigit():
        return None

    status = fields.get("status", "待付")
    paid_date = fields.get("paid_date", "")
    if status == "已付" and not paid_date:
        paid_date = sheets.today_str()

    return {
        "name": fields["name"],
        "amount": amount,
        "submit_date": fields.get("submit_date", sheets.today_str()),
        "progress": fields.get("progress", config.PAYMENT_PROGRESS_OPTIONS[0]),
        "status": status,
        "paid_date": paid_date,
        "note": fields.get("note", ""),
    }


async def mention_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    群組訊息中 @機器人 時觸發：
      - 沒有附加文字 -> 開選單
      - 內容含「桶裝水」-> 走桶裝水快速指令
      - 內容含「款項名稱」-> 走款項新增快速指令
      - 其他 -> 直接開選單
    """
    bot_username = context.bot.username
    raw_text = update.effective_message.text or ""

    # 隱私模式關閉後機器人會收到群組裡所有訊息，一定要先確認「這則訊息真的有@到機器人自己」，
    # 不能只看「訊息裡有沒有任何@提及」——不然別人互相 @ 對方時機器人也會誤判成被叫。
    # 這個判斷要放在權限檢查之前，否則設了白名單時，不相關的訊息也會被誤發「沒有權限」提示。
    if f"@{bot_username}".lower() not in raw_text.lower():
        return ConversationHandler.END

    if not _authorized(update):
        await _reject(update)
        return ConversationHandler.END

    remainder = _strip_mention(raw_text, bot_username)

    if not remainder:
        return await start(update, context)

    if _WATER_KEYWORD in remainder or _WATER_OUT_WORD in remainder or _WATER_IN_WORD in remainder:
        return await _handle_quick_water(update, context, remainder)
    if "款項名稱" in remainder:
        return await _handle_quick_payment(update, context, remainder)

    return await start(update, context)


async def _handle_quick_water(update: Update, context: ContextTypes.DEFAULT_TYPE, remainder: str):
    try:
        locations = sheets.list_water_locations()
    except Exception:
        logger.exception("快速指令讀取桶裝水表失敗，改開選單")
        return await start(update, context)

    lines = [ln.strip() for ln in remainder.splitlines() if ln.strip()]
    water_lines = [ln for ln in lines if _WATER_OUT_WORD in ln or _WATER_IN_WORD in ln]

    if len(water_lines) > 1:
        return await _handle_quick_water_batch(update, context, water_lines, locations)

    parsed = _parse_quick_water_command(remainder, locations)
    if not parsed:
        await update.effective_message.reply_text(
            f"{BOT_DISPLAY_NAME}\n"
            f"沒看懂「{remainder}」這個指令，格式要像這樣：\n"
            f"「地點 送水 5 桶」（扣桶）或「地點 儲值 100 桶」（補桶），「桶裝水」三個字可加可不加\n"
            f"（多筆的話一行一筆，分開換行即可）\n"
            f"先幫您打開選單操作："
        )
        return await start(update, context)

    loc, delta = parsed
    action_label = "入庫" if delta > 0 else "出庫"
    operator = _operator_name(update)

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
        f"👤 操作人：{operator}\n"
        f"已更新"
    )
    return ConversationHandler.END


async def _process_one_water_line(line: str, locations: list, operator: str) -> str:
    """批次模式專用：處理單一行桶裝水指令，回傳一行結果文字"""
    parsed = _parse_quick_water_command(line, locations)
    if not parsed:
        return f"❌ 看不懂「{line}」，已略過"

    loc, delta = parsed
    action_label = "入庫" if delta > 0 else "出庫"
    try:
        result = sheets.record_water_transaction(loc, delta)
    except Exception as e:
        logger.exception("批次桶裝水更新失敗")
        return f"❌ {loc['location']}：更新失敗（{e}）"

    return (
        f"✅ {result['location']} {action_label} → {result['old_stock']}桶→{result['new_stock']}桶"
        f"（{result['status']}，已同步{result.get('detail_tab', result['location'])}分頁）"
    )


async def _handle_quick_water_batch(
    update: Update, context: ContextTypes.DEFAULT_TYPE, water_lines: list, locations: list
):
    processing_msg = await update.effective_message.reply_text(
        f"{BOT_DISPLAY_NAME}\n🔄 動作：批次桶裝水登記（共 {len(water_lines)} 筆）\n⏳ 處理中，請稍候..."
    )

    operator = _operator_name(update)
    result_lines = []
    for i, line in enumerate(water_lines, start=1):
        line_result = await _process_one_water_line(line, locations, operator)
        result_lines.append(f"{i}. {line_result}")

    flagged = sum(1 for line in result_lines if "❌" in line)
    header_note = f"🔺 有 {flagged} 筆需要您額外確認（標記❌），建議優先查看\n\n" if flagged else ""

    await processing_msg.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 批次桶裝水登記完成（共 {len(water_lines)} 筆）　📅 {sheets.today_str()}\n"
        f"👤 操作人：{operator}\n\n"
        f"{header_note}"
        + "\n".join(result_lines)
    )
    return ConversationHandler.END


async def _handle_quick_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, remainder: str):
    blocks = _split_payment_blocks(remainder)
    if len(blocks) > 1:
        return await _handle_quick_payment_batch(update, context, blocks)

    parsed = _parse_quick_payment_command(remainder)
    if not parsed:
        await update.effective_message.reply_text(
            f"{BOT_DISPLAY_NAME}\n"
            f"沒看懂這筆款項資料，格式要像這樣（款項名稱、金額必填）：\n"
            f"款項名稱: 公務車ETC費用\n金額: 38\n進度: 已提交請款單及發票\n付款狀態: 待付\n\n"
            f"先幫您打開選單操作："
        )
        return await start(update, context)

    operator = _operator_name(update)
    note_for_sheet = parsed["note"]

    try:
        matches = sheets.find_pending_payment_exact(parsed["name"], parsed["amount"])
    except Exception:
        logger.exception("快速指令查找既有款項失敗")
        matches = []

    if len(matches) > 1:
        await update.effective_message.reply_text(
            f"{BOT_DISPLAY_NAME}\n"
            f"找到 {len(matches)} 筆「{parsed['name']}」金額相同、還沒付的款項，不確定要改哪一筆。\n"
            f"改用選單裡的「更新付款狀態」搜尋、指定要改的那一筆："
        )
        return await start(update, context)

    if len(matches) == 1:
        return await _apply_payment_update(
            update, matches[0], parsed, operator, note_for_sheet
        )

    # 沒有找到金額完全相同的既有款項，再看看有沒有金額很接近的（可能是打錯字/誤差）
    try:
        near_matches = sheets.find_near_pending_payment(
            parsed["name"], parsed["amount"], config.PAYMENT_AMOUNT_TOLERANCE
        )
    except Exception:
        logger.exception("快速指令查找相近既有款項失敗")
        near_matches = []

    if len(near_matches) == 1:
        near = near_matches[0]
        context.user_data["pending_near_payment"] = {
            "row": near["row"],
            "parsed": parsed,
            "operator": operator,
            "note_for_sheet": note_for_sheet,
        }
        keyboard = [
            [
                InlineKeyboardButton("✅ 是同一筆，更新", callback_data="qpnear_update"),
                InlineKeyboardButton("➕ 不是，新增一筆", callback_data="qpnear_addnew"),
            ]
        ]
        await update.effective_message.reply_text(
            f"{BOT_DISPLAY_NAME}\n"
            f"找到一筆金額接近但不完全相同的既有款項：\n"
            f"「{parsed['name']}」既有金額 NT${near['amount']:,}，這次輸入 NT${parsed['amount']}\n\n"
            f"是同一筆款項嗎？",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    # 沒有金額相符或相近的既有款項 -> 新增一筆
    return await _apply_payment_add(update, parsed, operator, note_for_sheet)


async def _apply_payment_update(update_or_query, row, parsed, operator, note_for_sheet):
    message = update_or_query.effective_message if isinstance(update_or_query, Update) else update_or_query.message
    processing_msg = await message.reply_text(
        f"{BOT_DISPLAY_NAME}\n🔄 動作：更新既有款項\n⏳ 處理中，請稍候..."
    )
    try:
        result = sheets.update_payment_fields(
            row,
            progress=parsed["progress"],
            status=parsed["status"],
            paid_date=parsed["paid_date"],
            note=note_for_sheet,
            operator=operator,
        )
    except Exception as e:
        logger.exception("快速指令更新既有款項失敗")
        await processing_msg.edit_text(f"❌ 更新失敗：{e}")
        return ConversationHandler.END

    extra = f"　實付日期：{result['paid_date']}" if result["paid_date"] else ""
    await processing_msg.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：更新既有款項　📅 {sheets.today_str()}\n"
        f"✅ 編號 {result['id']} → {result['name']}（NT${result['amount']}）\n"
        f"進度：{result['progress']}　付款狀態：{result['status']}{extra}\n"
        f"（找到既有款項，已直接編輯，沒有新增重複的一筆）\n"
        f"👤 操作人：{operator}\n"
        f"已更新"
    )
    return ConversationHandler.END


async def _apply_payment_add(update_or_query, parsed, operator, note_for_sheet):
    message = update_or_query.effective_message if isinstance(update_or_query, Update) else update_or_query.message
    processing_msg = await message.reply_text(
        f"{BOT_DISPLAY_NAME}\n🔄 動作：新增款項\n⏳ 處理中，請稍候..."
    )
    try:
        result = sheets.add_payment_record(
            name=parsed["name"],
            amount=parsed["amount"],
            submit_date=parsed["submit_date"],
            progress=parsed["progress"],
            status=parsed["status"],
            paid_date=parsed["paid_date"],
            note=note_for_sheet,
            operator=operator,
        )
    except Exception as e:
        logger.exception("快速指令新增款項失敗")
        await processing_msg.edit_text(f"❌ 新增失敗：{e}")
        return ConversationHandler.END

    extra = f"　實付日期：{result['paid_date']}" if result["paid_date"] else ""
    await processing_msg.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：新增款項　📅 日期：{result['submit_date']}\n"
        f"✅ 編號 {result['id']} → {result['name']}（NT${result['amount']}）\n"
        f"進度：{result['progress']}　付款狀態：{result['status']}{extra}\n"
        f"👤 操作人：{operator}\n"
        f"已更新"
    )
    return ConversationHandler.END


async def quick_payment_near_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pending = context.user_data.pop("pending_near_payment", None)
    if not pending:
        await query.edit_message_text(f"{BOT_DISPLAY_NAME}\n❌ 找不到待確認的資料，可能已逾時，請重新輸入指令。")
        return

    await query.edit_message_text(f"{BOT_DISPLAY_NAME}\n⏳ 處理中，請稍候...")

    if query.data == "qpnear_update":
        return await _apply_payment_update(
            query, pending["row"], pending["parsed"], pending["operator"], pending["note_for_sheet"]
        )
    return await _apply_payment_add(
        query, pending["parsed"], pending["operator"], pending["note_for_sheet"]
    )


async def _process_one_payment_block(update: Update, parsed: dict, snapshot: list, used_rows: set) -> str:
    """
    批次模式專用：處理單一筆款項（找既有的就編輯、找不到就新增），
    回傳一行結果文字，不單獨發訊息（批次會統一彙整成一則訊息）。
    比對用的 snapshot 是批次開始前讀的快照，不會即時重查，
    這樣同一批裡出現名稱+金額都相同的兩筆，才會各自成立一筆，不會互相誤判成同一筆。
    金額相近但不完全相同的狀況，批次模式不彈確認視窗，直接新增並在結果裡加警示，
    事後請自行到表格確認是否重複。
    """
    operator = _operator_name(update)
    note_for_sheet = parsed["note"]

    try:
        target_amount = int(parsed["amount"])
    except ValueError:
        target_amount = None

    exact_rows = [
        item["row"]
        for item in snapshot
        if item["name"] == parsed["name"]
        and item["amount"] == target_amount
        and item["row"] not in used_rows
    ]

    if len(exact_rows) > 1:
        return f"❓ {parsed['name']}（NT${parsed['amount']}）：找到多筆同名同金額還沒付的，請改用選單處理"

    if len(exact_rows) == 1:
        row = exact_rows[0]
        used_rows.add(row)
        try:
            result = sheets.update_payment_fields(
                row,
                progress=parsed["progress"],
                status=parsed["status"],
                paid_date=parsed["paid_date"],
                note=note_for_sheet,
                operator=operator,
            )
        except Exception as e:
            logger.exception("批次款項更新失敗")
            return f"❌ {parsed['name']}：更新失敗（{e}）"
        extra = f"，實付{result['paid_date']}" if result["paid_date"] else ""
        return f"✏️ 編號{result['id']} {result['name']}（NT${result['amount']}）→ {result['status']}{extra}（編輯既有）"

    warn = ""
    if target_amount is not None:
        near_rows = [
            item
            for item in snapshot
            if item["name"] == parsed["name"]
            and item["row"] not in used_rows
            and item["amount"] != target_amount
            and abs(item["amount"] - target_amount) <= config.PAYMENT_AMOUNT_TOLERANCE
        ]
        if near_rows:
            warn = f"⚠️ 金額與既有一筆（NT${near_rows[0]['amount']:,}）相近，請確認是否重複；"

    try:
        result = sheets.add_payment_record(
            name=parsed["name"],
            amount=parsed["amount"],
            submit_date=parsed["submit_date"],
            progress=parsed["progress"],
            status=parsed["status"],
            paid_date=parsed["paid_date"],
            note=note_for_sheet,
            operator=operator,
        )
    except Exception as e:
        logger.exception("批次款項新增失敗")
        return f"❌ {parsed['name']}：新增失敗（{e}）"
    extra = f"，實付{result['paid_date']}" if result["paid_date"] else ""
    return f"{warn}🆕 編號{result['id']} {result['name']}（NT${result['amount']}）→ {result['status']}{extra}（新增）"


async def _handle_quick_payment_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, blocks: list):
    processing_msg = await update.effective_message.reply_text(
        f"{BOT_DISPLAY_NAME}\n🔄 動作：批次處理款項（共 {len(blocks)} 筆）\n⏳ 處理中，請稍候..."
    )

    try:
        snapshot = sheets.get_pending_payment_index()
    except Exception:
        logger.exception("批次款項讀取既有索引失敗")
        snapshot = []
    used_rows = set()

    result_lines = []
    for i, block in enumerate(blocks, start=1):
        parsed = _parse_quick_payment_command(block)
        if not parsed:
            result_lines.append(f"{i}. ❌ 看不懂這筆資料（款項名稱/金額必填），已略過")
            continue
        line = await _process_one_payment_block(update, parsed, snapshot, used_rows)
        result_lines.append(f"{i}. {line}")

    flagged = sum(1 for line in result_lines if "⚠️" in line or "❓" in line or "❌" in line)
    header_note = (
        f"🔺 有 {flagged} 筆需要您額外確認（標記⚠️/❓/❌），建議優先查看\n\n" if flagged else ""
    )

    await processing_msg.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 批次款項處理完成（共 {len(blocks)} 筆）　📅 {sheets.today_str()}\n\n"
        f"{header_note}"
        + "\n".join(result_lines)
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
    operator = _operator_name(update)

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
        f"👤 操作人：{operator}\n"
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
    operator = _operator_name(update)

    await query.edit_message_text(
        f"{BOT_DISPLAY_NAME}\n📝 新增款項\n⏳ 處理中，請稍候..."
    )

    try:
        result = sheets.add_payment_record(
            name=context.user_data["pay_name"],
            amount=context.user_data["pay_amount"],
            submit_date=context.user_data["pay_date"],
            progress=progress,
            operator=operator,
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
        f"👤 操作人：{operator}\n"
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
        pay_status = v[6] if len(v) > 6 else ""
        label = f"#{v[0]} {v[3]}（{pay_status}）"
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

    pay_status = values[6] if len(values) > 6 else ""
    if pay_status == "已付":
        await query.edit_message_text(
            f"#{values[0]} {values[3]} 目前狀態已經是「已付」了，不需要重複更新。\n"
            f"輸入 /start 可返回主選單。"
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"#{values[0]} {values[3]}（NT${values[4]}）\n\n"
        f"確定要標記為「已付」嗎？若要附加備註，請直接輸入文字；\n"
        f"不需要備註請輸入「略過」。"
    )
    return PAY_UPDATE_NOTE


async def pay_update_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    note = None if note in ("略過", "skip", "") else note
    operator = _operator_name(update)

    row = context.user_data["pay_target_row"]
    processing = await update.message.reply_text(
        f"{BOT_DISPLAY_NAME}\n🔄 動作：更新付款狀態\n⏳ 處理中，請稍候..."
    )

    try:
        result = sheets.mark_payment_paid(row, note=note, operator=operator)
    except Exception as e:
        logger.exception("更新付款狀態失敗")
        await processing.edit_text(f"❌ 更新失敗：{e}")
        return ConversationHandler.END

    await processing.edit_text(
        f"{BOT_DISPLAY_NAME}\n"
        f"🔄 動作：更新付款狀態　📅 日期：{result['paid_date']}\n"
        f"✅ 編號 {result['id']} → {result['name']}（NT${result['amount']}）\n"
        f"付款狀態：待付 → 已付\n"
        f"👤 操作人：{operator}\n"
        f"已更新"
    )
    context.user_data.clear()
    await update.message.reply_text("輸入 /start 可繼續操作。")
    return ConversationHandler.END


# =========================================================
# 每日款項追蹤提醒（週一~週五 台灣時間 10:05）
# =========================================================

def _previous_business_day(target: datetime.date, holidays: set) -> datetime.date:
    """從 target 開始往前找，直到找到一個不是六日、也不是國定假日的日期"""
    d = target
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d -= datetime.timedelta(days=1)
    return d


def _is_monthly_water_report_day(today: datetime.date) -> bool:
    """判斷今天是不是這個月「10號」的有效提醒日（遇六日/國定假日會提前到最近的上班日）"""
    target = datetime.date(today.year, today.month, 10)
    holidays = config.TAIWAN_HOLIDAYS.get(today.year, set())
    effective = _previous_business_day(target, holidays)
    return today == effective


async def send_monthly_water_summary(context: ContextTypes.DEFAULT_TYPE):
    if not config.REMINDER_CHAT_ID:
        logger.warning("REMINDER_CHAT_ID 未設定，跳過每月桶裝水盤點提醒")
        return

    today = datetime.datetime.now(ZoneInfo("Asia/Taipei")).date()
    if not _is_monthly_water_report_day(today):
        return  # 不是這個月的有效提醒日（10號遇假日會提前），先不發

    try:
        locations = sheets.list_water_locations()
    except Exception:
        logger.exception("讀取桶裝水表失敗，每月盤點提醒中止")
        return

    threshold = config.WATER_MONTHLY_REFILL_THRESHOLD
    need_refill = [loc for loc in locations if loc["stock"] < threshold]

    lines = []
    for loc in locations:
        mark = "⬆️" if loc["stock"] < threshold else "✅"
        lines.append(f"{mark} {loc['location']}：{loc['stock']}桶")

    if need_refill:
        header = f"⬆️ 需要儲值（少於{threshold}桶）：{len(need_refill)} 間"
    else:
        header = f"所有地點都在 {threshold} 桶以上，暫不用儲值 🎉"

    text = (
        f"{BOT_DISPLAY_NAME}\n"
        f"🪣 每月桶裝水盤點　📅 {sheets.today_str()}\n\n"
        f"{header}\n\n" + "\n".join(lines)
    )

    try:
        await context.bot.send_message(chat_id=config.REMINDER_CHAT_ID, text=text)
    except Exception:
        logger.exception("發送每月桶裝水盤點提醒失敗")


async def send_payment_reminder(context: ContextTypes.DEFAULT_TYPE):
    if not config.REMINDER_CHAT_ID:
        logger.warning("REMINDER_CHAT_ID 未設定，跳過每日提醒")
        return

    sections = [f"{BOT_DISPLAY_NAME}\n📋 早安！今天的提醒　📅 {sheets.today_str()}"]

    # --- 款項追蹤 ---
    try:
        payment_summary = sheets.get_pending_payments()
    except Exception:
        logger.exception("讀取款項追蹤表失敗，這部分跳過")
        payment_summary = None

    if payment_summary is not None:
        if payment_summary["count"] == 0:
            sections.append("💰 款項追蹤：目前沒有需要追蹤的款項 🎉")
        else:
            lines = "\n".join(
                f"{i + 1}. #{item['id']} {item['name']}（NT${item['amount']:,}）"
                f" - {item['status']}／{item['progress']}"
                for i, item in enumerate(payment_summary["items"][:15])
            )
            more = ""
            if payment_summary["count"] > 15:
                more = f"\n...還有 {payment_summary['count'] - 15} 筆，輸入 /start 查完整清單"
            sections.append(
                f"💰 款項追蹤：{payment_summary['count']} 筆，共 NT${payment_summary['total']:,}\n{lines}{more}"
            )

    # --- 綜辦文件繳回 ---
    try:
        doc_summary = sheets.get_pending_docs()
    except Exception:
        logger.exception("讀取綜辦文件表失敗，這部分跳過")
        doc_summary = None

    if doc_summary is not None:
        if doc_summary["count"] == 0:
            sections.append("📮 綜辦文件繳回：目前沒有待處理的文件 🎉")
        else:
            lines = "\n".join(
                f"{i + 1}. {item['received_date']} {item['company']}"
                f" - {item['doc_type']}（{item['status']}）"
                for i, item in enumerate(doc_summary["items"][:15])
            )
            more = ""
            if doc_summary["count"] > 15:
                more = f"\n...還有 {doc_summary['count'] - 15} 筆"
            sections.append(f"📮 綜辦文件繳回：{doc_summary['count']} 筆\n{lines}{more}")

    text = "\n\n".join(sections)

    try:
        await context.bot.send_message(chat_id=config.REMINDER_CHAT_ID, text=text)
    except Exception:
        logger.exception("發送每日提醒失敗")


# =========================================================
# 組裝 Application
# =========================================================

async def _on_startup(application: Application):
    """機器人啟動完成（VPS重開機或服務被重啟時）私訊通知，讓您知道它還活著"""
    if not config.REMINDER_CHAT_ID:
        return
    try:
        now = datetime.datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d %H:%M")
        await application.bot.send_message(
            chat_id=config.REMINDER_CHAT_ID,
            text=(
                f"{BOT_DISPLAY_NAME}\n"
                f"✅ 機器人重新上線了　🕐 {now}（台灣時間）\n"
                f"（VPS 重開機或服務被重啟時會自動發這則通知）"
            ),
        )
    except Exception:
        logger.exception("發送上線通知失敗")


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(_on_startup).build()

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
    app.add_handler(CallbackQueryHandler(quick_payment_near_match, pattern="^qpnear_(update|addnew)$"))

    if config.REMINDER_CHAT_ID and app.job_queue is not None:
        app.job_queue.run_daily(
            send_payment_reminder,
            time=datetime.time(hour=10, minute=5, tzinfo=ZoneInfo("Asia/Taipei")),
            days=(1, 2, 3, 4, 5),  # 週一~週五（python-telegram-bot 的 days 是 0=週日起算）
            name="payment_reminder",
        )
        app.job_queue.run_daily(
            send_monthly_water_summary,
            time=datetime.time(hour=10, minute=5, tzinfo=ZoneInfo("Asia/Taipei")),
            days=(1, 2, 3, 4, 5),
            name="monthly_water_summary",
        )
    elif config.REMINDER_CHAT_ID:
        logger.warning(
            "JobQueue 未啟用，提醒排程不會運作。"
            "請確認 requirements.txt 有安裝 python-telegram-bot[job-queue]。"
        )

    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Luna 行政小幫手機器人啟動中...")
    application.run_polling()
