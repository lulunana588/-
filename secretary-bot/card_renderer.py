"""
產生「今日行事曆」PNG卡片
版面順序：標題 → 請假同仁 → 今日待辦 → 逾期事項
不使用emoji字元（避免字型缺emoji時顯示方框亂碼），改用手繪圖形。
"""
from PIL import Image, ImageDraw, ImageFont
import config

WIDTH = 900
PADDING = 40
LINE_H = 44
CHECKBOX_SIZE = 16


def _font(size, bold=False):
    path = config.FONT_BOLD if bold else config.FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _draw_checkbox(draw, x, y, checked: bool, color):
    box = [x, y, x + CHECKBOX_SIZE, y + CHECKBOX_SIZE]
    draw.rectangle(box, outline=color, width=2)
    if checked:
        draw.line([(x + 3, y + 8), (x + 6, y + 12), (x + 13, y + 3)], fill=color, width=2)


def _draw_dot(draw, x, y, color):
    r = 3
    draw.ellipse([x, y, x + r * 2, y + r * 2], fill=color)


def _draw_warn_triangle(draw, x, y, size, color):
    draw.polygon(
        [(x + size / 2, y), (x, y + size), (x + size, y + size)],
        outline=color, width=2,
    )
    draw.line([(x + size / 2, y + size * 0.35), (x + size / 2, y + size * 0.65)], fill=color, width=2)
    draw.ellipse([x + size / 2 - 1.5, y + size * 0.75, x + size / 2 + 1.5, y + size * 0.75 + 3], fill=color)


def _estimate_height(leaves, tasks, overdue):
    h = 200
    h += 60
    h += max(len(leaves), 1) * LINE_H
    h += 70
    h += max(len(tasks), 1) * LINE_H
    if overdue:
        h += 70 + len(overdue) * LINE_H
    h += PADDING
    return h


def render_today_card(date_str: str, weekday_zh: str, leaves: list, tasks: list, overdue: list) -> Image.Image:
    height = _estimate_height(leaves, tasks, overdue)
    img = Image.new("RGB", (WIDTH, height), config.COLOR_BG)
    draw = ImageDraw.Draw(img)

    y = PADDING

    draw.text((PADDING, y), "今日行事曆", font=_font(30, bold=True), fill=config.COLOR_MINT)
    date_disp = f"{date_str}（{weekday_zh}）"
    draw.text((WIDTH - PADDING - draw.textlength(date_disp, font=_font(20)), y + 6),
              date_disp, font=_font(20), fill=config.COLOR_TEXT_DIM)
    y += 50
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=config.COLOR_MINT_DIM, width=2)
    y += 30

    draw.rectangle([PADDING, y + 4, PADDING + 12, y + 16], fill=config.COLOR_YELLOW)
    draw.text((PADDING + 22, y), "今日請假", font=_font(20, bold=True), fill=config.COLOR_YELLOW)
    y += 40
    if leaves:
        for lv in leaves:
            note = f"（{lv['note']}）" if lv.get("note") else ""
            leave_type = lv.get("leave_type") or "請假"
            _draw_dot(draw, PADDING + 12, y + 10, config.COLOR_TEXT_DIM)
            draw.text((PADDING + 24, y), f"{lv['person_name']} {leave_type}{note}", font=_font(19), fill=config.COLOR_TEXT)
            y += LINE_H
    else:
        _draw_dot(draw, PADDING + 12, y + 10, config.COLOR_TEXT_DIM)
        draw.text((PADDING + 24, y), "今日無人請假", font=_font(19), fill=config.COLOR_TEXT_DIM)
        y += LINE_H
    y += 15
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=(40, 46, 54), width=1)
    y += 25

    draw.rectangle([PADDING, y + 4, PADDING + 12, y + 16], fill=config.COLOR_MINT)
    draw.text((PADDING + 22, y), f"今日待辦（{len(tasks)}）", font=_font(20, bold=True), fill=config.COLOR_MINT)
    y += 40
    if tasks:
        for t in tasks:
            done = t["status"] == "done"
            color = config.COLOR_TEXT_DIM if done else config.COLOR_TEXT
            _draw_checkbox(draw, PADDING + 10, y + 4, done, color)
            draw.text((PADDING + 36, y), f"#{t['id']}  {t['content']}", font=_font(19), fill=color)
            y += LINE_H
    else:
        draw.text((PADDING + 10, y), "目前沒有待辦事項", font=_font(19), fill=config.COLOR_TEXT_DIM)
        y += LINE_H

    if overdue:
        y += 15
        draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=(40, 46, 54), width=1)
        y += 25
        _draw_warn_triangle(draw, PADDING, y + 2, 16, config.COLOR_RED)
        draw.text((PADDING + 26, y), f"逾期未完成（{len(overdue)}）", font=_font(20, bold=True), fill=config.COLOR_RED)
        y += 40
        for t in overdue:
            days = t.get("overdue_days")
            urgent = isinstance(days, int) and days >= 3
            color = config.COLOR_RED if urgent else config.COLOR_ORANGE
            _draw_checkbox(draw, PADDING + 10, y + 4, False, color)
            day_disp = f"，已逾期{days}天" if isinstance(days, int) else ""
            line = f"#{t['id']}  {t['content']}　（原訂 {t['task_date']}{day_disp}）"
            draw.text((PADDING + 36, y), line, font=_font(19, bold=urgent), fill=color)
            y += LINE_H

    return img


def save_card(img: Image.Image, path: str):
    img.save(path)


# ───────────────── 本週行事曆 ─────────────────

WEEK_LINE_H = 30


def _estimate_week_height(days: list):
    h = 90  # 標題區
    for day in days:
        h += 46  # 日期列標頭
        n_leaves = len(day["leaves"])
        n_tasks = len(day["tasks"])
        h += max(n_leaves, 1) * WEEK_LINE_H if n_leaves else WEEK_LINE_H
        h += max(n_tasks, 1) * WEEK_LINE_H if n_tasks else WEEK_LINE_H
        h += 18  # 日期區塊間距
    h += PADDING
    return h


def render_week_card(week_start: str, week_end: str, days: list) -> Image.Image:
    """
    days: 依序 7 筆，每筆為
      {"date": "2026-08-11", "weekday_zh": "一", "leaves": [...], "tasks": [...]}
    """
    height = _estimate_week_height(days)
    img = Image.new("RGB", (WIDTH, height), config.COLOR_BG)
    draw = ImageDraw.Draw(img)

    y = PADDING
    draw.text((PADDING, y), "本週行事曆", font=_font(28, bold=True), fill=config.COLOR_MINT)
    range_disp = f"{week_start} ～ {week_end}"
    draw.text((WIDTH - PADDING - draw.textlength(range_disp, font=_font(18)), y + 6),
              range_disp, font=_font(18), fill=config.COLOR_TEXT_DIM)
    y += 46
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=config.COLOR_MINT_DIM, width=2)
    y += 24

    for day in days:
        date_disp = f"{day['date']}（{day['weekday_zh']}）"
        draw.text((PADDING, y), date_disp, font=_font(20, bold=True), fill=config.COLOR_TEXT)
        y += 34

        if day["leaves"]:
            names = "、".join(
                f"{lv['person_name']}{lv.get('leave_type') or '請假'}" + (f"({lv['note']})" if lv.get("note") else "")
                for lv in day["leaves"]
            )
            draw.text((PADDING + 14, y), names, font=_font(17), fill=config.COLOR_YELLOW)
            y += WEEK_LINE_H
        else:
            draw.text((PADDING + 14, y), "請假：無", font=_font(17), fill=config.COLOR_TEXT_DIM)
            y += WEEK_LINE_H

        if day["tasks"]:
            for t in day["tasks"]:
                done = t["status"] == "done"
                color = config.COLOR_TEXT_DIM if done else config.COLOR_TEXT
                _draw_checkbox(draw, PADDING + 14, y + 2, done, color)
                draw.text((PADDING + 38, y), f"#{t['id']} {t['content']}", font=_font(17), fill=color)
                y += WEEK_LINE_H
        else:
            draw.text((PADDING + 14, y), "待辦：無", font=_font(17), fill=config.COLOR_TEXT_DIM)
            y += WEEK_LINE_H

        y += 12
        draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=(40, 46, 54), width=1)
        y += 12

    return img


# ───────────────── 本月行事曆 ─────────────────

MONTH_LINE_H = 30


def _group_by_date(leaves: list, tasks: list):
    """把整月的leaves/tasks依日期分組，回傳 {date_str: {"leaves": [...], "tasks": [...]}}"""
    grouped = {}
    for lv in leaves:
        grouped.setdefault(lv["leave_date"], {"leaves": [], "tasks": []})["leaves"].append(lv)
    for t in tasks:
        grouped.setdefault(t["task_date"], {"leaves": [], "tasks": []})["tasks"].append(t)
    return grouped


def _estimate_month_height(active_days: list):
    h = 110  # 標題+摘要區
    if not active_days:
        return h + 60
    for _, data in active_days:
        h += 40  # 日期列標頭
        n_leaves = len(data["leaves"])
        n_tasks = len(data["tasks"])
        if n_leaves:
            h += MONTH_LINE_H
        if n_tasks:
            h += n_tasks * MONTH_LINE_H
        h += 16
    h += PADDING
    return h


def render_month_card(year: int, month: int, leaves: list, tasks: list, weekday_zh_map: dict) -> Image.Image:
    """
    leaves/tasks: 整月範圍查詢出來的原始清單（db.get_leaves_for_range / get_tasks_for_range）
    weekday_zh_map: {date_str: "一"} 用來標示星期幾
    只畫出「有請假或有待辦」的日子，空白日子不畫，避免圖片過長。
    """
    grouped = _group_by_date(leaves, tasks)
    active_days = sorted(grouped.items())  # [(date_str, {"leaves":[], "tasks":[]}), ...]

    height = _estimate_month_height(active_days)
    img = Image.new("RGB", (WIDTH, height), config.COLOR_BG)
    draw = ImageDraw.Draw(img)

    y = PADDING
    draw.text((PADDING, y), f"{year}年{month}月 行事曆", font=_font(28, bold=True), fill=config.COLOR_MINT)
    total_tasks = len(tasks)
    total_leaves = len(leaves)
    summary = f"共 {total_tasks} 項待辦、{total_leaves} 筆請假"
    draw.text((WIDTH - PADDING - draw.textlength(summary, font=_font(16)), y + 8),
              summary, font=_font(16), fill=config.COLOR_TEXT_DIM)
    y += 46
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=config.COLOR_MINT_DIM, width=2)
    y += 30

    if not active_days:
        draw.text((PADDING, y), "這個月目前沒有任何待辦或請假紀錄", font=_font(19), fill=config.COLOR_TEXT_DIM)
        return img

    for date_str, data in active_days:
        weekday_zh = weekday_zh_map.get(date_str, "")
        date_disp = f"{date_str}（{weekday_zh}）" if weekday_zh else date_str
        draw.text((PADDING, y), date_disp, font=_font(19, bold=True), fill=config.COLOR_TEXT)
        y += 32

        if data["leaves"]:
            names = "、".join(
                f"{lv['person_name']}{lv.get('leave_type') or '請假'}" + (f"({lv['note']})" if lv.get("note") else "")
                for lv in data["leaves"]
            )
            draw.text((PADDING + 14, y), names, font=_font(16), fill=config.COLOR_YELLOW)
            y += MONTH_LINE_H

        for t in data["tasks"]:
            done = t["status"] == "done"
            color = config.COLOR_TEXT_DIM if done else config.COLOR_TEXT
            _draw_checkbox(draw, PADDING + 14, y, done, color)
            draw.text((PADDING + 38, y - 2), f"#{t['id']} {t['content']}", font=_font(16), fill=color)
            y += MONTH_LINE_H

        y += 10
        draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=(40, 46, 54), width=1)
        y += 14

    return img
