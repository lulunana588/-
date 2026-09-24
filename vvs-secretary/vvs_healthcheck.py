"""VVS小秘書｜健康檢查（cron 每小時執行）

檢查項目：
  1. systemd 服務是否在跑
  2. 對外 HTTPS /health（一次驗證 DNS、Caddy、憑證、程式、資料庫）
  3. LINE token 是否有效（呼叫 bot info，不會發訊息）
  4. 最近一次備份是否在 30 小時內
  5. 磁碟剩餘空間

服務或 /health 異常時會先自動重啟一次再複查。
通知只推播給管理者（ADMIN_USER_ID，預設 OWNER_USER_IDS 第一位）：
  - 從正常變異常、或異常項目改變：立刻通知
  - 持續異常：每 6 小時提醒一次
  - 恢復正常：通知一次
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

import requests

import vvs_config as cfg
import vvs_line as line

SERVICE = 'vvs-secretary'
HEALTH_URL = cfg.PUBLIC_BASE_URL + '/health'
STATE_FILE = cfg.DATA_DIR / 'health_state.json'
BACKUP_DIR = cfg.DATA_DIR / 'backups'
BACKUP_MAX_AGE_H = 30
DISK_MIN_GB = 1.0
REMIND_EVERY_SEC = 6 * 3600
ADMIN = os.environ.get('ADMIN_USER_ID', '').strip() or (cfg.OWNER_USER_IDS[0] if cfg.OWNER_USER_IDS else '')


def log(msg):
    print(f'{datetime.now(cfg.TZ):%Y-%m-%d %H:%M} {msg}', flush=True)


# ---------------- 各項檢查：回傳 None 代表正常，否則回傳問題描述 ----------------

def check_service():
    r = subprocess.run(['systemctl', 'is-active', SERVICE], capture_output=True, text=True)
    state = r.stdout.strip()
    return None if state == 'active' else f'服務狀態 {state}'


def check_health_endpoint():
    try:
        r = requests.get(HEALTH_URL, timeout=15)
    except requests.RequestException as e:
        return f'連不到 {HEALTH_URL}（{type(e).__name__}）'
    try:
        data = r.json()
    except ValueError:
        return f'/health 回應 HTTP {r.status_code}，不是 JSON'
    if r.status_code == 200 and data.get('ok'):
        return None
    bad = [k for k, v in data.items() if k != 'ok' and v is not True]
    return f'/health 異常（HTTP {r.status_code}）：{", ".join(bad) or "未知"}'


def check_line_token():
    try:
        r = requests.get('https://api.line.me/v2/bot/info', timeout=15,
                         headers={'Authorization': f'Bearer {cfg.LINE_TOKEN}'})
    except requests.RequestException as e:
        return f'連不到 LINE API（{type(e).__name__}）'
    if r.status_code == 200:
        return None
    if r.status_code == 401:
        return 'LINE token 失效，需到 LINE Developers 重新發行'
    return f'LINE API 回應 HTTP {r.status_code}'


def check_backup():
    files = sorted(BACKUP_DIR.glob('secretary_*.db'), key=lambda p: p.stat().st_mtime)
    if not files:
        return '找不到任何本機備份'
    age_h = (time.time() - files[-1].stat().st_mtime) / 3600
    return None if age_h <= BACKUP_MAX_AGE_H else f'最近一次備份已是 {age_h:.0f} 小時前'


def check_disk():
    free_gb = shutil.disk_usage('/').free / 1024 ** 3
    return None if free_gb >= DISK_MIN_GB else f'磁碟剩 {free_gb:.1f} GB'


CHECKS = [
    ('服務', check_service),
    ('網址', check_health_endpoint),
    ('LINE', check_line_token),
    ('備份', check_backup),
    ('磁碟', check_disk),
]


def run_checks():
    problems = {}
    for name, fn in CHECKS:
        try:
            err = fn()
        except Exception as e:
            err = f'檢查時發生錯誤：{e}'
        if err:
            problems[name] = err
    return problems


# ---------------- 狀態與通知 ----------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'failing': False, 'last_alert': 0}


def save_state(state):
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')


def notify(text):
    if not ADMIN:
        log('未設定管理者，無法推播')
        return False
    ok = line.push(ADMIN, [line.text(text)])
    if not ok:
        log('LINE 推播失敗（可能是 token 或網路問題）')
    return ok


def main():
    problems = run_checks()
    healed = ''

    # 服務或網址出問題 → 自動重啟一次再複查
    if 'LINE' not in problems and ({'服務', '網址'} & problems.keys()):
        log('偵測到服務異常，嘗試自動重啟：' + '；'.join(problems.values()))
        subprocess.run(['systemctl', 'restart', SERVICE])
        time.sleep(8)
        before = problems
        problems = run_checks()
        if not ({'服務', '網址'} & problems.keys()):
            healed = '（剛剛自動重啟後已恢復：' + '；'.join(v for k, v in before.items() if k in ('服務', '網址')) + '）'

    state = load_state()
    now = time.time()

    if problems:
        detail = '\n'.join(f'・{k}：{v}' for k, v in problems.items())
        log('異常 ' + '；'.join(problems.values()))
        keys = sorted(problems)
        first = not state.get('failing')
        changed = keys != state.get('last_keys')
        if first or changed or now - state.get('last_alert', 0) >= REMIND_EVERY_SEC:
            title = ('🚨 小秘書健康檢查異常' if first or changed
                     else '🚨 小秘書仍然異常（每 6 小時提醒）')
            notify(f'{title}\n{detail}')
            state['last_alert'] = now
            state['last_keys'] = keys
        state['failing'] = True
    else:
        log('正常' + healed)
        if state.get('failing'):
            notify('✅ 小秘書已恢復正常')
        elif healed:
            notify('🔧 小秘書剛剛當掉，已自動重啟恢復\n' + healed.strip('（）'))
        state['failing'] = False
        state['last_keys'] = []

    save_state(state)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
