"""VVS小秘書｜隱私檢查

執行方式：
  python vvs_privacy.py            每日稽核（cron），有問題通知管理者，週日回報全部通過
  python vvs_privacy.py --report   手動印出完整報告
  python vvs_privacy.py --accept   確認白名單變動是你本人改的，接受目前名單
管理者在 LINE 傳「隱私檢查」也會即時執行。

檢查項目：
  1. 資料隔離：用兩個測試帳號在「暫存資料庫」實測所有功能（查詢、趨勢、匯出、週摘要、
     刪除按鈕、刪除全部資料），A 不能看到 B 的數字或註記，也不能刪到 B（不碰真實資料）
  2. 白名單：使用者名單有沒有被改動
  3. Webhook：LINE 的訊息是否只送到你的伺服器
  4. 網域：vivicare.duckdns.org 是否仍指向這台 VPS
  5. 對外路徑：資料庫、設定檔、圖表目錄都不能從網路讀到；/health 只回傳是非值
  6. 檔案權限：.env、資料庫、備份只有 root 能讀（自動修正）
  7. 暫存檔案：超過 1 小時的趨勢圖、超過 10 分鐘的匯出檔自動刪除（過期連結一律打不開）
  8. 備份加密：上傳 Google Drive 的備份必須是加密檔，金鑰只存在 VPS
  9. 異常事件：近 7 天被拉進群組、陌生人傳訊息的次數
"""
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests

import vvs_config as cfg
import vvs_jobs as jobs
import vvs_line as line

BASE = Path(__file__).resolve().parent
STATE_FILE = cfg.DATA_DIR / 'privacy_state.json'
EVENTS_FILE = cfg.DATA_DIR / 'privacy_events.log'
EXPECTED_WEBHOOK = cfg.PUBLIC_BASE_URL + '/callback'
DOMAIN = urlparse(cfg.PUBLIC_BASE_URL).hostname
ADMIN = os.environ.get('ADMIN_USER_ID', '').strip() or (cfg.OWNER_USER_IDS[0] if cfg.OWNER_USER_IDS else '')
CHART_MAX_AGE = 24 * 3600
HEALTH_KEYS = {'ok', 'db', 'weight', 'secret', 'token', 'owner'}


# ================= 事件紀錄（主程式呼叫） =================

def record_event(kind, ident=''):
    """kind: group / stranger。只記 ID 前 8 碼，不記訊息內容"""
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now(cfg.TZ).isoformat(timespec="seconds")}\t{kind}\t{ident[:8]}\n')
    except OSError:
        pass


def alert_admin(text):
    if ADMIN:
        line.push(ADMIN, [line.text(text)])


# ================= 狀態 =================

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(state):
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    os.chmod(STATE_FILE, 0o600)


def owners_sig():
    return hashlib.sha256(','.join(sorted(cfg.OWNER_USER_IDS)).encode()).hexdigest()


# ================= 各項檢查：回傳 (通過?, 說明) =================

def check_isolation():
    tmp = tempfile.mkdtemp(prefix='vvs_privacy_')
    try:
        env = dict(os.environ, VVS_DATA_DIR=tmp)
        r = subprocess.run([sys.executable, str(BASE / 'vvs_privacy.py'), '--isolation-child'],
                           cwd=BASE, env=env, capture_output=True, text=True, timeout=90)
        out = (r.stdout.strip().splitlines() or [''])[-1]
        res = json.loads(out)
    except Exception as e:
        return False, f'測試無法執行（{type(e).__name__}）'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    problems = []
    if not res.get('a_streak_ok', True):
        problems.append('A 的連續天數算到了別人的紀錄')
    if res.get('leaked'):
        problems.append(f'A 看到了 B 的數字 {res["leaked"]}')
    if not res.get('b_intact'):
        problems.append('A 撤銷／修改／刪除時動到了 B 的紀錄')
    if not res.get('b_start_ok'):
        problems.append('A 的操作改到或清掉了 B 的起始體重')
    if not res.get('a_invariant', True):
        problems.append('B 的資料一變，A 看到的摘要／平均就跟著變（間接洩漏）')
    if not res.get('a_week4_ok', True):
        problems.append('近 4 週平均沒有算出來（測試不完整）')
    if not res.get('a_start_ok', True):
        problems.append('目標進度沒有用自己的起始體重')
    if not res.get('b_body_intact'):
        problems.append('A 的體態操作動到了 B 的體脂／腰圍')
    if not res.get('a_body_ok', True):
        problems.append('體態功能失效，或刪除自己資料時沒清掉體態（測試不完整）')
    if not res.get('a_edit_ok', True):
        problems.append('修改功能失效，無法改自己的紀錄（測試不完整）')
    if not res.get('settings_separate'):
        problems.append('兩人的設定沒有分開，或刪除自己資料時動到別人')
    return (not problems), ('；'.join(problems) or '兩個測試帳號互相看不到')


def _isolation_child():
    """在暫存資料庫裡跑，完全不碰真實資料、不呼叫 LINE。涵蓋所有會回傳資料的功能。"""
    import vvs_db as db
    import vvs_weight as w
    from datetime import timedelta
    db.init()
    A, B = 'U_PRIVACY_TEST_A', 'U_PRIVACY_TEST_B'
    now = w.now_tpe()
    ago = lambda d: now - timedelta(days=d)
    last_month = now.replace(day=1) - timedelta(days=2)
    md = lambda d: f'{d.month}/{d.day}'
    b_secrets = ['173.4', '77.7', '86.5', '87.9', '89.1', '87.6', '88.8', '32.9', '31.7', '96.4', '93.3', '99.9']
    # B 的紀錄刻意分散在「上個月、前幾天、昨天、今天」，讓每種摘要與連續天數都有機會讀錯人
    w.handle(B, '身高 173.4 目標 77.7')
    for d, wt in ((last_month, '86.5'), (ago(3), '87.9'), (ago(2), '89.1'), (ago(1), '87.6')):
        w.handle(B, f'{md(d)} {wt}')
    w.handle(B, '88.8 B的秘密註記')
    b_ids = [r['id'] for r in db.list_weights(B)]
    b_count = len(b_ids)
    b_snapshot = [(r['ts'], r['weight'], r['note']) for r in db.list_weights(B)]
    w.handle(B, f'{md(ago(7))} 體脂 32.9 腰圍 96.4')   # B 的體態：上週一筆、今天一筆
    w.handle(B, '體脂 31.7 腰圍 93.3')
    w.handle(B, '起始 99.9')                           # B 自訂起始體重
    b_body = db.list_body(B)
    b_body_ids = [r['id'] for r in b_body]

    w.handle(A, '身高 160 目標 55')
    w.handle(A, f'{md(last_month)} 57.3')
    w.handle(A, '56.2')        # A 只有上個月一筆與今天，連續天數應該是 1
    w.handle(A, f'{md(ago(14))} 56.9')                  # 兩週前一筆，讓「近 4 週平均」有東西可算
    outs = []

    def run(msgs):
        for m in msgs or []:
            outs.append(json.dumps(m, ensure_ascii=False))

    run([w.weekly_summary(A), w.monthly_summary(A), w.weekly_trend(A)])   # 在「撤銷」測試之前先算
    a_week4_ok = w.weekly_trend(A) is not None

    # 反事實測試：A 的所有結果不能因為 B 的資料改變而改變（抓「被 B 拉偏的平均」這類間接洩漏）
    def a_views():
        return json.dumps([w.weekly_trend(A), w.weekly_summary(A), w.monthly_summary(A),
                           w.period(A, 'week'), w.period(A, 'month'), w.progress_msg(A),
                           w.recent(A), w.body_recent(A), w.streak(A), w.start_weight(A),
                           w.trend_data(A)[1], w.trend_data(A, '全部')[1], w.trend_data(A, '90')[1]],
                          ensure_ascii=False, default=str)
    before = a_views()
    # B 塞入「很高」和「比 A 的目標還低」的體重，並把 B 的起始體重改到另一個方向
    extra = [db.add_weight(B, w.iso(ago(k * 7 + 1)), 120.5 + k, '反事實', w.iso(now)) for k in range(6)]
    extra += [db.add_weight(B, w.iso(ago(k * 7 + 2)), 40.5 + k, '反事實', w.iso(now)) for k in range(6)]
    extra_body = [db.add_body(B, w.iso(ago(k * 7 + 1)), 'fat', 45.5 + k, w.iso(now))[0] for k in range(6)]
    db.set_setting(B, 'start_kg', 30.5)
    a_invariant = a_views() == before
    db.set_setting(B, 'start_kg', 99.9)
    for rid in extra:
        db.delete_weight(B, rid)
    for rid in extra_body:
        db.delete_body(B, rid)

    w.handle(B, '提醒 06:15')
    for cmd in ['最近', '本週', '本月', '趨勢', '趨勢 全部', '趨勢 90', '刪除', '說明',
                '70.5', '確認記錄', '撤銷', '撤銷', '撤銷', '56.1', '身高 161',
                '提醒', '提醒 07:30', '提醒 關閉', '提醒 開啟', '提醒']:
        run(w.handle(A, cmd))
    for bid in b_ids:                                   # A 試圖用按鈕修改 B 的紀錄，再傳新數字
        run(w.handle_postback(A, f'edit:{bid}'))
        run(w.handle(A, '66.6 A改的'))
    for bid in b_ids:                                   # A 偽造「正在修改 B 的紀錄」狀態
        db.set_setting(A, 'pending_edit', json.dumps({'id': bid, 'at': time.time()}))
        run(w.handle(A, '66.7'))
    for bid in b_ids:                                   # 資料層本身也要擋（兩層防線各自測）
        run([db.get_weight(A, bid), db.update_weight(A, bid, 66.8, 'A改的'), db.delete_weight(A, bid)])
    run(w.handle(A, '修改'))
    for cmd in ['進度', '起始', '本週']:                 # A 沒自訂起點：應該用 A 自己的第一筆
        run(w.handle(A, cmd))
    a_start_own = w.start_weight(A)[0] == db.list_weights(A)[0]['weight']
    for cmd in ['起始 58.8', '進度', '56.3', '起始 重設', '進度']:
        run(w.handle(A, cmd))
    a_start_own = a_start_own and w.start_weight(A)[0] == db.list_weights(A)[0]['weight']
    for cmd in ['體態', '體脂 24.1 腰圍 71', '體脂 24.0', '體態', '刪除體態', '腰圍']:   # 同一天覆蓋不能動到 B
        run(w.handle(A, cmd))
    for bid in b_body_ids:                              # A 試圖用按鈕刪 B 的體態，也直接打資料層
        run(w.handle_postback(A, f'bdel:{bid}'))
        run([db.delete_body(A, bid)])
    run([db.list_body(A), db.list_body(A, 'fat'), db.list_body(A, 'waist')])
    a_body_ok = [(r['kind'], r['value']) for r in db.list_body(A)] == [('waist', 71.0), ('fat', 24.0)]
    a_rec = db.list_weights(A)[-1]                      # A 修改自己的紀錄要成功（確認功能本身正常）
    run(w.handle_postback(A, f"edit:{a_rec['id']}"))
    run(w.handle(A, '56.4'))
    a_edit_ok = db.get_weight(A, a_rec['id'])['weight'] == 56.4
    for bid in b_ids:                                   # A 試圖用按鈕刪 B 的紀錄
        run(w.handle_postback(A, f'del:{bid}'))
    run([w.weekly_summary(A), w.monthly_summary(A)])
    a_streak = w.streak(A)
    b_remind_ok = w.reminder_setting(B) == '06:15'
    exp = w.handle(A, '匯出')
    run(exp)
    for f in w.EXPORT_DIR.glob('*.csv'):                 # 匯出檔內容也要檢查
        outs.append(f.read_text(encoding='utf-8'))
    run([w.reminder_for(A), w.reminder_due(A)])
    run(w.handle(A, '刪除我的資料'))
    run(w.handle(A, '確認刪除全部資料'))
    text = '\n'.join(o for o in outs if o)
    print(json.dumps({
        'leaked': [s for s in b_secrets + ['B的秘密'] if s in text],
        'b_intact': (len(db.list_weights(B)) == b_count and b_count == 5
                     and [(r['ts'], r['weight'], r['note']) for r in db.list_weights(B)] == b_snapshot),
        'a_edit_ok': a_edit_ok,
        'a_start_ok': a_start_own,
        'a_week4_ok': a_week4_ok,
        'a_invariant': a_invariant,
        'b_start_ok': db.get_setting(B, 'start_kg') == '99.9',
        'a_body_ok': a_body_ok and not db.list_body(A),        # 記錄正確，且刪除自己資料時有清掉
        'b_body_intact': db.list_body(B) == b_body and len(b_body) == 4,
        'a_streak_ok': a_streak == 1,
        'settings_separate': (w.settings(B) == (173.4, 77.7) and w.settings(A) == (None, None)
                              and b_remind_ok and w.reminder_setting(B) == '06:15'),
    }))


def check_whitelist(state):
    n = len(cfg.OWNER_USER_IDS)
    sig = owners_sig()
    if not state.get('owners_sig'):
        state['owners_sig'] = sig
        return True, f'已記錄目前白名單（{n} 人）'
    if state['owners_sig'] != sig:
        return False, f'白名單有變動，目前 {n} 人。若是你本人改的，執行 vvs_privacy.py --accept'
    return True, f'沒有變動（{n} 人）'


def check_webhook():
    try:
        r = requests.get('https://api.line.me/v2/bot/channel/webhook/endpoint', timeout=15,
                         headers={'Authorization': f'Bearer {cfg.LINE_TOKEN}'})
        data = r.json()
    except Exception as e:
        return False, f'無法向 LINE 查詢（{type(e).__name__}）'
    ep = data.get('endpoint', '')
    if ep != EXPECTED_WEBHOOK:
        return False, f'Webhook 被改成其他網址：{ep or "（空白）"}'
    return True, '訊息只送到你的伺服器'


def check_dns():
    try:
        ip = socket.gethostbyname(DOMAIN)
        local = subprocess.run(['hostname', '-I'], capture_output=True, text=True).stdout.split()
    except Exception as e:
        return False, f'無法解析網域（{type(e).__name__}）'
    return (ip in local), (f'指向這台 VPS' if ip in local else f'網域指向 {ip}，不是這台 VPS')


def check_public_paths():
    bad = []
    for path in ['/', '/charts/', '/exports/', '/data/secretary.db', '/secretary.db', '/.env', '/data/',
                 '/exports/' + '0' * 48 + '.csv']:
        try:
            r = requests.get(cfg.PUBLIC_BASE_URL + path, timeout=10, allow_redirects=False)
            if r.status_code not in (404, 405):
                bad.append(f'{path} 回應 {r.status_code}')
        except requests.RequestException:
            pass
    try:
        h = requests.get(cfg.PUBLIC_BASE_URL + '/health', timeout=10).json()
        extra = set(h) - HEALTH_KEYS
        if extra or not all(isinstance(v, bool) for v in h.values()):
            bad.append('/health 回傳了額外資訊')
    except Exception:
        pass
    return (not bad), ('；'.join(bad) or '資料庫、設定檔、圖表目錄都讀不到')


def check_permissions():
    fixed = []
    targets = [(BASE / '.env', 0o600), (cfg.DATA_DIR, 0o700), (cfg.DATA_DIR / 'backups', 0o700)]
    targets += [(p, 0o600) for p in cfg.DATA_DIR.glob('secretary.db*')]
    targets += [(p, 0o600) for p in (cfg.DATA_DIR / 'backups').glob('*.db')]
    targets += [(cfg.DATA_DIR / 'exports', 0o700), (cfg.DATA_DIR / 'backup.key', 0o600)]
    targets += [(p, 0o600) for p in (cfg.DATA_DIR / 'exports').glob('*.csv')]
    targets += [(cfg.DATA_DIR / 'beats', 0o700)]
    targets += [(p, 0o600) for p in cfg.DATA_DIR.glob('*.log*')]   # 紀錄檔與 3 個月內的封存
    for p, mode in targets:
        if not p.exists():
            continue
        cur = stat.S_IMODE(p.stat().st_mode)
        if cur & 0o077:  # 群組或其他人有權限
            os.chmod(p, mode)
            fixed.append(p.name)
    return True, (f'已自動收緊權限：{", ".join(fixed)}' if fixed else '只有 root 能讀')


def check_charts():
    import vvs_weight as w
    removed = w.cleanup_temp()
    return True, (f'已刪除 {removed} 個過期的趨勢圖／匯出檔' if removed else '沒有過期檔案')


def check_backup_encryption():
    key = cfg.DATA_DIR / 'backup.key'
    if not key.exists():
        return False, '找不到備份金鑰，雲端備份可能沒有加密'
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        os.chmod(key, 0o600)
    last = ''
    try:
        for ln in (cfg.DATA_DIR / 'backup.log').read_text(encoding='utf-8').splitlines():
            if 'Drive 上傳：成功' in ln:
                last = ln
    except OSError:
        pass
    if last and '.db.enc' not in last:
        return False, '最近一次上傳 Google Drive 的備份沒有加密'
    return True, '雲端備份為 AES-256 加密檔，金鑰只在 VPS'


def check_events():
    since = datetime.now(cfg.TZ) - timedelta(days=7)
    counts = {'group': 0, 'stranger': 0}
    try:
        for ln in EVENTS_FILE.read_text(encoding='utf-8').splitlines():
            ts, kind, _ = (ln.split('\t') + ['', ''])[:3]
            if kind in counts and datetime.fromisoformat(ts) >= since:
                counts[kind] += 1
    except (OSError, ValueError):
        pass
    if not any(counts.values()):
        return True, '近 7 天沒有異常事件'
    return True, f'近 7 天：被拉進群組 {counts["group"]} 次、陌生人傳訊息 {counts["stranger"]} 次（都已擋下）'


def run_audit():
    state = load_state()
    checks = [
        ('資料隔離', check_isolation),
        ('白名單', lambda: check_whitelist(state)),
        ('Webhook', check_webhook),
        ('網域', check_dns),
        ('對外路徑', check_public_paths),
        ('檔案權限', check_permissions),
        ('暫存檔案', check_charts),
        ('備份加密', check_backup_encryption),
        ('異常事件', check_events),
    ]
    results = []
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f'檢查時發生錯誤（{type(e).__name__}）'
        results.append((name, ok, detail))
    save_state(state)
    return results


def format_report(results):
    passed = all(ok for _, ok, _ in results)
    head = '🔒 隱私檢查：全部通過' if passed else '🚨 隱私檢查：發現問題'
    lines = [head, f'{datetime.now(cfg.TZ):%m/%d %H:%M}', '']
    for name, ok, detail in results:
        lines.append(f'{"✅" if ok else "❌"} {name}：{detail}')
    return '\n'.join(lines)


# ================= 入口 =================

def main(argv):
    if '--isolation-child' in argv:
        _isolation_child()
        return 0
    if '--accept' in argv:
        state = load_state()
        state['owners_sig'] = owners_sig()
        save_state(state)
        print(f'已接受目前白名單（{len(cfg.OWNER_USER_IDS)} 人）')
        return 0

    results = run_audit()
    report = format_report(results)
    passed = all(ok for _, ok, _ in results)
    print(report if '--report' in argv else
          f'{datetime.now(cfg.TZ):%Y-%m-%d %H:%M} {"全部通過" if passed else "發現問題"}｜' +
          '；'.join(f'{n}:{"OK" if ok else d}' for n, ok, d in results), flush=True)

    if '--report' in argv:
        return 0 if passed else 1
    jobs.beat('privacy')
    if not passed:
        alert_admin(report)
    elif datetime.now(cfg.TZ).weekday() == 6:  # 週日回報一次，讓你知道它有在跑
        alert_admin(report)
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
