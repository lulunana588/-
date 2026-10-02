"""VVS小秘書｜體重記錄模組

handle(uid, text)          → LINE 訊息列表；不是體重指令時回傳 None
handle_postback(uid, data) → 處理按鈕（修改／刪除指定紀錄）
隱私原則：每個函式都只讀寫 uid 本人的資料。
"""
import csv
import io
import json
import os
import re
import secrets
import time
import unicodedata
import uuid
from datetime import datetime, timedelta

import vvs_config as cfg
import vvs_db as db
import vvs_line as line

W_MIN, W_MAX = 30, 250
BACKDATE_HOUR = 8
JUMP_KG = 3.0                 # 跟前一筆差超過這個數字就先確認
PENDING_TTL = 10 * 60         # 待確認紀錄的有效時間
WIPE_TTL = 5 * 60             # 刪除全部資料的確認時間
EDIT_TTL = 5 * 60             # 選好要修改的那筆後，輸入新數字的時間
EXPORT_TTL = 10 * 60          # 匯出連結有效時間
CHART_TTL = 60 * 60           # 趨勢圖有效時間
REMIND_DEFAULT = '09:00'
AVG_DAYS, AVG_MIN_DAYS = 7, 3
EXPORT_DIR = cfg.DATA_DIR / 'exports'
QUICK = ['最近', '本週', '本月', '趨勢', '修改', '撤銷', '刪除', '說明']

RE_RECORD = re.compile(
    r'^(?:(\d{1,2})[/-](\d{1,2})\s+)?(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|公斤)?(?:\s+(.*))?$', re.I)
RE_SETTINGS = re.compile(
    r'^(?:身高\s*[:=]?\s*(\d{2,3}(?:\.\d)?)\s*(?:cm|公分)?)?'
    r'[\s,，、;；]*'
    r'(?:目標(?:體重)?\s*[:=]?\s*(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|公斤)?)?$', re.I)
RE_TREND = re.compile(r'^(?:趨勢|圖表)\s*(\d{1,4}|全部)?\s*(?:天|日)?$')
RE_START = re.compile(
    r'^起始(?:體重)?\s*[:=]?\s*(?:(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|公斤)?|(重設|重置|清除|預設))?$', re.I)
RE_REMIND = re.compile(
    r'^提醒(?:時間)?\s*[:=]?\s*(?:(關閉|關掉|關|停止)|(開啟|打開|開)|(\d{1,2})\s*(?:[:點時]\s*(\d{1,2})?\s*分?)?)?$')
NEED_SETUP = '💡 還沒設定身高和目標\n傳「身高 170 目標 70」就會幫你算 BMI 和距離目標'
WIPE_CONFIRM = '確認刪除全部資料'

# 體態：kind → (名稱, 單位, 最小, 最大)
BODY = {'fat': ('體脂', '%', 3, 70), 'waist': ('腰圍', ' cm', 40, 200)}
RE_BODY_DATE = re.compile(r'^(\d{1,2})[/-](\d{1,2})\s+(.*)$')
_BODY_ITEM = r'(體脂(?:率)?|腰圍)\s*[:=]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:%|cm|公分)?'
RE_BODY = re.compile(rf'^(?:{_BODY_ITEM}[\s,、;]*)+$', re.I)
RE_BODY_ITEM = re.compile(_BODY_ITEM, re.I)

HELP = """🤖 VVS小秘書｜使用手冊

━━━━━━━━━━━━━━
🟢 第一次使用
━━━━━━━━━━━━━━
傳自己的身高和目標：
　身高 165 目標 60
（之後想改隨時再傳）
目標進度從第一筆體重開始算，
想自己指定起點：起始 90（改回來：起始 重設）

👥 每個人的資料完全分開，只看得到自己的。

━━━━━━━━━━━━━━
📝 每天記錄
━━━━━━━━━━━━━━
・直接傳體重：84.5
・加註記：84.5 起床後
・補登：9/20 85.2
・跟上一筆差超過 3 公斤，會先請你確認，避免打錯

━━━━━━━━━━━━━━
📊 查詢
━━━━━━━━━━━━━━
最近　→ 最近 7 筆
本週／本月 → 摘要
進度　→ 起始 → 目前 → 目標，完成幾 %
趨勢　→ 最近 30 天折線圖
趨勢 90／趨勢 全部 → 更長的範圍

━━━━━━━━━━━━━━
✏️ 修改
━━━━━━━━━━━━━━
修改　→ 選一筆，改成正確的體重（日期不變）
撤銷　→ 刪掉最後新增的一筆
刪除　→ 選擇要刪掉哪一筆

━━━━━━━━━━━━━━
📏 體脂、腰圍
━━━━━━━━━━━━━━
・記錄：體脂 28.5／腰圍 82
・一起記：體脂 28.5 腰圍 82
・補登：9/30 腰圍 82
・同一天再傳一次會覆蓋（打錯直接重傳）
體態　→ 最近的體脂、腰圍變化
體態趨勢 → 體脂、腰圍的變化圖
刪除體態 → 選擇要刪掉哪一筆

━━━━━━━━━━━━━━
🔐 我的資料
━━━━━━━━━━━━━━
匯出　→ 下載自己的完整紀錄（CSV，10 分鐘有效）
刪除我的資料 → 清除自己全部紀錄

━━━━━━━━━━━━━━
⏰ 自動通知
━━━━━━━━━━━━━━
・每天 9 點還沒記錄會提醒（可自訂）
　提醒 07:30／提醒 關閉／提醒 開啟
・週日晚上 9 點收到本週摘要
・每月 1 號晚上 9 點收到上月摘要

━━━━━━━━━━━━━━
⚖️ 小叮嚀
━━━━━━━━━━━━━━
・固定時間量：起床、上完廁所、早餐前
・一天內差 0.5～1 公斤很正常
　看「7日平均」和「趨勢」比單日準"""


# ================= 入口 =================

def handle(uid, raw):
    t = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', raw or '').strip())
    low = t.lower()
    if low in ('說明', '規則', '使用規則', '使用手冊', 'help', '?'):
        return [line.text(HELP)]
    if t in ('確認記錄', '確認', '確定'):
        return [confirm_pending(uid)]
    if t == '取消':
        return [cancel_pending(uid)]
    if t == WIPE_CONFIRM:
        return [wipe_confirm(uid)]
    if t in ('刪除我的資料', '刪除全部資料', '清除我的資料'):
        return [wipe_request(uid)]
    if t in ('最近', '紀錄', '記錄'):
        return [recent(uid)]
    if t == '本週':
        return [period(uid, 'week')]
    if t == '本月':
        return [period(uid, 'month')]
    m = RE_TREND.match(t)
    if m:
        return trend(uid, m.group(1))
    m = RE_REMIND.match(t)
    if m:
        return [set_reminder(uid, m)]
    if low in ('撤銷', 'undo'):
        return [undo(uid)]
    if t in ('刪除', '刪除紀錄'):
        return [delete_picker(uid)]
    if t in ('修改', '修改紀錄', '編輯', '改'):
        return [edit_picker(uid)]
    if t in ('進度', '目標進度'):
        return [progress_msg(uid)]
    m = RE_START.match(t)
    if m:
        return [set_start(uid, m)]
    if t in ('體態趨勢', '體脂趨勢', '腰圍趨勢'):
        return body_trend(uid)
    if t in ('體態', '體脂', '腰圍', '體態紀錄'):
        return [body_recent(uid)]
    if t in ('刪除體態', '刪除體脂', '刪除腰圍'):
        return [body_delete_picker(uid)]
    b = body_parse(t)
    if b:
        return [body_record(uid, *b)]
    if t in ('匯出', '匯出資料', '下載'):
        return [export(uid)]
    m = RE_SETTINGS.match(t)
    if m and (m.group(1) or m.group(2)):
        return [set_settings(uid, m.group(1), m.group(2))]
    m = RE_RECORD.match(t)
    if m:
        e = edit_pending(uid)
        if e:
            return [apply_edit(uid, e, m)]
        return [record(uid, m)]
    return None


def handle_postback(uid, data):
    if (data or '').startswith('edit:'):
        try:
            rid = int(data[5:])
        except ValueError:
            return [line.text('這個按鈕已失效，請重新傳「修改」')]
        return [edit_start(uid, rid)]
    if (data or '').startswith('bdel:'):
        try:
            rid = int(data[5:])
        except ValueError:
            return [line.text('這個按鈕已失效，請重新傳「刪除體態」')]
        r = db.delete_body(uid, rid)            # 只刪得到自己的
        if not r:
            return [line.text('找不到這筆紀錄，可能已經刪除了')]
        when = datetime.fromisoformat(r['ts'])
        name, unit, _, _ = BODY[r['kind']]
        return [line.text(f"🗑️ 已刪除：{md(when)} {name} {fv(r['value'])}{unit}")]
    if (data or '').startswith('del:'):
        try:
            rid = int(data[4:])
        except ValueError:
            return [line.text('這個按鈕已失效，請重新傳「刪除」')]
        r = db.delete_weight(uid, rid)          # 只刪得到自己的
        if not r:
            return [line.text('找不到這筆紀錄，可能已經刪除了')]
        when = datetime.fromisoformat(r['ts'])
        return [line.text(f"🗑️ 已刪除：{md(when)} {when:%H:%M}  {fw(r['weight'])} kg")]
    return None


def help_msg():
    return line.text(HELP)


# ================= 計算 =================

def settings(uid):
    """只用使用者自己設定的值；沒設定就回傳 None，不套用任何預設"""
    h = db.get_setting(uid, 'height_cm')
    g = db.get_setting(uid, 'target_kg')
    return (float(h) if h else None), (float(g) if g else None)


# ================= 目標進度 =================

def start_weight(uid):
    """(起始體重, 說明)；自訂優先，否則用自己的第一筆。沒有資料回傳 (None, '')"""
    s = db.get_setting(uid, 'start_kg')
    if s:
        return float(s), '自訂'
    rows = db.list_weights(uid)
    if not rows:
        return None, ''
    return rows[0]['weight'], f"{md(datetime.fromisoformat(rows[0]['ts']))} 第一筆"


def progress(start, current, target):
    """完成百分比（可能 <0 或 >100）；起始等於目標時回傳 None"""
    if start is None or current is None or not target or abs(start - target) < 0.05:
        return None
    return (start - current) / (start - target) * 100


def progress_bar(pct, n=10):
    k = max(0, min(n, round(pct / 100 * n)))
    return '▓' * k + '░' * (n - k)


def progress_value(pct):
    if pct >= 100:
        return f'{progress_bar(pct)} 100% 🎉'
    if pct < 0:
        return f'{progress_bar(0)} 0%（比起始還遠）'
    return f'{progress_bar(pct)} {pct:.1f}%' if pct < 10 else f'{progress_bar(pct)} {pct:.0f}%'


def progress_for(uid, current):
    _, target = settings(uid)
    start, _ = start_weight(uid)
    return progress(start, current, target)


def progress_msg(uid):
    _, target = settings(uid)
    rows = db.list_weights(uid)
    if not target:
        return line.text(NEED_SETUP)
    if not rows:
        return line.text('還沒有任何紀錄，直接輸入體重開始吧，例如：84.5')
    start, src = start_weight(uid)
    cur = rows[-1]['weight']
    pct = progress(start, cur, target)
    if pct is None:
        return line.text(f'起始體重（{fw(start)} kg）跟目標一樣，沒辦法算進度\n可以傳「起始 90」指定起點')
    done = round(abs(start - cur), 2) if pct > 0 else 0
    total = round(abs(start - target), 2)
    return line.text('\n'.join([
        '🎯 目標進度',
        f'起始：{fw(start)} kg（{src}）',
        f'目前：{fw(cur)} kg',
        f'目標：{target:g} kg',
        '',
        progress_value(pct),
        f'已完成 {fw(min(done, total))}／{fw(total)} kg',
        '',
        '想改起點：起始 90；改回第一筆：起始 重設',
    ]))


def set_start(uid, m):
    if m.group(2):
        db.delete_setting(uid, 'start_kg')
        start, src = start_weight(uid)
        msg = f'✅ 起始體重改回第一筆：{fw(start)} kg（{src}）' if start else '✅ 已清除自訂起始體重'
        return line.text(msg)
    if not m.group(1):
        return progress_msg(uid)
    kg = float(m.group(1))
    if not (W_MIN <= kg <= W_MAX):
        return line.text(f'起始體重請輸入 {W_MIN}–{W_MAX} 之間（kg），例如：起始 90')
    db.set_setting(uid, 'start_kg', kg)
    return line.text(f'✅ 起始體重已設為 {kg:g} kg\n傳「進度」看完成幾 %')


def compute_rows(rows, height_cm, target_kg):
    """rows 需已依時間排序。7日平均：取近 7 天內每天的平均，再平均（至少 3 天有紀錄才顯示）"""
    h2 = (height_cm / 100) ** 2 if height_cm else None
    whens = [datetime.fromisoformat(r['ts']) for r in rows]
    by_day = {}
    for r, w in zip(rows, whens):
        by_day.setdefault(w.date(), []).append(r['weight'])
    day_mean = {d: sum(v) / len(v) for d, v in by_day.items()}

    out = []
    for i, (r, when) in enumerate(zip(rows, whens)):
        w = r['weight']
        prev = rows[i - 1]['weight'] if i else None
        window = [day_mean[d] for d in (when.date() - timedelta(days=k) for k in range(AVG_DAYS))
                  if d in day_mean]
        avg = round(sum(window) / len(window), 1) if len(window) >= AVG_MIN_DAYS else None
        out.append({
            **r,
            'when': when,
            'bmi': round(w / h2, 1) if h2 else None,
            'diff': None if prev is None else round(w - prev, 2),
            'pct': None if prev is None else (w - prev) / prev * 100,
            'avg': avg,
            'dist': round(w - target_kg, 2) if target_kg else None,
        })
    return out


def compute(uid):
    h, g = settings(uid)
    return compute_rows(db.list_weights(uid), h, g)


# ================= 記錄（含打錯防呆） =================

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
        if when and when > now:
            when = make_date(now.year - 1, mon, day)
        if not when:
            return line.text(f'日期「{mon}/{day}」不存在，請再確認')

    before = [r for r in db.list_weights(uid) if datetime.fromisoformat(r['ts']) <= when]
    if before and abs(w - before[-1]['weight']) > JUMP_KG:
        prev = before[-1]['weight']
        db.set_setting(uid, 'pending', json.dumps(
            {'w': w, 'ts': iso(when), 'note': note, 'back': backdated, 'at': time.time()}))
        msg = line.text(f'⚠️ 請確認一下\n這次輸入 {fw(w)} kg，跟上一筆 {fw(prev)} kg 差了 {fw(round(abs(w - prev), 2))} kg\n\n'
                        f'數字沒錯的話按「確認記錄」，打錯了按「取消」再重新輸入')
        msg['quickReply'] = line.quick_items([('✅ 確認記錄', '確認記錄'), ('✖️ 取消', '取消')])
        return msg
    return commit(uid, w, when, note, backdated)


def commit(uid, w, when, note, backdated):
    db.delete_setting(uid, 'pending')
    new_id = db.add_weight(uid, iso(when), w, note, iso(now_tpe()))
    rec = next(r for r in compute(uid) if r['id'] == new_id)
    return record_card(rec, uid, backdated)


def confirm_pending(uid):
    raw = db.get_setting(uid, 'pending')
    if not raw:
        return line.text('目前沒有等待確認的紀錄')
    p = json.loads(raw)
    if time.time() - p['at'] > PENDING_TTL:
        db.delete_setting(uid, 'pending')
        return line.text('確認時間已過，請重新輸入體重')
    return commit(uid, p['w'], datetime.fromisoformat(p['ts']), p['note'], p['back'])


def cancel_pending(uid):
    had = (db.get_setting(uid, 'pending') or db.get_setting(uid, 'pending_wipe')
           or db.get_setting(uid, 'pending_edit'))
    for k in ('pending', 'pending_wipe', 'pending_edit'):
        db.delete_setting(uid, k)
    return line.text('已取消，沒有記錄任何資料' if had else '目前沒有需要取消的操作')


def record_lines(o, target, pct=None):
    rows = []
    if o['bmi'] is not None:
        rows.append(('BMI', f"{o['bmi']:.1f}"))
    if o['diff'] is not None:
        rows.append(('較前次', f"{arrow(o['diff'])} {fd(o['diff'])} kg（{signed(o['pct'], 2)}%）"))
    if o['avg'] is not None:
        rows.append(('7日平均', f"{o['avg']:.1f} kg"))
    if target:
        rows.append((f'距離 {target:g} kg', dist_value(o['dist'])))
    if pct is not None:
        rows.append(('目標進度', progress_value(pct)))
    return rows


def record_card(o, uid, backdated):
    _, target = settings(uid)
    when = f"{md(o['when'])}（補登）" if backdated else f"{md(o['when'])} {o['when']:%H:%M}"
    sub = when + (f"｜{o['note']}" if o['note'] else '')
    latest = db.list_weights(uid)[-1]['weight']          # 進度一律看最新一筆（補登時也一樣）
    rows = record_lines(o, target, progress_for(uid, latest))

    alt = [f'✅ 已記錄 {sub}', f"體重：{fw(o['weight'])} kg"] + [f'{k}：{v}' for k, v in rows]
    if not target:
        alt.append(NEED_SETUP)

    body = [
        {'type': 'text', 'text': '✅ 已記錄', 'weight': 'bold', 'size': 'md', 'color': '#1DB446'},
        {'type': 'text', 'text': sub, 'size': 'xs', 'color': '#8C8C8C', 'wrap': True},
        {'type': 'box', 'layout': 'baseline', 'margin': 'md', 'contents': [
            {'type': 'text', 'text': f"{fw(o['weight'])}", 'size': '3xl', 'weight': 'bold',
             'color': '#111111', 'flex': 0},
            {'type': 'text', 'text': 'kg', 'size': 'md', 'color': '#8C8C8C', 'margin': 'sm'},
        ]},
    ]
    if rows:
        body.append({'type': 'separator', 'margin': 'lg'})
        body.append({'type': 'box', 'layout': 'vertical', 'margin': 'lg', 'spacing': 'sm', 'contents': [
            {'type': 'box', 'layout': 'horizontal', 'contents': [
                {'type': 'text', 'text': k, 'size': 'sm', 'color': '#8C8C8C', 'flex': 0},
                {'type': 'text', 'text': v, 'size': 'sm', 'color': '#111111', 'align': 'end', 'wrap': True},
            ]} for k, v in rows]})
    n = streak(uid)
    if n >= 2:
        alt.append(f'🔥 已連續記錄 {n} 天')
        body.append({'type': 'text', 'text': f'🔥 已連續記錄 {n} 天', 'size': 'xs', 'color': '#E67E22',
                     'margin': 'lg'})
    if not target:
        body.append({'type': 'text', 'text': NEED_SETUP, 'size': 'xs', 'color': '#8C8C8C',
                      'wrap': True, 'margin': 'lg'})
    return {'type': 'flex', 'altText': '\n'.join(alt)[:400],
            'contents': {'type': 'bubble', 'size': 'kilo',
                         'body': {'type': 'box', 'layout': 'vertical', 'contents': body}}}


# ================= 查詢 =================

def recent(uid):
    rows = compute(uid)[-7:][::-1]
    if not rows:
        return line.text('還沒有任何紀錄，直接輸入體重開始吧，例如：84.5')
    lines = [f'📋 最近 {len(rows)} 筆']
    for o in rows:
        d = '' if o['diff'] is None else f"  {arrow(o['diff'])}{fd(o['diff'])}"
        n = '｜' + o['note'] if o['note'] else ''
        lines.append(f"{md(o['when'])} {o['when']:%H:%M}  {fw(o['weight'])}{d}{n}")
    lines.append('\n要修改或刪掉某一筆，傳「修改」或「刪除」')
    return line.text('\n'.join(lines))


def weekly_trend(uid, now=None, weeks=4):
    """近 4 週平均每週變化：用每週平均體重比較（第一個有紀錄的週 → 最新有紀錄的週）。
    回傳 (說明行列表)；有紀錄的週不到 2 週回傳 None。只讀 uid 本人的資料"""
    now = now or now_tpe()
    starts = [_week_start(now) - timedelta(weeks=k) for k in range(weeks, -1, -1)]
    buckets = {s: [] for s in starts}
    for r in db.list_weights(uid):
        when = datetime.fromisoformat(r['ts'])
        ws = _week_start(when)
        if ws in buckets and when <= now:
            buckets[ws].append(r['weight'])
    means = [(s, sum(v) / len(v)) for s, v in buckets.items() if v]
    if len(means) < 2:
        return None
    (s0, m0), (s1, m1) = means[0], means[-1]
    rate = round((m1 - m0) / ((s1 - s0).days / 7), 2)
    chain = ' → '.join(f'{md(s)} {m:.1f}' for s, m in means)
    return [f'近 {weeks} 週平均：每週 {arrow(rate)} {fd(rate)} kg',
            f'（每週平均體重：{chain}）']


def summary_range(uid, start, end, title, label, show_streak=True, weeks4=False):
    """start <= 時間 < end 的摘要；只讀 uid 本人的資料"""
    all_rows = compute(uid)
    rows = [o for o in all_rows if start <= o['when'] < end]
    if not rows:
        return None
    _, target = settings(uid)
    ws = [o['weight'] for o in rows]
    last = rows[-1]
    before = [o for o in all_rows if o['when'] < start]
    base = before[-1] if before else rows[0]
    change = round(last['weight'] - base['weight'], 2)
    days = len({o['when'].date() for o in rows})
    out = [
        title,
        f'記錄：{days} 天、{len(rows)} 筆',
        f"最新：{fw(last['weight'])} kg" + (f"（BMI {last['bmi']:.1f}）" if last['bmi'] is not None else ''),
        f"變化：{arrow(change)} {fd(change)} kg（對比 {md(base['when'])} {fw(base['weight'])}）",
        f'平均：{sum(ws) / len(ws):.1f} kg',
        f'最低／最高：{fw(min(ws))}／{fw(max(ws))} kg',
    ]
    if last['avg'] is not None:
        out.append(f"7日平均：{last['avg']:.1f} kg")
    tr = weekly_trend(uid, end - timedelta(seconds=1)) if weeks4 else None
    if tr:
        out += tr
    out.append(dist_text(last['dist'], target) if target else NEED_SETUP)
    pct = progress_for(uid, last['weight'])
    if pct is not None:
        out.append(f'目標進度：{progress_value(pct)}')
    bl = body_period_line(uid, start, end)
    if bl:
        out.append(bl)
    n = streak(uid) if show_streak else 0
    if n >= 2:
        out.append(f'🔥 已連續記錄 {n} 天')
    return line.text('\n'.join(out))


def _week_start(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())


def _month_start(now):
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def period(uid, kind):
    now = now_tpe()
    if kind == 'week':
        start, label = _week_start(now), '本週'
    else:
        start, label = _month_start(now), f'{now.month}月'
    msg = summary_range(uid, start, now + timedelta(seconds=1), f'📊 {label}摘要（{md(start)} 起）', label,
                        weeks4=(kind == 'week'))
    return msg or line.text(f'{label}還沒有紀錄')


def weekly_summary(uid):
    """週日推播用；本週沒有紀錄就回傳 None"""
    now = now_tpe()
    start = _week_start(now)
    return summary_range(uid, start, now + timedelta(seconds=1),
                         f'📅 本週摘要（{md(start)}～{md(now)}）', '本週', weeks4=True)


def monthly_summary(uid):
    """每月 1 號推播上個月的摘要；上個月沒有紀錄就回傳 None"""
    this_start = _month_start(now_tpe())
    last_start = _month_start(this_start - timedelta(days=1))
    return summary_range(uid, last_start, this_start, f'🗓️ {last_start.month}月摘要', f'{last_start.month}月', show_streak=False)


def streak(uid):
    """到今天（或昨天）為止，連續有記錄的天數"""
    days = {datetime.fromisoformat(r['ts']).date() for r in db.list_weights(uid)}
    d = now_tpe().date()
    if d not in days:
        d -= timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= timedelta(days=1)
    return n


def chart_marks(rows, all_rows, target, losing=True):
    """畫面範圍內的最高、最低點（同值取最近的一次），以及首次達標（看全部紀錄）。
    回傳 {'high': i, 'low': i, 'goal': 畫面內的 i 或 None, 'goal_row': 首次達標那筆或 None}"""
    ws = [o['weight'] for o in rows]
    hi = max(range(len(ws)), key=lambda i: (ws[i], i))
    lo = min(range(len(ws)), key=lambda i: (ws[i], -i))
    goal_row = None
    if target:
        goal_row = next((o for o in all_rows
                         if (o['weight'] <= target if losing else o['weight'] >= target)), None)
    goal = next((i for i, o in enumerate(rows) if o is goal_row), None)
    return {'high': hi, 'low': lo, 'goal': goal, 'goal_row': goal_row}


def trend_data(uid, span=None):
    """趨勢圖要用的資料（只讀 uid 本人）：(rows, label, target, marks, 說明文字)；不足 2 筆回傳 None 與提示"""
    all_rows = compute(uid)
    if span == '全部':
        rows, label = all_rows, '全部紀錄'
    else:
        days = int(span) if span else 30
        days = max(7, min(days, 3650))
        cutoff = now_tpe() - timedelta(days=days)
        rows, label = [o for o in all_rows if o['when'] >= cutoff], f'最近 {days} 天'
    if len(rows) < 2:
        return None, f'{label}至少要有 2 筆紀錄才能畫趨勢圖'
    _, target = settings(uid)
    start, _ = start_weight(uid)
    losing = not (target and start is not None and target > start)
    marks = chart_marks(rows, all_rows, target, losing)
    last, hi, lo = rows[-1], rows[marks['high']], rows[marks['low']]
    text = [f"{label}｜最新 {fw(last['weight'])} kg" +
            (f"｜{dist_text(last['dist'], target)}" if target else '')]
    if marks['high'] != marks['low']:
        text.append(f"📈 最高 {fw(hi['weight'])}（{md(hi['when'])}）　📉 最低 {fw(lo['weight'])}（{md(lo['when'])}）")
    g = marks['goal_row']
    if g:
        text.append(f"🎉 首次達標：{md(g['when'])} {fw(g['weight'])} kg" +
                    ('' if marks['goal'] is not None else '（在這段範圍之前）'))
    text.append('想看更長可傳「趨勢 90」或「趨勢 全部」')
    return (rows, label, target, marks), '\n'.join(text)


def trend(uid, span=None):
    data, text = trend_data(uid, span)
    if not data:
        return [line.text(text)]
    rows, label, target, marks = data
    try:
        name = render_chart(rows, target, label, marks)
    except Exception:
        import logging
        logging.getLogger('vvs.weight').exception('趨勢圖產生失敗')
        return [line.text('趨勢圖暫時產生失敗，先用「最近」看文字紀錄')]
    return [line.image(f'{cfg.PUBLIC_BASE_URL}/charts/{name}'), line.text(text)]


# ================= 修改 =================

def undo(uid):
    r = db.delete_last(uid)
    if not r:
        return line.text('沒有可以撤銷的紀錄')
    when = datetime.fromisoformat(r['ts'])
    return line.text(f"🗑️ 已刪除：{md(when)} {when:%H:%M}  {fw(r['weight'])} kg")


def delete_picker(uid):
    rows = db.list_weights(uid)[-10:][::-1]
    if not rows:
        return line.text('還沒有任何紀錄可以刪除')
    items = []
    for r in rows:
        when = datetime.fromisoformat(r['ts'])
        items.append((f"🗑 {md(when)} {fw(r['weight'])}",
                      {'postback': f"del:{r['id']}", 'display': f"刪除 {md(when)} {when:%H:%M} {fw(r['weight'])}"}))
    items.append(('✖️ 取消', '取消'))
    msg = line.text('要刪除哪一筆？點下方按鈕（最近 10 筆，由新到舊）\n按了就會直接刪除')
    msg['quickReply'] = line.quick_items(items)
    return msg


def edit_picker(uid):
    rows = db.list_weights(uid)[-10:][::-1]
    if not rows:
        return line.text('還沒有任何紀錄可以修改')
    items = []
    for r in rows:
        when = datetime.fromisoformat(r['ts'])
        items.append((f"✏️ {md(when)} {fw(r['weight'])}",
                      {'postback': f"edit:{r['id']}", 'display': f"修改 {md(when)} {when:%H:%M} {fw(r['weight'])}"}))
    items.append(('✖️ 取消', '取消'))
    msg = line.text('要修改哪一筆？點下方按鈕（最近 10 筆，由新到舊）\n選好後再傳正確的體重')
    msg['quickReply'] = line.quick_items(items)
    return msg


def edit_start(uid, rid):
    r = db.get_weight(uid, rid)                  # 只找得到自己的
    if not r:
        db.delete_setting(uid, 'pending_edit')
        return line.text('找不到這筆紀錄，可能已經刪除了')
    db.set_setting(uid, 'pending_edit', json.dumps({'id': rid, 'at': time.time()}))
    when = datetime.fromisoformat(r['ts'])
    msg = line.text(f"✏️ 要把 {md(when)} {when:%H:%M} 的 {fw(r['weight'])} kg 改成多少？\n"
                    f"直接傳正確的體重，例如 {fw(r['weight'])}（也可以加註記）\n"
                    f"5 分鐘內有效，不改了按「取消」")
    msg['quickReply'] = line.quick_items([('✖️ 取消', '取消')])
    return msg


def edit_pending(uid):
    raw = db.get_setting(uid, 'pending_edit')
    if not raw:
        return None
    try:
        p = json.loads(raw)
        if time.time() - p['at'] <= EDIT_TTL:
            return p
    except (ValueError, KeyError, TypeError):
        pass
    db.delete_setting(uid, 'pending_edit')
    return None


def apply_edit(uid, p, m):
    if m.group(1):
        return line.text('修改只會改體重，日期維持原本那天\n請只傳數字，例如 84.3；不改了傳「取消」')
    w = float(m.group(3))
    if not (W_MIN <= w <= W_MAX):
        return line.text(f'「{m.group(3)}」看起來不太對，請輸入 {W_MIN}–{W_MAX} 之間的體重')
    db.delete_setting(uid, 'pending_edit')
    old = db.get_weight(uid, p.get('id'))
    if not old:
        return line.text('找不到這筆紀錄，可能已經刪除了')
    note = (m.group(4) or '')[:30] if m.group(4) else old['note']
    db.update_weight(uid, old['id'], w, note)    # 只改得到自己的
    when = datetime.fromisoformat(old['ts'])
    n = f'\n註記：{note}' if note else ''
    return line.text(f"✅ 已修改：{md(when)} {when:%H:%M}\n{fw(old['weight'])} → {fw(w)} kg{n}")


# ================= 體態（體脂、腰圍） =================

def body_parse(t):
    """「體脂 28.5 腰圍 82」→ (日期或 None, [(kind, 數值字串)])；不是體態指令回傳 None"""
    date, rest = None, t
    m = RE_BODY_DATE.match(t)
    if m:
        date, rest = (int(m.group(1)), int(m.group(2))), m.group(3)
    if not RE_BODY.match(rest):
        return None
    items = {}
    for name, val in RE_BODY_ITEM.findall(rest):
        items['fat' if name.startswith('體脂') else 'waist'] = val
    return date, list(items.items())


def fv(v):
    return f'{v:.2f}'.rstrip('0').rstrip('.')


def body_record(uid, date, items):
    now = now_tpe()
    when = now
    if date:
        mon, day = date
        when = make_date(now.year, mon, day)
        if when and when > now:
            when = make_date(now.year - 1, mon, day)
        if not when:
            return line.text(f'日期「{mon}/{day}」不存在，請再確認')
    for kind, val in items:
        name, unit, lo, hi = BODY[kind]
        if not (lo <= float(val) <= hi):
            return line.text(f'{name}「{val}」看起來不太對，請輸入 {lo}–{hi} 之間的數字')
    out = [f'✅ 已記錄 {md(when)}']
    for kind, val in items:
        v = float(val)
        name, unit, _, _ = BODY[kind]
        prev = [r for r in db.list_body(uid, kind) if r['ts'][:10] < iso(when)[:10]]
        _, replaced = db.add_body(uid, iso(when), kind, v, iso(now))
        s = f'{name} {fv(v)}{unit}'
        if prev:
            d = round(v - prev[-1]['value'], 2)
            s += f"　{arrow(d)}{fd(d)}（上次 {md(datetime.fromisoformat(prev[-1]['ts']))}）"
        else:
            s += '　第一筆'
        if replaced is not None:
            s += f'\n　（已取代當天原本的 {fv(replaced)}）'
        out.append(s)
    out.append('\n傳「體態」看變化')
    return line.text('\n'.join(out))


def body_period_line(uid, start, end):
    """期間內有量體脂／腰圍才回傳一行，例如「📏 體脂 28.5%（🔻-0.5）｜腰圍 82 cm」"""
    parts = []
    for kind, (name, unit, _, _) in BODY.items():
        rows = [(datetime.fromisoformat(r['ts']), r['value']) for r in db.list_body(uid, kind)]
        inside = [v for t, v in rows if start <= t < end]
        if not inside:
            continue
        before = [v for t, v in rows if t < start]
        s = f'{name} {fv(inside[-1])}{unit}'
        base = before[-1] if before else (inside[0] if len(inside) > 1 else None)
        if base is not None:
            d = round(inside[-1] - base, 2)
            s += f'（{arrow(d)}{fd(d)}）'
        parts.append(s)
    return '📏 ' + '｜'.join(parts) if parts else None


def body_trend(uid):
    rows = {k: [(datetime.fromisoformat(r['ts']), r['value']) for r in db.list_body(uid, k)] for k in BODY}
    kinds = [k for k in BODY if len(rows[k]) >= 2]
    if not kinds:
        return [line.text('體脂或腰圍至少要有 2 筆紀錄才能畫趨勢圖\n傳「體脂 28.5」或「腰圍 82」記錄')]
    try:
        name = render_body_chart({k: rows[k] for k in kinds})
    except Exception:
        import logging
        logging.getLogger('vvs.weight').exception('體態趨勢圖產生失敗')
        return [line.text('體態趨勢圖暫時產生失敗，先用「體態」看文字紀錄')]
    text = ['📏 體態趨勢（全部紀錄）']
    for k in kinds:
        nm, unit, _, _ = BODY[k]
        (t0, v0), (t1, v1) = rows[k][0], rows[k][-1]
        d = round(v1 - v0, 2)
        text.append(f'{nm}：{md(t0)} {fv(v0)} → {md(t1)} {fv(v1)}{unit}（{arrow(d)}{fd(d)}）')
    return [line.image(f'{cfg.PUBLIC_BASE_URL}/charts/{name}'), line.text('\n'.join(text))]


def body_recent(uid):
    lines = ['📏 體態紀錄']
    has = False
    for kind, (name, unit, _, _) in BODY.items():
        rows = db.list_body(uid, kind)
        if not rows:
            continue
        has = True
        lines.append(f'\n{name}（{unit.strip()}）')
        shown = rows[-6:]
        for i, r in enumerate(shown[::-1]):
            idx = len(rows) - 1 - i
            d = '' if idx == 0 else f"  {arrow(r['value'] - rows[idx - 1]['value'])}{fd(round(r['value'] - rows[idx - 1]['value'], 2))}"
            lines.append(f"{md(datetime.fromisoformat(r['ts']))}  {fv(r['value'])}{d}")
        if len(rows) > 1:
            total = round(rows[-1]['value'] - rows[0]['value'], 2)
            lines.append(f"從 {md(datetime.fromisoformat(rows[0]['ts']))} 起：{arrow(total)}{fd(total)}{unit}")
    if not has:
        return line.text('還沒有體脂或腰圍紀錄\n傳「體脂 28.5」或「腰圍 82」開始記錄')
    lines.append('\n要刪掉某一筆，傳「刪除體態」')
    return line.text('\n'.join(lines))


def body_delete_picker(uid):
    rows = db.list_body(uid)[-10:][::-1]
    if not rows:
        return line.text('還沒有任何體態紀錄可以刪除')
    items = []
    for r in rows:
        when = datetime.fromisoformat(r['ts'])
        name, unit, _, _ = BODY[r['kind']]
        items.append((f"🗑 {md(when)} {name}{fv(r['value'])}",
                      {'postback': f"bdel:{r['id']}", 'display': f"刪除 {md(when)} {name} {fv(r['value'])}{unit}"}))
    items.append(('✖️ 取消', '取消'))
    msg = line.text('要刪除哪一筆體態紀錄？點下方按鈕（最近 10 筆，由新到舊）\n按了就會直接刪除')
    msg['quickReply'] = line.quick_items(items)
    return msg


def set_settings(uid, height, target):
    msgs = []
    if height is not None:
        cm = float(height)
        if not (100 <= cm <= 250):
            return line.text('身高請輸入 100–250 之間（cm），例如：身高 170')
    if target is not None:
        kg = float(target)
        if not (W_MIN <= kg <= W_MAX):
            return line.text(f'目標請輸入 {W_MIN}–{W_MAX} 之間（kg），例如：目標 70')
    if height is not None:
        db.set_setting(uid, 'height_cm', cm)
        msgs.append(f'✅ 身高已設為 {cm:g} cm')
    if target is not None:
        db.set_setting(uid, 'target_kg', kg)
        msgs.append(f'✅ 目標已設為 {kg:g} kg')

    h, g = settings(uid)
    rows = compute(uid)
    if rows:
        last = rows[-1]
        if last['bmi'] is not None:
            msgs.append(f"最新 BMI：{last['bmi']:.1f}")
        if g:
            msgs.append(dist_text(last['dist'], g))
    if not h:
        msgs.append('💡 再傳「身高 170」就能算 BMI')
    if not g:
        msgs.append('💡 再傳「目標 70」就能算距離目標')
    return line.text('\n'.join(msgs))


# ================= 我的資料 =================

def export(uid):
    rows = compute(uid)
    body = db.list_body(uid)
    if not rows and not body:
        return line.text('還沒有任何紀錄可以匯出')
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['日期時間', '體重kg', '備註', 'BMI', '較前次kg', '7日平均kg'])
    for o in rows:
        w.writerow([f"{o['when']:%Y-%m-%d %H:%M}", f"{fw(o['weight'])}", o['note'],
                    '' if o['bmi'] is None else f"{o['bmi']:.1f}",
                    '' if o['diff'] is None else fd(o['diff']),
                    '' if o['avg'] is None else f"{o['avg']:.1f}"])
    if body:
        w.writerow([])
        w.writerow(['日期', '項目', '數值', '單位'])
        for r in body:
            name, unit, _, _ = BODY[r['kind']]
            w.writerow([r['ts'][:10], name, fv(r['value']), unit.strip()])
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EXPORT_DIR, 0o700)
    cleanup_temp()
    name = secrets.token_hex(24) + '.csv'
    path = EXPORT_DIR / name
    path.write_text('\ufeff' + buf.getvalue(), encoding='utf-8')   # BOM 讓 Excel 正確顯示中文
    os.chmod(path, 0o600)
    return line.text(f'📄 你的完整紀錄（體重 {len(rows)} 筆、體態 {len(body)} 筆）\n{cfg.PUBLIC_BASE_URL}/exports/{name}\n\n'
                     '・只包含你自己的資料\n・連結 10 分鐘後自動失效\n・請不要轉傳這個連結')


def wipe_request(uid):
    n = len(db.list_weights(uid))
    nb = len(db.list_body(uid))
    db.set_setting(uid, 'pending_wipe', str(time.time()))
    msg = line.text(f'⚠️ 確定要刪除你的全部資料嗎？\n包含 {n} 筆體重、{nb} 筆體態紀錄、身高和目標設定，刪除後無法復原。\n\n'
                    f'想先保留一份，可以先傳「匯出」。\n確定的話請在 5 分鐘內按「{WIPE_CONFIRM}」')
    msg['quickReply'] = line.quick_items([(f'🗑 {WIPE_CONFIRM}', WIPE_CONFIRM), ('✖️ 取消', '取消')])
    return msg


def wipe_confirm(uid):
    ts = db.get_setting(uid, 'pending_wipe')
    if not ts or time.time() - float(ts) > WIPE_TTL:
        db.delete_setting(uid, 'pending_wipe')
        return line.text('確認時間已過或沒有刪除請求，請重新傳「刪除我的資料」')
    n = db.wipe_user(uid)
    return line.text(f'✅ 已刪除你的 {n} 筆紀錄和所有設定\n\n'
                     '備註：系統每天的備份檔中仍有刪除前的資料，'
                     '伺服器上的備份會在 14 天內自動淘汰；雲端備份是加密檔，沒有金鑰無法打開。')


# ================= 提醒 =================

def reminder_setting(uid):
    """回傳 'HH:MM' 或 'off'"""
    return db.get_setting(uid, 'remind') or REMIND_DEFAULT


def set_reminder(uid, m):
    off, on, hh, mm = m.groups()
    now = now_tpe()
    if off:
        db.set_setting(uid, 'remind', 'off')
        return line.text('🔕 已關閉每日提醒\n想再打開傳「提醒 開啟」或「提醒 07:30」')
    if on:
        t = db.get_setting(uid, 'remind_last') or REMIND_DEFAULT
    elif hh is not None:
        h, mi = int(hh), int(mm or 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return line.text('時間格式不對，例如：提醒 07:30、提醒 21:00')
        t = f'{h:02d}:{mi:02d}'
    else:
        cur = reminder_setting(uid)
        state = '已關閉' if cur == 'off' else f'每天 {cur}'
        return line.text(f'⏰ 目前提醒：{state}\n\n改時間：提醒 07:30\n關閉：提醒 關閉\n打開：提醒 開啟')
    db.set_setting(uid, 'remind', t)
    db.set_setting(uid, 'remind_last', t)
    h, mi = map(int, t.split(':'))
    note = ''
    if (now.hour, now.minute) >= (h, mi):
        db.set_setting(uid, 'reminded_on', now.date().isoformat())
        note = '（今天的時間已過，明天開始）'
    return line.text(f'⏰ 好，每天 {t} 左右提醒你{note}\n當天已經記錄就不會提醒')


def reminder_due(uid, now=None):
    """cron 每 5 分鐘呼叫；到了本人設定的時間、今天還沒提醒過、也還沒記錄，才回傳提醒訊息"""
    now = now or now_tpe()
    t = reminder_setting(uid)
    if t == 'off':
        return None
    h, mi = map(int, t.split(':'))
    if (now.hour, now.minute) < (h, mi):
        return None
    today = now.date()
    if db.get_setting(uid, 'reminded_on') == today.isoformat():
        return None
    db.set_setting(uid, 'reminded_on', today.isoformat())
    if any(datetime.fromisoformat(r['ts']).date() == today for r in db.list_weights(uid)):
        return None
    return line.text('⏰ 今天還沒量體重喔，量完直接輸入數字即可')


def reminder_for(uid):
    """相容舊版：今天還沒記錄就回傳提醒（不看時間）"""
    today = now_tpe().date()
    if any(datetime.fromisoformat(r['ts']).date() == today for r in db.list_weights(uid)):
        return None
    return line.text('⏰ 今天還沒量體重喔，量完直接輸入數字即可')


# ================= 趨勢圖 =================

def render_chart(rows, target, label, marks=None):
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    fp = FontProperties(fname=cfg.FONT_PATH) if os.path.exists(cfg.FONT_PATH) else None
    xs = list(range(len(rows)))
    labels = [md(r['when']) for r in rows]
    dense = len(rows) > 60

    fig = Figure(figsize=(8, 4.5), dpi=120)
    ax = fig.subplots()
    ax.plot(xs, [r['weight'] for r in rows], marker='' if dense else 'o', ms=3, lw=1.5 if dense else 2,
            color='#4A90D9', label='體重')
    avg_pts = [(x, r['avg']) for x, r in zip(xs, rows) if r['avg'] is not None]
    if len(avg_pts) >= 2:
        ax.plot(*zip(*avg_pts), marker='o', ms=2, lw=2, color='#F5A623', label='7日平均')
    if target:
        ax.axhline(target, ls='--', lw=1.5, color='#7ED321', label=f'目標 {target:g} kg')

    if marks:
        def note(i, text, color, marker, dy, ms=8):
            w = rows[i]['weight']
            ax.plot(xs[i], w, marker=marker, ms=ms, color=color, zorder=5, ls='')
            ax.annotate(text, (xs[i], w), xytext=(0, dy), textcoords='offset points',
                        ha='center', va='bottom' if dy > 0 else 'top', color=color,
                        fontproperties=fp, fontsize=9, zorder=6)
        hi, lo, goal = marks['high'], marks['low'], marks['goal']
        if hi != lo:
            note(hi, f"最高 {fw(rows[hi]['weight'])}", '#D0021B', '^', 8)
            note(lo, f"最低 {fw(rows[lo]['weight'])}", '#2E7D32', 'v', -8)
        if goal is not None:
            dy = -22 if goal == lo and hi != lo else -8
            note(goal, f"達標 {md(rows[goal]['when'])}", '#E6A100', '*', dy, ms=16)
        ax.margins(y=0.18)

    step = max(1, len(xs) // 10)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels(labels[::step])
    ax.grid(alpha=0.3)
    ax.set_title(f'體重趨勢（{label}）', fontproperties=fp, fontsize=14)
    ax.legend(prop=fp, loc='best')
    fig.tight_layout()

    cfg.CHART_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_temp()
    name = uuid.uuid4().hex + '.png'
    fig.savefig(cfg.CHART_DIR / name, format='png')
    return name


def render_body_chart(series):
    """series: {kind: [(時間, 數值), ...]}；每個項目一張子圖"""
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    fp = FontProperties(fname=cfg.FONT_PATH) if os.path.exists(cfg.FONT_PATH) else None
    colors = {'fat': '#9B59B6', 'waist': '#16A085'}
    fig = Figure(figsize=(8, 3.2 * len(series)), dpi=120)
    axes = fig.subplots(len(series), 1, squeeze=False)[:, 0]
    for ax, (kind, pts) in zip(axes, series.items()):
        name, unit, _, _ = BODY[kind]
        xs = list(range(len(pts)))
        vs = [v for _, v in pts]
        ax.plot(xs, vs, marker='o', ms=4, lw=2, color=colors[kind])
        for i in {0, len(vs) - 1, max(xs, key=lambda i: (vs[i], i)), min(xs, key=lambda i: (vs[i], -i))}:
            ax.annotate(fv(vs[i]), (xs[i], vs[i]), xytext=(0, 7), textcoords='offset points',
                        ha='center', fontsize=9, color=colors[kind])
        step = max(1, len(xs) // 8)
        ax.set_xticks(xs[::step])
        ax.set_xticklabels([md(t) for t, _ in pts][::step])
        ax.set_title(f'{name}（{unit.strip()}）', fontproperties=fp, fontsize=12)
        ax.grid(alpha=0.3)
        ax.margins(y=0.2)
    fig.tight_layout()
    cfg.CHART_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_temp()
    name = uuid.uuid4().hex + '.png'
    fig.savefig(cfg.CHART_DIR / name, format='png')
    return name


def cleanup_temp():
    """刪除過期的趨勢圖與匯出檔；回傳刪除數量"""
    now = time.time()
    removed = 0
    for folder, pattern, ttl in ((cfg.CHART_DIR, '*.png', CHART_TTL), (EXPORT_DIR, '*.csv', EXPORT_TTL)):
        if not folder.exists():
            continue
        for p in folder.glob(pattern):
            try:
                if p.stat().st_mtime < now - ttl:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


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
    v = _clean(v, 2)
    return '🔻' if v < 0 else '🔺' if v > 0 else '➖'


def fw(w):
    """體重顯示：輸入幾位小數就顯示幾位（至少 1 位），不做四捨五入"""
    s = f'{w:.2f}'
    return s[:-1] if s.endswith('0') else s


def fd(v):
    """差異顯示：帶正負號，最多 2 位小數"""
    v = _clean(v, 2)
    return ('+' if v > 0 else '') + fw(v)


def dist_value(dist):
    if dist > 0:
        return f'還差 {fw(dist)} kg'
    if dist == 0:
        return '🎉 剛好達標'
    return f'🎉 已達標（低 {fw(-dist)} kg）'


def dist_text(dist, target):
    return f'距離 {target:g} kg：{dist_value(dist)}'


def selftest():
    """健康檢查用：驗證解析與計算邏輯"""
    assert RE_RECORD.match('84.5') and RE_RECORD.match('9/20 85.2 起床後')
    assert RE_SETTINGS.match('身高:170 目標70kg').groups() == ('170', '70')
    assert body_parse('體脂 28.5 腰圍 82') == (None, [('fat', '28.5'), ('waist', '82')])
    assert body_parse('9/30 腰圍82cm') == ((9, 30), [('waist', '82')])
    assert body_parse('體脂率:25.5%') == (None, [('fat', '25.5')]) and body_parse('84.5') is None
    assert RE_TREND.match('趨勢 90').group(1) == '90' and RE_TREND.match('趨勢全部').group(1) == '全部'
    rows = compute_rows([{'id': i, 'ts': f'2026-01-0{i}T08:00:00+08:00', 'weight': wt, 'note': ''}
                         for i, wt in [(1, 90.0), (2, 89.5), (3, 89.0), (3 + 1, 89.1)]], 180, 85)
    assert rows[1]['diff'] == -0.5 and rows[1]['bmi'] == 27.6 and rows[1]['dist'] == 4.5
    assert rows[1]['avg'] is None and rows[3]['avg'] == 89.4
    assert progress(90, 87.5, 85) == 50 and progress(60, 62, 65) == 40 and progress(85, 85, 85) is None
    assert progress_value(120).endswith('🎉') and progress_value(-10).startswith('░')
    m = chart_marks([{'weight': x} for x in (90, 88, 91, 87, 87)], [], None)
    assert (m['high'], m['low'], m['goal']) == (2, 4, None)
    a = [{'weight': x} for x in (72, 70.5, 69.8, 70.2)]
    m = chart_marks(a[1:], a, 70)
    assert m['goal'] == 1 and m['goal_row']['weight'] == 69.8
    assert chart_marks(a, a, 71, losing=False)['goal'] == 0
    assert progress_value(0.49).endswith(' 0.5%') and progress_value(42.4).endswith(' 42%')
    assert RE_START.match('起始 90').group(1) == '90' and RE_START.match('起始重設').group(2)
    return True
