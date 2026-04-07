import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", "accountant.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     TEXT    NOT NULL,
            group_name   TEXT    NOT NULL,
            date         TEXT    NOT NULL,
            sales        REAL,
            expenses     REAL,
            net_revenue  REAL,
            created_at   TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_record(group_id, group_name, date, sales, expenses, net_revenue):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM records WHERE group_id=? AND date=?", (group_id, date))
    existing = c.fetchone()
    if existing:
        c.execute(
            "UPDATE records SET sales=?, expenses=?, net_revenue=?, group_name=? WHERE group_id=? AND date=?",
            (sales, expenses, net_revenue, group_name, group_id, date),
        )
    else:
        c.execute(
            "INSERT INTO records (group_id, group_name, date, sales, expenses, net_revenue) VALUES (?,?,?,?,?,?)",
            (group_id, group_name, date, sales, expenses, net_revenue),
        )
    conn.commit()
    conn.close()


def get_all_records():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM records ORDER BY date DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_last_n_days(n: int):
    since = (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM records WHERE date >= ? ORDER BY date DESC", (since,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows
