"""VVS小秘書 SQLite 資料層"""
import sqlite3
import time
from contextlib import contextmanager

import vvs_config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS weight_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    ts         TEXT NOT NULL,          -- ISO 8601，台北時間 +08:00
    weight     REAL NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_user_ts ON weight_log(user_id, ts);

CREATE TABLE IF NOT EXISTS body_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    ts         TEXT NOT NULL,          -- ISO 8601，台北時間 +08:00
    kind       TEXT NOT NULL,          -- fat（體脂 %）／waist（腰圍 cm）
    value      REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_body_user_ts ON body_log(user_id, ts);

CREATE TABLE IF NOT EXISTS settings (
    user_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    seen_at  INTEGER NOT NULL
);
"""


def _conn():
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(cfg.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def tx():
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with tx() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)


def ping():
    with tx() as c:
        c.execute('SELECT COUNT(*) FROM weight_log').fetchone()


# ---------- 體重 ----------

def add_weight(uid, ts, weight, note, created_at):
    with tx() as c:
        cur = c.execute(
            'INSERT INTO weight_log(user_id, ts, weight, note, created_at) VALUES (?,?,?,?,?)',
            (uid, ts, weight, note, created_at))
        return cur.lastrowid


def list_weights(uid):
    with tx() as c:
        rows = c.execute(
            'SELECT id, ts, weight, note FROM weight_log WHERE user_id=? ORDER BY ts, id',
            (uid,)).fetchall()
    return [dict(r) for r in rows]


def delete_last(uid):
    """刪除最後新增的一筆（依新增順序，不是依日期）"""
    with tx() as c:
        row = c.execute(
            'SELECT id, ts, weight, note FROM weight_log WHERE user_id=? ORDER BY id DESC LIMIT 1',
            (uid,)).fetchone()
        if row:
            c.execute('DELETE FROM weight_log WHERE id=?', (row['id'],))
    return dict(row) if row else None


def delete_weight(uid, record_id):
    """刪除指定的一筆；只刪得到自己的（user_id 必須相符）"""
    with tx() as c:
        row = c.execute(
            'SELECT id, ts, weight, note FROM weight_log WHERE id=? AND user_id=?',
            (record_id, uid)).fetchone()
        if row:
            c.execute('DELETE FROM weight_log WHERE id=? AND user_id=?', (record_id, uid))
    return dict(row) if row else None


def get_weight(uid, record_id):
    """讀取指定的一筆；只讀得到自己的（user_id 必須相符）"""
    with tx() as c:
        row = c.execute(
            'SELECT id, ts, weight, note FROM weight_log WHERE id=? AND user_id=?',
            (record_id, uid)).fetchone()
    return dict(row) if row else None


def update_weight(uid, record_id, weight, note):
    """修改指定的一筆（體重與註記，日期不變）；只改得到自己的。回傳修改前的資料"""
    with tx() as c:
        row = c.execute(
            'SELECT id, ts, weight, note FROM weight_log WHERE id=? AND user_id=?',
            (record_id, uid)).fetchone()
        if row:
            c.execute('UPDATE weight_log SET weight=?, note=? WHERE id=? AND user_id=?',
                      (weight, note, record_id, uid))
    return dict(row) if row else None


def wipe_user(uid):
    """刪除這個人的全部體重、體態紀錄與設定；回傳刪除的紀錄筆數"""
    with tx() as c:
        n = c.execute('DELETE FROM weight_log WHERE user_id=?', (uid,)).rowcount
        n += c.execute('DELETE FROM body_log WHERE user_id=?', (uid,)).rowcount
        c.execute('DELETE FROM settings WHERE user_id=?', (uid,))
    return n


# ---------- 體態（體脂、腰圍） ----------

def add_body(uid, ts, kind, value, created_at):
    """同一天同一項目只留一筆：新的會取代舊的。回傳 (新 id, 被取代的舊值或 None)"""
    with tx() as c:
        old = c.execute(
            'SELECT id, value FROM body_log WHERE user_id=? AND kind=? AND substr(ts,1,10)=?',
            (uid, kind, ts[:10])).fetchone()
        if old:
            c.execute('DELETE FROM body_log WHERE id=? AND user_id=?', (old['id'], uid))
        cur = c.execute(
            'INSERT INTO body_log(user_id, ts, kind, value, created_at) VALUES (?,?,?,?,?)',
            (uid, ts, kind, value, created_at))
        return cur.lastrowid, (old['value'] if old else None)


def list_body(uid, kind=None):
    with tx() as c:
        if kind:
            rows = c.execute('SELECT id, ts, kind, value FROM body_log WHERE user_id=? AND kind=? '
                             'ORDER BY ts, id', (uid, kind)).fetchall()
        else:
            rows = c.execute('SELECT id, ts, kind, value FROM body_log WHERE user_id=? '
                             'ORDER BY ts, id', (uid,)).fetchall()
    return [dict(r) for r in rows]


def delete_body(uid, record_id):
    """刪除指定的一筆體態紀錄；只刪得到自己的"""
    with tx() as c:
        row = c.execute('SELECT id, ts, kind, value FROM body_log WHERE id=? AND user_id=?',
                        (record_id, uid)).fetchone()
        if row:
            c.execute('DELETE FROM body_log WHERE id=? AND user_id=?', (record_id, uid))
    return dict(row) if row else None


# ---------- 設定 ----------

def get_setting(uid, key, default=None):
    with tx() as c:
        row = c.execute('SELECT value FROM settings WHERE user_id=? AND key=?', (uid, key)).fetchone()
    return row['value'] if row else default


def set_setting(uid, key, value):
    with tx() as c:
        c.execute(
            'INSERT INTO settings(user_id, key, value) VALUES (?,?,?) '
            'ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value',
            (uid, key, str(value)))


def delete_setting(uid, key):
    with tx() as c:
        c.execute('DELETE FROM settings WHERE user_id=? AND key=?', (uid, key))


# ---------- Webhook 去重 ----------

def mark_event(event_id):
    """第一次看到回傳 True；LINE 重送的重複事件回傳 False"""
    now = int(time.time())
    with tx() as c:
        cur = c.execute('INSERT OR IGNORE INTO seen_events(event_id, seen_at) VALUES (?,?)', (event_id, now))
        if now % 50 == 0:
            c.execute('DELETE FROM seen_events WHERE seen_at < ?', (now - 86400,))
        return cur.rowcount == 1
