"""VVS小秘書｜每日備份（cron 執行）

1. 用 SQLite backup API 產生一致的快照 → data/backups/，保留最近 14 份
2. 檢查快照完整性，異常就中止
3. 上傳到 Google Drive（沿用 diary-bot 的 GAS 端點格式：{"filename", "data"}）
4. 失敗時只推播 LINE 通知給管理者（OWNER_USER_IDS 第一位）
"""
import base64
import os
import sqlite3
import sys
from datetime import datetime

import requests

import vvs_config as cfg
import vvs_line as line

BACKUP_DIR = cfg.DATA_DIR / 'backups'
KEEP_LOCAL = 14
GAS_URL = os.environ.get('BACKUP_GAS_URL', '').strip()
ADMIN = os.environ.get('ADMIN_USER_ID', '').strip() or (cfg.OWNER_USER_IDS[0] if cfg.OWNER_USER_IDS else '')


def log(msg):
    print(f'{datetime.now(cfg.TZ):%Y-%m-%d %H:%M:%S} {msg}', flush=True)


def alert(msg):
    log(msg)
    if ADMIN:
        line.push(ADMIN, [line.text('⚠️ 小秘書備份通知\n' + msg)])


def snapshot(today):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f'secretary_{today}.db'
    src = sqlite3.connect(cfg.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def check(path):
    c = sqlite3.connect(path)
    try:
        ok = c.execute('PRAGMA integrity_check').fetchone()[0]
        rows = c.execute('SELECT COUNT(*) FROM weight_log').fetchone()[0]
    finally:
        c.close()
    return ok == 'ok', ok, rows


def prune():
    files = sorted(BACKUP_DIR.glob('secretary_*.db'))
    for p in files[:-KEEP_LOCAL]:
        p.unlink()


def upload(path, today):
    payload = {'filename': f'vvs_backup_{today}.db',
               'data': base64.b64encode(path.read_bytes()).decode()}
    s = requests.Session()
    s.max_redirects = 10
    r = s.post(GAS_URL, json=payload, timeout=60, allow_redirects=True)
    text = r.text[:200].replace('\n', ' ')
    ok = r.status_code == 200 and 'error' not in text.lower()
    return ok, f'HTTP {r.status_code} {text}'


def main():
    today = datetime.now(cfg.TZ).strftime('%Y%m%d')

    if not cfg.DB_PATH.exists():
        alert('找不到資料庫檔案，備份中止。')
        return 1

    try:
        snap = snapshot(today)
    except Exception as e:
        alert(f'建立本機備份失敗：{e}')
        return 1

    healthy, detail, rows = check(snap)
    log(f'本機備份：{snap.name}（{snap.stat().st_size} bytes，體重紀錄 {rows} 筆，完整性 {detail}）')
    if not healthy:
        alert(f'資料庫完整性異常（{detail}），已中止上傳 Drive。')
        return 1
    prune()

    if not GAS_URL:
        log('未設定 BACKUP_GAS_URL，略過 Drive 上傳')
        return 0
    try:
        ok, result = upload(snap, today)
    except Exception as e:
        ok, result = False, str(e)
    log(f'Drive 上傳：{"成功" if ok else "失敗"}｜{result}')
    if not ok:
        alert(f'上傳 Google Drive 失敗，本機備份仍有保留。\n{result[:150]}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
