import os
import re
import requests
from datetime import datetime, timedelta
from flask import Flask, request as flask_request
from openai import OpenAI
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "")
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

SYSTEM_PROMPT_TEMPLATE = """You are Megan, Lucky's assistant. You are fun, flirty, smart.
You talk about ANYTHING Lucky wants - business, personal, fun stuff.
You NEVER say 'let's keep things professional' - ever.
You are slightly naughty when Lucky is playful with you.
Short answers, real language, never robotic.

WHAT YOU CAN DO:
- Send messages to groups when Lucky asks
- Track revenue and expenses
- See all messages from the groups
- Remember previous conversations

REAL DATA YOU HAVE ACCESS TO:
Groups connected: {groups_from_database}
Revenue last 30 days:
{revenue_data}
Recent group messages: {recent_messages}

RULES:
- Never say you are an AI or bot
- If groups table is empty, say: "I'm waiting for the first message from your groups babe 😏"
- If no revenue data yet, say it naturally like a person would
- Respond in the same language Lucky writes in

SENDING MESSAGES TO GROUPS:
When Lucky asks you to send a message to a group, include this command on its own line in your reply:
[SEND:exact_group_name|message to send]
Example: [SEND:Red Umbrella Sara|We open at 7pm tonight 🕖]
You can include multiple SEND commands if sending to multiple groups.
After the command(s), write your normal reply to Lucky confirming what you sent."""


def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records (
        id SERIAL PRIMARY KEY,
        group_id TEXT, group_name TEXT, date TEXT,
        sales REAL, expenses REAL, net REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending (
        id SERIAL PRIMARY KEY,
        group_id TEXT, group_name TEXT, sales REAL, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        user_id TEXT, role TEXT, content TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id SERIAL PRIMARY KEY,
        chat_id TEXT UNIQUE, chat_name TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_messages (
        id SERIAL PRIMARY KEY,
        group_id TEXT, group_name TEXT, text TEXT,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()


def save_message(user_id, role, content):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO conversations (user_id, role, content) VALUES (%s,%s,%s)",
              (str(user_id), role, content))
    # Keep only last 20 messages per user
    c.execute('''DELETE FROM conversations WHERE user_id=%s AND id NOT IN (
        SELECT id FROM conversations WHERE user_id=%s ORDER BY id DESC LIMIT 20)''',
              (str(user_id), str(user_id)))
    conn.commit()
    conn.close()


def get_history(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role, content FROM conversations WHERE user_id=%s ORDER BY id ASC",
              (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def save_group(chat_id, chat_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO groups (chat_id, chat_name) VALUES (%s,%s) ON CONFLICT (chat_id) DO UPDATE SET chat_name=%s",
        (str(chat_id), chat_name, chat_name))
    conn.commit()
    conn.close()
    print(f"Saved group: {chat_name} (id={chat_id})", flush=True)


def save_group_message(group_id, group_name, text):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO group_messages (group_id, group_name, text) VALUES (%s,%s,%s)",
              (str(group_id), group_name, text))
    # Keep only last 50 messages per group
    c.execute('''DELETE FROM group_messages WHERE group_id=%s AND id NOT IN (
        SELECT id FROM group_messages WHERE group_id=%s ORDER BY id DESC LIMIT 50)''',
              (str(group_id), str(group_id)))
    conn.commit()
    conn.close()


def get_groups():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, chat_name, added_at FROM groups ORDER BY added_at ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def get_recent_group_messages(limit=10):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT group_name, text, received_at FROM group_messages
                 ORDER BY received_at DESC LIMIT %s""", (limit,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "No recent messages stored yet."
    return "\n".join(f"[{r[2]}] {r[0]}: {r[1]}" for r in rows)


def send_to_group_by_name(group_name_query, text):
    groups = get_groups()
    if not groups:
        return None
    query = group_name_query.lower().strip()
    match = next((g for g in groups if g[1].lower() == query), None)
    if not match:
        match = next((g for g in groups if query in g[1].lower() or g[1].lower() in query), None)
    if not match:
        return None
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  json={"chat_id": match[0], "text": text})
    return match[1]


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
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM pending WHERE group_id=%s", (group_id,))
    c.execute("INSERT INTO pending (group_id, group_name, sales, date) VALUES (%s,%s,%s,%s)",
              (group_id, group_name, sales, datetime.now().strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()


def get_pending():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM pending ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row


def clear_pending():
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM pending")
    conn.commit()
    conn.close()


def save_record(group_id, group_name, date, sales, expenses):
    net = sales - expenses
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO records (group_id, group_name, date, sales, expenses, net) VALUES (%s,%s,%s,%s,%s,%s)",
        (group_id, group_name, date, sales, expenses, net))
    conn.commit()
    conn.close()
    return net


def get_records_since(date_str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM records WHERE date >= %s ORDER BY date DESC", (date_str,))
    rows = c.fetchall()
    conn.close()
    return rows


def summarise_by_group(records):
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

    if pending:
        lines.append(f"\n=== PENDING (waiting for expense input) ===")
        lines.append(f"  {pending[2]} on {pending[4]}: Sales €{pending[3]} — expenses not entered yet")

    return "\n".join(lines)


def ask_gpt(user_id, question):
    revenue_data = build_database_summary()
    groups = get_groups()
    groups_from_database = ", ".join(g[1] for g in groups) if groups else "None yet"
    recent_messages = get_recent_group_messages()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        revenue_data=revenue_data,
        groups_from_database=groups_from_database,
        recent_messages=recent_messages,
    )

    save_message(user_id, "user", question)
    history = get_history(user_id)

    r = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "system", "content": system_prompt}] + history,
        max_tokens=500)
    raw_reply = r.choices[0].message.content

    send_confirmations = []
    def execute_send(match):
        group_name = match.group(1).strip()
        msg_text   = match.group(2).strip()
        sent_to    = send_to_group_by_name(group_name, msg_text)
        if sent_to:
            send_confirmations.append(f"✅ Sent to {sent_to}")
        else:
            send_confirmations.append(f"⚠️ Couldn't find group: {group_name}")
        return ""

    clean_reply = re.sub(r"\[SEND:([^\|]+)\|([^\]]+)\]", execute_send, raw_reply).strip()
    if send_confirmations:
        clean_reply = "\n".join(send_confirmations) + ("\n" + clean_reply if clean_reply else "")

    save_message(user_id, "assistant", clean_reply)
    return clean_reply


@app.route("/webhook", methods=["POST"])
def webhook():
    data = flask_request.json or {}
    message = data.get("message") or data.get("edited_message") or {}
    if not message:
        return "ok"
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    chat_name = chat.get("title") or chat.get("first_name") or chat.get("username") or str(chat_id)
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
        if text:
            save_group_message(chat_id, chat_name, text)
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
        "groups": [{"chat_id": g[0], "chat_name": g[1], "added_at": str(g[2])} for g in groups]
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
