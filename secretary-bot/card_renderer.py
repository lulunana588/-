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
            _draw_dot(draw, PADDING + 12, y + 10, config.COLOR_TEXT_DIM)
            draw.text((PADDING + 24, y), f"{lv['person_name']} 請假{note}", font=_font(19), fill=config.COLOR_TEXT)
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
            _draw_checkbox(draw, PADDING + 10, y + 4, False, config.COLOR_ORANGE)
            line = f"#{t['id']}  {t['content']}　（原訂 {t['task_date']}）"
            draw.text((PADDING + 36, y), line, font=_font(19), fill=config.COLOR_ORANGE)
            y += LINE_H

    return img


def save_card(img: Image.Image, path: str):
    img.save(path)
