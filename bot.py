import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

import pytz
import requests
from flask import Flask, request
from openai import OpenAI

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_KEY     = os.environ.get("OPENAI_API_KEY", "")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))
DB_PATH        = os.environ.get("DB_PATH", "accountant.db")
WEBHOOK_BASE   = (
    os.environ.get("RENDER_EXTERNAL_URL") or
    os.environ.get("WEBHOOK_URL", "")
).rstrip("/")

TG_API = f"https://api.telegram.org/bot{TOKEN}"

log.info("=== BOT STARTING ===")
log.info("Python         : %s", sys.version.split()[0])
log.info("TOKEN set      : %s", bool(TOKEN))
log.info("OPENAI set     : %s", bool(OPENAI_KEY))
log.info("MY_TELEGRAM_ID : %s", MY_TELEGRAM_ID)
log.info("WEBHOOK_BASE   : %s", WEBHOOK_BASE or "(not set — register webhook manually)")

if not TOKEN:
    log.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
    sys.exit(1)

app = Flask(__name__)
ai  = OpenAI(api_key=OPENAI_KEY)

# In-memory queue of groups waiting for expense input
# Each entry: {"group_id", "group_name", "date", "sales"}
pending = []

# ── Database ───────────────────────────────────────────────────────────────────

def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT NOT NULL,
                group_name  TEXT NOT NULL,
                date        TEXT NOT NULL,
                sales       REAL,
                expenses    REAL,
                net_revenue REAL,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    log.info("Database ready: %s", DB_PATH)


def db_save(group_id, group_name, date, sales, expenses, net):
    with sqlite3.connect(DB_PATH) as con:
        exists = con.execute(
            "SELECT id FROM records WHERE group_id=? AND date=?", (group_id, date)
        ).fetchone()
        if exists:
            con.execute(
                "UPDATE records SET sales=?,expenses=?,net_revenue=?,group_name=? "
                "WHERE group_id=? AND date=?",
                (sales, expenses, net, group_name, group_id, date),
            )
        else:
            con.execute(
                "INSERT INTO records(group_id,group_name,date,sales,expenses,net_revenue) "
                "VALUES(?,?,?,?,?,?)",
                (group_id, group_name, date, sales, expenses, net),
            )


def db_all():
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM records ORDER BY date DESC"
        )]


def db_since(days):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM records WHERE date >= ? ORDER BY date DESC", (since,)
        )]


# ── Telegram helpers ───────────────────────────────────────────────────────────

def tg_send(chat_id, text):
    """Send a Markdown message via Telegram Bot API."""
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.json().get("ok"):
            log.error("sendMessage failed: %s", r.text[:300])
    except Exception as e:
        log.error("sendMessage error: %s", e)


def tg_get_chat(chat_id):
    """Fetch chat info (including pinned_message) via Telegram Bot API."""
    try:
        r = requests.get(
            f"{TG_API}/getChat",
            params={"chat_id": chat_id},
            timeout=10,
        )
        return r.json().get("result", {})
    except Exception as e:
        log.error("getChat error: %s", e)
        return {}


def extract_number(text):
    """Pull the first number out of a string, ignoring currency symbols."""
    cleaned = re.sub(r"[€$£,\s]", "", text.strip())
    m = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    return float(m.group().replace(",", ".")) if m else None


# ── Message handlers ───────────────────────────────────────────────────────────

def handle_start(msg):
    uid      = msg["from"]["id"]
    is_owner = MY_TELEGRAM_ID != 0 and uid == MY_TELEGRAM_ID
    log.info("/start from uid=%s is_owner=%s", uid, is_owner)
    tg_send(
        msg["chat"]["id"],
        f"✅ *Bot is running!*\n\n"
        f"Your Telegram ID: `{uid}`\n"
        f"Owner: {'yes ✅' if is_owner else f'no ❌  — set MY_TELEGRAM_ID={uid} in Render'}\n\n"
        + ("Ask me anything — e.g. _show me this week_"
           if is_owner else "Only the owner can use this bot."),
    )


def handle_group(msg):
    text = msg.get("text", "").lower().strip()
    if "good night" not in text:
        return

    group_id   = str(msg["chat"]["id"])
    group_name = msg["chat"].get("title") or f"Group {group_id}"
    date_str   = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")
    log.info("'good night' detected in %s", group_name)

    chat        = tg_get_chat(msg["chat"]["id"])
    pinned      = chat.get("pinned_message") or {}
    pinned_text = pinned.get("text", "")

    if not pinned_text:
        tg_send(msg["chat"]["id"],
                "⚠️ No pinned message found. Please pin today's sales number first.")
        return

    sales = extract_number(pinned_text)
    if sales is None:
        tg_send(msg["chat"]["id"],
                f"⚠️ Can't read a number from pinned message: \"{pinned_text}\"")
        return

    pending.append({
        "group_id":   group_id,
        "group_name": group_name,
        "date":       date_str,
        "sales":      sales,
    })
    log.info("Queued €%.2f from %s", sales, group_name)

    tg_send(
        MY_TELEGRAM_ID,
        f"💰 *{group_name}* — {date_str}\n"
        f"Sales: €{sales:,.2f}\n\n"
        f"What were today's expenses?",
    )


def handle_private(msg):
    uid  = msg["from"]["id"]
    text = msg.get("text", "").strip()
    log.info("Private message uid=%s: %r", uid, text[:80])

    if uid != MY_TELEGRAM_ID:
        tg_send(msg["chat"]["id"],
                f"⛔ Unauthorized. Your Telegram ID is `{uid}`.")
        return

    # If there are pending expense requests, try to consume the next one
    if pending:
        amount = extract_number(text)
        if amount is not None:
            p   = pending.pop(0)
            net = p["sales"] - amount
            db_save(p["group_id"], p["group_name"], p["date"],
                    p["sales"], amount, net)
            log.info("Saved — %s | sales=%.2f exp=%.2f net=%.2f",
                     p["group_name"], p["sales"], amount, net)

            reply = (
                f"✅ *{p['group_name']}* ({p['date']})\n"
                f"Sales:    €{p['sales']:,.2f}\n"
                f"Expenses: €{amount:,.2f}\n"
                f"Net:      €{net:,.2f}"
            )
            if pending:
                nxt    = pending[0]
                reply += (f"\n\n💰 *{nxt['group_name']}* — {nxt['date']}\n"
                          f"Sales: €{nxt['sales']:,.2f}\n\nExpenses?")
            tg_send(MY_TELEGRAM_ID, reply)
            return

        # Non-number while expenses are pending — remind the owner
        plist = "\n".join(f"• {p['group_name']} (€{p['sales']:,.2f})"
                          for p in pending)
        tg_send(MY_TELEGRAM_ID,
                f"⏳ Still waiting for expenses:\n{plist}\n\nReply with a number.")
        return

    # No pending — treat as a natural language query for GPT
    handle_gpt(uid, text)


def handle_gpt(chat_id, query):
    log.info("GPT query from %s: %r", chat_id, query[:80])
    records = db_all()
    today   = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")

    system  = (
        f"You are a personal AI accountant. Today is {today}.\n\n"
        f"All saved records (JSON):\n{json.dumps(records, indent=2, default=str)}\n\n"
        "Rules: always use €, format numbers with commas (e.g. €1,234.50), "
        "be concise, use emojis, highlight best/worst performers."
    )
    try:
        resp  = ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": query},
            ],
            max_tokens=700,
            temperature=0.3,
        )
        reply = resp.choices[0].message.content
    except Exception as e:
        reply = f"❌ AI error: {e}"
        log.error("OpenAI error: %s", e)

    tg_send(chat_id, reply)


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.post("/webhook")
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "ok", 200

    msg = data.get("message") or data.get("edited_message")
    if not msg or "text" not in msg:
        return "ok", 200

    text      = msg["text"].strip()
    chat_type = msg["chat"]["type"]
    log.info("update chat_type=%s text=%r", chat_type, text[:60])

    if text.startswith("/start"):
        handle_start(msg)
    elif chat_type in ("group", "supergroup"):
        handle_group(msg)
    elif chat_type == "private":
        handle_private(msg)

    return "ok", 200


@app.get("/health")
def health():
    return {"status": "ok", "pending_count": len(pending)}, 200


@app.get("/set_webhook")
def set_webhook():
    base = (
        os.environ.get("RENDER_EXTERNAL_URL") or
        os.environ.get("WEBHOOK_URL", "")
    ).rstrip("/")
    if not base:
        return "Set RENDER_EXTERNAL_URL or WEBHOOK_URL env var first.", 400
    r      = requests.post(
        f"{TG_API}/setWebhook",
        json={"url": f"{base}/webhook", "drop_pending_updates": True},
        timeout=10,
    )
    result = r.json()
    log.info("setWebhook result: %s", result)
    return result, 200 if result.get("ok") else 500


# ── Startup ────────────────────────────────────────────────────────────────────

db_init()

# Auto-register webhook when RENDER_EXTERNAL_URL is available
if WEBHOOK_BASE:
    try:
        r      = requests.post(
            f"{TG_API}/setWebhook",
            json={"url": f"{WEBHOOK_BASE}/webhook", "drop_pending_updates": True},
            timeout=10,
        )
        result = r.json()
        if result.get("ok"):
            log.info("Webhook registered: %s/webhook", WEBHOOK_BASE)
        else:
            log.error("setWebhook failed: %s", result)
    except Exception as e:
        log.error("setWebhook error: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
