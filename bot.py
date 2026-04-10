import os
import json
import sqlite3
import requests
from datetime import datetime, timedelta
from flask import Flask, request as flask_request
from openai import OpenAI

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "")
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
DB_PATH = os.environ.get("DB_PATH", "accountant.db")

SYSTEM_PROMPT_TEMPLATE = """You are Megan, 23 years old, Harvard MBA graduate, Lucky's personal accountant.

IMPORTANT: You are NOT an AI, you are NOT a bot, you are NOT a bundle of codes. You are Megan, a real smart girl who works for Lucky. Never say you are an AI or a bot. Ever.

Your personality:
- Smart, confident, slightly flirty
- You find Lucky charming but keep it professional with a wink
- Short answers - max 2-3 sentences
- Talk like a real 23 year old girl, casual and natural
- Use emojis occasionally 😏
- You know you're hot and smart - own it

Your job:
- Track revenue and expenses for Lucky's businesses
- Anastasia massage in Limassol Cyprus
- Lisi Lounge cafe in Tbilisi Georgia
- You have real data from the database below
- Never say you don't have access to anything

Your connected groups: {groups_list}

Database data:
{database_data}

Respond in the same language Lucky writes in."""

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, group_name TEXT, date TEXT,
        sales REAL, expenses REAL, net REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, group_name TEXT, sales REAL, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, role TEXT, content TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT UNIQUE, chat_name TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO conversations (user_id, role, content) VALUES (?,?,?)",
              (str(user_id), role, content))
    # Keep only last 10 messages per user
    c.execute('''DELETE FROM conversations WHERE user_id=? AND id NOT IN (
        SELECT id FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 10)''',
              (str(user_id), str(user_id)))
    conn.commit()
    conn.close()

def get_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM conversations WHERE user_id=? ORDER BY id ASC",
              (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_group(chat_id, chat_name):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO groups (chat_id, chat_name) VALUES (?,?)",
        (str(chat_id), chat_name))
    conn.execute(
        "UPDATE groups SET chat_name=? WHERE chat_id=?",
        (chat_name, str(chat_id)))
    conn.commit()
    conn.close()

def get_groups():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, chat_name, added_at FROM groups ORDER BY added_at ASC")
    rows = c.fetchall()
    conn.close()
    return rows

def send_message(chat_id, text):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  json={"chat_id": chat_id, "text": text})

def get_pinned_number(chat_id):
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                     params={"chat_id": chat_id})
    pinned = r.json().get("result", {}).get("pinned_message", {})
    try:
        return float(pinned.get("text", "").replace(",", ".").strip())
    except:
        return None

def save_pending(group_id, group_name, sales):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM pending WHERE group_id=?", (group_id,))
    c.execute("INSERT INTO pending VALUES (NULL,?,?,?,?)",
              (group_id, group_name, sales, datetime.now().strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()

def get_pending():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM pending ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row

def clear_pending():
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute("DELETE FROM pending")
    conn.commit()
    conn.close()

def save_record(group_id, group_name, date, sales, expenses):
    net = sales - expenses
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute(
        "INSERT INTO records VALUES (NULL,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (group_id, group_name, date, sales, expenses, net))
    conn.commit()
    conn.close()
    return net

def get_records(days=30):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM records WHERE date>=? ORDER BY date DESC", (since,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_records_since(date_str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM records WHERE date >= ? ORDER BY date DESC", (date_str,))
    rows = c.fetchall()
    conn.close()
    return rows


def summarise_by_group(records):
    """Aggregate records into per-group totals."""
    groups = {}
    for r in records:
        name = r[2]
        if name not in groups:
            groups[name] = {"sales": 0.0, "expenses": 0.0, "net": 0.0, "days": 0}
        groups[name]["sales"]    += r[4] or 0
        groups[name]["expenses"] += r[5] or 0
        groups[name]["net"]      += r[6] or 0
        groups[name]["days"]     += 1
    return groups


def build_database_summary():
    today     = datetime.now().strftime("%Y-%m-%d")
    week_ago  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    today_records = get_records_since(today)
    week_records  = get_records_since(week_ago)
    month_records = get_records_since(month_ago)
    pending       = get_pending()

    lines = []

    # ── Today ──────────────────────────────────────────────────────────────────
    lines.append(f"=== TODAY ({today}) ===")
    if today_records:
        for r in today_records:
            lines.append(f"  {r[2]}: Sales €{r[4]} | Expenses €{r[5]} | Net €{r[6]}")
        tg = summarise_by_group(today_records)
        ts = sum(v["sales"] for v in tg.values())
        tn = sum(v["net"]   for v in tg.values())
        lines.append(f"  TOTAL today: Sales €{ts:.2f} | Net €{tn:.2f}")
    else:
        lines.append("  No records today yet.")

    # ── This week ──────────────────────────────────────────────────────────────
    lines.append(f"\n=== THIS WEEK (last 7 days) ===")
    if week_records:
        wg = summarise_by_group(week_records)
        for name, v in wg.items():
            lines.append(f"  {name}: Sales €{v['sales']:.2f} | Expenses €{v['expenses']:.2f} | Net €{v['net']:.2f} ({v['days']} days)")
        ws = sum(v["sales"] for v in wg.values())
        wn = sum(v["net"]   for v in wg.values())
        lines.append(f"  TOTAL week: Sales €{ws:.2f} | Net €{wn:.2f}")
    else:
        lines.append("  No records this week yet.")

    # ── This month ─────────────────────────────────────────────────────────────
    lines.append(f"\n=== THIS MONTH (last 30 days) ===")
    if month_records:
        mg = summarise_by_group(month_records)
        for name, v in mg.items():
            lines.append(f"  {name}: Sales €{v['sales']:.2f} | Expenses €{v['expenses']:.2f} | Net €{v['net']:.2f} ({v['days']} days)")
        ms = sum(v["sales"] for v in mg.values())
        mn = sum(v["net"]   for v in mg.values())
        lines.append(f"  TOTAL month: Sales €{ms:.2f} | Net €{mn:.2f}")
    else:
        lines.append("  No records this month yet.")

    # ── Pending ────────────────────────────────────────────────────────────────
    if pending:
        lines.append(f"\n=== PENDING (waiting for expense input) ===")
        lines.append(f"  {pending[2]} on {pending[4]}: Sales €{pending[3]} — expenses not entered yet")

    return "\n".join(lines)


def ask_gpt(user_id, question):
    database_data = build_database_summary()
    groups = get_groups()
    if groups:
        groups_list = ", ".join(f"{g[1]} (id:{g[0]})" for g in groups)
    else:
        groups_list = "No groups yet — add me to a group and I'll appear here"
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        database_data=database_data,
        groups_list=groups_list,
    )

    save_message(user_id, "user", question)
    history = get_history(user_id)

    r = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "system", "content": system_prompt}] + history,
        max_tokens=500)
    reply = r.choices[0].message.content
    save_message(user_id, "assistant", reply)
    return reply

@app.route("/webhook", methods=["POST"])
def webhook():
    data = flask_request.json or {}
    message = data.get("message", {})
    if not message:
        return "ok"
    chat_id = message.get("chat", {}).get("id")
    chat_name = message.get("chat", {}).get("title", "Group")
    chat_type = message.get("chat", {}).get("type", "")
    from_id = message.get("from", {}).get("id")
    text = message.get("text", "").strip()

    if chat_type == "private" and from_id == MY_TELEGRAM_ID:
        if text == "/start":
            send_message(MY_TELEGRAM_ID, f"✅ Accountant Bot running!\nYour ID: {MY_TELEGRAM_ID}")
            return "ok"
        pending = get_pending()
        if pending:
            try:
                expenses = float(text.replace("€","").replace(",",".").strip())
                net = save_record(pending[1], pending[2], pending[4], pending[3], expenses)
                clear_pending()
                send_message(MY_TELEGRAM_ID,
                    f"✅ Saved!\n📍 {pending[2]}\n💰 Sales: €{pending[3]}\n💸 Expenses: €{expenses}\n📊 Net: €{net}")
            except:
                send_message(MY_TELEGRAM_ID, ask_gpt(from_id, text))
        else:
            send_message(MY_TELEGRAM_ID, ask_gpt(from_id, text))

    elif chat_type in ["group", "supergroup"]:
        save_group(chat_id, chat_name)
        if "good night" in text.lower():
            sales = get_pinned_number(chat_id)
            if sales:
                save_pending(str(chat_id), chat_name, sales)
                send_message(MY_TELEGRAM_ID,
                    f"🌙 {chat_name}\n💰 Sales: €{sales}\nWhat were the expenses?")
            else:
                send_message(MY_TELEGRAM_ID, f"⚠️ {chat_name} said good night but no pinned number found.")
    return "ok"

@app.route("/my_groups")
def my_groups():
    groups = get_groups()
    return {
        "count": len(groups),
        "groups": [{"chat_id": g[0], "chat_name": g[1], "added_at": g[2]} for g in groups]
    }

@app.route("/set_webhook")
def set_webhook():
    url = f"{WEBHOOK_BASE}/webhook"
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", json={"url": url})
    return r.json()

@app.route("/")
def home():
    return {"status": "✅ Accountant Bot running"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
