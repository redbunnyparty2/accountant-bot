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
    conn.commit()
    conn.close()

init_db()

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

def ask_gpt(question, records):
    data = "\n".join([f"{r[4]}: {r[2]} Sales:€{r[5]} Expenses:€{r[6]} Net:€{r[7]}" for r in records])
    r = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "system", "content": "You are a business accountant. Be concise."},
                  {"role": "user", "content": f"Data:\n{data}\n\nQuestion: {question}"}],
        max_tokens=300)
    return r.choices[0].message.content

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
                records = get_records(30)
                send_message(MY_TELEGRAM_ID, ask_gpt(text, records) if records else "Hi! 👋 I'm your AI accountant. Here's how I work:\n\n1. Add me to your Telegram groups as admin\n2. Your admin pins the daily sales number in the group\n3. When admin types 'good night' - I'll message you asking for expenses\n4. I'll calculate your net revenue automatically\n\nYou can also ask me anything like:\n- 'show me this week'\n- 'compare last 2 weeks'\n- 'which group made most this month'\n\nNo data yet - waiting for your first 'good night' report! 📊")
        else:
            records = get_records(30)
            send_message(MY_TELEGRAM_ID, ask_gpt(text, records) if records else "Hi! 👋 I'm your AI accountant. Here's how I work:\n\n1. Add me to your Telegram groups as admin\n2. Your admin pins the daily sales number in the group\n3. When admin types 'good night' - I'll message you asking for expenses\n4. I'll calculate your net revenue automatically\n\nYou can also ask me anything like:\n- 'show me this week'\n- 'compare last 2 weeks'\n- 'which group made most this month'\n\nNo data yet - waiting for your first 'good night' report! 📊")

    elif chat_type in ["group", "supergroup"]:
        if "good night" in text.lower():
            sales = get_pinned_number(chat_id)
            if sales:
                save_pending(str(chat_id), chat_name, sales)
                send_message(MY_TELEGRAM_ID,
                    f"🌙 {chat_name}\n💰 Sales: €{sales}\nWhat were the expenses?")
            else:
                send_message(MY_TELEGRAM_ID, f"⚠️ {chat_name} said good night but no pinned number found.")
    return "ok"

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
