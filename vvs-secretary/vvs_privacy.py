"""VVS小秘書｜隱私檢查

執行方式：
  python vvs_privacy.py            每日稽核（cron），有問題通知管理者，週日回報全部通過
  python vvs_privacy.py --report   手動印出完整報告
  python vvs_privacy.py --accept   確認白名單變動是你本人改的，接受目前名單
管理者在 LINE 傳「隱私檢查」也會即時執行。

檢查項目：
  1. 資料隔離：用兩個測試帳號在「暫存資料庫」實測，A 的任何查詢都不能出現 B 的數字，
     A 撤銷也不能刪到 B（不碰真實資料）
  2. 白名單：使用者名單有沒有被改動
  3. Webhook：LINE 的訊息是否只送到你的伺服器
  4. 網域：vivicare.duckdns.org 是否仍指向這台 VPS
  5. 對外路徑：資料庫、設定檔、圖表目錄都不能從網路讀到；/health 只回傳是非值
  6. 檔案權限：.env、資料庫、備份只有 root 能讀（自動修正）
  7. 趨勢圖：超過 24 小時的圖檔自動刪除
  8. 異常事件：近 7 天被拉進群組、陌生人傳訊息的次數
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
    if res.get('leaked'):
        problems.append(f'A 看到了 B 的數字 {res["leaked"]}')
    if not res.get('b_intact'):
        problems.append('A 撤銷時動到了 B 的紀錄')
    if not res.get('settings_separate'):
        problems.append('兩人的身高／目標沒有分開')
    return (not problems), ('；'.join(problems) or '兩個測試帳號互相看不到')


def _isolation_child():
    """在暫存資料庫裡跑，完全不碰真實資料、不呼叫 LINE"""
    import vvs_db as db
    import vvs_weight as w
    db.init()
    A, B = 'U_PRIVACY_TEST_A', 'U_PRIVACY_TEST_B'
    b_secrets = ['173.4', '77.7', '88.8', '89.9']
    w.handle(B, '身高 173.4 目標 77.7')
    w.handle(B, '9/1 89.9')
    w.handle(B, '88.8')
    w.handle(A, '身高 160 目標 55')
    w.handle(A, '56.2')
    outs = []
    for cmd in ['最近', '本週', '本月', '趨勢', '撤銷', '撤銷', '最近', '56.1', '說明', '身高 161']:
        for m in w.handle(A, cmd) or []:
            outs.append(m.get('text', '') + ' ' + m.get('originalContentUrl', ''))
    text = '\n'.join(outs)
    print(json.dumps({
        'leaked': [s for s in b_secrets if s in text],
        'b_intact': len(db.list_weights(B)) == 2,
        'settings_separate': w.settings(B) == (173.4, 77.7) and w.settings(A)[0] == 161.0,
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
    for path in ['/', '/charts/', '/data/secretary.db', '/secretary.db', '/.env', '/data/']:
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
    for p, mode in targets:
        if not p.exists():
            continue
        cur = stat.S_IMODE(p.stat().st_mode)
        if cur & 0o077:  # 群組或其他人有權限
            os.chmod(p, mode)
            fixed.append(p.name)
    return True, (f'已自動收緊權限：{", ".join(fixed)}' if fixed else '只有 root 能讀')


def check_charts():
    cutoff = time.time() - CHART_MAX_AGE
    removed = 0
    for p in cfg.CHART_DIR.glob('*.png') if cfg.CHART_DIR.exists() else []:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return True, (f'已刪除 {removed} 張過期圖檔' if removed else '沒有過期圖檔')


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
        ('趨勢圖', check_charts),
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
    if not passed:
        alert_admin(report)
    elif datetime.now(cfg.TZ).weekday() == 6:  # 週日回報一次，讓你知道它有在跑
        alert_admin(report)
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
