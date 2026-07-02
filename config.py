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
WATER_GID = int(os.getenv("WATER_GID", "332866068"))

# 剩餘桶數 <= 此門檻，狀態自動標示為「⚠️ 需補貨」
WATER_LOW_STOCK_THRESHOLD = int(os.getenv("WATER_LOW_STOCK_THRESHOLD", "20"))

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

# --- 授權可操作此機器人的 Telegram 使用者 (留空 = 不限制，建議正式上線後填寫) ---
_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(x) for x in _allowed.split(",") if x.strip().isdigit()}
