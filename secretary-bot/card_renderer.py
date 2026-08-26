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


def _estimate_height(leaves, tasks, overdue, due_soon=None):
    h = 200
    h += 60
    h += max(len(leaves), 1) * LINE_H
    h += 70
    h += max(len(tasks), 1) * LINE_H
    if due_soon:
        h += 70 + len(due_soon) * LINE_H
    if overdue:
        h += 70 + len(overdue) * LINE_H
    h += PADDING
    return h


def render_today_card(date_str: str, weekday_zh: str, leaves: list, tasks: list, overdue: list, due_soon: list = None) -> Image.Image:
    due_soon = due_soon or []
    height = _estimate_height(leaves, tasks, overdue, due_soon)
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

    if due_soon:
        y += 15
        draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=(40, 46, 54), width=1)
        y += 25
        draw.rectangle([PADDING, y + 4, PADDING + 12, y + 16], fill=config.COLOR_YELLOW)
        draw.text((PADDING + 22, y), f"即將到期（{len(due_soon)}）", font=_font(20, bold=True), fill=config.COLOR_YELLOW)
        y += 40
        for t in due_soon:
            days_left = t.get("days_left")
            days_disp = f"，還剩{days_left}天" if isinstance(days_left, int) else ""
            _draw_checkbox(draw, PADDING + 10, y + 4, False, config.COLOR_YELLOW)
            line = f"#{t['id']}  {t['content']}　（{t['task_date']}{days_disp}）"
            draw.text((PADDING + 36, y), line, font=_font(19), fill=config.COLOR_YELLOW)
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


# ───────────────── 本月行事曆（方格版） ─────────────────

MONTH_GRID_WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]  # 對應 datetime.weekday()：0=一...6=日
GRID_HEADER_H = 40      # 星期幾標頭列高度
GRID_DAYNUM_H = 32      # 日期數字那一行的高度
GRID_LINE_H = 18        # 每一行待辦/請假內容的高度
GRID_MAX_LINES = 7      # 每格最多逐條列出幾行，超過就在最後一行顯示「+N 更多」
GRID_CELL_BOTTOM_PAD = 10
GRID_TOP = 110          # 標題+摘要區高度（跟舊版一致，維持整體風格）
GRID_BORDER = (40, 46, 54)


def _group_by_date(leaves: list, tasks: list):
    """把整月的leaves/tasks依日期分組，回傳 {date_str: {"leaves": [...], "tasks": [...]}}"""
    grouped = {}
    for lv in leaves:
        grouped.setdefault(lv["leave_date"], {"leaves": [], "tasks": []})["leaves"].append(lv)
    for t in tasks:
        grouped.setdefault(t["task_date"], {"leaves": [], "tasks": []})["tasks"].append(t)
    return grouped


def _truncate_to_width(draw, text, font, max_width):
    """文字太長塞不進格子時，裁到剛好塞得下並加上刪節號"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def _day_lines(data):
    """把一天的leaves/tasks整理成逐行要顯示的清單：[(文字, 文字顏色, 圓點顏色或None), ...]
    請假排最前面（比較重要，人不在要先看到），待辦依原本順序接在後面，
    已完成的待辦用淡色文字+勾勾表示，不再用圓點（跟今日卡片的已完成風格一致）。
    """
    lines = []
    for lv in data["leaves"]:
        text = f"{lv['person_name']}{lv.get('leave_type') or '請假'}"
        lines.append((text, config.COLOR_YELLOW, config.COLOR_YELLOW))
    for t in data["tasks"]:
        done = t["status"] == "done"
        if done:
            lines.append((f"✓{t['content']}", config.COLOR_TEXT_DIM, None))
        else:
            lines.append((t["content"], config.COLOR_TEXT, config.COLOR_MINT))
    return lines


def render_month_card(
    year: int, month: int, leaves: list, tasks: list, weekday_zh_map: dict, today_str: str = None
) -> Image.Image:
    """
    畫成真正的方格月曆（橫向排一～日，每天一格），取代舊版條列式清單，
    這樣一眼就能看出整個月哪幾天比較忙、誰哪幾天請假，不用一行一行往下找。

    leaves/tasks: 整月範圍查詢出來的原始清單（db.get_leaves_for_range / get_tasks_for_range）
    weekday_zh_map: {date_str: "一"}，同時也用來取得這個月完整的日期清單（1號到月底每一天都要有）
    today_str: 今天的日期（YYYY-MM-DD），有帶的話今天那一格會用主色框起來；不是查當月就不用帶
    """
    grouped = _group_by_date(leaves, tasks)
    all_dates = sorted(weekday_zh_map.keys())

    total_tasks = len(tasks)
    total_leaves = len(leaves)

    if not all_dates:
        img = Image.new("RGB", (WIDTH, GRID_TOP + 60), config.COLOR_BG)
        draw = ImageDraw.Draw(img)
        draw.text((PADDING, PADDING), f"{year}年{month}月 行事曆", font=_font(28, bold=True), fill=config.COLOR_MINT)
        return img

    first_col = MONTH_GRID_WEEKDAYS_ZH.index(weekday_zh_map[all_dates[0]])
    n_days = len(all_dates)
    n_rows = -(-(first_col + n_days) // 7)  # 無條件進位

    # 格子高度不是固定的：算出這個月「最忙的一天」需要幾行才裝得下待辦/請假內容
    # （超過GRID_MAX_LINES行的天，只會顯示到上限、最後一行改顯示「+N 更多」，見下方畫格子的迴圈），
    # 再用這個最大行數統一算出每一格的高度，這樣資料少的月份格子還是維持精簡，
    # 資料多的月份格子會自動長高，把每項待辦/請假的完整內容都列出來，不會只看到數字。
    max_lines_needed = 0
    for date_str in all_dates:
        data = grouped.get(date_str, {"leaves": [], "tasks": []})
        n_lines = len(_day_lines(data))
        max_lines_needed = max(max_lines_needed, min(n_lines, GRID_MAX_LINES))
    cell_h = GRID_DAYNUM_H + max_lines_needed * GRID_LINE_H + GRID_CELL_BOTTOM_PAD

    height = GRID_TOP + GRID_HEADER_H + n_rows * cell_h + PADDING
    img = Image.new("RGB", (WIDTH, height), config.COLOR_BG)
    draw = ImageDraw.Draw(img)

    y = PADDING
    draw.text((PADDING, y), f"{year}年{month}月 行事曆", font=_font(28, bold=True), fill=config.COLOR_MINT)
    summary = f"共 {total_tasks} 項待辦、{total_leaves} 筆請假"
    draw.text((WIDTH - PADDING - draw.textlength(summary, font=_font(16)), y + 8),
              summary, font=_font(16), fill=config.COLOR_TEXT_DIM)
    y += 46
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=config.COLOR_MINT_DIM, width=2)
    y += 30

    grid_w = WIDTH - 2 * PADDING
    cell_w = grid_w // 7

    # 星期幾標頭列
    for col, wd in enumerate(MONTH_GRID_WEEKDAYS_ZH):
        cx = PADDING + col * cell_w
        color = config.COLOR_TEXT_DIM if wd in ("六", "日") else config.COLOR_TEXT
        draw.text((cx + cell_w / 2 - draw.textlength(wd, font=_font(15, bold=True)) / 2, y),
                  wd, font=_font(15, bold=True), fill=color)
    y += GRID_HEADER_H
    grid_top_y = y

    # 逐日畫格子
    line_font = _font(12)
    for i, date_str in enumerate(all_dates):
        col = (first_col + i) % 7
        row = (first_col + i) // 7
        cx = PADDING + col * cell_w
        cy = grid_top_y + row * cell_h

        is_today = today_str is not None and date_str == today_str
        draw.rectangle([cx, cy, cx + cell_w, cy + cell_h], outline=GRID_BORDER, width=1)
        if is_today:
            draw.rectangle([cx + 1, cy + 1, cx + cell_w - 1, cy + cell_h - 1], outline=config.COLOR_MINT, width=2)

        day_num = str(int(date_str[-2:]))
        day_color = config.COLOR_MINT if is_today else (
            config.COLOR_TEXT_DIM if weekday_zh_map[date_str] in ("六", "日") else config.COLOR_TEXT
        )
        draw.text((cx + 10, cy + 8), day_num, font=_font(18, bold=True), fill=day_color)

        data = grouped.get(date_str, {"leaves": [], "tasks": []})
        day_lines = _day_lines(data)
        inner_y = cy + GRID_DAYNUM_H
        inner_max_w = cell_w - 18

        if len(day_lines) > GRID_MAX_LINES:
            visible = day_lines[:GRID_MAX_LINES - 1]
            overflow = len(day_lines) - (GRID_MAX_LINES - 1)
        else:
            visible = day_lines
            overflow = 0

        for text, color, dot_color in visible:
            if dot_color:
                _draw_dot(draw, cx + 9, inner_y + 7, dot_color)
                text_x = cx + 18
            else:
                text_x = cx + 8
            draw.text((text_x, inner_y), _truncate_to_width(draw, text, line_font, inner_max_w),
                      font=line_font, fill=color)
            inner_y += GRID_LINE_H

        if overflow > 0:
            draw.text((cx + 8, inner_y), f"+{overflow} 更多", font=line_font, fill=config.COLOR_TEXT_DIM)

    return img
