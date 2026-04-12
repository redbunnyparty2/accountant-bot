import os
import re
import json
import time
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request as flask_request
from openai import OpenAI
import ssl
import pg8000
from urllib.parse import urlparse
import schedule

app = Flask(__name__)

BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MY_TELEGRAM_ID   = int(os.environ.get("MY_TELEGRAM_ID", "0"))
WEBHOOK_BASE     = os.environ.get("WEBHOOK_BASE", "")
openai_client    = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
DATABASE_URL     = os.environ.get("DATABASE_URL", "")

FAMILY_GROUP_KEYWORDS = ["family", "expense", "расход", "семья"]

GEL_TO_EUR = 1 / 3.0   # 1 EUR = 3.0 GEL
USD_TO_EUR = 1 / 1.08  # 1 EUR ≈ 1.08 USD

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
Employees:
{employee_data}

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
  <title>Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0d;color:#e0e0e0;padding:16px;max-width:960px;margin:auto}
    .section-header{display:flex;align-items:center;gap:10px;margin:24px 0 12px}
    .section-header h2{color:#fff;font-size:1rem;font-weight:600}
    .section-header .pill{font-size:0.7rem;padding:2px 8px;border-radius:999px;font-weight:500}
    .pill-biz{background:#14532d;color:#4ade80}
    .pill-fam{background:#1e1b4b;color:#a5b4fc}
    .pill-emp{background:#431407;color:#fb923c}
    .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
    .stat{background:#1a1a1a;border-radius:12px;padding:14px;text-align:center;border:1px solid #2a2a2a}
    .stat .lbl{font-size:0.68rem;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:0.5px}
    .stat .val{font-size:1.5rem;font-weight:700;color:#4ade80}
    .stat .val.neg{color:#f87171} .stat .val.neu{color:#a5b4fc}
    .card{background:#1a1a1a;border-radius:12px;padding:16px;margin-bottom:12px;border:1px solid #2a2a2a}
    .card h3{font-size:0.75rem;color:#666;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:12px}
    .chart-wrap{position:relative;height:190px}
    table{width:100%;border-collapse:collapse;font-size:0.79rem}
    td,th{padding:7px 6px;text-align:left;border-bottom:1px solid #1e1e1e}
    th{color:#555;font-weight:500;font-size:0.7rem;text-transform:uppercase}
    td.pos{color:#4ade80} td.neg{color:#f87171} td.neu{color:#a5b4fc}
    .cat-row{display:flex;align-items:center;gap:8px;margin-bottom:7px}
    .cat-row .cn{width:110px;font-size:0.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .cat-row .bg{flex:1;height:11px;background:#222;border-radius:5px;overflow:hidden}
    .cat-row .fill{height:100%;border-radius:5px}
    .cat-row .ca{width:68px;text-align:right;font-size:0.78rem;color:#777}
    .empty{color:#444;font-size:0.79rem;padding:6px 0}
    .divider{height:1px;background:#1e1e1e;margin:8px 0 20px}
    .ts{font-size:0.68rem;color:#333;text-align:right;margin-top:16px}
    @media(max-width:540px){.stats{grid-template-columns:1fr 1fr}.stat .val{font-size:1.2rem}}
  </style>
</head>
<body>

  <!-- ── BUSINESS ── -->
  <div class="section-header">
    <h2>📈 Business</h2><span class="pill pill-biz">revenue</span>
  </div>
  <div class="stats">
    <div class="stat"><div class="lbl">Sales this month</div><div class="val" id="b-sales">…</div></div>
    <div class="stat"><div class="lbl">Expenses</div><div class="val neg" id="b-exp">…</div></div>
    <div class="stat"><div class="lbl">Net profit</div><div class="val" id="b-net">…</div></div>
  </div>
  <div class="card">
    <h3>Daily revenue — last 30 days</h3>
    <div class="chart-wrap"><canvas id="dailyChart"></canvas></div>
  </div>
  <div class="card">
    <h3>Per group — this month</h3>
    <div class="chart-wrap"><canvas id="groupChart"></canvas></div>
  </div>
  <div class="card">
    <h3>Recent business transactions</h3>
    <table>
      <thead><tr><th>Date</th><th>Group</th><th>Category</th><th>Sales</th><th>Expenses</th><th>Net</th></tr></thead>
      <tbody id="biz-tbody"></tbody>
    </table>
  </div>

  <div class="divider"></div>

  <!-- ── FAMILY ── -->
  <div class="section-header">
    <h2>👨‍👩‍👧 Family</h2><span class="pill pill-fam">expenses</span>
  </div>
  <div class="stats">
    <div class="stat"><div class="lbl">Total this month</div><div class="val neu" id="f-total">…</div></div>
    <div class="stat"><div class="lbl">Top category</div><div class="val neu" id="f-top" style="font-size:1rem">…</div></div>
    <div class="stat"><div class="lbl">Entries</div><div class="val neu" id="f-count">…</div></div>
  </div>
  <div class="card">
    <h3>Breakdown by category</h3>
    <div id="fam-cats"></div>
  </div>

  <div class="divider"></div>

  <!-- ── EMPLOYEES ── -->
  <div class="section-header">
    <h2>👷 Employees</h2><span class="pill pill-emp">salary &amp; collections</span>
  </div>
  <div class="card">
    <h3>Summary</h3>
    <table>
      <thead><tr><th>Name</th><th>Group</th><th>Collections</th><th>Expenses</th><th>Salary paid</th><th>Balance owed</th></tr></thead>
      <tbody id="emp-tbody"></tbody>
    </table>
  </div>

  <div class="ts" id="ts"></div>

  <script>
  const fmt  = v => '\u20ac' + (+(v||0)).toFixed(0);
  const COLS = ['#6366f1','#8b5cf6','#ec4899','#f59e0b','#10b981','#3b82f6','#ef4444','#14b8a6'];
  const cOpts = () => ({
    responsive:true, maintainAspectRatio:false,
    plugins:{legend:{labels:{color:'#666',boxWidth:10,font:{size:10}}}},
    scales:{x:{ticks:{color:'#444',maxTicksLimit:8},grid:{color:'#161616'}},
            y:{ticks:{color:'#444'},grid:{color:'#161616'}}}
  });

  fetch('/dashboard/data').then(r=>r.json()).then(d=>{
    const b = d.business;

    // Business stats
    document.getElementById('b-sales').textContent = fmt(b.stats.total_sales);
    document.getElementById('b-exp').textContent   = fmt(b.stats.total_expenses);
    const bn = document.getElementById('b-net');
    bn.textContent = fmt(b.stats.total_net);
    if (b.stats.total_net < 0) bn.classList.add('neg');

    // Daily chart
    new Chart(document.getElementById('dailyChart'),{
      type:'line',
      data:{
        labels:b.daily.map(x=>x.date.slice(5)),
        datasets:[
          {label:'Sales',data:b.daily.map(x=>x.sales),borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,0.07)',tension:0.4,fill:true,pointRadius:2},
          {label:'Net',  data:b.daily.map(x=>x.net),  borderColor:'#6366f1',backgroundColor:'rgba(99,102,241,0.07)',tension:0.4,fill:true,pointRadius:2}
        ]
      }, options:cOpts()
    });

    // Per-group bar chart
    new Chart(document.getElementById('groupChart'),{
      type:'bar',
      data:{
        labels:b.by_group.map(x=>x.name),
        datasets:[
          {label:'Sales',data:b.by_group.map(x=>x.sales),backgroundColor:'rgba(74,222,128,0.75)',borderRadius:4},
          {label:'Net',  data:b.by_group.map(x=>x.net),  backgroundColor:'rgba(99,102,241,0.75)',borderRadius:4}
        ]
      }, options:cOpts()
    });

    // Business records table
    document.getElementById('biz-tbody').innerHTML = b.recent_records.length
      ? b.recent_records.map(r=>`<tr>
          <td>${r.date}</td><td>${r.group_name}</td>
          <td style="color:#555">${r.expense_category||''}</td>
          <td class="pos">${fmt(r.sales)}</td>
          <td class="neg">${fmt(r.expenses)}</td>
          <td class="${r.net>=0?'pos':'neg'}">${fmt(r.net)}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty">No records yet</td></tr>';

    // Family stats
    const f = d.family;
    document.getElementById('f-total').textContent = fmt(f.total);
    document.getElementById('f-count').textContent = f.count;
    document.getElementById('f-top').textContent   = f.by_category.length ? f.by_category[0].category : '—';
    const maxF = f.by_category.length ? Math.max(...f.by_category.map(c=>c.amount)) : 1;
    document.getElementById('fam-cats').innerHTML = f.by_category.length
      ? f.by_category.map((c,i)=>`<div class="cat-row">
          <div class="cn">${c.category}</div>
          <div class="bg"><div class="fill" style="width:${(c.amount/maxF*100).toFixed(0)}%;background:${COLS[i%COLS.length]}"></div></div>
          <div class="ca">${fmt(c.amount)}</div>
        </div>`).join('')
      : '<div class="empty">No family expenses yet</div>';

    // Employees
    document.getElementById('emp-tbody').innerHTML = d.employees.length
      ? d.employees.map(e=>`<tr>
          <td>${e.name}</td>
          <td style="color:#555">${e.group_name}</td>
          <td class="pos">${fmt(e.collections)}</td>
          <td class="neg">${fmt(e.expenses)}</td>
          <td class="neu">${fmt(e.paid)}</td>
          <td class="${e.balance_owed>0?'neg':'pos'}">${fmt(e.balance_owed)}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty">No employees yet</td></tr>';

    document.getElementById('ts').textContent = 'Updated: ' + new Date().toLocaleString();
  }).catch(e=>console.error(e));
  </script>
</body>
</html>"""


# ── DB ─────────────────────────────────────────────────────────────────────────

def get_conn():
    url = urlparse(DATABASE_URL)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.connect(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/"),
        user=url.username,
        password=url.password,
        ssl_context=ssl_ctx,
    )


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
        group_type TEXT DEFAULT 'business',
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS group_type TEXT DEFAULT 'business'")
    c.execute('''CREATE TABLE IF NOT EXISTS group_messages (
        id SERIAL PRIMARY KEY,
        group_id TEXT, group_name TEXT, text TEXT,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS family_expenses (
        id SERIAL PRIMARY KEY,
        date TEXT, category TEXT, description TEXT,
        amount REAL, added_by TEXT,
        payment_method TEXT DEFAULT 'cash',
        currency TEXT DEFAULT 'EUR',
        amount_original REAL,
        amount_eur REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("ALTER TABLE family_expenses ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'cash'")
    c.execute("ALTER TABLE family_expenses ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'EUR'")
    c.execute("ALTER TABLE family_expenses ADD COLUMN IF NOT EXISTS amount_original REAL")
    c.execute("ALTER TABLE family_expenses ADD COLUMN IF NOT EXISTS amount_eur REAL")
    c.execute('''CREATE TABLE IF NOT EXISTS family_balances (
        id SERIAL PRIMARY KEY,
        date TEXT, method TEXT, amount REAL,
        currency TEXT DEFAULT 'EUR', amount_eur REAL,
        added_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts_sent (
        id SERIAL PRIMARY KEY,
        alert_key TEXT UNIQUE,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS employees (
        id SERIAL PRIMARY KEY,
        name TEXT, group_id TEXT, group_name TEXT,
        base_salary REAL, base_days INTEGER,
        bonus_salary REAL, bonus_days INTEGER,
        bonus_start TEXT, bonus_end TEXT,
        start_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS employee_payments (
        id SERIAL PRIMARY KEY,
        employee_id INTEGER, date TEXT, amount REAL, note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS employee_collections (
        id SERIAL PRIMARY KEY,
        employee_id INTEGER, date TEXT, amount REAL, note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS employee_expenses (
        id SERIAL PRIMARY KEY,
        employee_id INTEGER, date TEXT, amount REAL, category TEXT, note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
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


def parse_currency_amount(text):
    """Return (currency, amount_original, amount_eur) by scanning text for currency markers."""
    t = text.strip()
    patterns = [
        # GEL — must check before generic number fallback
        (r'(\d+(?:[.,]\d+)?)\s*(?:lari|gel|ლ|₾)', "GEL", GEL_TO_EUR),
        (r'(?:lari|gel|ლ|₾)\s*(\d+(?:[.,]\d+)?)',  "GEL", GEL_TO_EUR),
        # USD
        (r'\$\s*(\d+(?:[.,]\d+)?)',                  "USD", USD_TO_EUR),
        (r'(\d+(?:[.,]\d+)?)\s*(?:usd|\$|dollar)',   "USD", USD_TO_EUR),
        # EUR explicit
        (r'€\s*(\d+(?:[.,]\d+)?)',                   "EUR", 1.0),
        (r'(\d+(?:[.,]\d+)?)\s*(?:eur|euro)',        "EUR", 1.0),
        # fallback — any bare number = EUR
        (r'(\d+(?:[.,]\d+)?)',                        "EUR", 1.0),
    ]
    for pattern, currency, rate in patterns:
        m = re.search(pattern, t, re.I)
        if m:
            amount = float(m.group(1).replace(",", "."))
            return currency, amount, round(amount * rate, 2)
    return None, None, None


def detect_payment_method(text):
    t = text.lower()
    if re.search(r'cash[\s_-]?home|наличные[\s_-]?дома', t):
        return "cash_home"
    if re.search(r'\bcart\b|\bcard\b|\bкарт', t):
        return "card"
    if re.search(r'\bcash\b|\bнал\b|\bналич', t):
        return "cash"
    return "cash"  # default


def is_balance_message(text):
    """True when message is ONLY a method + number, i.e. a balance report not an expense."""
    return bool(re.match(
        r'^\s*(?:cash[\s_-]?home|cart|card|cash)\s+\d+(?:[.,]\d+)?\s*(?:lari|gel|ლ|₾|eur|euro|usd|\$|dollar|€)?\s*$',
        text.strip(), re.I))


def detect_group_type(chat_name, chat_id=None):
    if any(kw in chat_name.lower() for kw in ["family", "семья", "расход"]):
        return "family"
    if chat_id:
        emp = get_employee_by_group(chat_id)
        if emp:
            return "employee"
    # Match group name against employee names
    employees = get_all_employees()
    name_lower = chat_name.lower()
    for emp in employees:
        emp_name = (emp[1] or "").lower()
        if emp_name and (emp_name in name_lower or name_lower in emp_name):
            return "employee"
    return "business"


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
    group_type = detect_group_type(chat_name, chat_id)
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO groups (chat_id, chat_name, group_type) VALUES (%s,%s,%s) "
        "ON CONFLICT (chat_id) DO UPDATE SET chat_name=%s, group_type=%s",
        (str(chat_id), chat_name, group_type, chat_name, group_type))
    conn.commit()
    conn.close()
    print(f"Saved group: {chat_name} ({group_type}) (id={chat_id})", flush=True)


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
    c.execute("SELECT chat_id, chat_name, added_at, group_type FROM groups ORDER BY added_at ASC")
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
    """Pre-parse currency/method, then use GPT for category+description. Returns dict or None."""
    currency, amount_original, amount_eur = parse_currency_amount(text)
    if not amount_original:
        return None
    payment_method = detect_payment_method(text)
    try:
        r = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": (
                    "Extract expense category and description. Reply ONLY with valid JSON: "
                    "{\"category\": \"...\", \"description\": \"...\"} "
                    "Categories: Housing, Food, Kids, Transport, Health, Entertainment, Other."
                )},
                {"role": "user", "content": text}
            ],
            max_tokens=80, temperature=0,
        )
        data = json.loads(r.choices[0].message.content.strip())
        gpt_cat = data.get("category", "Other")
        standard = categorize_expense(data.get("description", "") + " " + gpt_cat)
        return {
            "category":       standard if standard != "Other" else gpt_cat,
            "description":    data.get("description", text),
            "amount":         amount_eur,          # store in EUR
            "amount_original": amount_original,
            "currency":       currency,
            "payment_method": payment_method,
        }
    except:
        return {
            "category":        categorize_expense(text),
            "description":     text,
            "amount":          amount_eur,
            "amount_original": amount_original,
            "currency":        currency,
            "payment_method":  payment_method,
        }


def save_family_balance(date, method, amount, currency, amount_eur, added_by):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO family_balances (date, method, amount, currency, amount_eur, added_by) VALUES (%s,%s,%s,%s,%s,%s)",
        (date, method, amount, currency, amount_eur, added_by))
    conn.commit()
    conn.close()


def save_family_expense(date, category, description, amount, added_by,
                        payment_method="cash", currency="EUR", amount_original=None, amount_eur=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO family_expenses (date, category, description, amount, added_by, "
        "payment_method, currency, amount_original, amount_eur) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (date, category, description, amount, added_by,
         payment_method, currency, amount_original or amount, amount_eur or amount))
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
    by_cat    = {}
    by_method = {}
    for r in rows:
        amt_eur = r[9] if len(r) > 9 and r[9] else r[4] or 0
        by_cat[r[2]]   = by_cat.get(r[2], 0.0)   + amt_eur
        method = r[6] if len(r) > 6 and r[6] else "cash"
        by_method[method] = by_method.get(method, 0.0) + amt_eur
    total = sum(by_cat.values())
    lines = [f"  {cat}: €{amt:.2f}" for cat, amt in sorted(by_cat.items())]
    lines.append(f"  TOTAL: €{total:.2f}")
    lines.append(f"  By method: " + ", ".join(f"{m} €{v:.0f}" for m, v in by_method.items()))
    # Latest balances
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT method, amount, currency, amount_eur FROM family_balances ORDER BY created_at DESC LIMIT 6")
    bals = c.fetchall()
    conn.close()
    if bals:
        lines.append("  Current balances: " + ", ".join(
            f"{b[0]} {b[1]}{b[2]} (€{b[3]:.0f})" if b[2] != "EUR" else f"{b[0]} €{b[1]:.0f}"
            for b in bals))
    return "\n".join(lines)


# ── Employees ─────────────────────────────────────────────────────────────────

def get_all_employees():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM employees ORDER BY name ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def get_employee_by_group(group_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM employees WHERE group_id=%s LIMIT 1", (str(group_id),))
    row = c.fetchone()
    conn.close()
    return row


def calc_salary_owed(emp):
    """Return (total_owed, total_paid, balance) for an employee row."""
    emp_id       = emp[0]
    base_salary  = emp[4] or 0
    base_days    = emp[5] or 1
    bonus_salary = emp[6] or 0
    bonus_days   = emp[7] or 1
    bonus_start  = str(emp[8]) if emp[8] else None
    bonus_end    = str(emp[9]) if emp[9] else None
    start_date   = str(emp[10]) if emp[10] else None

    if not start_date:
        return 0.0, 0.0, 0.0

    base_daily  = base_salary / base_days
    bonus_daily = bonus_salary / bonus_days if bonus_salary else base_daily

    today = datetime.now().date()
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    if start > today:
        total_owed = 0.0
    else:
        total_days = (today - start).days + 1
        if bonus_start and bonus_end:
            bs = datetime.strptime(bonus_start, "%Y-%m-%d").date()
            be = datetime.strptime(bonus_end,   "%Y-%m-%d").date()
            overlap_start = max(start, bs)
            overlap_end   = min(today, be)
            bonus_worked  = max(0, (overlap_end - overlap_start).days + 1)
            base_worked   = total_days - bonus_worked
        else:
            bonus_worked = 0
            base_worked  = total_days
        total_owed = base_worked * base_daily + bonus_worked * bonus_daily

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(amount),0) FROM employee_payments WHERE employee_id=%s", (emp_id,))
    total_paid = c.fetchone()[0] or 0.0
    conn.close()
    return round(total_owed, 2), round(total_paid, 2), round(total_owed - total_paid, 2)


def save_employee_collection(emp_id, date, amount, note):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO employee_collections (employee_id, date, amount, note) VALUES (%s,%s,%s,%s)",
              (emp_id, date, amount, note))
    conn.commit()
    conn.close()


def save_employee_expense_record(emp_id, date, amount, category, note):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO employee_expenses (employee_id, date, amount, category, note) VALUES (%s,%s,%s,%s,%s)",
              (emp_id, date, amount, category, note))
    conn.commit()
    conn.close()


def save_employee_payment(emp_id, date, amount, note):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO employee_payments (employee_id, date, amount, note) VALUES (%s,%s,%s,%s)",
              (emp_id, date, amount, note))
    conn.commit()
    conn.close()


def parse_employee_message(text, is_lucky):
    """Return (type, amount, category, note) or (None,None,None,None)."""
    t = text.strip().replace("€", "")
    # Expense keywords take priority
    m = re.search(r'(?:spent|expense|exp|bought|потратил|купил)\s+(\d+(?:[.,]\d+)?)\s*(.*)', t, re.I)
    if m:
        amount   = float(m.group(1).replace(",", "."))
        note_txt = m.group(2).strip() or t
        return "expense", amount, categorize_expense(t), t
    # Extract any number
    m = re.search(r'(\d+(?:[.,]\d+)?)', t)
    if not m:
        return None, None, None, None
    amount = float(m.group(1).replace(",", "."))
    if is_lucky:
        return "payment", amount, "", t
    else:
        return "collection", amount, "", t


def build_employees_summary():
    employees = get_all_employees()
    if not employees:
        return "No employees registered yet."
    lines = []
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_conn()
    c = conn.cursor()
    for emp in employees:
        emp_id = emp[0]
        owed, paid, balance = calc_salary_owed(emp)
        c.execute("SELECT COALESCE(SUM(amount),0) FROM employee_collections WHERE employee_id=%s AND date>=%s",
                  (emp_id, month_ago))
        collections = c.fetchone()[0] or 0
        c.execute("SELECT COALESCE(SUM(amount),0) FROM employee_expenses WHERE employee_id=%s AND date>=%s",
                  (emp_id, month_ago))
        expenses = c.fetchone()[0] or 0
        lines.append(f"  {emp[1]} ({emp[3]}): Owed €{owed} | Paid €{paid} | Balance €{balance} | Collections €{collections:.0f} | Expenses €{expenses:.0f}")
    conn.close()
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
        employee_data        = build_employees_summary(),
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
        employee = get_employee_by_group(chat_id)
        if employee and text:
            is_lucky_msg = (from_id == MY_TELEGRAM_ID)
            etype, amount, category, note = parse_employee_message(text, is_lucky_msg)
            today_str = datetime.now().strftime("%Y-%m-%d")
            if etype == "payment" and amount:
                save_employee_payment(employee[0], today_str, amount, note)
                send_message(chat_id, f"💸 Payment €{amount:.0f} saved for {employee[1]}")
            elif etype == "collection" and amount:
                save_employee_collection(employee[0], today_str, amount, note)
                send_message(chat_id, f"✅ Collection €{amount:.0f} saved for {employee[1]}")
            elif etype == "expense" and amount:
                save_employee_expense_record(employee[0], today_str, amount, category, note)
                send_message(chat_id, f"💰 Expense €{amount:.0f} ({category}) saved for {employee[1]}")
        elif is_family_group(chat_name):
            today_str = datetime.now().strftime("%Y-%m-%d")
            if is_balance_message(text):
                # Balance report: "cash 2600", "card 580 gel", "cash home 1200"
                method = detect_payment_method(text)
                currency, orig, eur = parse_currency_amount(text)
                if orig:
                    save_family_balance(today_str, method, orig, currency, eur, sender)
                    if currency != "EUR":
                        send_message(chat_id, f"💰 Balance saved: {method} {orig:.0f} {currency} (€{eur:.0f})")
                    else:
                        send_message(chat_id, f"💰 Balance saved: {method} €{orig:.0f}")
            else:
                expense = parse_expense_with_gpt(text, sender)
                if expense:
                    save_family_expense(
                        date           = today_str,
                        category       = expense["category"],
                        description    = expense["description"],
                        amount         = expense["amount"],
                        added_by       = sender,
                        payment_method = expense["payment_method"],
                        currency       = expense["currency"],
                        amount_original= expense["amount_original"],
                        amount_eur     = expense["amount"],
                    )
                    orig = expense["amount_original"]
                    curr = expense["currency"]
                    eur  = expense["amount"]
                    method = expense["payment_method"]
                    if curr != "EUR":
                        send_message(chat_id,
                            f"✅ {expense['category']} {orig:.0f} {curr} → €{eur:.0f} ({method})")
                    else:
                        send_message(chat_id,
                            f"✅ {expense['category']} €{eur:.0f} ({method})")
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

    # Build group_id → group_type mapping
    groups_info = get_groups()  # (chat_id, chat_name, added_at, group_type)
    type_by_id  = {g[0]: (g[3] or "business") for g in groups_info}

    # All records, split by group type
    all_records = get_records_since(month_ago)
    biz_records = [r for r in all_records if type_by_id.get(str(r[1]), "business") == "business"]

    # Business stats
    biz_sales = sum(r[4] or 0 for r in biz_records)
    biz_exp   = sum(r[5] or 0 for r in biz_records)
    biz_net   = sum(r[6] or 0 for r in biz_records)

    # Business daily
    daily_map = {}
    for r in biz_records:
        d = r[3]
        if d not in daily_map:
            daily_map[d] = {"date": d, "sales": 0.0, "net": 0.0}
        daily_map[d]["sales"] += r[4] or 0
        daily_map[d]["net"]   += r[6] or 0
    daily_list = sorted(daily_map.values(), key=lambda x: x["date"])

    # Business by group
    by_group = summarise_by_group(biz_records)
    by_group_list = [{"name": k, "sales": round(v["sales"], 2), "net": round(v["net"], 2)}
                     for k, v in by_group.items()]

    # Business recent records
    biz_recent = [r for r in get_records_since(week_ago)
                  if type_by_id.get(str(r[1]), "business") == "business"][:20]
    recent_list = [{"date": r[3], "group_name": r[2],
                    "expense_category": r[7] if len(r) > 7 else "",
                    "sales": r[4], "expenses": r[5], "net": r[6]}
                   for r in biz_recent]

    # Family expenses
    fam_rows   = get_family_expenses_since(month_ago)
    fam_by_cat = {}
    for r in fam_rows:
        fam_by_cat[r[2]] = fam_by_cat.get(r[2], 0.0) + (r[4] or 0)
    fam_list  = [{"category": k, "amount": round(v, 2)}
                 for k, v in sorted(fam_by_cat.items(), key=lambda x: -x[1])]
    fam_total = round(sum(fam_by_cat.values()), 2)

    # Employee data
    employees = get_all_employees()
    emp_list  = []
    conn2 = get_conn()
    c2    = conn2.cursor()
    for emp in employees:
        emp_id = emp[0]
        owed, paid, balance = calc_salary_owed(emp)
        c2.execute("SELECT COALESCE(SUM(amount),0) FROM employee_collections WHERE employee_id=%s AND date>=%s",
                   (emp_id, month_ago))
        cols = c2.fetchone()[0] or 0
        c2.execute("SELECT COALESCE(SUM(amount),0) FROM employee_expenses WHERE employee_id=%s AND date>=%s",
                   (emp_id, month_ago))
        exps = c2.fetchone()[0] or 0
        emp_list.append({"name": emp[1], "group_name": emp[3] or "",
                         "collections": round(float(cols), 2), "expenses": round(float(exps), 2),
                         "paid": paid, "balance_owed": balance})
    conn2.close()

    return {
        "business": {
            "stats":          {"total_sales": round(biz_sales, 2),
                               "total_expenses": round(biz_exp, 2),
                               "total_net": round(biz_net, 2)},
            "daily":          daily_list,
            "by_group":       by_group_list,
            "recent_records": recent_list,
        },
        "family": {
            "by_category": fam_list,
            "total":       fam_total,
            "count":       len(fam_rows),
        },
        "employees": emp_list,
    }


# ── Other endpoints ────────────────────────────────────────────────────────────

@app.route("/my_groups")
def my_groups():
    groups = get_groups()
    return {"count": len(groups),
            "groups": [{"chat_id": g[0], "chat_name": g[1], "added_at": str(g[2]),
                        "group_type": g[3] or "business"} for g in groups]}


@app.route("/employees")
def employees_endpoint():
    employees = get_all_employees()
    result = []
    for emp in employees:
        owed, paid, balance = calc_salary_owed(emp)
        result.append({
            "id": emp[0], "name": emp[1], "group_name": emp[3],
            "base_salary": emp[4], "base_days": emp[5],
            "bonus_salary": emp[6], "bonus_days": emp[7],
            "bonus_start": str(emp[8]) if emp[8] else None,
            "bonus_end":   str(emp[9]) if emp[9] else None,
            "start_date":  str(emp[10]) if emp[10] else None,
            "salary_owed": owed, "salary_paid": paid, "balance": balance,
        })
    return {"count": len(result), "employees": result}


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
