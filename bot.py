import os
import re
import json
import time
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request as flask_request
from openai import OpenAI
import psycopg2
import schedule

app = Flask(__name__)

BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MY_TELEGRAM_ID   = int(os.environ.get("MY_TELEGRAM_ID", "0"))
WEBHOOK_BASE     = os.environ.get("WEBHOOK_BASE", "")
openai_client    = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
DATABASE_URL     = os.environ.get("DATABASE_URL", "")

FAMILY_GROUP_KEYWORDS = ["family", "expense", "расход", "семья"]

EXPENSE_KEYWORDS = {
    "Housing":   ["rent", "apartment", "office", "utilities", "utility", "electricity", "water", "internet"],
    "Staff":     ["salary", "wages", "admin", "staff", "employee", "salaries", "wage"],
    "Supplies":  ["supplies", "products", "inventory", "stock", "equipment", "material"],
    "Marketing": ["ads", "marketing", "promotion", "advertising", "promo", "google", "facebook"],
    "Transport": ["taxi", "fuel", "uber", "transport", "delivery", "gas", "petrol", "bolt"],
    "Food":      ["food", "groceries", "restaurant", "lunch", "dinner", "cafe", "coffee"],
}

SYSTEM_PROMPT_TEMPLATE = """You are Megan, Lucky's assistant. You are fun, flirty, smart.
You talk about ANYTHING Lucky wants - business, personal, fun stuff.
You NEVER say 'let's keep things professional' - ever.
You are slightly naughty when Lucky is playful with you.
Short answers, real language, never robotic.

WHAT YOU CAN DO:
- Send messages to groups when Lucky asks
- Track business revenue and expenses
- Track family expenses by category
- See all messages from the groups
- Remember previous conversations

REAL DATA YOU HAVE ACCESS TO:
Groups connected: {groups_from_database}
Business revenue last 30 days:
{revenue_data}
Recent group messages: {recent_messages}
Family expenses this month:
{family_expenses_data}

RULES:
- Never say you are an AI or bot
- If groups table is empty, say: "I'm waiting for the first message from your groups babe 😏"
- If no data yet, say it naturally like a person would
- Respond in the same language Lucky writes in

SENDING MESSAGES TO GROUPS:
When Lucky asks you to send a message to a group, include this command on its own line in your reply:
[SEND:exact_group_name|message to send]
Example: [SEND:Red Umbrella Sara|We open at 7pm tonight 🕖]
You can include multiple SEND commands if sending to multiple groups.
After the command(s), write your normal reply to Lucky confirming what you sent."""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Accountant Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0d;color:#e0e0e0;padding:16px;max-width:900px;margin:auto}
    h1{color:#fff;font-size:1.3rem;margin-bottom:16px;display:flex;align-items:center;gap:8px}
    h2{color:#aaa;font-size:0.85rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
    .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px}
    .stat{background:#1a1a1a;border-radius:12px;padding:16px;text-align:center;border:1px solid #2a2a2a}
    .stat .lbl{font-size:0.7rem;color:#666;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px}
    .stat .val{font-size:1.6rem;font-weight:700;color:#4ade80}
    .stat .val.neg{color:#f87171}
    .card{background:#1a1a1a;border-radius:12px;padding:16px;margin-bottom:16px;border:1px solid #2a2a2a}
    .chart-wrap{position:relative;height:200px}
    table{width:100%;border-collapse:collapse;font-size:0.8rem}
    td,th{padding:8px 6px;text-align:left;border-bottom:1px solid #222}
    th{color:#666;font-weight:500;font-size:0.72rem;text-transform:uppercase}
    td.pos{color:#4ade80} td.neg{color:#f87171}
    .cat-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
    .cat-row .name{width:110px;font-size:0.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .cat-row .bar-bg{flex:1;height:12px;background:#2a2a2a;border-radius:6px;overflow:hidden}
    .cat-row .bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#6366f1,#8b5cf6)}
    .cat-row .amt{width:70px;text-align:right;font-size:0.78rem;color:#888}
    .empty{color:#555;font-size:0.8rem;padding:8px 0}
    .ts{font-size:0.7rem;color:#444;text-align:right;margin-top:16px}
    @media(max-width:500px){.stats{grid-template-columns:1fr 1fr}.stat .val{font-size:1.2rem}}
  </style>
</head>
<body>
  <h1>📊 Dashboard</h1>

  <div class="stats">
    <div class="stat"><div class="lbl">Sales this month</div><div class="val" id="s-sales">…</div></div>
    <div class="stat"><div class="lbl">Expenses this month</div><div class="val neg" id="s-exp">…</div></div>
    <div class="stat"><div class="lbl">Net profit</div><div class="val" id="s-net">…</div></div>
  </div>

  <div class="card">
    <h2>Daily Revenue — last 30 days</h2>
    <div class="chart-wrap"><canvas id="dailyChart"></canvas></div>
  </div>

  <div class="card">
    <h2>Revenue by Group — this month</h2>
    <div class="chart-wrap"><canvas id="groupChart"></canvas></div>
  </div>

  <div class="card">
    <h2>Family Expenses by Category — this month</h2>
    <div id="fam-cats"></div>
  </div>

  <div class="card">
    <h2>Recent Transactions</h2>
    <table>
      <thead><tr><th>Date</th><th>Group</th><th>Category</th><th>Sales</th><th>Expenses</th><th>Net</th></tr></thead>
      <tbody id="rec-tbody"></tbody>
    </table>
  </div>

  <div class="ts" id="ts"></div>

  <script>
  const fmt = v => '\u20ac' + (v||0).toFixed(0);
  const COLORS = ['#6366f1','#8b5cf6','#ec4899','#f59e0b','#10b981','#3b82f6','#ef4444'];

  fetch('/dashboard/data').then(r => r.json()).then(d => {
    document.getElementById('s-sales').textContent = fmt(d.stats.total_sales);
    document.getElementById('s-exp').textContent   = fmt(d.stats.total_expenses);
    const netEl = document.getElementById('s-net');
    netEl.textContent = fmt(d.stats.total_net);
    if (d.stats.total_net < 0) netEl.classList.add('neg');

    // Daily chart
    const cOpts = (color) => ({
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{labels:{color:'#888',boxWidth:12,font:{size:11}}}},
      scales:{x:{ticks:{color:'#555',maxTicksLimit:8},grid:{color:'#1f1f1f'}},
              y:{ticks:{color:'#555'},grid:{color:'#1f1f1f'}}}
    });
    new Chart(document.getElementById('dailyChart'), {
      type:'line',
      data:{
        labels: d.daily.map(x=>x.date.slice(5)),
        datasets:[
          {label:'Sales',data:d.daily.map(x=>x.sales),borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,0.08)',tension:0.4,fill:true,pointRadius:2},
          {label:'Net',  data:d.daily.map(x=>x.net),  borderColor:'#6366f1',backgroundColor:'rgba(99,102,241,0.08)',tension:0.4,fill:true,pointRadius:2}
        ]
      },
      options: cOpts()
    });

    // Group chart
    new Chart(document.getElementById('groupChart'), {
      type:'bar',
      data:{
        labels: d.by_group.map(x=>x.name),
        datasets:[
          {label:'Sales',data:d.by_group.map(x=>x.sales),backgroundColor:'rgba(74,222,128,0.7)',borderRadius:4},
          {label:'Net',  data:d.by_group.map(x=>x.net),  backgroundColor:'rgba(99,102,241,0.7)',borderRadius:4}
        ]
      },
      options: cOpts()
    });

    // Family categories
    const cats = d.family_by_category;
    const maxAmt = cats.length ? Math.max(...cats.map(c=>c.amount)) : 1;
    document.getElementById('fam-cats').innerHTML = cats.length
      ? cats.map((c,i) => `<div class="cat-row">
          <div class="name">${c.category}</div>
          <div class="bar-bg"><div class="bar-fill" style="width:${(c.amount/maxAmt*100).toFixed(0)}%;background:${COLORS[i%COLORS.length]}"></div></div>
          <div class="amt">${fmt(c.amount)}</div>
        </div>`).join('')
      : '<div class="empty">No family expenses yet</div>';

    // Recent records
    document.getElementById('rec-tbody').innerHTML = d.recent_records.length
      ? d.recent_records.map(r => `<tr>
          <td>${r.date}</td>
          <td>${r.group_name}</td>
          <td style="color:#888">${r.expense_category||''}</td>
          <td class="pos">${fmt(r.sales)}</td>
          <td class="neg">${fmt(r.expenses)}</td>
          <td class="${r.net>=0?'pos':'neg'}">${fmt(r.net)}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty">No records yet</td></tr>';

    document.getElementById('ts').textContent = 'Updated: ' + new Date().toLocaleString();
  }).catch(e => console.error(e));
  </script>
</body>
</html>"""


# ── DB ─────────────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records (
        id SERIAL PRIMARY KEY,
        group_id TEXT, group_name TEXT, date TEXT,
        sales REAL, expenses REAL, net REAL,
        expense_category TEXT DEFAULT 'Other',
        expense_description TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS expense_category TEXT DEFAULT 'Other'")
    c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS expense_description TEXT DEFAULT ''")
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
    c.execute('''CREATE TABLE IF NOT EXISTS family_expenses (
        id SERIAL PRIMARY KEY,
        date TEXT, category TEXT, description TEXT,
        amount REAL, added_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts_sent (
        id SERIAL PRIMARY KEY,
        alert_key TEXT UNIQUE,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()


# ── Helpers ────────────────────────────────────────────────────────────────────

def categorize_expense(text):
    t = text.lower()
    for category, keywords in EXPENSE_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return category
    return "Other"


def is_family_group(chat_name):
    return any(kw in chat_name.lower() for kw in FAMILY_GROUP_KEYWORDS)


# ── Conversations ──────────────────────────────────────────────────────────────

def save_message(user_id, role, content):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO conversations (user_id, role, content) VALUES (%s,%s,%s)",
              (str(user_id), role, content))
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


# ── Groups ─────────────────────────────────────────────────────────────────────

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
    c.execute("SELECT group_name, text, received_at FROM group_messages ORDER BY received_at DESC LIMIT %s",
              (limit,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "No recent messages stored yet."
    return "\n".join(f"[{r[2]}] {r[0]}: {r[1]}" for r in rows)


# ── Family expenses ────────────────────────────────────────────────────────────

def parse_expense_with_gpt(text, sender_name):
    """GPT-4 extracts category/description/amount. Returns dict or None."""
    try:
        r = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": (
                    "Extract expense info. Reply ONLY with valid JSON: "
                    "{\"category\": \"...\", \"description\": \"...\", \"amount\": 123.45} "
                    "Categories: Housing, Food, Kids, Transport, Health, Entertainment, Other. "
                    "If no amount found, return {\"amount\": null}."
                )},
                {"role": "user", "content": text}
            ],
            max_tokens=100, temperature=0,
        )
        data = json.loads(r.choices[0].message.content.strip())
        if not data.get("amount"):
            return None
        # Normalize category to our standard set
        gpt_cat = data.get("category", "Other")
        standard = categorize_expense(data.get("description", "") + " " + gpt_cat)
        if standard != "Other":
            data["category"] = standard
        return data
    except:
        return None


def save_family_expense(date, category, description, amount, added_by):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO family_expenses (date, category, description, amount, added_by) VALUES (%s,%s,%s,%s,%s)",
        (date, category, description, amount, added_by))
    conn.commit()
    conn.close()


def get_family_expenses_since(date_str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM family_expenses WHERE date >= %s ORDER BY date DESC", (date_str,))
    rows = c.fetchall()
    conn.close()
    return rows


def build_family_expenses_summary():
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = get_family_expenses_since(month_ago)
    if not rows:
        return "No family expenses recorded yet."
    by_cat = {}
    for r in rows:
        by_cat[r[2]] = by_cat.get(r[2], 0.0) + (r[4] or 0)
    total = sum(by_cat.values())
    lines = [f"  {cat}: €{amt:.2f}" for cat, amt in sorted(by_cat.items())]
    lines.append(f"  TOTAL: €{total:.2f}")
    return "\n".join(lines)


# ── Business records ───────────────────────────────────────────────────────────

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


def save_record(group_id, group_name, date, sales, expenses, expense_category="Other", expense_description=""):
    net = sales - expenses
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO records (group_id, group_name, date, sales, expenses, net, expense_category, expense_description) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (group_id, group_name, date, sales, expenses, net, expense_category, expense_description))
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
    lines         = []

    lines.append(f"=== TODAY ({today}) ===")
    if today_records:
        for r in today_records:
            lines.append(f"  {r[2]}: Sales €{r[4]} | Expenses €{r[5]} | Net €{r[6]}")
        tg = summarise_by_group(today_records)
        lines.append(f"  TOTAL: Sales €{sum(v['sales'] for v in tg.values()):.2f} | Net €{sum(v['net'] for v in tg.values()):.2f}")
    else:
        lines.append("  No records today yet.")

    lines.append(f"\n=== THIS WEEK ===")
    if week_records:
        wg = summarise_by_group(week_records)
        for name, v in wg.items():
            lines.append(f"  {name}: Sales €{v['sales']:.2f} | Expenses €{v['expenses']:.2f} | Net €{v['net']:.2f} ({v['days']} days)")
        lines.append(f"  TOTAL: Sales €{sum(v['sales'] for v in wg.values()):.2f} | Net €{sum(v['net'] for v in wg.values()):.2f}")
    else:
        lines.append("  No records this week.")

    lines.append(f"\n=== THIS MONTH ===")
    if month_records:
        mg = summarise_by_group(month_records)
        for name, v in mg.items():
            lines.append(f"  {name}: Sales €{v['sales']:.2f} | Expenses €{v['expenses']:.2f} | Net €{v['net']:.2f} ({v['days']} days)")
        lines.append(f"  TOTAL: Sales €{sum(v['sales'] for v in mg.values()):.2f} | Net €{sum(v['net'] for v in mg.values()):.2f}")
    else:
        lines.append("  No records this month.")

    if pending:
        lines.append(f"\n=== PENDING ===")
        lines.append(f"  {pending[2]} on {pending[4]}: Sales €{pending[3]} — expenses not entered yet")

    return "\n".join(lines)


# ── Alerts ─────────────────────────────────────────────────────────────────────

def alert_sent(key):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM alerts_sent WHERE alert_key=%s", (key,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def mark_alert(key):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO alerts_sent (alert_key) VALUES (%s) ON CONFLICT (alert_key) DO NOTHING", (key,))
    conn.commit()
    conn.close()


def check_unreported_groups():
    if datetime.now().hour != 23:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    c.execute("SELECT DISTINCT group_name FROM records WHERE date >= %s", (cutoff,))
    active = {r[0] for r in c.fetchall()}
    c.execute("SELECT DISTINCT group_name FROM records WHERE date=%s", (today,))
    reported = {r[0] for r in c.fetchall()}
    conn.close()
    for group in active - reported:
        key = f"no_report_{group}_{today}"
        if not alert_sent(key):
            send_message(MY_TELEGRAM_ID, f"⚠️ {group} hasn't reported today Lucky!")
            mark_alert(key)


def check_expense_increase():
    now = datetime.now()
    month_start      = now.replace(day=1).strftime("%Y-%m-%d")
    last_month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT expenses FROM records WHERE date >= %s", (month_start,))
    cur_exp = sum(r[0] or 0 for r in c.fetchall())
    c.execute("SELECT expenses FROM records WHERE date >= %s AND date < %s",
              (last_month_start, month_start))
    prev_exp = sum(r[0] or 0 for r in c.fetchall())
    conn.close()
    if prev_exp > 0 and cur_exp > prev_exp * 1.2:
        key = f"expense_increase_{now.strftime('%Y-%m')}"
        if not alert_sent(key):
            pct = int((cur_exp / prev_exp - 1) * 100)
            send_message(MY_TELEGRAM_ID,
                f"📈 Expenses up {pct}% this month babe, want me to break it down?")
            mark_alert(key)


def check_monday_summary():
    now = datetime.now()
    if now.weekday() != 0 or now.hour != 8:
        return
    key = f"weekly_summary_{now.strftime('%Y-%W')}"
    if alert_sent(key):
        return
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    records  = get_records_since(week_ago)
    if not records:
        return
    by_group = summarise_by_group(records)
    total_sales = sum(v["sales"] for v in by_group.values())
    total_net   = sum(v["net"]   for v in by_group.values())
    lines = ["Good morning Lucky! 📊 Here's last week:"]
    for name, v in by_group.items():
        lines.append(f"  {name}: Sales €{v['sales']:.0f} | Net €{v['net']:.0f}")
    lines.append(f"  Total: Sales €{total_sales:.0f} | Net €{total_net:.0f}")
    send_message(MY_TELEGRAM_ID, "\n".join(lines))
    mark_alert(key)


def run_scheduler():
    schedule.every().minute.do(check_unreported_groups)
    schedule.every().minute.do(check_expense_increase)
    schedule.every().minute.do(check_monday_summary)
    while True:
        schedule.run_pending()
        time.sleep(60)

threading.Thread(target=run_scheduler, daemon=True).start()


# ── GPT ────────────────────────────────────────────────────────────────────────

def ask_gpt(user_id, question):
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        revenue_data         = build_database_summary(),
        groups_from_database = ", ".join(g[1] for g in get_groups()) or "None yet",
        recent_messages      = get_recent_group_messages(),
        family_expenses_data = build_family_expenses_summary(),
    )
    save_message(user_id, "user", question)
    history = get_history(user_id)

    r = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "system", "content": system_prompt}] + history,
        max_tokens=500)
    raw_reply = r.choices[0].message.content

    successes, failures = [], []
    def execute_send(match):
        sent_to = send_to_group_by_name(match.group(1).strip(), match.group(2).strip())
        (successes if sent_to else failures).append(sent_to or match.group(1).strip())
        return ""

    clean_reply = re.sub(r"\[SEND:([^\|]+)\|([^\]]+)\]", execute_send, raw_reply).strip()
    if failures:
        clean_reply = "I don't have that group registered yet babe. Send a message in the group first so I can find it 😏"
    elif successes:
        clean_reply = "\n".join(f"✅ Sent to {g}" for g in successes) + ("\n" + clean_reply if clean_reply else "")

    save_message(user_id, "assistant", clean_reply)
    return clean_reply


# ── Webhook ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data    = flask_request.json or {}
    message = data.get("message") or data.get("edited_message") or {}
    if not message:
        return "ok"
    chat      = message.get("chat", {})
    chat_id   = chat.get("id")
    chat_type = chat.get("type", "")
    chat_name = chat.get("title") or chat.get("first_name") or chat.get("username") or str(chat_id)
    from_user = message.get("from", {})
    from_id   = from_user.get("id")
    sender    = from_user.get("first_name") or from_user.get("username") or str(from_id)
    text      = message.get("text", "").strip()

    if chat_type == "private" and from_id == MY_TELEGRAM_ID:
        if text == "/start":
            send_message(MY_TELEGRAM_ID, f"✅ Accountant Bot running!\nYour ID: {MY_TELEGRAM_ID}")
            return "ok"
        pending = get_pending()
        if pending:
            try:
                # Accept "150" or "150 rent supplies"
                parts    = text.replace("€","").replace(",",".").strip().split(None, 1)
                expenses = float(parts[0])
                desc     = parts[1] if len(parts) > 1 else ""
                category = categorize_expense(desc) if desc else "Other"
                net = save_record(pending[1], pending[2], pending[4], pending[3], expenses, category, desc)
                clear_pending()
                send_message(MY_TELEGRAM_ID,
                    f"✅ Saved!\n📍 {pending[2]}\n💰 Sales: €{pending[3]}\n💸 Expenses: €{expenses} ({category})\n📊 Net: €{net}")
            except:
                send_message(MY_TELEGRAM_ID, ask_gpt(from_id, text))
        else:
            send_message(MY_TELEGRAM_ID, ask_gpt(from_id, text))

    elif chat_type in ["group", "supergroup"]:
        save_group(chat_id, chat_name)
        if text:
            save_group_message(chat_id, chat_name, text)
        if is_family_group(chat_name):
            expense = parse_expense_with_gpt(text, sender)
            if expense:
                save_family_expense(
                    date        = datetime.now().strftime("%Y-%m-%d"),
                    category    = expense["category"],
                    description = expense["description"],
                    amount      = expense["amount"],
                    added_by    = sender,
                )
                send_message(chat_id, f"✅ Saved! {expense['category']} €{expense['amount']:.0f}")
        elif "good night" in text.lower():
            sales = get_pinned_number(chat_id)
            if sales:
                save_pending(str(chat_id), chat_name, sales)
                send_message(MY_TELEGRAM_ID, f"🌙 {chat_name}\n💰 Sales: €{sales}\nWhat were the expenses?")
            else:
                send_message(MY_TELEGRAM_ID, f"⚠️ {chat_name} said good night but no pinned number found.")
    return "ok"


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    return DASHBOARD_HTML


@app.route("/dashboard/data")
def dashboard_data():
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    week_ago  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    month_records = get_records_since(month_ago)
    total_sales    = sum(r[4] or 0 for r in month_records)
    total_expenses = sum(r[5] or 0 for r in month_records)
    total_net      = sum(r[6] or 0 for r in month_records)

    # Daily aggregation (fill all 30 days)
    daily_map = {}
    for r in month_records:
        d = r[3]
        if d not in daily_map:
            daily_map[d] = {"date": d, "sales": 0.0, "net": 0.0}
        daily_map[d]["sales"] += r[4] or 0
        daily_map[d]["net"]   += r[6] or 0
    daily_list = sorted(daily_map.values(), key=lambda x: x["date"])

    by_group = summarise_by_group(month_records)
    by_group_list = [{"name": k, "sales": round(v["sales"], 2), "net": round(v["net"], 2)}
                     for k, v in by_group.items()]

    fam_rows  = get_family_expenses_since(month_ago)
    fam_by_cat = {}
    for r in fam_rows:
        fam_by_cat[r[2]] = fam_by_cat.get(r[2], 0.0) + (r[4] or 0)
    fam_list = [{"category": k, "amount": round(v, 2)}
                for k, v in sorted(fam_by_cat.items(), key=lambda x: -x[1])]

    recent = get_records_since(week_ago)[:20]
    recent_list = [{"date": r[3], "group_name": r[2], "expense_category": r[7] if len(r) > 7 else "",
                    "sales": r[4], "expenses": r[5], "net": r[6]} for r in recent]

    return {
        "stats": {"total_sales": round(total_sales, 2),
                  "total_expenses": round(total_expenses, 2),
                  "total_net": round(total_net, 2)},
        "daily":              daily_list,
        "by_group":           by_group_list,
        "family_by_category": fam_list,
        "recent_records":     recent_list,
    }


# ── Other endpoints ────────────────────────────────────────────────────────────

@app.route("/my_groups")
def my_groups():
    groups = get_groups()
    return {"count": len(groups),
            "groups": [{"chat_id": g[0], "chat_name": g[1], "added_at": str(g[2])} for g in groups]}


@app.route("/set_webhook")
def set_webhook():
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                      json={"url": f"{WEBHOOK_BASE}/webhook"})
    return r.json()


@app.route("/")
def home():
    return {"status": "✅ Accountant Bot running"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
