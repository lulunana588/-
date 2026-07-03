# -*- coding: utf-8 -*-
"""
資產清冊管理機器人(逐筆版,自動判別辦公室)
按鈕模式:
    /start -> 輸入編號查詢 -> 選擇要修改的欄位 -> 輸入新值
快速文字指令(不用斜線,直接傳文字;群組裡前面加 @機器人):
    查詢 A-01-101
    改 A-01-101 所在區域 座位010
    改 A-01-101 使用部門 客服部
    改 A-01-101 員編 XS1234
    改 A-01-101 保管人 小美
    改 A-01-101 使用狀況 使用中
    備註 A-01-101 設備送修中
    領用 A-01-101 小美 客服部 XS1234 座位010   (花名 部門 員編 所在區域)

編號如果同時存在於兩間辦公室(少見),bot 會跳出按鈕請你手動選一次。
"""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    ADMIN_CHAT_ID,
    OFFICES,
    STATUS_OPTIONS,
    EDITABLE_FIELDS,
    BATCH_ACTION_TYPES,
)
import sheet_utils
import llm_parser
import batch_rules

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# {chat_id: {"office":..., "sheet":..., "row":..., "asset_id":..., "awaiting":..., "pending":...}}
USER_STATE = {}

FIELD_LABELS = {**EDITABLE_FIELDS, "使用狀況": "status"}  # 顯示標籤 -> COLUMNS key
REVERSE_FIELD_LABELS = {v: k for k, v in FIELD_LABELS.items()}

FIELD_DISPLAY_LABELS = {
    "status": "使用狀況",
    "location": "所在區域",
    "keeper": "保管人",
    "department": "使用部門",
    "emp_id": "員編",
}


def format_record(record: dict, office: str, note: str) -> str:
    lines = [
        f"編號:{record['id']}(辦公室:{office})",
        f"名稱:{record['name']}　規格:{record['spec'] or record['spec2']}",
        f"使用狀況:{record['status']}",
        f"所在區域:{record['location'] or '（未填）'}",
        f"使用部門:{record['department'] or '（未填）'}",
        f"員編:{record['emp_id'] or '（未填）'}",
        f"保管人:{record['keeper'] or '（未填）'}",
    ]
    if note:
        lines.append(f"備註紀錄:\n{note}")
    return "\n".join(lines)


def record_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔁 切換使用狀況(庫存⇄使用中)", callback_data="edit:status")],
        [InlineKeyboardButton("📍 修改所在區域", callback_data="edit:location")],
        [InlineKeyboardButton("🏢 修改使用部門", callback_data="edit:department")],
        [InlineKeyboardButton("🆔 修改員編", callback_data="edit:emp_id")],
        [InlineKeyboardButton("🙋 修改保管人", callback_data="edit:keeper")],
        [InlineKeyboardButton("📝 新增備註紀錄", callback_data="edit:note")],
        [InlineKeyboardButton("🔎 查詢別筆", callback_data="new_query")],
    ]
    return InlineKeyboardMarkup(keyboard)


def looks_like_batch_text(text: str) -> bool:
    """粗略判斷:多行文字且包含至少一種批次異動關鍵字,才當作批次貼上處理"""
    if "\n" not in text:
        return False
    return any(keyword in text for keyword in BATCH_ACTION_TYPES)


def resolve_asset(asset_id: str):
    """
    自動在所有辦公室搜尋編號。
    回傳 ("ok", (office, sheet_name, row, record)) / ("not_found", None) / ("ambiguous", matches)
    """
    matches = sheet_utils.find_asset_any_office(asset_id)
    if not matches:
        return "not_found", None
    if len(matches) > 1:
        return "ambiguous", matches
    office, sheet_name, row, record = matches[0]
    return "ok", (office, sheet_name, row, record)


async def ask_disambiguate(message, chat_id: int, asset_id: str, pending: dict):
    """編號同時存在多間辦公室時,跳按鈕請使用者手動選一次"""
    USER_STATE[chat_id] = {"awaiting": "disambiguate", "pending": pending}
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"disamb:{name}")] for name in OFFICES
    ]
    await message.reply_text(
        f"⚠️ 編號 {asset_id} 在多間辦公室都有紀錄,請選擇要操作哪一間:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------- 按鈕選單模式 ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    USER_STATE[chat_id] = {"awaiting": "asset_id"}
    await update.message.reply_text("📋 資產清冊管理\n請輸入資產編號,例如 A-01-101")


async def show_record(message, chat_id: int, asset_id: str, forced_office: str = None):
    """message 需為一個具備 reply_text 的物件(Update.message 或 CallbackQuery.message)"""
    if forced_office:
        found = sheet_utils.find_asset(forced_office, asset_id)
        if not found:
            await message.reply_text(f"⚠️ 在「{forced_office}」找不到編號:{asset_id}")
            return
        office, sheet_name, row, record = forced_office, found[0], found[1], found[2]
    else:
        status, result = resolve_asset(asset_id)
        if status == "not_found":
            await message.reply_text(
                f"⚠️ 找不到編號:{asset_id}\n請確認編號是否正確,或再輸入一次。"
            )
            return
        if status == "ambiguous":
            await ask_disambiguate(message, chat_id, asset_id, {"action": "query", "asset_id": asset_id})
            return
        office, sheet_name, row, record = result

    note = sheet_utils.get_note(office, sheet_name, row)
    USER_STATE[chat_id] = {
        "office": office,
        "sheet": sheet_name,
        "row": row,
        "asset_id": record["id"],
        "awaiting": None,
    }
    text = format_record(record, office, note)
    await message.reply_text(text, reply_markup=record_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
    state = USER_STATE.setdefault(chat_id, {})

    if data == "new_query":
        state["awaiting"] = "asset_id"
        await query.edit_message_text("請輸入資產編號,例如 A-01-101")

    elif data.startswith("edit:"):
        field_key = data.split(":", 1)[1]
        if not state.get("row"):
            await query.edit_message_text("⚠️ 請先查詢一筆資產。輸入 /start 重新開始。")
            return
        if field_key == "status":
            found = sheet_utils.find_asset(state["office"], state["asset_id"])
            if not found:
                await query.edit_message_text("⚠️ 找不到這筆資產,可能已被刪除。")
                return
            _, _, record = found
            new_status = "使用中" if record["status"] == "庫存" else "庫存"
            sheet_utils.update_field(state["office"], state["sheet"], state["row"], "status", new_status)
            await query.edit_message_text(f"✅ {state['asset_id']} 使用狀況已改為:{new_status}")
            await show_record(query.message, chat_id, state["asset_id"], forced_office=state["office"])
        else:
            state["awaiting"] = f"field:{field_key}"
            label = REVERSE_FIELD_LABELS.get(field_key, field_key)
            await query.edit_message_text(f"請輸入「{label}」的新內容(資產編號:{state['asset_id']}):")

    elif data.startswith("disamb:"):
        office = data.split(":", 1)[1]
        pending = state.get("pending") or {}
        action = pending.get("action")
        asset_id = pending.get("asset_id")
        state["awaiting"] = None
        state["pending"] = None

        if action == "query":
            await show_record(query.message, chat_id, asset_id, forced_office=office)
        elif action == "update":
            await quick_update(query.message, chat_id, asset_id, pending["field_label"], pending["value"], forced_office=office)
        elif action == "note":
            await quick_note(query.message, chat_id, asset_id, pending["note_text"], forced_office=office)
        elif action == "checkout":
            await quick_checkout(
                query.message, chat_id, asset_id,
                pending["person"], pending["department"], pending["emp_id"], pending["location"],
                forced_office=office,
            )

    elif data == "batch_confirm":
        pending = state.get("pending_batch") or []
        success, skipped = 0, 0
        for entry in pending:
            if entry["ok"] and entry["found"]:
                sheet_utils.update_fields(entry["office"], entry["sheet"], entry["row"], entry["fields"])
                sheet_utils.append_note(entry["office"], entry["sheet"], entry["row"], entry["note"])
                success += 1
            else:
                skipped += 1
        state["pending_batch"] = None
        await query.edit_message_text(f"✅ 已寫入 {success} 筆,略過 {skipped} 筆。")

    elif data == "batch_cancel":
        state["pending_batch"] = None
        await query.edit_message_text("已取消,沒有寫入任何資料。")


# ---------- 批次異動清單解析 + 預覽 ----------

async def run_batch_preview(message, chat_id: int, text: str):
    try:
        actions = llm_parser.parse_batch_text(text)
    except Exception as e:
        logger.error("批次解析失敗: %s", e)
        await message.reply_text(f"⚠️ 解析失敗,請確認格式或稍後再試。\n({e})")
        return

    if not actions:
        await message.reply_text("⚠️ 沒有解析出任何可辨識的異動項目,請確認貼上的內容格式。")
        return

    pending = []
    lines = [f"📋 解析出 {len(actions)} 筆異動,請確認:\n"]
    for a in actions:
        asset_id = a["asset_id"]
        ok, fields, note, error_msg = batch_rules.build_plan(a)
        status, result = resolve_asset(asset_id)

        if status == "not_found":
            pending.append({"asset_id": asset_id, "type": a["type"], "ok": False, "found": False})
            lines.append(f"❌ {asset_id}({a['type']}):找不到此編號")
            continue
        if status == "ambiguous":
            pending.append({"asset_id": asset_id, "type": a["type"], "ok": False, "found": False})
            lines.append(f"⚠️ {asset_id}({a['type']}):此編號在多間辦公室都有,無法自動判斷,請用「改 編號 ...」單筆處理")
            continue

        office, sheet_name, row, record = result
        entry = {
            "asset_id": asset_id,
            "type": a["type"],
            "fields": fields,
            "note": note,
            "ok": ok,
            "found": True,
            "office": office,
            "sheet": sheet_name,
            "row": row,
        }
        pending.append(entry)

        if not ok:
            lines.append(f"⚠️ {asset_id}({a['type']}):{error_msg},不會自動寫入")
        else:
            field_desc = "、".join(f"{FIELD_DISPLAY_LABELS.get(k,k)}→{v}" for k, v in fields.items())
            lines.append(f"✅ {asset_id}({a['type']},{office}):{field_desc or '(欄位不變)'}\n　備註+「{note}」")

    valid_count = sum(1 for e in pending if e["ok"] and e["found"])
    lines.append(f"\n共 {valid_count} 筆會被寫入,{len(pending) - valid_count} 筆會略過。")

    USER_STATE.setdefault(chat_id, {})["pending_batch"] = pending

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 確認寫入", callback_data="batch_confirm")],
            [InlineKeyboardButton("❌ 取消", callback_data="batch_cancel")],
        ]
    )
    await message.reply_text("\n".join(lines), reply_markup=keyboard)


# ---------- 文字輸入處理(選單流程的下一步 + 快速指令) ----------

async def process_text(message, chat_id: int, text: str):
    """文字指令的核心處理邏輯,私訊(text_router)跟群組 @mention 都會呼叫這裡"""
    text = text.strip()
    if not text:
        return

    state = USER_STATE.get(chat_id, {})
    awaiting = state.get("awaiting")

    # --- 批次異動清單貼上偵測 ---
    if not awaiting and looks_like_batch_text(text):
        await run_batch_preview(message, chat_id, text)
        return

    # --- 選單流程中,正在等待輸入編號 ---
    if awaiting == "asset_id":
        state["awaiting"] = None
        await show_record(message, chat_id, text)
        return

    # --- 選單流程中,正在等待輸入某欄位新值 ---
    if awaiting and awaiting.startswith("field:"):
        field_key = awaiting.split(":", 1)[1]
        office, sheet_name, row, asset_id = state["office"], state["sheet"], state["row"], state["asset_id"]
        if field_key == "note":
            sheet_utils.append_note(office, sheet_name, row, text)
            await message.reply_text(f"✅ {asset_id} 已新增備註紀錄:{text}")
        else:
            sheet_utils.update_field(office, sheet_name, row, field_key, text)
            label = REVERSE_FIELD_LABELS.get(field_key, field_key)
            await message.reply_text(f"✅ {asset_id} 的「{label}」已更新為:{text}")
        state["awaiting"] = None
        await show_record(message, chat_id, asset_id, forced_office=office)
        return

    # --- 快速指令模式 ---
    parts = text.split()
    if not parts:
        return
    keyword = parts[0]

    if keyword == "查詢" and len(parts) >= 2:
        asset_id = " ".join(parts[1:])
        await show_record(message, chat_id, asset_id)

    elif keyword == "改" and len(parts) >= 4:
        asset_id, field_label = parts[1], parts[2]
        value = " ".join(parts[3:])
        await quick_update(message, chat_id, asset_id, field_label, value)

    elif keyword == "備註" and len(parts) >= 3:
        asset_id = parts[1]
        note_text = " ".join(parts[2:])
        await quick_note(message, chat_id, asset_id, note_text)

    elif keyword == "領用" and len(parts) >= 6:
        asset_id, person, department, emp_id = parts[1], parts[2], parts[3], parts[4]
        location = " ".join(parts[5:])
        await quick_checkout(message, chat_id, asset_id, person, department, emp_id, location)

    else:
        await message.reply_text(
            "看不懂這個指令,可以:\n"
            "・查詢 編號\n"
            "・改 編號 欄位 新值\n"
            "・備註 編號 內容\n"
            "・領用 編號 花名 部門 員編 所在區域\n"
            "・或直接貼一整段異動清單\n"
            "・打 /start 用按鈕選單"
        )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """私訊(或群組裡以 / 開頭以外的一般文字)入口"""
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    await process_text(update.message, chat_id, text)


async def mention_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群組裡 @機器人 的訊息入口(即使隱私模式開著,Telegram 也會把這種訊息送給 bot)"""
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    bot_username = context.bot.username
    cleaned = text.replace(f"@{bot_username}", "").strip()
    if not cleaned:
        await start(update, context)
        return
    await process_text(update.message, chat_id, cleaned)


async def quick_update(message, chat_id: int, asset_id: str, field_label: str, value: str, forced_office: str = None):
    field_key = FIELD_LABELS.get(field_label)
    if not field_key:
        await message.reply_text(f"⚠️ 欄位請填:{'、'.join(FIELD_LABELS.keys())}")
        return
    if field_key == "status" and value not in STATUS_OPTIONS:
        await message.reply_text(f"⚠️ 使用狀況請填:{'、'.join(STATUS_OPTIONS)}")
        return

    if forced_office:
        found = sheet_utils.find_asset(forced_office, asset_id)
        if not found:
            await message.reply_text(f"⚠️ 在「{forced_office}」找不到編號:{asset_id}")
            return
        office, sheet_name, row = forced_office, found[0], found[1]
    else:
        status, result = resolve_asset(asset_id)
        if status == "not_found":
            await message.reply_text(f"⚠️ 找不到編號:{asset_id}")
            return
        if status == "ambiguous":
            await ask_disambiguate(
                message, chat_id, asset_id,
                {"action": "update", "asset_id": asset_id, "field_label": field_label, "value": value},
            )
            return
        office, sheet_name, row, _ = result

    sheet_utils.update_field(office, sheet_name, row, field_key, value)
    await message.reply_text(f"✅ {asset_id}({office})的「{field_label}」已更新為:{value}")


async def quick_note(message, chat_id: int, asset_id: str, note_text: str, forced_office: str = None):
    if forced_office:
        found = sheet_utils.find_asset(forced_office, asset_id)
        if not found:
            await message.reply_text(f"⚠️ 在「{forced_office}」找不到編號:{asset_id}")
            return
        office, sheet_name, row = forced_office, found[0], found[1]
    else:
        status, result = resolve_asset(asset_id)
        if status == "not_found":
            await message.reply_text(f"⚠️ 找不到編號:{asset_id}")
            return
        if status == "ambiguous":
            await ask_disambiguate(
                message, chat_id, asset_id,
                {"action": "note", "asset_id": asset_id, "note_text": note_text},
            )
            return
        office, sheet_name, row, _ = result

    sheet_utils.append_note(office, sheet_name, row, note_text)
    await message.reply_text(f"✅ {asset_id}({office})已新增備註紀錄:{note_text}")


async def quick_checkout(message, chat_id: int, asset_id: str, person: str, department: str, emp_id: str, location: str, forced_office: str = None):
    """領用:設定使用狀況=使用中、保管人=花名、使用部門、員編、所在區域,並加備註紀錄"""
    if forced_office:
        found = sheet_utils.find_asset(forced_office, asset_id)
        if not found:
            await message.reply_text(f"⚠️ 在「{forced_office}」找不到編號:{asset_id}")
            return
        office, sheet_name, row = forced_office, found[0], found[1]
    else:
        status, result = resolve_asset(asset_id)
        if status == "not_found":
            await message.reply_text(f"⚠️ 找不到編號:{asset_id}")
            return
        if status == "ambiguous":
            await ask_disambiguate(
                message, chat_id, asset_id,
                {
                    "action": "checkout", "asset_id": asset_id, "person": person,
                    "department": department, "emp_id": emp_id, "location": location,
                },
            )
            return
        office, sheet_name, row, _ = result

    fields = {
        "status": "使用中",
        "keeper": person,
        "department": department,
        "emp_id": emp_id,
        "location": location,
    }
    sheet_utils.update_fields(office, sheet_name, row, fields)
    sheet_utils.append_note(office, sheet_name, row, f"領用(領用人:{person})")
    await message.reply_text(
        f"✅ {asset_id}({office})已登記領用:\n"
        f"保管人:{person}　使用部門:{department}　員編:{emp_id}　所在區域:{location}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ 資產機器人發生錯誤:{context.error}")
    except Exception:
        pass


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Entity("mention"), mention_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)
    logger.info("資產清冊機器人啟動中...")
    app.run_polling()


if __name__ == "__main__":
    main()
