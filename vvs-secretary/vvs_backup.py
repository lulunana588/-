"""VVS小秘書｜每日備份（cron 執行）

1. 用 SQLite backup API 產生一致的快照 → data/backups/，保留最近 14 份
2. 檢查快照完整性，異常就中止
3. 用 AES-256 加密後才上傳 Google Drive（金鑰只存在 VPS 的 data/backup.key）；
   上傳給 GAS（vvs_backup_gas.gs）時要帶通行碼 BACKUP_GAS_TOKEN；雲端只保留 60 天；
   上傳前先試解密比對，確認加密檔可還原
   還原：openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in 檔名.db.enc -out 還原.db -pass file:data/backup.key
4. 失敗時只推播 LINE 通知給管理者（OWNER_USER_IDS 第一位）

每月還原演練（由每小時的健康檢查在每月第一次執行時呼叫；也可手動：python vvs_backup.py --drill）：
  取最新一份本機備份 → 在暫存區還原 → 檢查完整性、資料表、每個帳號的紀錄都讀得出來
  → 再做一次加密→解密比對 → 確認金鑰沒有被換過
  → 從雲端下載最新一份，用金鑰解密並檢查完整性（確認雲端那份真的打得開）（金鑰一換，舊的雲端備份就要用舊金鑰才打得開）
  通知只說「通過／哪一項失敗」，不含任何人的筆數或數字。
"""
import base64
import hashlib
import shutil
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
GAS_TOKEN = os.environ.get('BACKUP_GAS_TOKEN', '').strip()
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


def gas_call(action, timeout=60, **kw):
    """呼叫雲端備份 GAS；回傳 dict（失敗時 ok=False 並附 error）"""
    s = requests.Session()
    s.max_redirects = 10
    r = s.post(GAS_URL, json={'token': GAS_TOKEN, 'action': action, **kw},
               timeout=timeout, allow_redirects=True)
    try:
        res = r.json()
    except ValueError:
        return {'ok': False, 'error': f'HTTP {r.status_code}，回應不是 JSON（GAS 網址可能失效）'}
    return res if isinstance(res, dict) else {'ok': False, 'error': '回應格式不對'}


def upload(path, today):
    fname = f'vvs_backup_{today}.db.enc'
    res = gas_call('upload', filename=fname, data=base64.b64encode(encrypt(path)).decode())
    if not res.get('ok'):
        return False, f"{fname}｜{res.get('error', '未知錯誤')}"
    pruned = res.get('pruned') or 0
    return True, f"{fname}｜{res.get('size')} bytes" + (f'｜清掉 {pruned} 份 60 天前的舊檔' if pruned else '')


# ================= 每月還原演練 =================

DRILL_MAX_AGE_DAYS = 2
KEY_FP_FILE = cfg.DATA_DIR / 'backup.key.fp'
TABLES = ('weight_log', 'body_log', 'settings')


def key_fingerprint(key_file):
    return hashlib.sha256(open(key_file, 'rb').read().strip()).hexdigest()[:8]


def _ids(path, table, uid, before=None):
    c = sqlite3.connect(path)
    try:
        q, args = f'SELECT id FROM {table} WHERE user_id=?', [uid]
        if before:
            q, args = q + ' AND created_at <= ?', args + [before]
        return {r[0] for r in c.execute(q, args)}
    finally:
        c.close()


def drill(db_path, backup_dir, key_file, fp_file, owners, now):
    """回傳 (是否通過, 給管理者看的訊息)；訊息不含任何人的筆數或數字"""
    files = sorted(backup_dir.glob('secretary_*.db'))
    if not files:
        return False, '找不到任何本機備份'
    src = files[-1]
    day = datetime.strptime(src.stem.split('_')[1], '%Y%m%d').replace(tzinfo=cfg.TZ)
    if (now - day).days > DRILL_MAX_AGE_DAYS:
        return False, f'最新的備份是 {day:%m/%d}，已經超過 {DRILL_MAX_AGE_DAYS} 天沒有新備份'
    snap_time = datetime.fromtimestamp(src.stat().st_mtime, cfg.TZ).isoformat(timespec='seconds')
    with tempfile.TemporaryDirectory() as tmp:
        restored = os.path.join(tmp, 'restored.db')
        shutil.copy(src, restored)
        c = sqlite3.connect(restored)
        try:
            ok = c.execute('PRAGMA integrity_check').fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        if ok != 'ok':
            return False, f'還原後的資料庫完整性異常（{ok}）'
        missing = [t for t in TABLES if t not in tables]
        if missing and missing != ['body_log']:       # 體態表是 10 月才加的，舊備份可以沒有
            return False, f'還原後少了資料表：{"、".join(missing)}'
        for n, uid in enumerate(owners, 1):
            for table in TABLES[:2]:
                if table not in tables:
                    continue
                live = _ids(db_path, table, uid, before=snap_time)
                if not live <= _ids(restored, table, uid):
                    return False, f'第 {n} 個帳號有紀錄在備份裡讀不到（{table}）'
    try:
        encrypt(src)
    except Exception as e:
        return False, f'加密→解密還原比對失敗：{e}'
    fp = key_fingerprint(key_file)
    old = fp_file.read_text(encoding='utf-8').strip() if fp_file.exists() else ''
    if old and old != fp:
        return False, (f'備份金鑰被換過（指紋 {old} → {fp}）\n'
                       '之前上傳到雲端的備份要用舊金鑰才打得開，請確認舊金鑰還有保存')
    fp_file.write_text(fp, encoding='utf-8')
    os.chmod(fp_file, 0o600)
    return True, (f'✅ 每月備份還原演練通過\n備份：{day:%m/%d}\n'
                  '・還原後資料庫完整、資料表齊全\n'
                  '・每個帳號的紀錄都能完整讀出\n'
                  '・加密 → 解密還原內容一致\n'
                  f'・金鑰指紋：{fp}\n'
                  '（請確認你另外保存的那份金鑰，指紋也是這個）')


def cloud_check(fetch, key_file, now):
    """從雲端下載最新一份 → 用金鑰解密 → 檢查完整性。回傳 (是否通過, 說明)"""
    res = fetch()
    if not res.get('ok'):
        return False, f"雲端備份下載失敗：{res.get('error', '未知錯誤')}"
    name = res.get('name', '')
    try:
        day = datetime.strptime(name.split('_')[2][:8], '%Y%m%d').replace(tzinfo=cfg.TZ)
    except (IndexError, ValueError):
        return False, f'雲端備份檔名不對：{name[:40]}'
    if (now - day).days > DRILL_MAX_AGE_DAYS:
        return False, f'雲端最新的備份是 {day:%m/%d}，超過 {DRILL_MAX_AGE_DAYS} 天沒有上傳成功'
    with tempfile.TemporaryDirectory() as tmp:
        enc, dec = os.path.join(tmp, 'c.enc'), os.path.join(tmp, 'c.db')
        with open(enc, 'wb') as f:
            f.write(base64.b64decode(res.get('data', '')))
        r = subprocess.run(OPENSSL + ['-d', '-in', enc, '-out', dec, '-pass', f'file:{key_file}'],
                           capture_output=True)
        if r.returncode != 0:
            return False, '雲端備份用目前的金鑰解不開'
        c = sqlite3.connect(dec)
        try:
            ok = c.execute('PRAGMA integrity_check').fetchone()[0]
            tables = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as e:
            return False, f'雲端備份解密後不是正常的資料庫（{e}）'
        finally:
            c.close()
        if ok != 'ok' or 'weight_log' not in tables:
            return False, '雲端備份解密後資料庫不完整'
    return True, f'・雲端最新一份（{day:%m/%d}）可下載、可解密、資料庫完整'


def run_drill():
    now = datetime.now(cfg.TZ)
    ok, msg = drill(cfg.DB_PATH, BACKUP_DIR, ensure_key(), KEY_FP_FILE, cfg.OWNER_USER_IDS, now)
    if ok:
        if GAS_URL and GAS_TOKEN:
            try:
                cok, cmsg = cloud_check(lambda: gas_call('latest', timeout=120), KEY_FILE, now)
            except Exception as e:
                cok, cmsg = False, f'雲端備份下載失敗：{e}'
        else:
            cok, cmsg = False, '還沒設定雲端備份（BACKUP_GAS_URL／BACKUP_GAS_TOKEN）'
        if cok:
            msg = msg.replace('\n・金鑰指紋', '\n' + cmsg + '\n・金鑰指紋')
        else:
            ok, msg = False, f'本機備份沒問題，但雲端那份有狀況：\n{cmsg}'
    log(('還原演練通過' if ok else '還原演練失敗：') + ('' if ok else msg))
    if ADMIN:
        line.push(ADMIN, [line.text(msg if ok else '⚠️ 小秘書每月備份還原演練失敗\n' + msg)])
    jobs.beat('drill')
    return ok


def drill_selftest():
    """部署用：在暫存目錄建一個假資料庫，確認演練抓得到「讀不到的紀錄」和「換過金鑰」"""
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        live, bdir, key, fp = d / 'live.db', d / 'backups', d / 'k', d / 'fp'
        bdir.mkdir()
        key.write_text(secrets.token_hex(32))
        old = '2000-01-01T00:00:00+08:00'
        c = sqlite3.connect(live)
        c.executescript('CREATE TABLE weight_log(id INTEGER PRIMARY KEY, user_id, ts, weight, note, created_at);'
                        'CREATE TABLE body_log(id INTEGER PRIMARY KEY, user_id, ts, kind, value, created_at);'
                        'CREATE TABLE settings(user_id, key, value);')
        c.executemany('INSERT INTO weight_log(user_id, ts, weight, note, created_at) VALUES (?,?,?,?,?)',
                      [('A', old, 60, '', old), ('B', old, 70, '', old)])
        c.commit()
        c.close()
        now = datetime.now(cfg.TZ)
        snap = bdir / f'secretary_{now:%Y%m%d}.db'
        global KEY_FILE
        real_key, KEY_FILE = KEY_FILE, key
        try:
            shutil.copy(live, snap)
            assert drill(live, bdir, key, fp, ['A', 'B'], now)[0]
            c = sqlite3.connect(live)                         # 備份後才新增的不算缺
            c.execute('INSERT INTO weight_log(user_id, ts, weight, note, created_at) VALUES (?,?,?,?,?)',
                      ('B', old, 71, '', '2999-01-01T00:00:00+08:00'))
            c.commit()
            c.close()
            assert drill(live, bdir, key, fp, ['A', 'B'], now)[0]
            c = sqlite3.connect(snap)                         # 備份裡少了 B 的一筆 → 要抓到
            c.execute("DELETE FROM weight_log WHERE user_id='B'")
            c.commit()
            c.close()
            ok, msg = drill(live, bdir, key, fp, ['A', 'B'], now)
            assert not ok and '第 2 個帳號' in msg and '70' not in msg
            shutil.copy(live, snap)
            key.write_text(secrets.token_hex(32))             # 換金鑰 → 要抓到
            assert not drill(live, bdir, key, fp, ['A', 'B'], now)[0]

            # 雲端驗證：正常的加密檔要通過；用別把金鑰加密的、太舊的、下載失敗的都要抓到
            good = {'ok': True, 'name': f'vvs_backup_{now:%Y%m%d}.db.enc',
                    'data': base64.b64encode(encrypt(live)).decode()}
            assert cloud_check(lambda: good, key, now)[0]
            other = d / 'k2'
            other.write_text(secrets.token_hex(32))
            assert not cloud_check(lambda: good, other, now)[0]
            assert not cloud_check(lambda: {**good, 'name': 'vvs_backup_20000101.db.enc'}, key, now)[0]
            assert not cloud_check(lambda: {'ok': False, 'error': 'unauthorized'}, key, now)[0]
        finally:
            KEY_FILE = real_key
    return True


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

    if not GAS_URL or not GAS_TOKEN:
        alert('還沒設定 BACKUP_GAS_URL／BACKUP_GAS_TOKEN，今天沒有上傳雲端（本機備份仍有保留）。')
        return 1
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
    if '--drill' in sys.argv:
        jobs.monthly_due('drill')          # 手動跑過，這個月健康檢查就不再自動跑一次
        sys.exit(0 if run_drill() else 1)
    try:
        rc = main()
    finally:
        jobs.beat('backup')
    sys.exit(rc)
