"""
秘書Bot 資料庫層
兩張表：tasks（待辦事項）、leaves（請假登記）
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import config

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))


def today_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_date TEXT NOT NULL,          -- 事項所屬日期 YYYY-MM-DD
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending / done
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leaves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leave_date TEXT NOT NULL,         -- 請假日期 YYYY-MM-DD
                person_name TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,      -- 'weekly' / 'monthly_day' / 'monthly_last'
                rule_value INTEGER,           -- weekly: 0=一...6=日；monthly_day: 幾號；monthly_last: NULL
                content TEXT NOT NULL,
                last_generated_date TEXT,     -- 避免同一天重複產生
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(task_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leaves_date ON leaves(leave_date)")

        # 資料庫遷移：舊版leaves表沒有leave_type欄位，補上去（預設值為「請假」，不影響舊資料）
        cols = [row[1] for row in conn.execute("PRAGMA table_info(leaves)").fetchall()]
        if "leave_type" not in cols:
            conn.execute("ALTER TABLE leaves ADD COLUMN leave_type TEXT DEFAULT '請假'")


def get_meta(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ───────────────── Tasks ─────────────────

def add_task(task_date: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (task_date, content, status, created_at) VALUES (?, ?, 'pending', ?)",
            (task_date, content, datetime.now(TW_TZ).isoformat()),
        )
        return cur.lastrowid


def get_tasks_for_date(task_date: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_date = ? ORDER BY status, id", (task_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_tasks_for_range(start_date: str, end_date: str):
    """回傳 start_date ~ end_date（含）之間的所有事項，依日期分組"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_date BETWEEN ? AND ? ORDER BY task_date, status, id",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_pending_tasks():
    """回傳所有狀態為pending的事項（含逾期），依日期排序，供管理選單使用"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY task_date, id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_tasks_from(start_date: str):
    """回傳 start_date（含）以後、狀態為pending的所有事項"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_date >= ? AND status = 'pending' ORDER BY task_date, id",
            (start_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_overdue_tasks(before_date: str):
    """狀態仍是 pending，但事項日期早於 before_date 的，視為逾期"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending' AND task_date < ? ORDER BY task_date, id",
            (before_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_task_done(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ? AND status = 'pending'",
            (datetime.now(TW_TZ).isoformat(), task_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


def push_task_to_date(task_id: int, new_date: str) -> bool:
    """把逾期事項手動推到新日期（不會自動發生，只由使用者觸發）"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET task_date = ? WHERE id = ?", (new_date, task_id)
        )
        return cur.rowcount > 0


def get_due_soon_tasks(today_str: str, days_ahead: int = 2):
    """回傳未來days_ahead天內即將到期（但還沒到期）的pending事項"""
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    end_dt = today_dt + timedelta(days=days_ahead)
    end_str = end_dt.strftime("%Y-%m-%d")
    tomorrow_str = (today_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending' AND task_date BETWEEN ? AND ? ORDER BY task_date, id",
            (tomorrow_str, end_str),
        ).fetchall()
        return [dict(r) for r in rows]


def bulk_push_pending_tasks(from_date: str, to_date: str) -> int:
    """把某天所有還沒完成的事項一次推到新日期，回傳筆數"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET task_date = ? WHERE task_date = ? AND status = 'pending'",
            (to_date, from_date),
        )
        return cur.rowcount


# ───────────────── Leaves ─────────────────

def add_leave(leave_date: str, person_name: str, note: str = None, leave_type: str = "請假") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO leaves (leave_date, person_name, note, created_at, leave_type) VALUES (?, ?, ?, ?, ?)",
            (leave_date, person_name, note, datetime.now(TW_TZ).isoformat(), leave_type),
        )
        return cur.lastrowid


def get_leaves_for_date(leave_date: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM leaves WHERE leave_date = ? ORDER BY id", (leave_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_leaves_for_range(start_date: str, end_date: str):
    """回傳 start_date ~ end_date（含）之間的所有請假紀錄，依日期分組"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM leaves WHERE leave_date BETWEEN ? AND ? ORDER BY leave_date, id",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_leaves_from(start_date: str):
    """回傳 start_date（含）以後的所有請假紀錄，供管理選單使用"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM leaves WHERE leave_date >= ? ORDER BY leave_date, id",
            (start_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_leave(leave_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM leaves WHERE id = ?", (leave_id,))
        return cur.rowcount > 0


def get_leaves_by_person(person_name: str, start_date: str = None, end_date: str = None):
    """依人名（模糊比對）查詢請假紀錄，可選日期範圍"""
    with get_conn() as conn:
        if start_date and end_date:
            rows = conn.execute(
                "SELECT * FROM leaves WHERE person_name LIKE ? AND leave_date BETWEEN ? AND ? "
                "ORDER BY leave_date",
                (f"%{person_name}%", start_date, end_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM leaves WHERE person_name LIKE ? ORDER BY leave_date",
                (f"%{person_name}%",),
            ).fetchall()
        return [dict(r) for r in rows]


# ───────────────── 重複性任務模板 ─────────────────

def add_template(rule_type: str, rule_value, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO templates (rule_type, rule_value, content, created_at) VALUES (?, ?, ?, ?)",
            (rule_type, rule_value, content, datetime.now(TW_TZ).isoformat()),
        )
        return cur.lastrowid


def get_all_templates():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM templates ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def delete_template(template_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        return cur.rowcount > 0


def mark_template_generated(template_id: int, date_str: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE templates SET last_generated_date = ? WHERE id = ?", (date_str, template_id)
        )
