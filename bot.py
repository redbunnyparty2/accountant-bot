import asyncio
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, request
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import database as db

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Config ─────────────────────────────────────────────────────────────────────

TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_KEY     = os.environ.get("OPENAI_API_KEY", "")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))
WEBHOOK_BASE   = (
    os.environ.get("WEBHOOK_URL") or
    os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")

logger.info("=== STARTING ===")
logger.info("Python           : %s", sys.version)
logger.info("TOKEN set        : %s", bool(TOKEN))
logger.info("OPENAI_KEY set   : %s", bool(OPENAI_KEY))
logger.info("MY_TELEGRAM_ID   : %s", MY_TELEGRAM_ID)
logger.info("WEBHOOK_BASE     : %s", WEBHOOK_BASE or "(not set)")

if not TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN not set — exiting")
    sys.exit(1)

ai  = OpenAI(api_key=OPENAI_KEY)
app = Flask(__name__)

# Pending expense queue
pending: list[dict] = []

# ── Persistent async event loop in a daemon thread ─────────────────────────────
#
# Python 3.12+ removed implicit loop creation. The correct pattern for mixing
# sync Flask with async PTB is:
#   1. Explicitly create one event loop.
#   2. Run it forever in a background thread.
#   3. Submit coroutines via run_coroutine_threadsafe() — no asyncio.run() conflicts.
#
_loop   = asyncio.new_event_loop()
_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_thread.start()


def _run(coro, timeout: int = 60):
    """Submit a coroutine to the persistent loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_number(text: str):
    cleaned = re.sub(r"[€$£,\s]", "", text.strip())
    m = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    return float(m.group().replace(",", ".")) if m else None


# ── PTB async handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    is_owner = MY_TELEGRAM_ID != 0 and uid == MY_TELEGRAM_ID
    logger.info("/start uid=%s owner=%s", uid, is_owner)
    await update.message.reply_text(
        f"✅ Bot is running!\n\n"
        f"Your Telegram ID: {uid}\n"
        f"Owner: {'yes ✅' if is_owner else 'no ❌  — set MY_TELEGRAM_ID=' + str(uid) + ' in Render'}\n\n"
        + ("Ask me anything — e.g. _show me this week_" if is_owner
           else "Only the owner can use this bot."),
        parse_mode="Markdown",
    )


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if "good night" not in update.message.text.lower():
        return

    group_id   = str(update.message.chat_id)
    group_name = update.message.chat.title or f"Group {group_id}"
    date_str   = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")
    logger.info("'good night' in %s", group_name)

    try:
        chat = await context.bot.get_chat(update.message.chat_id)
    except Exception as e:
        logger.error("get_chat failed: %s", e)
        await update.message.reply_text("⚠️ Could not read chat info.")
        return

    pinned_text = (chat.pinned_message.text or "") if chat.pinned_message else ""
    if not pinned_text:
        await update.message.reply_text("⚠️ No pinned message found. Pin today's sales number first.")
        return

    sales = extract_number(pinned_text)
    if sales is None:
        await update.message.reply_text(f"⚠️ Can't read a number from: \"{pinned_text}\"")
        return

    pending.append({"group_id": group_id, "group_name": group_name, "date": date_str, "sales": sales})
    logger.info("Queued €%.2f from %s", sales, group_name)

    await context.bot.send_message(
        chat_id=MY_TELEGRAM_ID,
        text=f"💰 *{group_name}* — {date_str}\nSales: €{sales:,.2f}\n\nWhat were today's expenses?",
        parse_mode="Markdown",
    )


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.message.chat_id
    text = update.message.text.strip()
    logger.info("DM uid=%s: %r", uid, text[:80])

    if uid != MY_TELEGRAM_ID:
        await update.message.reply_text(f"⛔ Unauthorized. Your ID: {uid}")
        return

    if pending:
        amount = extract_number(text)
        if amount is not None:
            p   = pending.pop(0)
            net = p["sales"] - amount
            db.save_record(p["group_id"], p["group_name"], p["date"], p["sales"], amount, net)
            logger.info("Saved %s sales=%.2f exp=%.2f net=%.2f",
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
            await update.message.reply_text(reply, parse_mode="Markdown")
            return

        plist = "\n".join(f"• {p['group_name']} (€{p['sales']:,.2f})" for p in pending)
        await update.message.reply_text(
            f"⏳ Waiting for expenses:\n{plist}\n\nReply with a number."
        )

    await _ask_gpt(update, text)


async def _ask_gpt(update: Update, query: str):
    logger.info("GPT query: %r", query[:80])
    records = db.get_all_records()
    today   = datetime.now(pytz.timezone("Asia/Nicosia")).strftime("%Y-%m-%d")
    prompt  = (
        f"You are a personal AI accountant. Today: {today}.\n\n"
        f"Records (JSON):\n{json.dumps(records, indent=2, default=str)}\n\n"
        "Use € for currency. Format numbers with commas. Be concise and use emojis."
    )
    try:
        resp  = ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user",   "content": query},
            ],
            max_tokens=700,
            temperature=0.3,
        )
        reply = resp.choices[0].message.content
    except Exception as e:
        reply = f"❌ AI error: {e}"
        logger.error("GPT error: %s", e)
    await update.message.reply_text(reply)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("PTB error: %s", context.error, exc_info=context.error)


# ── Build & initialise PTB (in the persistent loop) ───────────────────────────

ptb = Application.builder().token(TOKEN).build()
ptb.add_error_handler(_on_error)
ptb.add_handler(CommandHandler("start", cmd_start))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.ChatType.PRIVATE, on_group_message))
ptb.add_handler(MessageHandler(filters.TEXT &  filters.ChatType.PRIVATE, on_private_message))

_run(ptb.initialize())
logger.info("PTB initialised")

# ── Flask routes ───────────────────────────────────────────────────────────────

@app.post("/webhook")
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "ok", 200
    try:
        update = Update.de_json(data, ptb.bot)
        _run(ptb.process_update(update))
    except Exception as e:
        logger.error("process_update error: %s", e)
    return "ok", 200


@app.get("/health")
def health():
    return {"status": "ok", "pending": len(pending)}, 200


# ── Weekly summary ─────────────────────────────────────────────────────────────

async def _weekly_summary():
    records = db.get_last_n_days(7)
    if not records:
        await ptb.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text="📊 *Weekly Summary*\n\nNo records in the last 7 days.",
            parse_mode="Markdown",
        )
        return

    by_group: dict = {}
    for r in records:
        g = r["group_name"]
        if g not in by_group:
            by_group[g] = {"sales": 0.0, "expenses": 0.0, "net": 0.0}
        by_group[g]["sales"]    += r["sales"] or 0
        by_group[g]["expenses"] += r["expenses"] or 0
        by_group[g]["net"]      += r["net_revenue"] or 0

    ts = sum(v["sales"] for v in by_group.values())
    te = sum(v["expenses"] for v in by_group.values())
    tn = sum(v["net"] for v in by_group.values())

    lines = ["📊 *Weekly Summary (last 7 days)*\n"]
    for g, v in sorted(by_group.items(), key=lambda x: x[1]["net"], reverse=True):
        lines.append(
            f"*{g}*\n"
            f"  Sales: €{v['sales']:,.2f}\n"
            f"  Expenses: €{v['expenses']:,.2f}\n"
            f"  Net: €{v['net']:,.2f}\n"
        )
    lines += [
        "─────────────────",
        f"*Total Sales:*    €{ts:,.2f}",
        f"*Total Expenses:* €{te:,.2f}",
        f"*Total Net:*      €{tn:,.2f}",
    ]
    await ptb.bot.send_message(
        chat_id=MY_TELEGRAM_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


def _weekly_summary_job():
    logger.info("Weekly summary job triggered")
    _run(_weekly_summary())


scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Nicosia"))
scheduler.add_job(_weekly_summary_job, "cron", day_of_week="mon", hour=8, minute=0)
scheduler.start()
logger.info("Scheduler started — weekly summary Mondays 08:00 Nicosia")

# ── Register webhook ───────────────────────────────────────────────────────────

if WEBHOOK_BASE:
    try:
        _run(ptb.bot.set_webhook(
            url=f"{WEBHOOK_BASE}/webhook",
            drop_pending_updates=True,
        ))
        logger.info("Webhook registered: %s/webhook", WEBHOOK_BASE)
    except Exception as e:
        logger.error("set_webhook failed: %s", e)
else:
    logger.warning("RENDER_EXTERNAL_URL not set — webhook not registered automatically")

# ── Database init ──────────────────────────────────────────────────────────────

try:
    db.init_db()
    logger.info("Database ready: %s", os.environ.get("DB_PATH", "accountant.db"))
except Exception as e:
    logger.critical("Database init failed: %s", e)
    sys.exit(1)

# ── Dev entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
