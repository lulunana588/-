"""
鵝鵝日報小幫手 - diary_bot.py  ✦ 優化版 v2.0
═══════════════════════════════════════════════
功能清單：
 1. Groq 三分類意圖判斷：leave / cancel / diary
    - 硬規則防呆：句子太短、無日期線索、日期數量過多 → 強制反問
    - leave：解析日期+假別 → 寫入 special_days → 文字確認
    - cancel：解析日期 → 從 special_days 刪除 → 文字確認
    - diary：結構化拆解 → 產出 V1+V2 → 存入 SQLite → 附月曆 PNG
 2. 「補充：XXX」— 追加修正今天已存的日報內容
 3. 「員工自評」/ 「員工自評 YYYY-MM-DD YYYY-MM-DD」— 彙整日報 → 三維評分
 4. 「月曆」— 單獨重新產生當月月曆圖
 5. 「請假記錄」/ 「請假記錄 7月」— 查看請假清單
 6. 「查看X/XX日報」— 查看特定日期日報
 7. 每日 14:00（台灣時間）平日提醒推播（JobQueue）
 8. 每週日自動備份 SQLite → Google Drive（JobQueue）
 9. 每小時健康檢查（由外部 health_check.sh 呼叫）
═══════════════════════════════════════════════
"""

import os
import json
import logging
import re
import sqlite3
import calendar
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    ContextTypes, filters, JobQueue
)
from groq import Groq
from opencc import OpenCC
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

# ── 環境變數 ────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
GAS_BACKUP_URL     = "https://script.google.com/macros/s/AKfycbzLByF3oltzhfk6n24sfxbQQPerYeBAX-OrfQwYS0BsjSodmJtEuSFJzAhZqFYUsPTb/exec"
DB_PATH            = "/root/diary-bot/diary.db"
CALENDAR_IMG_PATH  = "/root/diary-bot/calendar_output.png"
FONT_PATH          = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"

# ── 防呆硬規則 ──────────────────────────────────────
MIN_LEAVE_TEXT_LEN      = 5    # 請假句子最少字元數
MAX_LEAVE_DATES         = 5    # 一次最多標記/取消日期數量
RELATIVE_DATE_KEYWORDS  = ("明天", "明日", "後天", "后天", "下週", "下周", "今天", "今日")
LEAVE_KEYWORDS          = ("請假", "病假", "事假", "年假", "特休", "生理假", "月假",
                           "例假", "假", "不來", "休息", "休假", "取消", "撤銷")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)
cc = OpenCC('t2s')  # 繁→簡（V2用）
TAIWAN_TZ = timezone(timedelta(hours=8))


# ══════════════════════════════════════════════════════
#  SYSTEM PROMPT — 日報解析（優化版 v2.0）
# ══════════════════════════════════════════════════════
DIARY_PARSE_PROMPT = """你是集團綜合部-行政組（L2）的日報整理助理。
使用者是行政組長 Luna，負責管理商務中心、共享服務中心（忠孝/宏國辦）、混合辦、客服中心等多個辦公場域，
工作範疇含：出勤管理、資產採購、廠商對接、設施維護、門禁管理、費用核銷、AI工具開發、制度優化等。

任務：把 Luna 輸入的口語化工作描述（繁簡中文皆可）
整理成「結構化日報 JSON」。

⚠️ 嚴格要求：僅輸出純JSON，禁止任何額外文字，禁止 markdown code fence。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【模組標籤】依事項性質選用最貼切的標籤（可重複）：

[出勤管理]   — 點名/出缺席核實/考勤異常/班表調整/出勤追蹤
[資產採購]   — 採購申請/比價/訂購/到貨驗收/庫存清點/盤點
[廠商管理]   — 廠商溝通/請款單製作/合約跟進/驗收確認/報價比較
[設施維護]   — 設備報修/清潔安排/空間整備/環境巡檢/水電維修協調
[門禁管理]   — 門禁卡申辦/補辦/停用/門禁紀錄查核/訪客管理
[費用核銷]   — 報銷表單/備用金/帳單確認/付款追蹤/費用申請
[行政支援]   — 跨部門協作/文件列印/資料整理/會議支援/離職流程/信件往來
[人員管理]   — 人員培訓/工作指派/績效追蹤/團隊協調/新進作業
[制度優化]   — SOP建立與修訂/流程改善/系統設定調整/標準化作業
[AI工具開發] — Bot建置/自動化腳本/工具開發與測試/系統維護
[其他行政]   — 不屬於以上類別的一般行政事務

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【輸出 JSON 格式】（唯一輸出，不得有其他任何文字）：
{
  "today_results": [
    {
      "tag": "模組標籤（從上方11個中選）",
      "text": "事項描述（見撰寫規範）",
      "status": "完成 | 跟進中 | 待確認 | 已上報"
    }
  ],
  "blockers": "卡點描述：卡在哪一步 + 原因 + 已採取什麼行動（若無填「無」）",
  "exceptions": "超出日常預期的異常或特殊事項，需主管知悉者（若無填「無」）",
  "tomorrow_plan": [
    "明日任務描述1（含具體動作與對象）",
    "明日任務描述2"
  ],
  "ai_usage": "今日使用AI工具情況，含用途與估計節省時間（若無填「無」）"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【事項描述撰寫規範】

① 必須以動作動詞開頭：
   完成／推進／協調／執行／核實／整理／跟進／確認／安排／提交／回覆／建立／更新／處理

② 盡量包含三要素：【動作】+【對象（辦公室/人名/品項/系統）】+【結果/數量/狀態】
   ✅ 完成商務中心3支公務門號續租申辦，文件已提交業務窗口
   ✅ 協調清潔廠商執行混合辦6月深度清潔，共2區塊完成驗收
   ❌ 處理門號的事情
   ❌ 跟廠商聯絡

③ 保留原文中的具體數字（X份/X位/X件/X張/X支）
④ 跨多個辦公室或不同類別的事項，各自拆分為獨立條目
⑤ 跨當天未完成的事項，status 標記「跟進中」而非「完成」

【status 判斷原則】
 完成   → 當天完全結案
 跟進中 → 已動作但需後續確認或等待
 待確認 → 等待他人回應或決定
 已上報 → 已向上級或相關方通報

【ai_usage 偵測原則】
若 Luna 提及使用 AI 工具（Claude / Groq / Bot / 自動化腳本等），
須摘要說明：用途 + 完成的任務 + 估算節省時間（分鐘）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【其他規則】
- today_results 至少一條，按描述合理拆分，不硬湊、不捏造
- 若 Luna 未明確提及「明天要做什麼」，tomorrow_plan 留空陣列 []
- 不捏造 Luna 未提到的細節、數字、人名、地點
- 輸出語言：繁體中文（V2格式轉換由程式負責，此處輸出繁體即可）
"""

# ══════════════════════════════════════════════════════
#  SYSTEM PROMPT — 意圖分類（三分類）
# ══════════════════════════════════════════════════════
INTENT_PROMPT = """你是一個意圖分類器，將使用者輸入分為三類：leave / cancel / diary。

leave  = 明確要標記某些具體日期為請假/特殊狀態
cancel = 明確要取消/撤銷某些具體日期的標記
diary  = 一般工作描述，或不確定的輸入

輸出格式（純JSON，唯一輸出）：
{
  "intent": "leave | cancel | diary",
  "dates": ["YYYY-MM-DD", ...],
  "leave_type": "病假 | 事假 | 年假 | 生理假 | 其他",
  "confidence": "high | low"
}

規則：
- dates 只列出有明確出現的具體日期，不推斷、不補全
- 若輸入模糊或不確定，intent 填 diary，confidence 填 low
- 只有 high confidence 時才填 leave 或 cancel
- 不要添加任何JSON以外的文字
"""

# ══════════════════════════════════════════════════════
#  SYSTEM PROMPT — 員工自評生成
# ══════════════════════════════════════════════════════
SELF_REVIEW_PROMPT = """你是一位行政組的績效評核助理。
根據提供的日報紀錄，為 Luna（資深行政專員，L2）產出月度員工自評。

評分標準（寬鬆標準，體現成長與努力）：
- 三個維度分數：各在 85–95 分之間
- AI應用分數：在 85–93 分之間
- 整體完成率依日報提交率計算

輸出格式（純JSON，唯一輸出）：
{
  "period": "YYYY-MM-DD ~ YYYY-MM-DD",
  "completion_rate": "XX%（X個工作日已填/共X個工作日）",
  "dimension_scores": {
    "執行力": { "score": 90, "comment": "評語（具體、正向，2-3句）" },
    "協作溝通": { "score": 88, "comment": "評語" },
    "創新優化": { "score": 87, "comment": "評語" }
  },
  "ai_score": { "score": 90, "comment": "AI工具應用評語（含具體使用案例）" },
  "highlights": ["本期亮點1", "本期亮點2", "本期亮點3"],
  "improvement": "改善建議（溫和、建設性，1-2句）",
  "overall_summary": "整體自評摘要（3-4句，正式繁體中文，可直接複製到系統填報）"
}
不要輸出任何JSON以外的文字。
"""


# ══════════════════════════════════════════════════════
#  資料庫操作
# ══════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS diaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            raw_input TEXT,
            data_json TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS special_days (
            date TEXT PRIMARY KEY,
            type TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS self_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            review_json TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_diary(date_str: str, raw_input: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO diaries (date, raw_input, data_json, created_at) VALUES (?, ?, ?, ?)",
        (date_str, raw_input,
         json.dumps(data, ensure_ascii=False),
         datetime.now(TAIWAN_TZ).isoformat())
    )
    conn.commit()
    conn.close()


def get_latest_diary_for_date(date_str: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, raw_input, data_json FROM diaries WHERE date=? ORDER BY id DESC LIMIT 1",
        (date_str,)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1], json.loads(row[2])
    return None


def update_diary(diary_id: int, raw_input: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE diaries SET raw_input=?, data_json=? WHERE id=?",
        (raw_input, json.dumps(data, ensure_ascii=False), diary_id)
    )
    conn.commit()
    conn.close()


def get_diaries_in_range(start: str, end: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT date, data_json FROM diaries WHERE date>=? AND date<=? ORDER BY date ASC",
        (start, end)
    )
    rows = c.fetchall()
    conn.close()
    return [(r[0], json.loads(r[1])) for r in rows]


def get_special_days(year: int, month: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    prefix = f"{year:04d}-{month:02d}"
    c.execute("SELECT date, type FROM special_days WHERE date LIKE ?", (prefix + "%",))
    rows = c.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def save_special_day(date_str: str, leave_type: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO special_days (date, type) VALUES (?, ?)", (date_str, leave_type))
    conn.commit()
    conn.close()


def delete_special_day(date_str: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM special_days WHERE date=?", (date_str,))
    conn.commit()
    conn.close()


def get_all_special_days(month_prefix: str = None) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if month_prefix:
        c.execute("SELECT date, type FROM special_days WHERE date LIKE ? ORDER BY date", (month_prefix + "%",))
    else:
        c.execute("SELECT date, type FROM special_days ORDER BY date")
    rows = c.fetchall()
    conn.close()
    return rows


def save_self_review(label: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO self_reviews (label, review_json, created_at) VALUES (?, ?, ?)",
        (label, json.dumps(data, ensure_ascii=False), datetime.now(TAIWAN_TZ).isoformat())
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════
#  Groq API 呼叫
# ══════════════════════════════════════════════════════
def _groq_json(system_prompt: str, user_text: str, temperature: float = 0.3) -> dict:
    """呼叫 Groq，回傳 JSON dict（含防呆清洗）"""
    resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        temperature=temperature,
    )
    raw = resp.choices[0].message.content.strip()
    # 清除可能夾帶的 markdown fence
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)


def call_groq_intent(user_text: str) -> dict:
    return _groq_json(INTENT_PROMPT, user_text, temperature=0.1)


def call_groq_parse(user_text: str) -> dict:
    return _groq_json(DIARY_PARSE_PROMPT, user_text, temperature=0.3)


def call_groq_self_review(diary_summary: str) -> dict:
    return _groq_json(SELF_REVIEW_PROMPT, diary_summary, temperature=0.4)


# ══════════════════════════════════════════════════════
#  日報格式化：V1（系統表單格式）
# ══════════════════════════════════════════════════════
def build_v1(data: dict, date_str: str) -> str:
    STATUS_ICON = {
        "完成": "✅", "跟進中": "🔄", "待確認": "⏳", "已上報": "📢"
    }

    lines = [
        "📋 集團綜合部－行政組｜工作日報",
        "══════════════════════════",
        f"日期：{date_str}　　職級：L2",
        f"填報人：Luna（資深行政專員）",
        "══════════════════════════",
        "",
        "① 今日執行結果 RESULTS",
        "──────────────────────",
    ]

    for item in data.get("today_results", []):
        icon = STATUS_ICON.get(item.get("status", "完成"), "✅")
        lines.append(f"{icon} [{item['tag']}]")
        lines.append(f"   {item['text']}")
        if item.get("status") and item["status"] != "完成":
            lines.append(f"   狀態：{item['status']}")
        lines.append("")

    lines += [
        "② 阻塞事項 BLOCKERS",
        "──────────────────────",
        data.get("blockers", "無"),
        "",
        "③ 異常上報 REVIEW",
        "──────────────────────",
        data.get("exceptions", "無"),
        "",
        "④ 明日計畫 NEXT STEPS",
        "──────────────────────",
    ]

    plan = data.get("tomorrow_plan", [])
    if plan:
        for i, p in enumerate(plan, 1):
            lines.append(f"{i}. {p}")
    else:
        lines.append("（待補充）")

    ai = data.get("ai_usage", "無")
    if ai and ai != "無":
        lines += ["", "⑤ AI 工具應用紀錄", "──────────────────────", ai]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
#  日報格式化：V2（簡中 Telegram 格式）
# ══════════════════════════════════════════════════════
def build_v2(data: dict, date_str: str) -> str:
    STATUS_ZH = {
        "完成": "✅", "跟進中": "🔄", "待確認": "⏳", "已上報": "📢"
    }

    results_lines = []
    for item in data.get("today_results", []):
        icon = STATUS_ZH.get(item.get("status", "完成"), "✅")
        text = cc.convert(item["text"])
        tag  = cc.convert(item["tag"])
        results_lines.append(f"{icon} [{tag}] {text}")
    results_text = "\n".join(results_lines) if results_lines else "（无）"

    blockers_cn  = cc.convert(data.get("blockers", "无"))
    exceptions_cn = cc.convert(data.get("exceptions", "无"))

    plan = data.get("tomorrow_plan", [])
    if plan:
        plan_text = "\n".join(f"{i}. {cc.convert(p)}" for i, p in enumerate(plan, 1))
    else:
        plan_text = "（待补充）"

    ai = data.get("ai_usage", "无")
    ai_section = ""
    if ai and ai != "無" and ai != "无":
        ai_cn = cc.convert(ai)
        ai_section = f"\n【AI工具应用】\n{ai_cn}"

    v2 = (
        f"# 日报\n"
        f"[REPORT-ORG:集团综合部-行政组] [LEVEL:L2] [TYPE:日报] [DATE:{date_str}]\n"
        f"提交人：Luna｜职位：资深行政专员｜层级：L2\n"
        f"{'─' * 28}\n"
        f"\n【今日结果】\n{results_text}\n"
        f"\n【无碍/阻碍】\n{blockers_cn}\n"
        f"\n【专项安排/异常上报】\n{exceptions_cn}\n"
        f"\n【明日动作】\n{plan_text}"
        f"{ai_section}"
    )
    return v2


# ══════════════════════════════════════════════════════
#  月曆圖生成（深紫色主題）
# ══════════════════════════════════════════════════════
LEAVE_COLOR_MAP = {
    "病假":   (180, 60,  60),
    "事假":   (180, 80,  80),
    "年假":   (200, 100, 140),
    "生理假": (220, 100, 160),
    "其他":   (120, 120, 130),
}

def generate_calendar_image(year: int, month: int) -> str:
    now = datetime.now(TAIWAN_TZ)
    today = now.strftime("%Y-%m-%d")

    special = get_special_days(year, month)

    # 計算本月工作日（排除週六日）
    cal = calendar.monthcalendar(year, month)
    all_workdays = []
    for week in cal:
        for i, day in enumerate(week):
            if day != 0 and i < 5:  # 週一~週五
                all_workdays.append(f"{year:04d}-{month:02d}-{day:02d}")

    # 已填日報的日期
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    prefix = f"{year:04d}-{month:02d}"
    c.execute("SELECT DISTINCT date FROM diaries WHERE date LIKE ?", (prefix + "%",))
    submitted_dates = {r[0] for r in c.fetchall()}
    conn.close()

    workday_count = len(all_workdays)
    submitted_workdays = [d for d in all_workdays if d in submitted_dates and d not in special]
    submitted_count = len(submitted_workdays)
    rate = submitted_count / workday_count * 100 if workday_count > 0 else 0

    # ── 繪圖設定 ──
    W, H = 700, 560
    BG         = (18,  16,  35)
    SURFACE    = (30,  25,  55)
    ACCENT     = (130, 100, 220)
    TODAY_RING = (255, 190, 50)
    TEXT_MAIN  = (235, 230, 255)
    TEXT_SUB   = (150, 140, 190)
    GREEN_DONE = (60,  160, 100)
    GREY_OFF   = (50,  45,  75)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    try:
        font_title  = ImageFont.truetype(FONT_PATH, 22)
        font_header = ImageFont.truetype(FONT_PATH, 16)
        font_day    = ImageFont.truetype(FONT_PATH, 18)
        font_small  = ImageFont.truetype(FONT_PATH, 13)
        font_stat   = ImageFont.truetype(FONT_PATH, 15)
    except Exception:
        font_title  = ImageFont.load_default()
        font_header = font_day = font_small = font_stat = font_title

    # 標題
    title = f"{year}年{month}月  日報進度"
    draw.text((W // 2, 28), title, font=font_title, fill=TEXT_MAIN, anchor="mm")

    # 統計列
    stat_text = f"已填 {submitted_count} 天 ／ 工作日 {workday_count} 天 ／ 完成率 {rate:.0f}%"
    draw.text((W // 2, 56), stat_text, font=font_stat, fill=TEXT_SUB, anchor="mm")

    # 進度條
    bar_x, bar_y, bar_w, bar_h = 60, 72, W - 120, 10
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=5, fill=GREY_OFF)
    fill_w = int(bar_w * rate / 100)
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], radius=5, fill=ACCENT)

    # 星期標題
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    col_w = (W - 80) // 7
    for i, wd in enumerate(weekdays):
        cx = 40 + i * col_w + col_w // 2
        color = TEXT_SUB if i < 5 else (180, 80, 80)
        draw.text((cx, 100), wd, font=font_header, fill=color, anchor="mm")

    # 分隔線
    draw.line([(40, 114), (W - 40, 114)], fill=SURFACE, width=1)

    # 日期格子
    row_y = 130
    for week in cal:
        for i, day in enumerate(week):
            if day == 0:
                continue
            date_str_cell = f"{year:04d}-{month:02d}-{day:02d}"
            cx = 40 + i * col_w + col_w // 2
            cy = row_y + 24

            is_weekend   = (i >= 5)
            is_today     = (date_str_cell == today)
            is_submitted = (date_str_cell in submitted_dates)
            is_special   = (date_str_cell in special)
            leave_type   = special.get(date_str_cell, "")

            # 背景圓
            r = 20
            if is_special:
                circle_color = LEAVE_COLOR_MAP.get(leave_type, LEAVE_COLOR_MAP["其他"])
                draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=circle_color)
            elif is_submitted and not is_weekend:
                draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=GREEN_DONE)
            elif is_weekend:
                draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=GREY_OFF)

            # 今日外框
            if is_today:
                draw.ellipse([cx-r-3, cy-r-3, cx+r+3, cy+r+3], outline=TODAY_RING, width=2)

            # 日期數字
            if is_weekend:
                num_color = (120, 110, 150)
            elif is_special or is_submitted:
                num_color = (240, 240, 240)
            else:
                num_color = TEXT_MAIN
            draw.text((cx, cy), str(day), font=font_day, fill=num_color, anchor="mm")

            # 假別小字
            if is_special and leave_type:
                draw.text((cx, cy + r + 8), leave_type, font=font_small, fill=TEXT_SUB, anchor="mm")

        row_y += 68

    # 圖例
    legend_items = [
        (GREEN_DONE, "已填日報"),
        (LEAVE_COLOR_MAP["病假"], "病假"),
        (LEAVE_COLOR_MAP["年假"], "年/特休"),
        (LEAVE_COLOR_MAP["生理假"], "生理假"),
        (LEAVE_COLOR_MAP["其他"], "其他假"),
        (GREY_OFF, "例假日"),
    ]
    lx, ly = 40, H - 36
    for color, label in legend_items:
        draw.ellipse([lx, ly + 2, lx + 12, ly + 14], fill=color)
        draw.text((lx + 16, ly + 8), label, font=font_small, fill=TEXT_SUB, anchor="lm")
        lx += 90

    img.save(CALENDAR_IMG_PATH)
    return CALENDAR_IMG_PATH


# ══════════════════════════════════════════════════════
#  防呆：請假意圖硬規則檢查
# ══════════════════════════════════════════════════════
def _pass_leave_guard(text: str, dates: list) -> tuple[bool, str]:
    """回傳 (通過, 拒絕原因)"""
    if len(text.strip()) < MIN_LEAVE_TEXT_LEN:
        return False, "輸入太短，我不確定你的意思喔，請說清楚一點（例如：「明天病假」、「7/10 請年假」）。"

    has_leave_kw = any(kw in text for kw in LEAVE_KEYWORDS)
    if not has_leave_kw:
        return False, None  # 不像請假指令，交由外層重新判斷

    has_digit = bool(re.search(r"\d", text))
    has_relative = any(kw in text for kw in RELATIVE_DATE_KEYWORDS)
    if not has_digit and not has_relative:
        return False, "看不到具體日期喔～請補充日期（例如：「7/10 病假」或「明天請假」）。"

    if len(dates) > MAX_LEAVE_DATES:
        return False, f"偵測到超過 {MAX_LEAVE_DATES} 個日期，怕誤判，請一次只標記幾天喔。"

    return True, None


# ══════════════════════════════════════════════════════
#  月曆發送
# ══════════════════════════════════════════════════════
async def send_calendar(update: Update, year: int, month: int):
    try:
        img_path = generate_calendar_image(year, month)
        with open(img_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=f"📅 {year}年{month}月 日報進度")
    except Exception as e:
        logger.exception("月曆圖生成失敗")
        await update.message.reply_text(f"⚠️ 月曆產生失敗：{e}")


# ══════════════════════════════════════════════════════
#  Google Drive 備份
# ══════════════════════════════════════════════════════
def backup_to_drive():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("SELECT * FROM diaries ORDER BY date DESC LIMIT 100")
        diaries = [dict(r) for r in c.fetchall()]
        c.execute("SELECT * FROM special_days ORDER BY date")
        specials = [dict(r) for r in c.fetchall()]
        c.execute("SELECT * FROM self_reviews ORDER BY created_at DESC LIMIT 10")
        reviews = [dict(r) for r in c.fetchall()]
        conn.close()

        payload = {
            "action": "backup",
            "timestamp": datetime.now(TAIWAN_TZ).isoformat(),
            "diaries": diaries,
            "special_days": specials,
            "self_reviews": reviews,
        }
        session = requests.Session()
        resp = session.post(GAS_BACKUP_URL, json=payload, timeout=30)
        logger.info(f"備份回應：{resp.status_code} {resp.text[:100]}")
    except Exception as e:
        logger.exception(f"備份失敗：{e}")


# ══════════════════════════════════════════════════════
#  排程：每日提醒 & 週日備份
# ══════════════════════════════════════════════════════
async def daily_reminder(context):
    now = datetime.now(TAIWAN_TZ)
    if now.weekday() >= 5:
        return
    today = now.strftime("%Y-%m-%d")
    existing = get_latest_diary_for_date(today)
    if existing:
        return
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            f"📋 Luna，今天（{today}）的日報還沒填喔！\n\n"
            "直接傳一段今天做了什麼給我，我幫你整理好 🙌"
        )
    )


async def weekly_backup(context):
    now = datetime.now(TAIWAN_TZ)
    if now.weekday() != 6:
        return
    backup_to_drive()
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="☁️ 週日自動備份完成，資料已同步至 Google Drive。"
    )


# ══════════════════════════════════════════════════════
#  主訊息處理
# ══════════════════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    now = datetime.now(TAIWAN_TZ)
    today_str = now.strftime("%Y-%m-%d")

    # 白名單：只服務指定使用者
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        await update.message.reply_text("這個 Bot 只服務特定使用者喔 🙅")
        return

    # ── 指令：月曆 ──────────────────────────────────
    if user_text in ("月曆", "日曆", "月历", "日历"):
        await send_calendar(update, now.year, now.month)
        return

    # ── 指令：請假記錄 ──────────────────────────────
    leave_record_match = re.match(r"^請假記錄\s*(\d{1,2}月?)?$", user_text)
    if leave_record_match:
        month_hint = leave_record_match.group(1)
        if month_hint:
            m = int(re.search(r"\d+", month_hint).group())
            prefix = f"{now.year:04d}-{m:02d}"
        else:
            prefix = None

        rows = get_all_special_days(prefix)
        if not rows:
            await update.message.reply_text("目前沒有請假記錄喔。")
        else:
            lines = ["📅 請假記錄：\n"]
            for d, t in rows:
                lines.append(f"• {d}　{t}")
            await update.message.reply_text("\n".join(lines))
        return

    # ── 指令：查看特定日期日報 ─────────────────────
    date_lookup = re.search(r"查看\s*(\d{1,2})[/／](\d{1,2})\s*日?報?", user_text)
    if date_lookup:
        m, d = int(date_lookup.group(1)), int(date_lookup.group(2))
        try:
            target = datetime(now.year, m, d).strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("日期格式無法識別，請用「查看 7/15 日報」這種格式。")
            return
        existing = get_latest_diary_for_date(target)
        if not existing:
            await update.message.reply_text(f"{target} 沒有日報記錄。")
            return
        _, _, data = existing
        await update.message.reply_text(build_v1(data, target))
        await update.message.reply_text(build_v2(data, target))
        return

    # ── 指令：員工自評 ──────────────────────────────
    if user_text.startswith("員工自評") or user_text.startswith("员工自评"):
        date_range = re.findall(r"(\d{4}-\d{2}-\d{2})", user_text)
        if len(date_range) >= 2:
            start_str, end_str = date_range[0], date_range[1]
        else:
            start_str = now.strftime("%Y-%m-01")
            end_str = today_str

        await update.message.reply_text("彙整日報資料中，請稍候…")

        entries = get_diaries_in_range(start_str, end_str)
        if not entries:
            await update.message.reply_text(f"找不到 {start_str} ~ {end_str} 的日報資料。")
            return

        # 計算工作日完成率
        import calendar as cal_mod
        s = datetime.strptime(start_str, "%Y-%m-%d")
        e = datetime.strptime(end_str, "%Y-%m-%d")
        total_workdays = sum(
            1 for i in range((e - s).days + 1)
            if (s + timedelta(days=i)).weekday() < 5
        )
        diary_dates = {d for d, _ in entries}

        # 組合日報摘要
        summary_lines = [
            f"日報彙整期間：{start_str} ~ {end_str}",
            f"已填工作日：{len(diary_dates)} / {total_workdays}",
            "",
            "── 各日工作摘要 ──",
        ]
        for date_str, data in entries:
            results = data.get("today_results", [])
            texts = [f"[{r['tag']}] {r['text']}" for r in results]
            summary_lines.append(f"\n{date_str}：")
            summary_lines.extend(texts)
            ai = data.get("ai_usage", "")
            if ai and ai != "無":
                summary_lines.append(f"  AI應用：{ai}")

        summary = "\n".join(summary_lines)

        try:
            review = call_groq_self_review(summary)
            review["_workday_total"] = total_workdays
            review["_diary_count"] = len(diary_dates)
            save_self_review(f"{start_str}~{end_str}", review)

            d_scores = review.get("dimension_scores", {})
            ai_score = review.get("ai_score", {})
            highlights = review.get("highlights", [])
            improvement = review.get("improvement", "")
            overall = review.get("overall_summary", "")

            msg = [
                "🏆 員工自評報告",
                "══════════════════════════",
                f"評估期間：{review.get('period', f'{start_str} ~ {end_str}')}",
                f"日報完成率：{len(diary_dates)}/{total_workdays} 個工作日",
                "",
                "📊 三維評分",
                "──────────────────────",
            ]
            for dim, val in d_scores.items():
                msg.append(f"• {dim}：{val.get('score')} 分")
                msg.append(f"  {val.get('comment', '')}")
            msg += [
                "",
                f"🤖 AI應用：{ai_score.get('score')} 分",
                f"  {ai_score.get('comment', '')}",
                "",
                "⭐ 本期亮點",
                "──────────────────────",
            ]
            for h in highlights:
                msg.append(f"• {h}")
            msg += [
                "",
                "💡 改善建議",
                "──────────────────────",
                improvement,
                "",
                "📝 整體自評摘要（可直接貼上系統）",
                "──────────────────────",
                overall,
            ]
            await update.message.reply_text("\n".join(msg))
        except Exception as e:
            logger.exception("員工自評生成失敗")
            await update.message.reply_text(f"自評生成失敗：{e}")
        return

    # ── 指令：補充 ──────────────────────────────────
    if user_text.startswith("補充：") or user_text.startswith("补充："):
        supplement = user_text[3:].strip()
        existing = get_latest_diary_for_date(today_str)
        if not existing:
            await update.message.reply_text("今天還沒有日報，請先傳今天的工作描述。")
            return
        diary_id, raw_old, data_old = existing
        new_raw = raw_old + "\n補充：" + supplement
        try:
            await update.message.reply_text("處理補充內容中…")
            new_data = call_groq_parse(new_raw)
            update_diary(diary_id, new_raw, new_data)
            await update.message.reply_text("✅ 補充完成！更新後的日報：")
            await update.message.reply_text(build_v1(new_data, today_str))
            await update.message.reply_text(build_v2(new_data, today_str))
            await send_calendar(update, now.year, now.month)
        except Exception as e:
            logger.exception("補充處理失敗")
            await update.message.reply_text(f"補充處理失敗：{e}")
        return

    # ── 意圖判斷：請假 or 日報 ──────────────────────
    await update.message.reply_text("收到，整理中…")
    try:
        intent_data = call_groq_intent(user_text)
        intent     = intent_data.get("intent", "diary")
        dates      = intent_data.get("dates", [])
        leave_type = intent_data.get("leave_type", "其他")
        confidence = intent_data.get("confidence", "low")

        # 請假處理
        if intent == "leave" and confidence == "high":
            passed, reason = _pass_leave_guard(user_text, dates)
            if not passed:
                if reason:
                    await update.message.reply_text(reason)
                else:
                    # reason 為 None 表示根本不像請假，轉為日報處理
                    intent = "diary"
            else:
                for d in dates:
                    save_special_day(d, leave_type)
                dates_str = "、".join(dates)
                await update.message.reply_text(f"✅ 已標記：{dates_str} → {leave_type}")
                await send_calendar(update, now.year, now.month)
                return

        # 取消請假
        if intent == "cancel" and confidence == "high":
            passed, reason = _pass_leave_guard(user_text, dates)
            if not passed:
                if reason:
                    await update.message.reply_text(reason)
                    return
                intent = "diary"
            else:
                for d in dates:
                    delete_special_day(d)
                dates_str = "、".join(dates)
                await update.message.reply_text(f"✅ 已取消標記：{dates_str}")
                await send_calendar(update, now.year, now.month)
                return

        # 日報處理
        if intent == "diary":
            data = call_groq_parse(user_text)
            save_diary(today_str, user_text, data)
            await update.message.reply_text(build_v1(data, today_str))
            await update.message.reply_text(build_v2(data, today_str))
            await send_calendar(update, now.year, now.month)

    except Exception as e:
        logger.exception("訊息處理失敗")
        await update.message.reply_text(f"處理時發生錯誤，請再試一次。\n錯誤：{e}")


# ══════════════════════════════════════════════════════
#  /start 指令
# ══════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "嗨 Luna！我是鵝鵝日報小幫手 📋\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "【主要功能】\n"
        "• 直接傳工作描述 → 自動整理 V1+V2 日報 + 月曆圖\n"
        "• 傳「補充：XXX」→ 追加修正今日日報\n"
        "• 傳請假句子（含日期）→ Bot 自動辨識並標記\n"
        "• 傳取消句子（含日期）→ Bot 自動辨識並取消\n"
        "• 傳「月曆」→ 查看本月進度圖\n"
        "• 傳「請假記錄」→ 查看所有請假\n"
        "• 傳「請假記錄 7月」→ 查看特定月份\n"
        "• 傳「查看 7/15 日報」→ 查看特定日期日報\n"
        "• 傳「員工自評」→ 產出本月評分報告\n"
        "• 傳「員工自評 2026-07-01 2026-07-31」→ 自訂期間\n"
        "━━━━━━━━━━━━━━━━\n"
        "輸入太模糊的話，Bot 會反問，不會亂猜 🙅\n\n"
        "範例：\n"
        "「今天完成商務中心3支門號續租，整理7月採購清單，"
        "協調清潔廠商驗收，明天跟進燈具維修進度」"
    )


# ══════════════════════════════════════════════════════
#  主程式
# ══════════════════════════════════════════════════════
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # 指令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 排程：每日 14:00 台灣時間提醒（UTC 06:00）
    app.job_queue.run_daily(daily_reminder, time=datetime(2000, 1, 1, 6, 0, 0).time())

    # 排程：每週日 22:00 台灣時間備份（UTC 14:00）
    app.job_queue.run_daily(weekly_backup, time=datetime(2000, 1, 1, 14, 0, 0).time())

    logger.info("鵝鵝日報小幫手 v2.0 啟動中…")
    app.run_polling()


if __name__ == "__main__":
    main()
