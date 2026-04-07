import json
import logging
import os
import re
from datetime import datetime, time

import pytz
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MY_TELEGRAM_ID = int(os.environ["MY_TELEGRAM_ID"])

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Queue of pending expense requests from groups
# Each item: {"group_id", "group_name", "date", "sales"}
pending_expenses: list[dict] = []


# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_number(text: str) -> float | None:
    cleaned = re.sub(r"[€$£,\s]", "", text.strip())
    match = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    if match:
        return float(match.group().replace(",", "."))
    return None


# ─── /start command ───────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == MY_TELEGRAM_ID:
        await update.message.reply_text(
            "✅ *Bot is running!*\n\n"
            "You are registered as the owner.\n\n"
            "You can ask me anything, e.g.:\n"
            "• _show me this week_\n"
            "• _compare last 2 weeks_\n"
            "• _which group made most this month_\n\n"
            "I'll message you here every time a group sends 'good night'.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"👋 Bot is active.\n\nYour Telegram ID is: `{user_id}`",
            parse_mode="Markdown",
        )


# ─── Group handler ─────────────────────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Watches group chats for 'good night' from the admin."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower().strip()
    if "good night" not in text:
        return

    group_id = str(update.message.chat_id)
    group_name = update.message.chat.title or f"Group {group_id}"
    date_str = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")

    # Read pinned message
    chat = await context.bot.get_chat(update.message.chat_id)
    if not chat.pinned_message or not chat.pinned_message.text:
        await update.message.reply_text(
            "⚠️ No pinned message found. Please pin today's sales number first."
        )
        return

    sales = extract_number(chat.pinned_message.text)
    if sales is None:
        await update.message.reply_text(
            f"⚠️ Couldn't read a number from the pinned message: \"{chat.pinned_message.text}\""
        )
        return

    pending_expenses.append(
        {"group_id": group_id, "group_name": group_name, "date": date_str, "sales": sales}
    )

    await context.bot.send_message(
        chat_id=MY_TELEGRAM_ID,
        text=(
            f"💰 *{group_name}* — {date_str}\n"
            f"Sales: €{sales:,.2f}\n\n"
            f"What were today's expenses?"
        ),
        parse_mode="Markdown",
    )
    logger.info("Good night received from %s — sales €%.2f queued", group_name, sales)


# ─── Private message handler ───────────────────────────────────────────────────

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all private messages from the owner."""
    if update.message.chat_id != MY_TELEGRAM_ID:
        return

    text = update.message.text.strip()

    # If there are pending expense requests, try to consume one
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

            reply = (
                f"✅ *Saved — {pending['group_name']}* ({pending['date']})\n"
                f"Sales:    €{pending['sales']:,.2f}\n"
                f"Expenses: €{amount:,.2f}\n"
                f"Net:      €{net:,.2f}"
            )

            # Prompt for next pending if any
            if pending_expenses:
                nxt = pending_expenses[0]
                reply += (
                    f"\n\n💰 *{nxt['group_name']}* — {nxt['date']}\n"
                    f"Sales: €{nxt['sales']:,.2f}\n\n"
                    f"What were today's expenses?"
                )

            await update.message.reply_text(reply, parse_mode="Markdown")
            return

        # Non-number while expenses pending — remind owner
        pending_list = "\n".join(
            f"• {p['group_name']} (€{p['sales']:,.2f})" for p in pending_expenses
        )
        reminder = f"⏳ Still waiting for expenses for:\n{pending_list}\n\nReply with a number, or ask your question after."
        await update.message.reply_text(reminder)

    # Natural language query → GPT-4o
    await handle_ai_query(update, text)


# ─── AI query ─────────────────────────────────────────────────────────────────

async def handle_ai_query(update: Update, query: str):
    records = db.get_all_records()
    data_json = json.dumps(records, indent=2, default=str)
    today = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")

    system_prompt = f"""You are a personal AI accountant assistant. You have full access to the owner's sales and expense records across multiple business locations.

Today's date: {today}

All records (JSON):
{data_json}

Rules:
- Always use € for currency
- Format numbers with comma separators (e.g. €1,234.50)
- Be concise and clear
- Use emojis to make summaries readable
- When comparing periods, show totals and highlight the best performer
- If no data exists for a requested period, say so clearly"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            max_tokens=700,
            temperature=0.3,
        )
        reply = response.choices[0].message.content
    except Exception as e:
        reply = f"❌ AI error: {e}"
        logger.error("OpenAI error: %s", e)

    await update.message.reply_text(reply)


# ─── Weekly summary job ────────────────────────────────────────────────────────

async def weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    records = db.get_last_n_days(7)

    if not records:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text="📊 *Weekly Summary*\n\nNo records found for the past 7 days.",
            parse_mode="Markdown",
        )
        return

    by_group: dict[str, dict] = {}
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

    lines = ["📊 *Weekly Summary* (last 7 days)\n"]
    for group, vals in sorted(by_group.items(), key=lambda x: x[1]["net"], reverse=True):
        lines.append(
            f"*{group}*\n"
            f"  Sales: €{vals['sales']:,.2f}\n"
            f"  Expenses: €{vals['expenses']:,.2f}\n"
            f"  Net: €{vals['net']:,.2f}\n"
        )
    lines.append("─────────────────")
    lines.append(f"*Total Sales:*    €{total_sales:,.2f}")
    lines.append(f"*Total Expenses:* €{total_expenses:,.2f}")
    lines.append(f"*Total Net:*      €{total_net:,.2f}")

    await context.bot.send_message(
        chat_id=MY_TELEGRAM_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()
    logger.info("Database initialized")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # /start command — works in private and groups
    app.add_handler(CommandHandler("start", start))

    # Group messages — detect "good night"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.ChatType.PRIVATE, handle_group_message))

    # Private messages — expenses + AI queries
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message))

    # Weekly summary every Monday at 08:00 Limassol time
    tz = pytz.timezone("Asia/Nicosia")
    app.job_queue.run_daily(
        weekly_summary,
        time=time(8, 0, 0, tzinfo=tz),
        days=(0,),  # Monday
        name="weekly_summary",
    )

    logger.info("Bot started — polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
