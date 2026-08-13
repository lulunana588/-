import os

# ══════════════════════════════════════════════
# ⚠️ 部署前請先填好這三項（其他不用動）
TELEGRAM_BOT_TOKEN = os.getenv("SECRETARY_BOT_TOKEN", "填入新Bot的Token（跟BotFather申請）")
TELEGRAM_CHAT_ID = os.getenv("SECRETARY_CHAT_ID", "填入要推播的對象chat_id（你個人或群組）")
OWNER_USER_ID = os.getenv("SECRETARY_OWNER_ID", "填入你的Telegram個人ID，8656008330")
# ══════════════════════════════════════════════

BASE_DIR = "/root/secretary-bot"
DB_PATH = f"{BASE_DIR}/secretary.db"
CARD_OUTPUT_PATH = f"{BASE_DIR}/today_card.png"

# 每個工作日推播時間（台灣時間）
PUSH_HOUR = 10
PUSH_MINUTE = 0
PUSH_WEEKDAYS = {0, 1, 2, 3, 4}  # 週一=0 ... 週五=4，週六日不推

TAIWAN_TZ_OFFSET_HOURS = 8  # 台灣時間 = UTC+8

# 視覺風格：深色 + 薄荷綠（沿用 diary-bot 風格）
COLOR_BG = (13, 17, 23)
COLOR_CARD = (22, 27, 34)
COLOR_MINT = (63, 185, 148)
COLOR_MINT_DIM = (45, 130, 105)
COLOR_TEXT = (230, 237, 243)
COLOR_TEXT_DIM = (139, 148, 158)
COLOR_RED = (248, 81, 73)
COLOR_YELLOW = (210, 153, 34)
COLOR_ORANGE = (219, 109, 40)


def _find_font(*candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]  # 找不到就回傳第一個，PIL會fallback成預設字型


FONT_REGULAR = _find_font(
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
)
FONT_BOLD = _find_font(
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
)
