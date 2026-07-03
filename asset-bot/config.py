# -*- coding: utf-8 -*-
import os

# ====== Telegram 設定 ======
TELEGRAM_BOT_TOKEN = os.environ.get("ASSET_BOT_TOKEN", "請填入你的 Bot Token")
ADMIN_CHAT_ID = 8656008330  # Luna 的 Telegram ID,錯誤通知用

# ====== Google Sheets 設定 ======
GOOGLE_SERVICE_ACCOUNT_FILE = "/root/asset-bot/service_account.json"

# 兩個辦公室對應的試算表(逐筆資產清冊,非彙總表)
OFFICES = {
    "商務中心": {
        "spreadsheet_id": "1fojyuUK4MEKNzlQybl-3lxQ64ginknIXydxESFreYhs",
    },
    "共享服務中心": {
        "spreadsheet_id": "1VtLBWgtLtkgsNfwyXqbmKs6nYban46eSoD0u2r1zS8o",
    },
}

# 每個辦公室試算表底下,實際存放逐筆資產紀錄的分頁名稱(依序搜尋)
DETAIL_SHEETS = ["辦公室資產", "資訊類資產"]

# 欄位對應(A~O),兩個辦公室、兩個分頁欄位結構一致
COLUMNS = {
    "id": "A",              # 編號
    "name": "B",             # 名稱
    "spec": "C",              # 規格
    "serial": "D",             # 序號
    "purchase_unit": "E",     # 採購單位
    "status": "F",             # 使用狀況(庫存/使用中)
    "company": "G",            # 所在公司
    "location": "H",           # 所在區域
    "department": "I",         # 使用部門
    "emp_id": "J",              # 員編
    "keeper": "K",              # 保管人
    "in_date": "L",             # 入庫日期
    "pickup_date": "M",         # 領取日期
    "spec2": "N",               # 規格(重複欄)
    "note": "O",                # 備註(顯示文字)
}

HEADER_ROW = 1
STATUS_OPTIONS = ["庫存", "使用中", "已報廢", "已調出"]

# 本點管理 / 跨點調撥 這兩個分頁,用來記錄領用/入庫/遺失/調入/調出/報廢/購入的異動歷程
LOCAL_LOG_SHEET = "本点管理"       # 欄位: A任務 B日期 C花名 D說明 E編號 F名稱 G規格
TRANSFER_LOG_SHEET = "跨點調撥"    # 欄位: A任務 B日期 C部門 D說明 E編號 F名稱 G規格

# 同仁可透過 bot 修改的欄位(中文標籤 -> COLUMNS key)
EDITABLE_FIELDS = {
    "所在區域": "location",
    "使用部門": "department",
    "員編": "emp_id",
    "保管人": "keeper",
}

# ====== Groq LLM 設定(用於解析整段貼上的異動清單文字)======
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "請填入你的 Groq API Key")
GROQ_MODEL = "llama-3.3-70b-versatile"

# 批次異動支援的類型(用同一套 LLM 解析 + 預覽確認;購入因為要新建資料,走獨立指令流程)
BATCH_ACTION_TYPES = ["入庫", "領用", "故障", "換座位", "變更保管人", "遺失", "調入", "調出", "報廢"]
