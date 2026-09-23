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


# ---------- Webhook 去重 ----------

def mark_event(event_id):
    """第一次看到回傳 True；LINE 重送的重複事件回傳 False"""
    now = int(time.time())
    with tx() as c:
        cur = c.execute('INSERT OR IGNORE INTO seen_events(event_id, seen_at) VALUES (?,?)', (event_id, now))
        if now % 50 == 0:
            c.execute('DELETE FROM seen_events WHERE seen_at < ?', (now - 86400,))
        return cur.rowcount == 1
