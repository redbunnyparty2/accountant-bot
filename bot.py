import json
import logging
import os
import re
import sys
from datetime import datetime, time as dtime

import pytz
from dotenv import load_dotenv
from openai import OpenAI
from telegram import ParseMode
from telegram.ext import (
    CommandHandler,
    Filters,
    MessageHandler,
    Updater,
)

import database as db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))

logger.info("=== BOT STARTING ===")
logger.info("TELEGRAM_BOT_TOKEN set: %s", bool(TELEGRAM_BOT_TOKEN))
logger.info("OPENAI_API_KEY set: %s", bool(OPENAI_API_KEY))
logger.info("MY_TELEGRAM_ID: %s", MY_TELEGRAM_ID)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Pending expense queue: list of {"group_id", "group_name", "date", "sales"}
pending_expenses = []


# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_number(text):
    cleaned = re.sub(r"[€$£,\s]", "", text.strip())
    match = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    if match:
        return float(match.group().replace(",", "."))
    return None


# ─── /start ───────────────────────────────────────────────────────────────────

def start(update, context):
    user_id = update.effective_user.id
    is_owner = MY_TELEGRAM_ID != 0 and user_id == MY_TELEGRAM_ID
    logger.info("/start from user_id=%s is_owner=%s", user_id, is_owner)
    update.message.reply_text(
        f"Bot is running!\n\n"
        f"Your Telegram ID: {user_id}\n"
        f"Owner match: {'yes' if is_owner else 'no — set MY_TELEGRAM_ID=' + str(user_id) + ' in Render'}\n\n"
        f"{'Ask me anything, e.g. show me this week' if is_owner else 'Only the owner can use this bot.'}"
    )


# ─── Group handler ────────────────────────────────────────────────────────────

def handle_group_message(update, context):
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower().strip()
    if "good night" not in text:
        return

    group_id = str(update.message.chat_id)
    group_name = update.message.chat.title or f"Group {group_id}"
    date_str = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")
    logger.info("'good night' detected in %s", group_name)

    try:
        chat = context.bot.get_chat(update.message.chat_id)
    except Exception as e:
        logger.error("get_chat failed: %s", e)
        update.message.reply_text("Error reading chat info.")
        return

    if not chat.pinned_message or not chat.pinned_message.text:
        update.message.reply_text("No pinned message found. Please pin today's sales number first.")
        return

    sales = extract_number(chat.pinned_message.text)
    if sales is None:
        update.message.reply_text(
            f"Couldn't read a number from the pinned message: \"{chat.pinned_message.text}\""
        )
        return

    pending_expenses.append(
        {"group_id": group_id, "group_name": group_name, "date": date_str, "sales": sales}
    )
    logger.info("Queued sales %.2f from %s", sales, group_name)

    context.bot.send_message(
        chat_id=MY_TELEGRAM_ID,
        text=(
            f"*{group_name}* — {date_str}\n"
            f"Sales: {sales:,.2f}\n\n"
            f"What were today's expenses?"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Private message handler ──────────────────────────────────────────────────

def handle_private_message(update, context):
    user_id = update.message.chat_id
    text = update.message.text.strip()
    logger.info("Private message from user_id=%s: %r", user_id, text[:80])

    if user_id != MY_TELEGRAM_ID:
        update.message.reply_text(
            f"Unauthorized. Your Telegram ID is {user_id}."
        )
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
            update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
            return

        pending_list = "\n".join(
            f"- {p['group_name']} ({p['sales']:,.2f})" for p in pending_expenses
        )
        update.message.reply_text(
            f"Still waiting for expenses for:\n{pending_list}\n\nPlease reply with a number."
        )

    # Natural language → GPT-4o
    ask_ai(update, text)


# ─── AI query ─────────────────────────────────────────────────────────────────

def ask_ai(update, query):
    logger.info("AI query: %r", query[:80])
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

    update.message.reply_text(reply)


# ─── Weekly summary (job) ─────────────────────────────────────────────────────

def weekly_summary(context):
    logger.info("Running weekly summary job")
    records = db.get_last_n_days(7)

    if not records:
        context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text="*Weekly Summary*\n\nNo records for the past 7 days.",
            parse_mode=ParseMode.MARKDOWN,
        )
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
        lines.append(
            f"*{g}*\n"
            f"  Sales: {v['sales']:,.2f}\n"
            f"  Expenses: {v['expenses']:,.2f}\n"
            f"  Net: {v['net']:,.2f}\n"
        )
    lines += [
        "---",
        f"*Total Sales:*    {total_sales:,.2f}",
        f"*Total Expenses:* {total_expenses:,.2f}",
        f"*Total Net:*      {total_net:,.2f}",
    ]
    context.bot.send_message(
        chat_id=MY_TELEGRAM_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    try:
        db.init_db()
        logger.info("Database ready: %s", os.environ.get("DB_PATH", "accountant.db"))
    except Exception as e:
        logger.critical("Database init failed: %s", e)
        sys.exit(1)

    updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.chat_type.private, handle_group_message))
    dp.add_handler(MessageHandler(Filters.text & Filters.chat_type.private, handle_private_message))

    tz = pytz.timezone("Asia/Nicosia")
    updater.job_queue.run_daily(
        weekly_summary,
        time=dtime(8, 0, 0, tzinfo=tz),
        days=(0,),
        name="weekly_summary",
    )
    logger.info("Weekly summary job scheduled — Mondays 08:00 Nicosia")

    logger.info("Starting polling...")
    updater.start_polling()
    logger.info("Bot is running.")
    updater.idle()


if __name__ == "__main__":
    main()
