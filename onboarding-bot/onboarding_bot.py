#!/usr/bin/env python3
"""
入職離職工作流小幫手
依交接資料整理：智影 / SETV / 商務中心 各自的入職、離職checklist（含門禁系統操作步驟提示）；客服中心 / 運營中心 / 共享服務中心(宏國) 目前為通用佔位checklist，待補充實際SOP
用法：
  /start          → 開始新案件（選辦公室 → 選入職/離職 → 輸入姓名 → 出現checklist）
  /list           → 查看目前進行中的案件與完成度
  按項目按鈕      → 打勾 / 取消
  按 ℹ️ 按鈕      → 彈出該項目的操作步驟提示
"""
import sqlite3
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# ══════════════════════════════════════════════
BOT_TOKEN = "8896511907:AAE_3eCAtwl74DP94kF9meORmOhLA10dWzo"
NOTIFY_CHAT_ID = "填入要推播完成通知的群組ID"   # 案件全部完成時會推播到這個群組
DB_PATH = "/root/onboarding-bot/onboarding.db"
# ══════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)

CHOOSE_OFFICE, CHOOSE_TYPE, ENTER_NAME = range(3)

OFFICES = ["智影", "SETV", "商務中心", "客服中心", "運營中心", "共享服務中心(宏國)"]
CASE_TYPES = ["入職", "離職"]

# 依交接資料整理的checklist
CHECKLISTS = {
    ("商務中心", "入職"): [
        {"key": "badge", "label": "門禁卡（設定早/中/晚班權限）",
         "hint": "新光門禁系統，需設定早/中/晚三個班次的門禁權限。"},
        {"key": "employee_form", "label": "填寫員工資料表", "hint": None},
        {"key": "health_form", "label": "健康檢查調查表", "hint": None},
        {"key": "handbook", "label": "員工手冊同意書簽署", "hint": None},
        {"key": "nickname", "label": "詢問花名並同步人事", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("商務中心", "離職"): [
        {"key": "confirm_time", "label": "跟人事確認離職時間",
         "hint": "通常約定 17:00 或 18:00。"},
        {"key": "return_form", "label": "填寫離職人員物品繳回表（資產）", "hint": None},
        {"key": "password", "label": "請離職人員寫下帳密", "hint": None},
        {"key": "asset_check", "label": "點交設備", "hint": None},
        {"key": "reset_device", "label": "設備重置交接",
         "hint": "手機由前台自行重置；主機/筆電交給網管（林至彪）重置；Android 用「恢復原廠設定」。"},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
        {"key": "badge_revoke", "label": "門禁卡收回/停用", "hint": None},
    ],
    ("混合辦", "入職"): [
        {"key": "badge_add", "label": "門禁系統新增員工資料與卡號權限",
         "hint": ("桌面門禁系統 → 輸入密碼登入 → 3.人事管理 → 3.1員工資料庫 → 新增 → "
                  "輸入員工編號/姓名/部門 → 卡機權限管理 → 手動輸入新卡號 → "
                  "第一個加入（卡號顯示在預設卡片）→ 裝置選「僅限大門」→ 儲存下載，確認執行結果。")},
        {"key": "cloud_list", "label": "更新雲端員工名單（天勤台灣-花名冊/座位表）", "hint": None},
        {"key": "asset_register", "label": "資產領用登記（資產清冊-天勤台灣）", "hint": None},
        {"key": "wifi", "label": "提供WiFi帳密（視需要）", "hint": None},
    ],
    ("混合辦", "離職"): [
        {"key": "badge_delete", "label": "門禁卡刪除",
         "hint": ("桌面門禁系統 → 3.人事管理 → 3.1員工資料庫 → 點選人名 → 修改 → "
                  "卡機權限管理 → 點選已設定之舊卡號 → 第二個加入 → 裝置選「大門」→ "
                  "卡片權限刪除，確認執行結果後可關閉畫面。")},
        {"key": "one_click_leave", "label": "人事管理 → 基本資料 → 一鍵離職", "hint": None},
        {"key": "badge_end_date", "label": "修改門禁卡結束日（可選）",
         "hint": "3.3員工卡片管理 → 點選人名 → 修改 → 有效結束日改為離職前一天 → 確定。"},
        {"key": "asset_return", "label": "資產繳回登記（繳回綜辦紀錄表）", "hint": None},
        {"key": "sim_check", "label": "SIM卡/門號處理確認", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("SETV", "入職"): [
        {"key": "confirm_info", "label": "確認新人資訊、座位、設備規格",
         "hint": "若消息從燕兒那邊得知，需再跟小饅確認新人資訊、座位及設備規格。"},
        {"key": "prep_asset_form", "label": "準備設備、填寫新人資產領用表（能填的先填）", "hint": None},
        {"key": "checkin", "label": "報到當天接待，帶至會議室填資料",
         "hint": "花名與人事相關流程由小饅處理，前台負責接待與帶位。"},
    ],
    ("SETV", "離職"): [
        {"key": "prep_return_form", "label": "準備離職人員物品繳回表（能填的先填）", "hint": None},
        {"key": "copy_form", "label": "離職物品繳回表複印一份給小饅", "hint": None},
        {"key": "reset_laptop", "label": "通知網管重置離職人員筆電",
         "hint": "訊息傳給網管（標），請他過來重置。"},
    ],
    ("智影", "入職"): [
        {"key": "prep_asset_form", "label": "準備新人領用表與設備",
         "hint": "人事資料由高允貞準備，前台只需準備新人領用表跟設備。"},
        {"key": "check_stock", "label": "確認庫存有無該組別所需設備規格",
         "hint": "平面用14 Pro；剪輯用高規主機（可能加配筆電）；其他一般用Air13 256G。一看到高允貞發新人資訊就馬上確認。"},
        {"key": "orientation", "label": "帶新人回位子、介紹環境",
         "hint": "含門禁磁扣、廁所、茶水間、攝影棚（需先問過製片組）、電梯、換鞋拖鞋。此步驟通常由高允貞執行。"},
        {"key": "asset_form_to_tt", "label": "資產領用表掃描檔案給田田", "hint": None},
        {"key": "cloud_list", "label": "更新雲端人員表/座位表",
         "hint": "含雲端人員表、MD雲端座位表、綜辦座位表、MD座位表。"},
        {"key": "remind_check_device", "label": "提醒員工檢查設備",
         "hint": "發放設備時提醒員工先檢查設備，若有異常（例如連接埠無法使用）要馬上跟前台說，避免後續賠付爭議。"},
    ],
    ("智影", "離職"): [
        {"key": "notify_group", "label": "到崗離職對接群通知", "hint": None},
        {"key": "check_asset_condition", "label": "確認資產外觀/鍵盤/連接埠",
         "hint": "可先讓人員自行寫下帳密；前台檢查完後再讓人員簽本名，審計簽單。"},
        {"key": "collect_device", "label": "設備當天收回", "hint": None},
        {"key": "sim_check", "label": "確認SIM卡月租/充值狀態並記錄", "hint": None},
        {"key": "transfer_check", "label": "確認設備移轉對象",
         "hint": "留職停薪設備需找保管人簽物品歸還表；若職級較高留停需要帶手機，需審計同意。"},
    ],
    # ⚠️ 以下辦公室尚無交接資料，先放通用佔位checklist讓按鍵可用
    # 有實際SOP後，比照上面格式替換掉即可
    ("客服中心", "入職"): [
        {"key": "badge", "label": "門禁卡/磁扣開通", "hint": "⚠️ 待補充：請提供實際門禁系統操作步驟"},
        {"key": "employee_form", "label": "填寫員工資料表", "hint": None},
        {"key": "asset_register", "label": "資產領用登記", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("客服中心", "離職"): [
        {"key": "confirm_time", "label": "跟人事確認離職時間", "hint": None},
        {"key": "return_form", "label": "填寫離職人員物品繳回表（資產）", "hint": None},
        {"key": "reset_device", "label": "設備重置交接", "hint": "⚠️ 待補充：確認實際負責重置的窗口"},
        {"key": "badge_revoke", "label": "門禁卡收回/停用", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("運營中心", "入職"): [
        {"key": "badge", "label": "門禁卡/磁扣開通", "hint": "⚠️ 待補充：請提供實際門禁系統操作步驟"},
        {"key": "employee_form", "label": "填寫員工資料表", "hint": None},
        {"key": "asset_register", "label": "資產領用登記", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("運營中心", "離職"): [
        {"key": "confirm_time", "label": "跟人事確認離職時間", "hint": None},
        {"key": "return_form", "label": "填寫離職人員物品繳回表（資產）", "hint": None},
        {"key": "reset_device", "label": "設備重置交接", "hint": "⚠️ 待補充：確認實際負責重置的窗口"},
        {"key": "badge_revoke", "label": "門禁卡收回/停用", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("共享服務中心(宏國)", "入職"): [
        {"key": "badge", "label": "門禁卡/磁扣開通", "hint": "⚠️ 待補充：請提供實際門禁系統操作步驟"},
        {"key": "employee_form", "label": "填寫員工資料表", "hint": None},
        {"key": "asset_register", "label": "資產領用登記", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
    ("共享服務中心(宏國)", "離職"): [
        {"key": "confirm_time", "label": "跟人事確認離職時間", "hint": None},
        {"key": "return_form", "label": "填寫離職人員物品繳回表（資產）", "hint": None},
        {"key": "reset_device", "label": "設備重置交接", "hint": "⚠️ 待補充：確認實際負責重置的窗口"},
        {"key": "badge_revoke", "label": "門禁卡收回/停用", "hint": None},
        {"key": "cloud_list", "label": "更新雲端員工名單", "hint": None},
    ],
}


# ── DB ──────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            office TEXT, case_type TEXT, name TEXT,
            created_at TEXT, closed_at TEXT, status TEXT DEFAULT 'open',
            created_by TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER, item_key TEXT, label TEXT, hint TEXT,
            done INTEGER DEFAULT 0, done_at TEXT,
            FOREIGN KEY(case_id) REFERENCES cases(id)
        )
    """)
    conn.commit()
    conn.close()


def create_case(office, case_type, name, created_by):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO cases (office, case_type, name, created_at, created_by) VALUES (?,?,?,?,?)",
        (office, case_type, name, datetime.now().strftime("%Y-%m-%d %H:%M"), created_by)
    )
    case_id = cur.lastrowid
    for item in CHECKLISTS[(office, case_type)]:
        cur.execute(
            "INSERT INTO items (case_id, item_key, label, hint) VALUES (?,?,?,?)",
            (case_id, item["key"], item["label"], item["hint"])
        )
    conn.commit()
    conn.close()
    return case_id


def get_case(case_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    items = conn.execute("SELECT * FROM items WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
    conn.close()
    return case, items


def toggle_item(case_id, item_key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT done FROM items WHERE case_id=? AND item_key=?", (case_id, item_key)
    ).fetchone()
    new_val = 0 if row[0] else 1
    done_at = datetime.now().strftime("%Y-%m-%d %H:%M") if new_val else None
    cur.execute(
        "UPDATE items SET done=?, done_at=? WHERE case_id=? AND item_key=?",
        (new_val, done_at, case_id, item_key)
    )
    conn.commit()
    conn.close()


def all_done(case_id):
    conn = sqlite3.connect(DB_PATH)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM items WHERE case_id=? AND done=0", (case_id,)
    ).fetchone()[0]
    conn.close()
    return remaining == 0


def close_case(case_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE cases SET status='closed', closed_at=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), case_id)
    )
    conn.commit()
    conn.close()


def open_cases():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM cases WHERE status='open' ORDER BY id").fetchall()
    conn.close()
    return rows


# ── UI ──────────────────────────────────────
def build_checklist_markup(case_id, items):
    rows = []
    for it in items:
        mark = "✅" if it["done"] else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {it['label']}", callback_data=f"tg:{case_id}:{it['item_key']}")])
        if it["hint"]:
            rows.append([InlineKeyboardButton("ℹ️ 查看步驟", callback_data=f"hint:{case_id}:{it['item_key']}")])
    return InlineKeyboardMarkup(rows)


def checklist_text(case, items):
    done_n = sum(1 for i in items if i["done"])
    total = len(items)
    return (f"📋 {case['office']}｜{case['case_type']}\n"
            f"👤 {case['name']}\n"
            f"進度：{done_n}/{total}\n\n"
            f"點項目打勾，ℹ️查看操作步驟")


# ── Handlers ────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(o, callback_data=f"office:{o}")] for o in OFFICES]
    await update.message.reply_text("要辦理哪個辦公室的入職/離職？", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_OFFICE


async def choose_office(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    office = query.data.split(":")[1]
    context.user_data["office"] = office
    kb = [[InlineKeyboardButton(t, callback_data=f"type:{t}")] for t in CASE_TYPES]
    await query.edit_message_text(f"辦公室：{office}\n請選擇類型：", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    case_type = query.data.split(":")[1]
    context.user_data["case_type"] = case_type
    await query.edit_message_text(f"辦公室：{context.user_data['office']}｜類型：{case_type}\n\n請輸入此人姓名：")
    return ENTER_NAME


async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    office = context.user_data["office"]
    case_type = context.user_data["case_type"]
    created_by = update.effective_user.first_name

    case_id = create_case(office, case_type, name, created_by)
    case, items = get_case(case_id)

    await update.message.reply_text(
        checklist_text(case, items),
        reply_markup=build_checklist_markup(case_id, items)
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已取消。")
    return ConversationHandler.END


async def toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, case_id, item_key = query.data.split(":")
    case_id = int(case_id)

    toggle_item(case_id, item_key)
    case, items = get_case(case_id)

    await query.edit_message_text(
        checklist_text(case, items),
        reply_markup=build_checklist_markup(case_id, items)
    )

    if all_done(case_id):
        close_case(case_id)
        msg = f"🎉 {case['office']}｜{case['case_type']}辦理完成\n👤 {case['name']}\n經辦人：{case['created_by']}"
        await context.bot.send_message(chat_id=query.message.chat_id, text=msg)
        if not NOTIFY_CHAT_ID.startswith("填入"):
            await context.bot.send_message(chat_id=NOTIFY_CHAT_ID, text=msg)


async def hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, case_id, item_key = query.data.split(":")
    case_id = int(case_id)
    _, items = get_case(case_id)
    hint = next((i["hint"] for i in items if i["item_key"] == item_key), None)
    await query.answer(text=hint or "此項目沒有額外步驟說明", show_alert=True)


async def list_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = open_cases()
    if not rows:
        await update.message.reply_text("目前沒有進行中的案件 ✅")
        return
    lines = ["📂 進行中的案件：\n"]
    for r in rows:
        _, items = get_case(r["id"])
        done_n = sum(1 for i in items if i["done"])
        lines.append(f"#{r['id']} {r['office']}｜{r['case_type']}｜{r['name']}（{done_n}/{len(items)}）")
    await update.message.reply_text("\n".join(lines))


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_OFFICE: [CallbackQueryHandler(choose_office, pattern=r"^office:")],
            CHOOSE_TYPE: [CallbackQueryHandler(choose_type, pattern=r"^type:")],
            ENTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("list", list_cases))
    app.add_handler(CallbackQueryHandler(toggle_callback, pattern=r"^tg:"))
    app.add_handler(CallbackQueryHandler(hint_callback, pattern=r"^hint:"))

    app.run_polling()


if __name__ == "__main__":
    main()
