import json
import logging
import os
import re
import sys
from datetime import datetime

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, request
from openai import OpenAI

import database as db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))
# Render sets RENDER_EXTERNAL_URL automatically; fallback to manual WEBHOOK_URL
WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

logger.info("=== BOT STARTING ===")
logger.info("TOKEN set: %s", bool(TOKEN))
logger.info("OPENAI_API_KEY set: %s", bool(OPENAI_API_KEY))
logger.info("MY_TELEGRAM_ID: %s", MY_TELEGRAM_ID)
logger.info("WEBHOOK_URL: %s", WEBHOOK_URL or "(not set)")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

# Pending expense queue: list of {"group_id", "group_name", "date", "sales"}
pending_expenses = []


# ─── Telegram helpers ──────────────────────────────────────────────────────────

def tg_send(chat_id, text, parse_mode="Markdown"):
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if not r.json().get("ok"):
            logger.error("sendMessage failed: %s", r.text)
    except Exception as e:
        logger.error("sendMessage error: %s", e)


def tg_get_chat(chat_id):
    try:
        r = requests.get(f"{TELEGRAM_API}/getChat", params={"chat_id": chat_id}, timeout=10)
        return r.json().get("result", {})
    except Exception as e:
        logger.error("getChat error: %s", e)
        return {}


def extract_number(text):
    cleaned = re.sub(r"[€$£,\s]", "", text.strip())
    match = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    if match:
        return float(match.group().replace(",", "."))
    return None


# ─── Update handlers ──────────────────────────────────────────────────────────

def handle_start(message):
    user_id = message["from"]["id"]
    is_owner = MY_TELEGRAM_ID != 0 and user_id == MY_TELEGRAM_ID
    logger.info("/start from user_id=%s is_owner=%s", user_id, is_owner)
    tg_send(
        message["chat"]["id"],
        f"Bot is running!\n\n"
        f"Your Telegram ID: {user_id}\n"
        f"Owner match: {'yes' if is_owner else 'no — set MY_TELEGRAM_ID=' + str(user_id) + ' in Render'}\n\n"
        f"{'Ask me anything, e.g. show me this week' if is_owner else 'Only the owner can use this bot.'}",
        parse_mode=None,
    )


def handle_group_message(message):
    text = message.get("text", "").lower().strip()
    if "good night" not in text:
        return

    group_id = str(message["chat"]["id"])
    group_name = message["chat"].get("title") or f"Group {group_id}"
    date_str = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")
    logger.info("'good night' detected in %s", group_name)

    chat = tg_get_chat(message["chat"]["id"])
    pinned = chat.get("pinned_message", {})
    pinned_text = pinned.get("text", "") if pinned else ""

    if not pinned_text:
        tg_send(message["chat"]["id"], "No pinned message found. Please pin today's sales number first.", parse_mode=None)
        return

    sales = extract_number(pinned_text)
    if sales is None:
        tg_send(message["chat"]["id"], f"Couldn't read a number from the pinned message: \"{pinned_text}\"", parse_mode=None)
        return

    pending_expenses.append(
        {"group_id": group_id, "group_name": group_name, "date": date_str, "sales": sales}
    )
    logger.info("Queued sales %.2f from %s", sales, group_name)

    tg_send(
        MY_TELEGRAM_ID,
        f"*{group_name}* — {date_str}\nSales: {sales:,.2f}\n\nWhat were today's expenses?",
    )


def handle_private_message(message):
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()
    logger.info("Private message from user_id=%s: %r", user_id, text[:80])

    if user_id != MY_TELEGRAM_ID:
        tg_send(message["chat"]["id"], f"Unauthorized. Your Telegram ID is {user_id}.", parse_mode=None)
        return

    if pending_expenses:
        amount = extract_number(text)
        if amount is not None:
            pending = pending_expenses.pop(0)
            net = pending["sales"] - amount
            db.save_record(
                group_id=pending["group_id"],
                group_name=pending["group_name"],
                date=pending["date"],
                sales=pending["sales"],
                expenses=amount,
                net_revenue=net,
            )
            logger.info("Saved: %s sales=%.2f expenses=%.2f net=%.2f",
                        pending["group_name"], pending["sales"], amount, net)

            reply = (
                f"*Saved — {pending['group_name']}* ({pending['date']})\n"
                f"Sales:    {pending['sales']:,.2f}\n"
                f"Expenses: {amount:,.2f}\n"
                f"Net:      {net:,.2f}"
            )
            if pending_expenses:
                nxt = pending_expenses[0]
                reply += (
                    f"\n\n*{nxt['group_name']}* — {nxt['date']}\n"
                    f"Sales: {nxt['sales']:,.2f}\n\nWhat were today's expenses?"
                )
            tg_send(MY_TELEGRAM_ID, reply)
            return

        pending_list = "\n".join(f"- {p['group_name']} ({p['sales']:,.2f})" for p in pending_expenses)
        tg_send(MY_TELEGRAM_ID, f"Still waiting for expenses for:\n{pending_list}\n\nPlease reply with a number.", parse_mode=None)

    ask_ai(message["chat"]["id"], text)


def ask_ai(chat_id, query):
    logger.info("AI query from %s: %r", chat_id, query[:80])
    records = db.get_all_records()
    data_json = json.dumps(records, indent=2, default=str)
    today = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")

    system_prompt = (
        f"You are a personal AI accountant. Today is {today}.\n\n"
        f"Sales/expense records:\n{data_json}\n\n"
        "Always use euro symbol. Format numbers with commas. Be concise. Use emojis."
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            max_tokens=700,
            temperature=0.3,
        )
        reply = resp.choices[0].message.content
    except Exception as e:
        reply = f"AI error: {e}"
        logger.error("OpenAI error: %s", e)

    tg_send(chat_id, reply)


# ─── Weekly summary ────────────────────────────────────────────────────────────

def weekly_summary():
    logger.info("Running weekly summary job")
    records = db.get_last_n_days(7)

    if not records:
        tg_send(MY_TELEGRAM_ID, "*Weekly Summary*\n\nNo records for the past 7 days.")
        return

    by_group = {}
    for r in records:
        g = r["group_name"]
        if g not in by_group:
            by_group[g] = {"sales": 0.0, "expenses": 0.0, "net": 0.0}
        by_group[g]["sales"] += r["sales"] or 0
        by_group[g]["expenses"] += r["expenses"] or 0
        by_group[g]["net"] += r["net_revenue"] or 0

    total_sales = sum(v["sales"] for v in by_group.values())
    total_expenses = sum(v["expenses"] for v in by_group.values())
    total_net = sum(v["net"] for v in by_group.values())

    lines = ["*Weekly Summary (last 7 days)*\n"]
    for g, v in sorted(by_group.items(), key=lambda x: x[1]["net"], reverse=True):
        lines.append(f"*{g}*\n  Sales: {v['sales']:,.2f}\n  Expenses: {v['expenses']:,.2f}\n  Net: {v['net']:,.2f}\n")
    lines += [
        "---",
        f"*Total Sales:*    {total_sales:,.2f}",
        f"*Total Expenses:* {total_expenses:,.2f}",
        f"*Total Net:*      {total_net:,.2f}",
    ]
    tg_send(MY_TELEGRAM_ID, "\n".join(lines))


# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "ok", 200

    message = data.get("message") or data.get("edited_message")
    if not message or "text" not in message:
        return "ok", 200

    text = message.get("text", "")
    chat_type = message["chat"]["type"]

    logger.info("Update — chat_type=%s text=%r", chat_type, text[:60])

    if text.startswith("/start"):
        handle_start(message)
    elif chat_type == "private":
        handle_private_message(message)
    elif chat_type in ("group", "supergroup"):
        handle_group_message(message)

    return "ok", 200


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "pending_expenses": len(pending_expenses)}, 200


@app.route("/set_webhook", methods=["GET"])
def set_webhook_route():
    return _set_webhook()


# ─── Webhook registration ──────────────────────────────────────────────────────

def _set_webhook():
    if not WEBHOOK_URL:
        msg = "WEBHOOK_URL / RENDER_EXTERNAL_URL not set — cannot register webhook"
        logger.warning(msg)
        return msg, 500

    url = f"{WEBHOOK_URL}/webhook"
    r = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": url}, timeout=10)
    result = r.json()
    if result.get("ok"):
        logger.info("Webhook registered: %s", url)
        return f"Webhook set to {url}", 200
    else:
        logger.error("setWebhook failed: %s", result)
        return f"Failed: {result}", 500


# ─── Startup ──────────────────────────────────────────────────────────────────

def startup():
    if not TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    try:
        db.init_db()
        logger.info("Database ready: %s", os.environ.get("DB_PATH", "accountant.db"))
    except Exception as e:
        logger.critical("Database init failed: %s", e)
        sys.exit(1)

    # Register webhook with Telegram
    _set_webhook()

    # Schedule weekly summary — Mondays 08:00 Nicosia time
    tz = pytz.timezone("Asia/Nicosia")
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(weekly_summary, "cron", day_of_week="mon", hour=8, minute=0)
    scheduler.start()
    logger.info("Weekly summary scheduled — Mondays 08:00 Nicosia")


startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
