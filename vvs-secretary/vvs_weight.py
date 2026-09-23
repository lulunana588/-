"""VVS小秘書｜體重記錄模組

handle(uid, text) → LINE 訊息列表；不是體重指令時回傳 None，
讓主程式交給其他模組（之後的 AI、天氣、匯率…）處理。
"""
import os
import re
import time
import uuid
from datetime import datetime, timedelta

import vvs_config as cfg
import vvs_db as db
import vvs_line as line

W_MIN, W_MAX = 30, 250
BACKDATE_HOUR = 8
QUICK = ['最近', '本週', '本月', '趨勢', '撤銷', '說明']

RE_RECORD = re.compile(
    r'^(?:(\d{1,2})[/-](\d{1,2})\s+)?(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|公斤)?(?:\s+(.*))?$', re.I)
RE_HEIGHT = re.compile(r'^身高\s*(\d{2,3}(?:\.\d)?)\s*(?:cm|公分)?$', re.I)
RE_TARGET = re.compile(r'^目標\s*(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|公斤)?$', re.I)

HELP = """🤖 VVS小秘書｜體重記錄使用規則

━━━━━━━━━━━━━━
🟢 第一次使用（只要做一次）
━━━━━━━━━━━━━━
先告訴小秘書你的身高和目標：
　身高 165
　目標 60
（換成自己的數字，之後想改隨時再傳一次）

👥 兩個人的資料完全分開，各自只看得到自己的紀錄。

━━━━━━━━━━━━━━
📝 每天記錄
━━━━━━━━━━━━━━
① 直接傳體重數字
　84.5

② 想加註記，數字後面空一格
　84.5 起床後
　84.5 睡前

③ 忘記記錄可以補登（月/日 空一格 體重）
　9/20 85.2

④ 打錯了？傳「撤銷」
　會刪掉你最後新增的那一筆

小秘書會自動算好：
BMI、跟上一次比增減多少、7筆平均、距離目標還差多少

━━━━━━━━━━━━━━
📊 查詢（下方按鈕也能直接點）
━━━━━━━━━━━━━━
最近　→ 最近 7 筆紀錄
本週　→ 這週摘要
本月　→ 這個月摘要
趨勢　→ 最近 30 筆的折線圖
說明　→ 忘記指令時看這裡

━━━━━━━━━━━━━━
⚖️ 量體重小規則
━━━━━━━━━━━━━━
・固定時間量：起床、上完廁所、吃早餐前
・用同一台體重計，穿著盡量一樣
・一天記一筆就夠了
・一天內差個 0.5～1 公斤很正常
　看「7筆平均」和「趨勢」比看單日數字準

━━━━━━━━━━━━━━
⏰ 每日提醒
━━━━━━━━━━━━━━
早上 9 點還沒記錄的人，小秘書會提醒一次
已經記錄的人不會被打擾

━━━━━━━━━━━━━━
❗ 小提醒
━━━━━━━━━━━━━━
・體重請輸入 30～250 之間的數字
・數字和註記、日期和數字之間都要空一格
・傳其他文字小秘書看不懂，會提示你看說明"""


# ================= 入口 =================

def handle(uid, raw):
    t = re.sub(r'\s+', ' ', (raw or '').strip())
    low = t.lower()
    if low in ('說明', '規則', '使用規則', 'help', '?', '？'):
        return [line.text(HELP)]
    if t in ('最近', '紀錄', '記錄'):
        return [recent(uid)]
    if t == '本週':
        return [period(uid, 'week')]
    if t == '本月':
        return [period(uid, 'month')]
    if t in ('趨勢', '圖表'):
        return trend(uid)
    if low in ('撤銷', '刪除', 'undo'):
        return [undo(uid)]
    m = RE_HEIGHT.match(t)
    if m:
        return [set_height(uid, float(m.group(1)))]
    m = RE_TARGET.match(t)
    if m:
        return [set_target(uid, float(m.group(1)))]
    m = RE_RECORD.match(t)
    if m:
        return [record(uid, m)]
    return None


def help_msg():
    return line.text(HELP)


# ================= 計算 =================

def settings(uid):
    h = float(db.get_setting(uid, 'height_cm', cfg.DEFAULT_HEIGHT_CM))
    g = float(db.get_setting(uid, 'target_kg', cfg.DEFAULT_TARGET_KG))
    return h, g


def compute_rows(rows, height_cm, target_kg):
    """rows 需已依時間排序；回傳附帶 BMI、差異、增減%、7筆平均、距離目標的列表"""
    h2 = (height_cm / 100) ** 2
    out = []
    for i, r in enumerate(rows):
        w = r['weight']
        prev = rows[i - 1]['weight'] if i else None
        avg = round(sum(x['weight'] for x in rows[i - 6:i + 1]) / 7, 1) if i >= 6 else None
        out.append({
            **r,
            'when': datetime.fromisoformat(r['ts']),
            'bmi': round(w / h2, 1),
            'diff': None if prev is None else round(w - prev, 1),
            'pct': None if prev is None else (w - prev) / prev * 100,
            'avg': avg,
            'dist': round(w - target_kg, 1),
        })
    return out


def compute(uid):
    h, g = settings(uid)
    return compute_rows(db.list_weights(uid), h, g)


# ================= 指令 =================

def record(uid, m):
    w = float(m.group(3))
    if not (W_MIN <= w <= W_MAX):
        return line.text(f'「{m.group(3)}」看起來不太對，請輸入 {W_MIN}–{W_MAX} 之間的體重')
    note = (m.group(4) or '')[:30]
    now = now_tpe()
    when, backdated = now, bool(m.group(1))

    if backdated:
        mon, day = int(m.group(1)), int(m.group(2))
        when = make_date(now.year, mon, day)
        if when and when > now:  # 未來日期視為去年
            when = make_date(now.year - 1, mon, day)
        if not when:
            return line.text(f'日期「{mon}/{day}」不存在，請再確認')

    new_id = db.add_weight(uid, iso(when), w, note, iso(now))
    rec = next(r for r in compute(uid) if r['id'] == new_id)
    return line.text(record_text(rec, uid, backdated))


def record_text(o, uid, backdated):
    _, target = settings(uid)
    when = f"{md(o['when'])}（補登）" if backdated else f"{md(o['when'])} {o['when']:%H:%M}"
    lines = [f"✅ 已記錄 {when}{'｜' + o['note'] if o['note'] else ''}",
             f"體重：{o['weight']:.1f} kg",
             f"BMI：{o['bmi']:.1f}"]
    if o['diff'] is not None:
        lines.append(f"較前次：{arrow(o['diff'])} {signed(o['diff'], 1)} kg（{signed(o['pct'], 2)}%）")
    if o['avg'] is not None:
        lines.append(f"7筆平均：{o['avg']:.1f} kg")
    lines.append(dist_text(o['dist'], target))
    return '\n'.join(lines)


def recent(uid):
    rows = compute(uid)[-7:][::-1]
    if not rows:
        return line.text('還沒有任何紀錄，直接輸入體重開始吧，例如：84.5')
    lines = [f'📋 最近 {len(rows)} 筆']
    for o in rows:
        d = '' if o['diff'] is None else f"  {arrow(o['diff'])}{signed(o['diff'], 1)}"
        n = '｜' + o['note'] if o['note'] else ''
        lines.append(f"{md(o['when'])} {o['when']:%H:%M}  {o['weight']:.1f}{d}{n}")
    return line.text('\n'.join(lines))


def period(uid, kind):
    now = now_tpe()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == 'week':
        start, label = midnight - timedelta(days=now.weekday()), '本週'
    else:
        start, label = midnight.replace(day=1), f'{now.month}月'

    all_rows = compute(uid)
    rows = [o for o in all_rows if o['when'] >= start]
    if not rows:
        return line.text(f'{label}還沒有紀錄')

    _, target = settings(uid)
    ws = [o['weight'] for o in rows]
    last = rows[-1]
    before = [o for o in all_rows if o['when'] < start]
    base = before[-1] if before else rows[0]
    change = round(last['weight'] - base['weight'], 1)

    return line.text('\n'.join([
        f'📊 {label}摘要（{md(start)} 起）',
        f'記錄：{len(rows)} 筆',
        f"最新：{last['weight']:.1f} kg（BMI {last['bmi']:.1f}）",
        f"變化：{arrow(change)} {signed(change, 1)} kg（對比 {md(base['when'])} {base['weight']:.1f}）",
        f'平均：{sum(ws) / len(ws):.1f} kg',
        f'最低／最高：{min(ws):.1f}／{max(ws):.1f} kg',
        dist_text(last['dist'], target),
    ]))


def trend(uid):
    rows = compute(uid)[-30:]
    if len(rows) < 2:
        return [line.text('至少要有 2 筆紀錄才能畫趨勢圖')]
    _, target = settings(uid)
    try:
        name = render_chart(rows, target)
    except Exception:
        import logging
        logging.getLogger('vvs.weight').exception('趨勢圖產生失敗')
        return [line.text('趨勢圖暫時產生失敗，先用「最近」看文字紀錄')]
    last = rows[-1]
    return [line.image(f'{cfg.PUBLIC_BASE_URL}/charts/{name}'),
            line.text(f"最新 {last['weight']:.1f} kg｜{dist_text(last['dist'], target)}")]


def undo(uid):
    r = db.delete_last(uid)
    if not r:
        return line.text('沒有可以撤銷的紀錄')
    when = datetime.fromisoformat(r['ts'])
    return line.text(f"🗑️ 已刪除：{md(when)} {when:%H:%M}  {r['weight']:.1f} kg")


def set_height(uid, cm):
    if not (100 <= cm <= 250):
        return line.text('身高請輸入 100–250 之間（cm）')
    db.set_setting(uid, 'height_cm', cm)
    rows = compute(uid)
    extra = f"\n最新 BMI：{rows[-1]['bmi']:.1f}" if rows else ''
    return line.text(f'✅ 身高已設為 {cm:g} cm{extra}')


def set_target(uid, kg):
    if not (W_MIN <= kg <= W_MAX):
        return line.text(f'目標請輸入 {W_MIN}–{W_MAX} 之間（kg）')
    db.set_setting(uid, 'target_kg', kg)
    rows = compute(uid)
    extra = '\n' + dist_text(rows[-1]['dist'], kg) if rows else ''
    return line.text(f'✅ 目標已設為 {kg:g} kg{extra}')


def reminder_for(uid):
    """今天還沒記錄就回傳提醒訊息，否則 None"""
    today = now_tpe().date()
    if any(datetime.fromisoformat(r['ts']).date() == today for r in db.list_weights(uid)):
        return None
    return line.text('⏰ 今天還沒量體重喔，量完直接輸入數字即可')


# ================= 趨勢圖 =================

def render_chart(rows, target):
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    fp = FontProperties(fname=cfg.FONT_PATH) if os.path.exists(cfg.FONT_PATH) else None
    xs = list(range(len(rows)))
    labels = [md(r['when']) for r in rows]

    fig = Figure(figsize=(8, 4.5), dpi=120)
    ax = fig.subplots()
    ax.plot(xs, [r['weight'] for r in rows], marker='o', ms=3, lw=2, color='#4A90D9', label='體重')
    avg_pts = [(x, r['avg']) for x, r in zip(xs, rows) if r['avg'] is not None]
    if avg_pts:
        ax.plot(*zip(*avg_pts), marker='o', ms=2, lw=2, color='#F5A623', label='7筆平均')
    ax.axhline(target, ls='--', lw=1.5, color='#7ED321', label=f'目標 {target:g} kg')

    step = max(1, len(xs) // 10)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels(labels[::step])
    ax.grid(alpha=0.3)
    ax.set_title(f'體重趨勢（最近 {len(rows)} 筆）', fontproperties=fp, fontsize=14)
    ax.legend(prop=fp, loc='best')
    fig.tight_layout()

    cfg.CHART_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_charts()
    name = uuid.uuid4().hex + '.png'
    fig.savefig(cfg.CHART_DIR / name, format='png')
    return name


def _cleanup_charts(max_age_sec=2 * 86400):
    cutoff = time.time() - max_age_sec
    for p in cfg.CHART_DIR.glob('*.png'):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# ================= 小工具 =================

def now_tpe():
    return datetime.now(cfg.TZ)


def make_date(y, mon, day):
    try:
        return datetime(y, mon, day, BACKDATE_HOUR, 0, tzinfo=cfg.TZ)
    except ValueError:
        return None


def iso(dt):
    return dt.isoformat(timespec='seconds')


def md(dt):
    return f'{dt.month}/{dt.day}'


def _clean(v, d):
    v = round(v, d)
    return 0.0 if v == 0 else v


def signed(v, d):
    v = _clean(v, d)
    return ('+' if v > 0 else '') + f'{v:.{d}f}'


def arrow(v):
    v = _clean(v, 1)
    return '🔻' if v < 0 else '🔺' if v > 0 else '➖'


def dist_text(dist, target):
    if dist > 0:
        return f'距離 {target:g} kg：還差 {dist:.1f} kg'
    if dist == 0:
        return f'🎉 剛好達標 {target:g} kg'
    return f'🎉 已達標（低於 {target:g} kg 共 {-dist:.1f} kg）'


def selftest():
    """健康檢查用：驗證解析與計算邏輯"""
    assert RE_RECORD.match('84.5') and RE_RECORD.match('9/20 85.2 起床後')
    rows = compute_rows([{'id': 1, 'ts': '2026-01-01T08:00:00+08:00', 'weight': 90.0, 'note': ''},
                         {'id': 2, 'ts': '2026-01-02T08:00:00+08:00', 'weight': 89.5, 'note': ''}], 180, 85)
    assert rows[1]['diff'] == -0.5 and rows[1]['bmi'] == 27.6 and rows[1]['dist'] == 4.5
    return True
