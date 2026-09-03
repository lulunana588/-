# config.py
# 讀取 .env 內的設定值，統一管理 Token / 試算表 ID / 服務帳戶金鑰路徑

import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# --- Google 服務帳戶金鑰 (JSON 檔案路徑) ---
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "/root/luna_bot/service_account.json"
)

# --- 桶裝水庫存表 ---
WATER_SHEET_ID = os.getenv(
    "WATER_SHEET_ID", "1rBdc0jHRmunHJ01eA5hkdF25mCWgfW8WUbCuUHeeFYc"
)
WATER_GID = int(os.getenv("WATER_GID", "2140693624"))

# 剩餘桶數 <= 此門檻，狀態自動標示為「⚠️ 需補貨」
WATER_LOW_STOCK_THRESHOLD = int(os.getenv("WATER_LOW_STOCK_THRESHOLD", "20"))

# 桶裝水地點「別名」對照表：左邊是您平常習慣打的簡稱，右邊要對到「總覽」表地點欄位裡
# 實際出現的文字（子字串即可）。快速指令解析時會先查這個表，查不到才用關鍵字模糊比對。
# 可以自行增修，例如：
#   WATER_LOCATION_ALIASES = {
#       "敦化": "共享服務中心忠孝辦",
#       "松山辦水寶貝": "客服中心松山辦",
#   }
WATER_LOCATION_ALIASES = {}

# --- 款項追蹤表 ---
PAYMENT_SHEET_ID = os.getenv(
    "PAYMENT_SHEET_ID", "11qC7Om4eVhBdZZUtrpCoL7oAyY5nEYR5lZ3I4MPUYkk"
)
PAYMENT_GID = int(os.getenv("PAYMENT_GID", "1471681931"))

# 款項追蹤表新增款項時，「進度」欄位的常用選項（可自行增修）
PAYMENT_PROGRESS_OPTIONS = [
    "已提交請款單及發票",
    "已提交請款單候補發票",
    "尚未提交",
]

# 快速指令比對「是否為同一筆既有款項」時，金額允許的誤差範圍（NT$）
# 完全相同金額 -> 直接視為同一筆自動編輯；金額在誤差範圍內但不完全相同 -> 會先跳出確認，不會自動合併
PAYMENT_AMOUNT_TOLERANCE = int(os.getenv("PAYMENT_AMOUNT_TOLERANCE", "50"))

# --- 允許授權可操作此機器人的 Telegram 使用者 (留空 = 不限制，建議正式上線後填寫) ---
_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(x) for x in _allowed.split(",") if x.strip().isdigit()}

# --- 每日款項追蹤提醒（週一~週五 台灣時間 10:05 私訊提醒）---
_reminder_chat_id = os.getenv("REMINDER_CHAT_ID", "8656008330")
REMINDER_CHAT_ID = int(_reminder_chat_id) if _reminder_chat_id.strip().isdigit() else None

# --- 綜辦文件繳回追蹤表 ---
DOC_SHEET_ID = os.getenv(
    "DOC_SHEET_ID", "1jirTj5n-V5nEwoOmRhI0NQ1Lz9zIovLtwTFR0hCvD9Y"
)
DOC_GID = int(os.getenv("DOC_GID", "0"))

# --- 每月桶裝水盤點提醒（每月10號 台灣時間 10:05，遇假日/國定假日提前到最近的上班日）---
# 剩餘桶數 < 此數字，會在月度盤點報告裡標示「需要儲值」
WATER_MONTHLY_REFILL_THRESHOLD = int(os.getenv("WATER_MONTHLY_REFILL_THRESHOLD", "100"))

# 台灣國定假日行事曆（依政府行政機關辦公日曆表），用來判斷「10號遇假日要提前」。
# 只需要列當年度會影響到「10號」附近判斷的日期就好，但為了保險起見這裡列出全年連假區間。
# 每年切換記得更新這裡；沒有列出的年份，只會用「六日」判斷，不會考慮國定假日。
TAIWAN_HOLIDAYS = {
    2026: {
        "2026-01-01",  # 元旦
        # 農曆春節連假（小年夜~初三~補假）
        "2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
        "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
        # 和平紀念日連假
        "2026-02-27", "2026-02-28", "2026-03-01",
        # 兒童節/清明節連假
        "2026-04-03", "2026-04-04", "2026-04-05",
        # 勞動節連假
        "2026-05-01", "2026-05-02", "2026-05-03",
        # 端午節連假
        "2026-06-19", "2026-06-20", "2026-06-21",
        # 中秋節/教師節連假
        "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28",
        # 國慶日連假
        "2026-10-09", "2026-10-10", "2026-10-11",
        # 台灣光復節連假
        "2026-10-24", "2026-10-25", "2026-10-26",
        # 行憲紀念日連假
        "2026-12-25", "2026-12-26", "2026-12-27",
    },
}
# 群組話題ID設定（2026/09/03 新增）
# 用於：完成桶裝水/款項操作後，把確認訊息同步發到對應的群組話題
GROUP_CHAT_ID = -1003755120614
WATER_TOPIC_THREAD_ID = 1046
PAYMENT_TOPIC_THREAD_ID = 3884
