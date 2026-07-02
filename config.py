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
    "已提交請款單，未附發票",
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
