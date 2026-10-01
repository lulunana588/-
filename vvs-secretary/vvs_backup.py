"""VVS小秘書｜每日備份（cron 執行）

1. 用 SQLite backup API 產生一致的快照 → data/backups/，保留最近 14 份
2. 檢查快照完整性，異常就中止
3. 用 AES-256 加密後才上傳 Google Drive（金鑰只存在 VPS 的 data/backup.key）；
   上傳前先試解密比對，確認加密檔可還原
   還原：openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in 檔名.db.enc -out 還原.db -pass file:data/backup.key
4. 失敗時只推播 LINE 通知給管理者（OWNER_USER_IDS 第一位）
"""
import base64
import hashlib
import secrets
import subprocess
import tempfile
import os
import sqlite3
import sys
from datetime import datetime

import requests

import vvs_config as cfg
import vvs_jobs as jobs
import vvs_line as line

BACKUP_DIR = cfg.DATA_DIR / 'backups'
KEEP_LOCAL = 14
KEY_FILE = cfg.DATA_DIR / 'backup.key'
OPENSSL = ['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '200000']
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


def ensure_key():
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_hex(32), encoding='utf-8')
        os.chmod(KEY_FILE, 0o600)
        log('已產生新的備份金鑰 data/backup.key，請務必另外保存一份')
    os.chmod(KEY_FILE, 0o600)
    return KEY_FILE


def encrypt(path):
    """加密並驗證可解密還原；回傳加密後的 bytes"""
    key = ensure_key()
    with tempfile.TemporaryDirectory() as tmp:
        enc, dec = os.path.join(tmp, 'x.enc'), os.path.join(tmp, 'x.dec')
        subprocess.run(OPENSSL + ['-salt', '-in', str(path), '-out', enc, '-pass', f'file:{key}'],
                       check=True, capture_output=True)
        subprocess.run(OPENSSL + ['-d', '-in', enc, '-out', dec, '-pass', f'file:{key}'],
                       check=True, capture_output=True)
        digest = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
        if digest(dec) != digest(str(path)):
            raise RuntimeError('加密檔試解密後內容不一致')
        return open(enc, 'rb').read()


def upload(path, today):
    fname = f'vvs_backup_{today}.db.enc'
    payload = {'filename': fname,
               'data': base64.b64encode(encrypt(path)).decode()}
    s = requests.Session()
    s.max_redirects = 10
    r = s.post(GAS_URL, json=payload, timeout=60, allow_redirects=True)
    text = r.text[:200].replace('\n', ' ')
    ok = r.status_code == 200 and 'error' not in text.lower()
    return ok, f'{fname}｜HTTP {r.status_code} {text}'


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
    try:
        rc = main()
    finally:
        jobs.beat('backup')
    sys.exit(rc)
