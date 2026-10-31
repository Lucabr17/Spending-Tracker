from __future__ import annotations

import json
import uuid
from html import escape
from datetime import date, datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import re
from datetime import timedelta

import db as _db
from db import fetch_all, fetch_one, get_db, get_historical_month_count

# Every write goes through these wrappers so cached pages refresh straight away.
def _writes(fn):
    def wrapped(*a, **k):
        out=fn(*a, **k)
        try: _q.clear()
        except NameError: pass
        return out
    return wrapped
execute=_writes(_db.execute)
insert_transaction=_writes(_db.insert_transaction)
update_transaction=_writes(_db.update_transaction)
delete_transaction=_writes(_db.delete_transaction)
delete_recurring_expense=_writes(_db.delete_recurring_expense)
process_due_recurring=_writes(_db.process_due_recurring)
try:
    from utils import (
        CATEGORIES, EXPENSE_CATEGORIES, ask_gemini, categories_for,
        extract_image_transactions, get_secret, parse_natural_language,
        transaction_fingerprint, auto_categorize, merchant_keys,
        parse_csv_transactions, _strip_dates,
    )
except ImportError:
    from utils import (
        CATEGORIES, ask_gemini, extract_image_transactions, get_secret,
        parse_natural_language, transaction_fingerprint,
    )
    EXPENSE_CATEGORIES=[c for c in CATEGORIES if c not in {"Wage","Other Income"}]
    def categories_for(transaction_type):
        return ["Wage","Other Income"] if transaction_type=="Income" else EXPENSE_CATEGORIES

st.set_page_config(page_title="Personal Finance OS",page_icon="💰",
                   layout="wide",initial_sidebar_state="expanded")
get_db()

if not st.session_state.get("authenticated") and get_secret("APP_PASSWORD"):
    st.title("🔐 Personal Finance OS")
    p=st.text_input("Password",type="password")
    if st.button("Sign in",type="primary"):
        if p==get_secret("APP_PASSWORD"):st.session_state.authenticated=True;st.rerun()
        else:st.error("Password non valida.")
    st.stop()

st.markdown("""
<style>
.block-container{max-width:1400px;padding-top:1.6rem;padding-bottom:3rem}
h1{font-weight:800!important;letter-spacing:-.02em}
h2,h3{font-weight:750!important;letter-spacing:-.01em}
div[data-testid="stMetric"]{border:1px solid rgba(100,116,139,.18);border-radius:16px;padding:16px 18px;min-height:104px;background:#fff;box-shadow:0 1px 3px rgba(15,23,42,.05)}
div[data-testid="stMetricValue"]{font-size:1.7rem;font-weight:750}
div[data-testid="stMetricLabel"] p{font-weight:600;color:#475569}
.big{font-size:2rem;font-weight:800}
.card{padding:16px;border-radius:14px;background:white;border:1px solid rgba(100,116,139,.18)}
.income{border-left:5px solid #16a34a;background:#f0fdf4}
.expense{border-left:5px solid #dc2626;background:#fef2f2}
.sub{font-size:.92rem;font-weight:650;margin-top:.35rem}
.kpi{background:#fff;border:1px solid rgba(100,116,139,.16);border-radius:18px;padding:18px 20px 16px;
     box-shadow:0 1px 3px rgba(15,23,42,.05);border-top:4px solid var(--accent,#2563eb);height:100%}
.kpi-label{font-size:.78rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#64748b}
.kpi-value{font-size:1.9rem;font-weight:800;color:#0f172a;margin-top:6px;line-height:1.15}
.kpi-delta{font-size:.92rem;font-weight:650;margin-top:6px}
.kpi-sub{font-size:.8rem;color:#94a3b8;margin-top:2px}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:650;background:#eff6ff;color:#1d4ed8}
button[data-baseweb="tab"] p{font-size:1rem;font-weight:650}
div[data-testid="stDataFrame"],div[data-testid="stDataEditor"]{border-radius:12px}
div[data-testid="stExpander"] details{border-radius:14px;background:#fff}
.sec{font-size:1.05rem;font-weight:750;color:#0f172a;margin:.4rem 0 .5rem}
.bud{margin:0 0 14px}
.bud-top{display:flex;justify-content:space-between;font-size:.92rem;margin-bottom:5px}
.bud-track{height:9px;background:#e2e8f0;border-radius:6px;overflow:hidden}
.bud-track div{height:100%;border-radius:6px}
.bud-note{font-size:.78rem;font-weight:600;margin-top:3px}
.txr{display:flex;justify-content:space-between;align-items:center;padding:9px 2px;border-bottom:1px solid #f1f5f9}
.txr-d{font-weight:600;font-size:.93rem;color:#0f172a}
.txr-s{font-size:.78rem;color:#64748b;margin-top:2px}
.txr-a{font-weight:700;font-size:.95rem;white-space:nowrap}
section[data-testid="stSidebar"]{background:#fff;border-right:1px solid #eef2f7}
</style>
""",unsafe_allow_html=True)

def euro(v): return f"€ {float(v):,.2f}"

def as_date(v):
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date): return v
    return date.fromisoformat(str(v)[:10])

def previous_month(d):
    return date(d.year-1, 12, 1) if d.month==1 else date(d.year, d.month-1, 1)

def delta_line(current, previous, invert=False):
    d=float(current)-float(previous)
    good=(d<0) if invert else (d>0)
    color="#16a34a" if d and good else "#dc2626" if d else "#64748b"
    sign="+" if d>0 else ""
    return f'<div class="sub" style="color:{color}">{sign}{euro(d)} vs last month</div>'

INCOME_COLOR="rgba(22,163,74,0.45)"
EXPENSE_COLOR="rgba(220,38,38,0.45)"

def style_chart(fig, height=480):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#0f172a"),
        title=dict(x=0, xanchor="left", y=0.97),
        legend=dict(orientation="h", yanchor="top", y=-0.18, x=0, bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=64, r=16, t=56, b=64), height=height,
    )
    fig.update_xaxes(showgrid=False, automargin=True)
    fig.update_yaxes(gridcolor="rgba(148,163,184,.28)", zeroline=False, automargin=True, title_standoff=12)
    return fig

@st.cache_data(ttl=600, show_spinner=False)
def _q(sql, params=None):
    """Cached read for the Wealth page. Cleared whenever a snapshot changes."""
    return fetch_all(sql, params)

def bust_cache():
    _q.clear()

ACCOUNT_KINDS=["Cash","Investment"]
CASH_COLORS=["#1d4ed8","#3b82f6","#60a5fa","#93c5fd","#0e7490","#06b6d4","#67e8f9","#bfdbfe","#1e3a8a","#0284c7"]
INV_COLORS=["#ea580c","#f97316","#fb923c","#fdba74","#c2410c","#f59e0b","#fbbf24","#fed7aa","#9a3412","#d97706"]

def guess_kind(name):
    # Your rule: only the Investments account is investing; everything else is cash.
    return "Investment" if "invest" in (name or "").lower() else "Cash"

def get_account_types():
    ensure_net_worth_items()
    return {str(r[0]).strip().lower(): r[1] for r in _q("SELECT name, kind FROM account_types")}

def kind_of(name, types):
    return types.get((name or "").strip().lower()) or guess_kind(name)

def save_account_types(items):
    for it in items:
        if it["kind"]=="Asset":
            execute("INSERT INTO account_types(name,kind) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind",
                    [it["name"].strip().lower(), it.get("group") or guess_kind(it["name"])])

def new_line(name="", amount=0.0, group=None):
    return {"uid": uuid.uuid4().hex[:8], "name": name, "amount": float(amount or 0),
            "group": group or guess_kind(name)}

def clear_prefix(prefix):
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefix):
            del st.session_state[key]

def edit_lines(rows, prefix, with_type=False):
    remove_uid=None
    for i, row in enumerate(rows):
        nk, ak, tk=f"{prefix}n{row['uid']}", f"{prefix}a{row['uid']}", f"{prefix}t{row['uid']}"
        if with_type:
            c1,ct,c2,c3=st.columns([3,1.6,2,.8])
        else:
            c1,c2,c3=st.columns([3,2,.8]); ct=None
        vis="visible" if i==0 else "collapsed"
        if nk not in st.session_state: st.session_state[nk]=row["name"]
        if ak not in st.session_state: st.session_state[ak]=float(row["amount"])
        row["name"]=c1.text_input("Name", key=nk, label_visibility=vis)
        if with_type:
            if tk not in st.session_state: st.session_state[tk]=row.get("group") or guess_kind(row["name"])
            row["group"]=ct.selectbox("Type", ACCOUNT_KINDS, key=tk, label_visibility=vis)
        row["amount"]=float(c2.number_input("Amount", min_value=0., step=100., key=ak, label_visibility=vis))
        if i==0: c3.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
        if c3.button("Remove", key=f"{prefix}r{row['uid']}"): remove_uid=row["uid"]
    if remove_uid:
        rows[:]=[r for r in rows if r["uid"]!=remove_uid]
        st.rerun()

def collect_lines(rows, kind):
    items=[]
    for row in rows:
        name=(row.get("name") or "").strip()
        amount=float(row.get("amount") or 0)
        if amount>0 and not name:
            return None
        if name and amount>0:
            items.append({"kind": kind, "name": name, "amount": amount,
                          "group": row.get("group") or guess_kind(name)})
    return items

def merge_items(items):
    merged, index=[], {}
    for it in items:
        key=(it["kind"], it["name"].strip().lower())
        if key in index:
            merged[index[key]]["amount"]+=it["amount"]
        else:
            index[key]=len(merged)
            merged.append(dict(it))
    return merged

def ensure_net_worth_items():
    # Runs the setup once per server process instead of on every query (much faster on MotherDuck).
    _net_worth_setup()

@st.cache_resource
def _net_worth_setup():
    execute("""
        CREATE TABLE IF NOT EXISTS account_types (
            name VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL CHECK(kind IN ('Cash','Investment'))
        )
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS category_rules (
            merchant VARCHAR PRIMARY KEY,
            category VARCHAR NOT NULL
        )
    """)
    execute("CREATE TABLE IF NOT EXISTS app_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    if not fetch_one("SELECT 1 FROM app_meta WHERE key='account_types_v2'"):
        # One-time fix: brokers like Trading 212 / MEXEM were saved as Investment by the old default.
        execute("UPDATE account_types SET kind='Cash' WHERE kind='Investment' AND lower(name) NOT LIKE '%invest%'")
        execute("INSERT INTO app_meta(key,value) VALUES ('account_types_v2','done')")
    execute("CREATE SEQUENCE IF NOT EXISTS net_worth_items_id_seq START 1")
    execute("""
        CREATE TABLE IF NOT EXISTS net_worth_items (
            id BIGINT PRIMARY KEY DEFAULT nextval('net_worth_items_id_seq'),
            snapshot_id BIGINT NOT NULL,
            kind VARCHAR NOT NULL CHECK(kind IN ('Asset','Liability')),
            name VARCHAR NOT NULL,
            amount DECIMAL(18,2) NOT NULL CHECK(amount >= 0)
        )
    """)
    rows=fetch_all("""
        SELECT id, cash, investments, real_estate, other_assets,
               student_loans, credit_card_debt, other_liabilities
        FROM net_worth n
        WHERE NOT EXISTS (SELECT 1 FROM net_worth_items i WHERE i.snapshot_id=n.id)
    """)
    mapping=[
        ("Asset","Cash",1),("Asset","Investments",2),("Asset","Real Estate",3),
        ("Asset","Other Assets",4),("Liability","Student loans",5),
        ("Liability","Credit card debt",6),("Liability","Other liabilities",7),
    ]
    for row in rows:
        inserted=0
        for kind,name,idx in mapping:
            amount=float(row[idx] or 0)
            if amount>0:
                execute("INSERT INTO net_worth_items(snapshot_id,kind,name,amount) VALUES (?,?,?,?)",[row[0],kind,name,amount])
                inserted+=1
        if inserted==0:
            execute("INSERT INTO net_worth_items(snapshot_id,kind,name,amount) VALUES (?,?,?,?)",[row[0],"Asset","Cash",0])
    return True

def _rollup(items):
    # Every asset that is not an investment counts as cash.
    cash=inv=real=other=loans=cards=oliab=0.0
    for it in items:
        name=it["name"].strip().lower(); amt=float(it["amount"])
        if it["kind"]=="Asset":
            if (it.get("group") or guess_kind(name))=="Investment": inv+=amt
            else: cash+=amt
        else:
            if "student" in name or "studio" in name: loans+=amt
            elif "credit" in name or "carta" in name: cards+=amt
            else: oliab+=amt
    return cash,inv,real,other,loans,cards,oliab

def _insert_items(snapshot_id, items):
    for it in items:
        name=it["name"].strip()
        if name:
            execute("INSERT INTO net_worth_items(snapshot_id,kind,name,amount) VALUES (?,?,?,?)",[snapshot_id,it["kind"],name,float(it["amount"])])

def save_snapshot(snapshot_date, items):
    ensure_net_worth_items()
    cash,inv,real,other,loans,cards,oliab=_rollup(items)
    sid=execute("""
        INSERT INTO net_worth
        (snapshot_date,cash,investments,real_estate,other_assets,student_loans,credit_card_debt,other_liabilities)
        VALUES (?,?,?,?,?,?,?,?)
        RETURNING id
    """,[snapshot_date,cash,inv,real,other,loans,cards,oliab]).fetchone()[0]
    _insert_items(sid, items)
    save_account_types(items)
    bust_cache()

def replace_snapshot(snapshot_id, snapshot_date, items):
    ensure_net_worth_items()
    cash,inv,real,other,loans,cards,oliab=_rollup(items)
    execute("""
        UPDATE net_worth
        SET snapshot_date=?,cash=?,investments=?,real_estate=?,other_assets=?,
            student_loans=?,credit_card_debt=?,other_liabilities=?
        WHERE id=?
    """,[snapshot_date,cash,inv,real,other,loans,cards,oliab,snapshot_id])
    execute("DELETE FROM net_worth_items WHERE snapshot_id=?",[snapshot_id])
    _insert_items(snapshot_id, items)
    save_account_types(items)
    bust_cache()

def lines_from_snapshot(snapshot_id, types):
    items=get_snapshot_items(snapshot_id)
    assets=[new_line(n, a, kind_of(n, types)) for k,n,a in items if k=="Asset" and str(n).strip()]
    liabs=[new_line(n, a) for k,n,a in items if k=="Liability"]
    assets.sort(key=lambda r: r["group"]!="Cash")   # cash accounts first, investments after
    return assets, liabs

def default_lines():
    """Accounts (and last balances) from the most recent snapshot, so you don't retype them."""
    latest=(_q("SELECT id, snapshot_date FROM net_worth ORDER BY snapshot_date DESC, id DESC LIMIT 1") or [None])[0]
    if latest:
        assets, liabs=lines_from_snapshot(latest[0], get_account_types())
        if assets:
            return assets, liabs, as_date(latest[1])
    return [new_line("Cash"), new_line("Investments", 0, "Investment")], [], None

def remove_snapshot(snapshot_id):
    ensure_net_worth_items()
    execute("DELETE FROM net_worth_items WHERE snapshot_id=?",[snapshot_id])
    execute("DELETE FROM net_worth WHERE id=?",[snapshot_id])
    bust_cache()

def get_snapshot_items(snapshot_id):
    ensure_net_worth_items()
    return _q("""
        SELECT kind,name,amount FROM net_worth_items
        WHERE snapshot_id=?
        ORDER BY CASE kind WHEN 'Asset' THEN 0 ELSE 1 END, id
    """,[snapshot_id])

def get_asset_history():
    ensure_net_worth_items()
    return _q("""
        SELECT n.snapshot_date,i.name,SUM(i.amount)
        FROM net_worth_items i JOIN net_worth n ON n.id=i.snapshot_id
        WHERE i.kind='Asset'
        GROUP BY n.snapshot_date,i.name
        ORDER BY n.snapshot_date,i.name
    """)

def get_net_worth_history():
    ensure_net_worth_items()
    return _q("""
        SELECT n.snapshot_date,
          COALESCE((SELECT SUM(CASE WHEN i.kind='Asset' THEN i.amount ELSE -i.amount END)
                    FROM net_worth_items i WHERE i.snapshot_id=n.id),
                   n.cash+n.investments+n.real_estate+n.other_assets
                   -COALESCE(n.student_loans,0)-COALESCE(n.credit_card_debt,0)-COALESCE(n.other_liabilities,0))
        FROM net_worth n ORDER BY n.snapshot_date,n.id
    """)

def get_net_worth_records():
    ensure_net_worth_items()
    return _q("""
        SELECT n.id,n.snapshot_date,
          COALESCE((SELECT SUM(amount) FROM net_worth_items i WHERE i.snapshot_id=n.id AND i.kind='Asset'),
                   n.cash+n.investments+n.real_estate+n.other_assets),
          COALESCE((SELECT SUM(amount) FROM net_worth_items i WHERE i.snapshot_id=n.id AND i.kind='Liability'),
                   COALESCE(n.student_loans,0)+COALESCE(n.credit_card_debt,0)+COALESCE(n.other_liabilities,0)),
          COALESCE((SELECT SUM(CASE WHEN i.kind='Asset' THEN i.amount ELSE -i.amount END)
                    FROM net_worth_items i WHERE i.snapshot_id=n.id),
                   n.cash+n.investments+n.real_estate+n.other_assets
                   -COALESCE(n.student_loans,0)-COALESCE(n.credit_card_debt,0)-COALESCE(n.other_liabilities,0))
        FROM net_worth n ORDER BY n.snapshot_date DESC,n.id DESC
    """)

# ---------------------------------------------------------------------
# Wealth page building blocks
# ---------------------------------------------------------------------
PLOT_CONFIG={"displayModeBar": False}

def signed_euro(v):
    v=float(v)
    return ("+" if v>0 else "−" if v<0 else "")+euro(abs(v))

def pct_change(cur, prev):
    return None if not prev else (float(cur)-float(prev))/abs(float(prev))*100

def kpi(col, label, value, delta=None, pct=None, sub=None, accent="#2563eb", invert=False, text=None, delta_text=None):
    """Summary card. invert=True makes an increase red (e.g. spending)."""
    d=""
    if delta is not None:
        good=(delta<0) if invert else (delta>0)
        color="#64748b" if not delta else "#16a34a" if good else "#dc2626"
        arrow="▲" if delta>0 else "▼" if delta<0 else "■"
        p=f" · {pct:+.1f}%" if pct is not None else ""
        d=f'<div class="kpi-delta" style="color:{color}">{arrow} {delta_text or signed_euro(delta)}{p}</div>'
    s=f'<div class="kpi-sub">{sub}</div>' if sub else ""
    v=text if text is not None else (("−"+euro(abs(value))) if float(value)<0 else euro(value))
    col.markdown(f'<div class="kpi" style="--accent:{accent}"><div class="kpi-label">{label}</div>'
                 f'<div class="kpi-value">{v}</div>{d}{s}</div>', unsafe_allow_html=True)

def add_months(d, n):
    t=d.year*12+d.month-1+n
    return date(t//12, t%12+1, 1)

# Money moved into investments is saving, not spending.
NON_SPENDING=("Investing",)
_NS_SQL="('"+"','".join(NON_SPENDING)+"')"

def tx_totals(start, end):
    r=_q(f"""SELECT COALESCE(SUM(CASE WHEN transaction_type='Income' THEN amount END),0),
                   COALESCE(SUM(CASE WHEN transaction_type='Expense' AND category NOT IN {_NS_SQL} THEN amount END),0),
                   COALESCE(SUM(CASE WHEN transaction_type='Expense' AND category IN {_NS_SQL} THEN amount END),0),
                   COUNT(*)
            FROM transactions WHERE transaction_date>=? AND transaction_date<?""", [start, end])[0]
    inc, spend, inv=float(r[0]), float(r[1]), float(r[2])
    saved=inc-spend
    return {"income": inc, "expenses": spend, "invested": inv, "net": saved, "cash_left": saved-inv,
            "rate": saved/inc*100 if inc else None, "invest_rate": inv/inc*100 if inc else None, "count": int(r[3])}

def cat_totals(start, end, typ="Expense", include_investing=False):
    extra="" if (include_investing or typ!="Expense") else f" AND category NOT IN {_NS_SQL}"
    return {c: float(v) for c, v in _q(f"""SELECT category, SUM(amount) FROM transactions
        WHERE transaction_type=? AND transaction_date>=? AND transaction_date<?{extra} GROUP BY 1 ORDER BY 2 DESC""",
        [typ, start, end])}

def monthly_flow(start, end):
    """[(month, income, spending, invested)]"""
    rows=_q(f"""SELECT DATE_TRUNC('month', transaction_date)::DATE,
                      SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),
                      SUM(CASE WHEN transaction_type='Expense' AND category NOT IN {_NS_SQL} THEN amount ELSE 0 END),
                      SUM(CASE WHEN transaction_type='Expense' AND category IN {_NS_SQL} THEN amount ELSE 0 END)
               FROM transactions WHERE transaction_date>=? AND transaction_date<? GROUP BY 1""", [start, end])
    got={as_date(m): (float(i), float(e), float(v)) for m, i, e, v in rows}
    out, d=[], start
    while d<end:
        out.append((d, *got.get(d, (0.0, 0.0, 0.0)))); d=add_months(d, 1)
    return out

def flow_chart(flow, height=400):
    x=[f[0] for f in flow]
    fig=go.Figure()
    fig.add_trace(go.Bar(name="Income", x=x, y=[f[1] for f in flow], marker_color="#22c55e",
                         hovertemplate="Income € %{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(name="Spending", x=x, y=[f[2] for f in flow], marker_color="#f87171",
                         hovertemplate="Spending € %{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(name="Invested", x=x, y=[f[3] for f in flow], marker_color="#a78bfa",
                         hovertemplate="Invested € %{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(name="Saved (income − spending)", x=x, y=[f[1]-f[2] for f in flow], mode="lines+markers",
                             line=dict(color="#0f172a", width=2.5, shape="spline"),
                             hovertemplate="Saved € %{y:,.0f}<extra></extra>"))
    fig.update_layout(barmode="group", hovermode="x unified", bargap=0.25, bargroupgap=0.05)
    style_chart(fig, height)
    fig.update_layout(margin=dict(t=10))
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    fig.update_yaxes(tickprefix="€ ", separatethousands=True)
    return fig

def spending_anomalies(year, month):
    """Categories this month well above their usual monthly spend (investing excluded)."""
    try:
        return _q(f"""
            WITH monthly_category AS (
                SELECT DATE_TRUNC('month', transaction_date) AS mth, category, SUM(amount) AS spending
                FROM transactions
                WHERE transaction_type='Expense' AND category NOT IN {_NS_SQL}
                GROUP BY 1, 2
            ),
            historical AS (
                SELECT category, AVG(spending) AS avg_spend, STDDEV_POP(spending) AS std_spend
                FROM monthly_category WHERE mth < MAKE_DATE(?, ?, 1)
                GROUP BY category
            ),
            current_month AS (
                SELECT category, spending AS current_spend
                FROM monthly_category WHERE mth = MAKE_DATE(?, ?, 1)
            )
            SELECT c.category, c.current_spend, h.avg_spend, COALESCE(h.std_spend, 0)
            FROM current_month c JOIN historical h USING (category)
            WHERE c.current_spend > h.avg_spend + GREATEST(COALESCE(h.std_spend, 0)*2, h.avg_spend*0.5)
            ORDER BY c.current_spend DESC""", [year, month, year, month])
    except Exception:
        return None

def tx_months():
    ms=[as_date(r[0]) for r in _q("SELECT DISTINCT DATE_TRUNC('month', transaction_date)::DATE FROM transactions ORDER BY 1 DESC")]
    cur=date.today().replace(day=1)
    if cur not in ms: ms.insert(0, cur)
    return ms

def get_budgets():
    return {c: float(v) for c, v in _q("SELECT category, monthly_limit FROM budgets WHERE monthly_limit>0")}

def budget_bars(spent, budgets):
    html=""
    for cat, lim in sorted(budgets.items(), key=lambda x: -spent.get(x[0], 0)/x[1]):
        s=spent.get(cat, 0); p=s/lim*100 if lim else 0
        color="#16a34a" if p<80 else "#d97706" if p<=100 else "#dc2626"
        note=f"{euro(lim-s)} left" if s<=lim else f"{euro(s-lim)} over"
        html+=(f'<div class="bud"><div class="bud-top"><span><b>{cat}</b></span><span>{euro(s)} '
               f'<span style="color:#94a3b8">/ {euro(lim)}</span></span></div>'
               f'<div class="bud-track"><div style="width:{min(p,100):.1f}%;background:{color}"></div></div>'
               f'<div class="bud-note" style="color:{color}">{p:.0f}% · {note}</div></div>')
    return html

def tx_rows_html(rows):
    html=""
    for d, amt, cat, typ, desc in rows:
        color="#16a34a" if typ=="Income" else "#0f172a"
        sign="+" if typ=="Income" else "−"
        html+=(f'<div class="txr"><div><div class="txr-d">{escape((desc or cat)[:42])}</div>'
               f'<div class="txr-s">{as_date(d):%d %b} · <span class="pill">{cat}</span></div></div>'
               f'<div class="txr-a" style="color:{color}">{sign}{euro(amt)}</div></div>')
    return html or '<div class="txr-s">Nothing yet.</div>'

def clean_nl_description(text):
    s=_strip_dates(text or "")
    s=re.sub(r"(?:€|\beur\b)?\s*\d[\d.,]*(?:\s*(?:€|\beuro?\b))?", " ", s, flags=re.I)
    s=re.sub(r"\s+", " ", s).strip(" -,·")
    return s or (text or "").strip()

def card(title):
    st.markdown(f'<div class="sec">{title}</div>', unsafe_allow_html=True)

def lines_df(lines, with_type):
    if with_type:
        return pd.DataFrame([{"Account": r["name"], "Type": r["group"], "Amount": float(r["amount"])} for r in lines],
                            columns=["Account","Type","Amount"])
    return pd.DataFrame([{"Account": r["name"], "Amount": float(r["amount"])} for r in lines], columns=["Account","Amount"])

def lines_editor(df, key, with_type):
    cfg={"Account": st.column_config.TextColumn("Account", width="medium"),
         "Amount": st.column_config.NumberColumn("Amount (€)", min_value=0.0, step=0.01, format="€ %.2f")}
    if with_type:
        cfg["Type"]=st.column_config.SelectboxColumn("Type", options=ACCOUNT_KINDS, width="small",
                                                     help="Only your Investments account counts as investing — everything else is cash. Leave empty to apply that rule.")
    return st.data_editor(df, key=key, num_rows="dynamic", hide_index=True, width="stretch", column_config=cfg)

def _clean(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or (not isinstance(v, str) and pd.isna(v)) else v

def items_from_df(df, kind):
    """Rows -> items. Returns None when an amount has no account name."""
    items=[]
    for r in df.to_dict("records"):
        name=str(_clean(r.get("Account")) or "").strip()
        amount=float(_clean(r.get("Amount")) or 0)
        if amount>0 and not name:
            return None
        if name and amount>0:
            group=_clean(r.get("Type")) if kind=="Asset" else None
            items.append({"kind": kind, "name": name, "amount": amount, "group": group or guess_kind(name)})
    return items

def build_series(types):
    """dates (sorted), {account: {date: amount}}, {account: kind}"""
    dates, series=[], {}
    for dt, name, amt in get_asset_history():
        dt=as_date(dt)
        if dt not in dates: dates.append(dt)
        series.setdefault(name, {})[dt]=float(amt or 0)
    dates.sort()
    return dates, series, {n: kind_of(n, types) for n in series}

def group_values(dates, series, kinds, kind):
    return {dt: sum(v.get(dt, 0) for n, v in series.items() if kinds[n]==kind) for dt in dates}

def networth_values():
    out={}
    for dt, v in get_net_worth_history():
        out[as_date(dt)]=float(v or 0)
    return out

def monthly_series(values):
    """Last snapshot of each month, with change vs the previous month in € and %."""
    by_month={}
    for dt in sorted(values):
        by_month[(dt.year, dt.month)]=(dt, float(values[dt]))
    rows, prev=[], None
    for (y, m), (dt, v) in sorted(by_month.items()):
        ch=None if prev is None else v-prev
        rows.append({"month": date(y, m, 1), "snapshot": dt, "value": v, "change": ch,
                     "pct": None if prev is None else pct_change(v, prev)})
        prev=v
    return rows

def learn_category(description, category):
    """Remember the category you chose for this merchant."""
    if category in (None, "", "Other", "Other Income"):
        return
    ensure_net_worth_items()
    for key in merchant_keys(description):
        if key:
            execute("INSERT INTO category_rules(merchant,category) VALUES(?,?) "
                    "ON CONFLICT(merchant) DO UPDATE SET category=excluded.category", [key, category])

def smart_category(description, transaction_type, suggested):
    """Your past choice for this exact merchant > a specific keyword/AI match > your choice for the brand > guess."""
    ensure_net_worth_items()
    valid=categories_for(transaction_type)
    full, first=merchant_keys(description)
    rules={k: c for k, c in fetch_all("SELECT merchant, category FROM category_rules WHERE merchant IN (?, ?)",
                                     [full or "", first or ""]) if c in valid}
    if full in rules: return rules[full], "learned"
    if suggested in valid and suggested not in ("Other", "Other Income"): return suggested, "auto"
    if first in rules: return rules[first], "learned"
    return (suggested if suggested in valid else valid[0]), "auto"

PIE_COLORS=["#16a34a","#2563eb","#0891b2","#d97706","#dc2626","#7c3aed","#0f766e","#db2777","#64748b","#ca8a04"]

def years():
    r=fetch_all("SELECT DISTINCT EXTRACT(YEAR FROM transaction_date)::INTEGER FROM transactions ORDER BY 1 DESC")
    y=[int(x[0]) for x in r]
    if date.today().year not in y:y.insert(0,date.today().year)
    return y

def month_name(m):
    return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m-1]

def income_split(y,m):
    r=fetch_one("""
    SELECT
      COALESCE(SUM(CASE WHEN transaction_type='Income' AND
        (category='Wage' OR lower(coalesce(description,'')) LIKE '%salary%'
         OR lower(coalesce(description,'')) LIKE '%stipendio%'
         OR lower(coalesce(description,'')) LIKE '%wage%'
         OR lower(coalesce(description,'')) LIKE '%payroll%')
        THEN amount ELSE 0 END),0),
      COALESCE(SUM(CASE WHEN transaction_type='Income' AND
        NOT (category='Wage' OR lower(coalesce(description,'')) LIKE '%salary%'
         OR lower(coalesce(description,'')) LIKE '%stipendio%'
         OR lower(coalesce(description,'')) LIKE '%wage%'
         OR lower(coalesce(description,'')) LIKE '%payroll%')
        THEN amount ELSE 0 END),0)
    FROM transactions
    WHERE EXTRACT(YEAR FROM transaction_date)=?
      AND EXTRACT(MONTH FROM transaction_date)=?
    """,[y,m])
    return float(r[0]),float(r[1])

def monthly_rows(y):
    return fetch_all("""
    WITH months AS (SELECT range AS month_num FROM range(1,13)),
    income_by_month AS (SELECT EXTRACT(MONTH FROM transaction_date)::INTEGER AS month_num,SUM(amount) AS income FROM transactions WHERE transaction_type='Income' AND EXTRACT(YEAR FROM transaction_date)=? GROUP BY 1),
    expense_by_month AS (SELECT EXTRACT(MONTH FROM transaction_date)::INTEGER AS month_num,SUM(amount) AS expenses FROM transactions WHERE transaction_type='Expense' AND EXTRACT(YEAR FROM transaction_date)=? GROUP BY 1)
    SELECT months.month_num,COALESCE(income_by_month.income,0),COALESCE(expense_by_month.expenses,0)
    FROM months LEFT JOIN income_by_month USING(month_num) LEFT JOIN expense_by_month USING(month_num) ORDER BY months.month_num
    """,[y,y])

def yearly_rows():
    return fetch_all("""
    SELECT EXTRACT(YEAR FROM transaction_date)::INTEGER AS year,
      SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),
      SUM(CASE WHEN transaction_type='Expense' THEN amount ELSE 0 END)
    FROM transactions GROUP BY 1 ORDER BY 1
    """)

with st.sidebar:
    st.markdown("## 💰 Finance OS")
    page=st.radio("Navigation",["🏠 Dashboard","📝 Data Entry","📊 Reports","💎 Wealth & Strategy","🤖 AI Insights"],
                  label_visibility="collapsed")
    st.divider()
    if not st.session_state.get("recurring_checked"):
        st.session_state.recurring_checked=True
        try:
            n=process_due_recurring(date.today())
            if n: st.toast(f"🔁 Added {n} recurring expense(s) that were due")
        except Exception:
            pass
    st.caption(f"📅 {date.today():%A %d %B %Y}")
    if get_secret("APP_PASSWORD") and st.button("Log out", width="stretch"):
        st.session_state.authenticated=False; st.rerun()

TODAY=date.today()
MONTH_START=TODAY.replace(day=1)

# =====================================================================
# DASHBOARD
# =====================================================================
if page=="🏠 Dashboard":
    st.title("🏠 Dashboard")
    st.caption(f"{TODAY:%B %Y} so far — compared with the same point last month.")
    nm=add_months(MONTH_START, 1); pm=add_months(MONTH_START, -1)
    cur=tx_totals(MONTH_START, nm)
    prev=tx_totals(pm, min(pm+timedelta(days=TODAY.day), MONTH_START))
    k1,k2,k3,k4,k5=st.columns(5)
    kpi(k1, "💵 Money in", cur["income"], cur["income"]-prev["income"], pct_change(cur["income"], prev["income"]),
        "This month", "#16a34a")
    kpi(k2, "💳 Spending", cur["expenses"], cur["expenses"]-prev["expenses"], pct_change(cur["expenses"], prev["expenses"]),
        "Excludes investing", "#dc2626", invert=True)
    kpi(k3, "📈 Invested", cur["invested"], cur["invested"]-prev["invested"], None,
        f"{cur['invest_rate']:.0f}% of income" if cur["invest_rate"] is not None else "This month", "#7c3aed")
    rate, prate=cur["rate"], prev["rate"]
    kpi(k4, "🐷 Savings rate", 0, None if rate is None or prate is None else rate-prate, None,
        f"Saved {euro(cur['net'])} · {euro(cur['cash_left'])} kept as cash", "#2563eb",
        text="—" if rate is None else f"{rate:.0f}%",
        delta_text=None if rate is None or prate is None else f"{rate-prate:+.1f} pp")
    recs=get_net_worth_records()
    if recs:
        types=get_account_types(); dates_, series_, kinds_=build_series(types)
        last=dates_[-1] if dates_ else None
        c_now=sum(v.get(last,0) for n,v in series_.items() if kinds_[n]=="Cash")
        i_now=sum(v.get(last,0) for n,v in series_.items() if kinds_[n]=="Investment")
        nwv, nwp=float(recs[0][4]), (float(recs[1][4]) if len(recs)>1 else None)
        kpi(k5, "💎 Net worth", nwv, None if nwp is None else nwv-nwp, pct_change(nwv, nwp),
            f"Cash {euro(c_now)} · Invested {euro(i_now)}", "#0f172a")
    else:
        kpi(k5, "💎 Net worth", 0, sub="Add a snapshot in Wealth", accent="#0f172a", text="—")
    st.write("")

    left, right=st.columns([2, 1], gap="large")
    with left:
        card("📈 Last 12 months")
        fig=flow_chart(monthly_flow(add_months(MONTH_START, -11), nm), 400)
        st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
    with right:
        card("🧾 Where it went this month")
        st.caption("Spending only — investing is shown separately.")
        cats=cat_totals(MONTH_START, nm)
        if cats:
            top=list(cats.items())[:8][::-1]
            f2=go.Figure(go.Bar(x=[v for _,v in top], y=[c for c,_ in top], orientation="h",
                                marker_color="#60a5fa", text=[euro(v) for _,v in top], textposition="auto",
                                hovertemplate="%{y}: € %{x:,.2f}<extra></extra>"))
            style_chart(f2, 400)
            f2.update_layout(margin=dict(t=10, l=10, r=10, b=10), showlegend=False)
            f2.update_xaxes(visible=False); f2.update_yaxes(gridcolor="rgba(0,0,0,0)")
            st.plotly_chart(f2, width="stretch", config=PLOT_CONFIG)
        else:
            st.info("No expenses recorded this month yet.")

    b1, b2, b3=st.columns(3, gap="large")
    with b1:
        card("🎯 Budgets this month")
        budgets=get_budgets()
        if budgets:
            st.markdown(budget_bars(cats, budgets), unsafe_allow_html=True)
            days_left=(nm-TODAY).days
            room=sum(budgets.values())-sum(cats.get(c,0) for c in budgets)
            if room>0 and days_left>0:
                st.caption(f"{euro(room)} left across budgets · about {euro(room/days_left)} per day for {days_left} days")
        else:
            st.caption("No budgets yet. Set monthly limits in **📝 Data Entry → 🎯 Budgets** to track them here.")
    with b2:
        card("🕒 Recent transactions")
        st.markdown(tx_rows_html(_q("""SELECT transaction_date, amount, category, transaction_type, description
            FROM transactions ORDER BY transaction_date DESC, id DESC LIMIT 7""")), unsafe_allow_html=True)
    with b3:
        card("🔁 Coming up (30 days)")
        up=_q("""SELECT next_due_date, amount, category, 'Expense', name FROM recurring_expenses
                 WHERE active AND next_due_date<=? ORDER BY next_due_date LIMIT 7""", [TODAY+timedelta(days=30)])
        st.markdown(tx_rows_html(up) if up else '<div class="txr-s">No recurring payments due in the next 30 days.</div>',
                    unsafe_allow_html=True)
        if up:
            st.caption(f"Total {euro(sum(float(r[1]) for r in up))}")

# =====================================================================
# DATA ENTRY
# =====================================================================
elif page=="📝 Data Entry":
    st.title("📝 Data Entry")
    tab_quick, tab_import, tab_rec, tab_bud, tab_tx=st.tabs(
        ["⚡ Quick add", "📸 Import", "🔁 Recurring", "🎯 Budgets", "📋 Transactions"])

    # ---------- quick add ----------
    with tab_quick:
        left, right=st.columns(2, gap="large")
        with left:
            card("💬 Just type it")
            st.caption("e.g. *Woolworths 54,20 ieri* · *Stipendio 2.000 oggi* · *Netflix 15.99 12/10*. "
                       "Check the category, then save.")
            if st.session_state.pop("nl_clear", False):
                st.session_state.nl_text=""
                st.session_state.nl_sig=""
            nl=st.text_input("Describe the transaction", key="nl_text", label_visibility="collapsed",
                             placeholder="Woolworths 54,20 ieri")
            parsed=None
            if nl.strip():
                try:
                    parsed=parse_natural_language(nl)
                    parsed["description"]=clean_nl_description(nl)
                    parsed["category"]=smart_category(parsed["description"], parsed["transaction_type"], parsed["category"])[0]
                except ValueError as e:
                    st.warning(str(e))
            if parsed:
                sig=f"{parsed['transaction_date']}|{parsed['amount']}|{parsed['transaction_type']}|{parsed['category']}|{parsed['description']}"
                if st.session_state.get("nl_sig")!=sig:
                    st.session_state.nl_sig=sig
                    st.session_state.nl_date=parsed["transaction_date"]
                    st.session_state.nl_amount=float(parsed["amount"])
                    st.session_state.nl_type=parsed["transaction_type"]
                    st.session_state.nl_desc=parsed["description"]
                    st.session_state.nl_cat=parsed["category"]
                with st.container(border=True):
                    c1,c2,c3=st.columns(3)
                    d=c1.date_input("Date", key="nl_date", format="DD/MM/YYYY")
                    a=c2.number_input("Amount (€)", min_value=0.01, step=0.01, key="nl_amount")
                    typ=c3.selectbox("Type", ["Expense","Income"], key="nl_type")
                    cats=categories_for(typ)
                    if st.session_state.get("nl_cat") not in cats:
                        st.session_state.nl_cat=cats[0]
                    c4,c5=st.columns(2)
                    cat=c4.selectbox("Category", cats, key="nl_cat")
                    desc=c5.text_input("Description", key="nl_desc")
                    if st.button("Save", type="primary", key="nl_save", width="stretch"):
                        fp=transaction_fingerprint(d,a,cat,typ,desc)
                        if insert_transaction(d,a,cat,typ,desc,"natural_language",fp):
                            learn_category(desc, cat)
                            st.session_state.nl_clear=True
                            st.toast(f"Saved: {desc} {euro(a)} ✅")
                            st.rerun()
                        else:
                            st.toast("Already saved — duplicate skipped")
        with right:
            card("✍️ Manual entry")
            typ=st.segmented_control("Type", ["Expense","Income"], default="Expense", key="manual_typ") or "Expense"
            cats=categories_for(typ)
            if "manual_cat" in st.session_state and st.session_state.manual_cat not in cats:
                st.session_state.manual_cat=cats[0]
            with st.form("manual", clear_on_submit=True, border=True):
                c1,c2=st.columns(2)
                d=c1.date_input("Date", TODAY, format="DD/MM/YYYY")
                a=c2.number_input("Amount (€)", min_value=0.01, step=0.01)
                c3,c4=st.columns(2)
                cat=c3.selectbox("Category", cats, key="manual_cat")
                desc=c4.text_input("Description", placeholder="Woolworths")
                if st.form_submit_button("Save", type="primary", width="stretch"):
                    fp=transaction_fingerprint(d,a,cat,typ,desc)
                    if insert_transaction(d,a,cat,typ,desc,"manual",fp):
                        learn_category(desc, cat)
                        st.toast(f"Saved: {desc or cat} {euro(a)} ✅")
                    else:
                        st.toast("Already saved — duplicate skipped")
        card("🕒 Last added")
        st.markdown(tx_rows_html(_q("""SELECT transaction_date, amount, category, transaction_type, description
            FROM transactions ORDER BY created_at DESC, id DESC LIMIT 5""")), unsafe_allow_html=True)

    # ---------- import ----------
    with tab_import:
        st.caption("Upload a screenshot from your banking app, a receipt photo, or a CSV export from your bank. "
                   "Every transaction is categorised automatically — fix anything wrong before importing and the app "
                   "remembers your choice for that merchant next time.")
        src=st.segmented_control("Source", ["📸 Screenshot", "📄 CSV file"], default="📸 Screenshot", key="imp_src") or "📸 Screenshot"
        def stage(rows, source):
            for r in rows:
                r["category"], r["how"]=smart_category(r["description"], r["transaction_type"], r.get("category"))
            st.session_state.ocr_rows=rows
            st.session_state.ocr_source=source
            st.session_state.ocr_ver=st.session_state.get("ocr_ver", 0)+1
        if src=="📸 Screenshot":
            imgf=st.file_uploader("Upload screenshot", type=["png","jpg","jpeg","webp"], key="img_upload")
            if imgf:
                from PIL import Image
                im=Image.open(imgf).convert("RGB")
                c_img, c_btn=st.columns([1, 1])
                c_img.image(im, width="stretch")
                with c_btn:
                    if not get_secret("GEMINI_API_KEY"):
                        st.info("Tip: add a GEMINI_API_KEY secret for much better reading and categorisation of bank screenshots.")
                    if st.button("🔍 Scan screenshot", type="primary", key="scan_img", width="stretch"):
                        with st.spinner("Reading transactions…"):
                            rows, raw=extract_image_transactions(im)
                            stage(rows, "image_ocr")
                        if rows: st.toast(f"{len(rows)} transaction(s) found")
                        else: st.warning("No transactions recognised. Try a sharper screenshot, or add it from Quick add.")
        else:
            csvf=st.file_uploader("Upload CSV", type=["csv","txt"], key="csv_upload",
                                  help="Needs at least a date and an amount column. Negative amounts are treated as expenses.")
            if csvf and st.button("📄 Read CSV", type="primary", key="read_csv"):
                try:
                    rows=parse_csv_transactions(csvf.getvalue())
                    stage(rows, "csv")
                    st.toast(f"{len(rows)} transaction(s) found")
                except ValueError as e:
                    st.error(str(e))
        if st.session_state.get("ocr_rows"):
            card("Review & import")
            rows=st.session_state.ocr_rows
            df=pd.DataFrame([{
                "Import": True,
                "Date": x["transaction_date"],
                "Description": x.get("description") or "Bank/card transaction",
                "Amount": float(x["amount"]),
                "Type": x.get("transaction_type") or "Expense",
                "Category": x.get("category") or "Other",
                "": "🧠 learned" if x.get("how")=="learned" else "",
            } for x in rows])
            edited=st.data_editor(
                df, key=f"ocr_editor_{st.session_state.get('ocr_ver', 0)}", hide_index=True, width="stretch",
                num_rows="fixed", disabled=[""],
                column_config={
                    "Import": st.column_config.CheckboxColumn(width="small"),
                    "Date": st.column_config.DateColumn(format="DD/MM/YYYY", width="small"),
                    "Description": st.column_config.TextColumn(width="large"),
                    "Amount": st.column_config.NumberColumn(min_value=0.01, step=0.01, format="€ %.2f", width="small"),
                    "Type": st.column_config.SelectboxColumn(options=["Expense", "Income"], width="small"),
                    "Category": st.column_config.SelectboxColumn(options=CATEGORIES, width="medium"),
                    "": st.column_config.TextColumn(width="small"),
                })
            chosen=edited[edited["Import"]==True]
            st.caption(f"{len(chosen)} selected · expenses {euro(chosen.loc[chosen['Type']=='Expense','Amount'].sum())} · "
                       f"income {euro(chosen.loc[chosen['Type']=='Income','Amount'].sum())}")
            b1, b2=st.columns([3, 1])
            if b1.button("✅ Import selected", type="primary", key="import_ocr", width="stretch"):
                ins=dup=0
                for r in chosen.to_dict("records"):
                    d=as_date(r["Date"]); amount=float(r["Amount"]); typ=r["Type"] or "Expense"
                    desc=str(r["Description"] or "").strip()
                    cat=r["Category"] if r["Category"] in categories_for(typ) else auto_categorize(desc, typ)
                    fp=transaction_fingerprint(d, amount, cat, typ, desc)
                    if insert_transaction(d, amount, cat, typ, desc, st.session_state.get("ocr_source", "image_ocr"), fp): ins+=1
                    else: dup+=1
                    learn_category(desc, cat)
                st.session_state.ocr_rows=[]
                st.toast(f"Imported {ins}" + (f" · skipped {dup} duplicate(s)" if dup else "") + " ✅")
                st.rerun()
            if b2.button("Discard", key="discard_ocr", width="stretch"):
                st.session_state.ocr_rows=[]
                st.rerun()

    # ---------- recurring ----------
    with tab_rec:
        st.caption("Recurring expenses are added automatically when they fall due (checked each time you open the app).")
        recs=_q("SELECT id,name,amount,category,frequency,next_due_date,active FROM recurring_expenses ORDER BY active DESC, next_due_date")
        per_month={"Weekly": 52/12, "Monthly": 1, "Quarterly": 1/3, "Yearly": 1/12}
        monthly=sum(float(r[2])*per_month[r[4]] for r in recs if r[6])
        m1,m2,m3=st.columns(3)
        kpi(m1, "Monthly cost", monthly, sub=f"{sum(1 for r in recs if r[6])} active", accent="#7c3aed")
        kpi(m2, "Yearly cost", monthly*12, sub="All active recurring", accent="#7c3aed")
        nxt=next((r for r in recs if r[6]), None)
        kpi(m3, "Next payment", float(nxt[2]) if nxt else 0, text=None if nxt else "—",
            sub=f"{nxt[1]} · {as_date(nxt[5]):%d %b}" if nxt else "Nothing scheduled", accent="#7c3aed")
        st.write("")
        if recs:
            rv=st.session_state.setdefault("rec_ver", 0)
            rdf=pd.DataFrame([{"id": r[0], "Name": r[1], "Amount": float(r[2]), "Category": r[3], "Frequency": r[4],
                               "Next due": as_date(r[5]), "Active": bool(r[6]), "Delete": False} for r in recs]).set_index("id")
            red=st.data_editor(rdf, key=f"rec_editor_{rv}", hide_index=True, width="stretch", num_rows="fixed",
                column_config={
                    "Amount": st.column_config.NumberColumn(min_value=0.01, format="€ %.2f"),
                    "Category": st.column_config.SelectboxColumn(options=EXPENSE_CATEGORIES),
                    "Frequency": st.column_config.SelectboxColumn(options=["Weekly","Monthly","Quarterly","Yearly"]),
                    "Next due": st.column_config.DateColumn(format="DD/MM/YYYY"),
                    "Active": st.column_config.CheckboxColumn(width="small"),
                    "Delete": st.column_config.CheckboxColumn("🗑️", width="small"),
                })
            c1, c2=st.columns([3, 1])
            if c1.button("💾 Save changes", type="primary", key="rec_save", width="stretch"):
                changed=0
                for rid, r in red.iterrows():
                    o=rdf.loc[rid]
                    if r["Delete"]:
                        delete_recurring_expense(int(rid)); changed+=1
                    elif any(r[k]!=o[k] for k in ("Name","Amount","Category","Frequency","Next due","Active")):
                        execute("""UPDATE recurring_expenses SET name=?,amount=?,category=?,frequency=?,next_due_date=?,active=?
                                   WHERE id=?""", [r["Name"], float(r["Amount"]), r["Category"], r["Frequency"],
                                                   as_date(r["Next due"]), bool(r["Active"]), int(rid)])
                        changed+=1
                st.session_state.rec_ver=rv+1
                st.toast(f"{changed} change(s) saved ✅" if changed else "Nothing changed")
                st.rerun()
            if c2.button("▶️ Run due now", key="rec_run", width="stretch"):
                n=process_due_recurring(TODAY)
                st.toast(f"{n} transaction(s) created")
                st.rerun()
        with st.expander("➕ Add a recurring expense", expanded=not recs):
            with st.form("rec", clear_on_submit=True, border=False):
                c1,c2=st.columns(2)
                name=c1.text_input("Name", placeholder="Netflix")
                ra=c2.number_input("Amount (€)", min_value=0.01, step=0.01)
                rc=c1.selectbox("Category", EXPENSE_CATEGORIES, index=EXPENSE_CATEGORIES.index("Subscriptions"), key="rc")
                freq=c2.selectbox("Frequency", ["Weekly","Monthly","Quarterly","Yearly"], index=1)
                nd=st.date_input("Next due", TODAY, format="DD/MM/YYYY")
                if st.form_submit_button("Add", type="primary"):
                    if not name.strip():
                        st.error("Give it a name.")
                    else:
                        execute("INSERT INTO recurring_expenses(name,amount,category,frequency,next_due_date) VALUES(?,?,?,?,?)",
                                [name.strip(),ra,rc,freq,nd])
                        st.toast("Recurring expense added ✅")
                        st.rerun()

    # ---------- budgets ----------
    with tab_bud:
        st.caption("Set a monthly limit per category (leave 0 for no budget). Progress shows on the Dashboard.")
        bv=st.session_state.setdefault("bud_ver", 0)
        nm=add_months(MONTH_START, 1)
        spent=cat_totals(MONTH_START, nm)
        avg3={c: v/3 for c, v in cat_totals(add_months(MONTH_START, -3), MONTH_START).items()}
        budgets=get_budgets()
        suggest=st.session_state.get("bud_suggest_ver")==bv
        def suggested(c):
            if budgets.get(c): return budgets[c]
            if suggest and avg3.get(c): return float(-(-avg3[c]//10)*10)   # round up to €10
            return 0.0
        bdf=pd.DataFrame([{"Category": c, "Monthly budget": suggested(c), "Spent this month": spent.get(c, 0.0),
                           "3-month average": avg3.get(c, 0.0)} for c in EXPENSE_CATEGORIES if c not in NON_SPENDING])
        bed=st.data_editor(bdf, key=f"bud_editor_{bv}", hide_index=True, width="stretch", num_rows="fixed",
                           disabled=["Category", "Spent this month", "3-month average"],
                           column_config={
                               "Monthly budget": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="€ %.2f"),
                               "Spent this month": st.column_config.NumberColumn(format="€ %.2f"),
                               "3-month average": st.column_config.NumberColumn(format="€ %.2f"),
                           })
        tot=float(bed["Monthly budget"].fillna(0).sum())
        st.caption(f"Total monthly budget: **{euro(tot)}**")
        c1, c2=st.columns([3, 1])
        if c1.button("💾 Save budgets", type="primary", key="bud_save", width="stretch"):
            for r in bed.to_dict("records"):
                lim=float(r["Monthly budget"] or 0)
                if lim>0:
                    execute("INSERT INTO budgets(category,monthly_limit) VALUES(?,?) ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
                            [r["Category"], lim])
                else:
                    execute("DELETE FROM budgets WHERE category=?", [r["Category"]])
            st.session_state.bud_ver=bv+1
            st.toast("Budgets saved ✅")
            st.rerun()
        if c2.button("✨ Suggest from average", key="bud_suggest_btn", width="stretch",
                     help="Fills empty budgets with your 3-month average, rounded up to €10. Review, then save."):
            st.session_state.bud_suggest_ver=bv+1
            st.session_state.bud_ver=bv+1
            st.rerun()

    # ---------- transactions ----------
    with tab_tx:
        months=tx_months()
        f1,f2,f3,f4=st.columns([1.2,1,1.2,2])
        month=f1.selectbox("Month", ["All"]+months, key="txf_month",
                           format_func=lambda v: "All months" if v=="All" else f"{v:%B %Y}")
        typ_f=f2.selectbox("Type", ["All","Expense","Income"], key="txf_type")
        cat_f=f3.selectbox("Category", ["All"]+CATEGORIES, key="txf_cat")
        q=f4.text_input("Search", placeholder="Woolworths, rent, stipendio…", key="txf_q")
        start=None if month=="All" else month
        end=None if month=="All" else add_months(month, 1)
        rows=_q("""
            SELECT id, transaction_date, amount, category, transaction_type, description, source
            FROM transactions
            WHERE (? IS NULL OR transaction_date>=?) AND (? IS NULL OR transaction_date<?)
              AND (? IS NULL OR transaction_type=?) AND (? IS NULL OR category=?)
              AND (?='' OR lower(coalesce(description,'')) LIKE '%'||lower(?)||'%' OR lower(category) LIKE '%'||lower(?)||'%')
            ORDER BY transaction_date DESC, id DESC LIMIT 1000
        """, [start, start, end, end, None if typ_f=="All" else typ_f, None if typ_f=="All" else typ_f,
              None if cat_f=="All" else cat_f, None if cat_f=="All" else cat_f, q or "", q or "", q or ""])
        inc=sum(float(r[2]) for r in rows if r[4]=="Income")
        exp=sum(float(r[2]) for r in rows if r[4]=="Expense" and r[3] not in NON_SPENDING)
        invd=sum(float(r[2]) for r in rows if r[4]=="Expense" and r[3] in NON_SPENDING)
        st.caption(f"**{len(rows)}** transaction{'s' if len(rows)!=1 else ''}"
                   f"{' (latest 1000)' if len(rows)==1000 else ''} · in {euro(inc)} · spent {euro(exp)}"
                   f"{f' · invested {euro(invd)}' if invd else ''} · saved {signed_euro(inc-exp)}")
        if not rows:
            st.info("No transactions for these filters.")
        else:
            tv=st.session_state.setdefault("tx_ver", 0)
            tdf=pd.DataFrame([{"id": r[0], "Date": as_date(r[1]), "Description": r[5] or "", "Amount": float(r[2]),
                               "Type": r[4], "Category": r[3], "Source": r[6] or "", "Delete": False} for r in rows]).set_index("id")
            ted=st.data_editor(tdf, key=f"tx_editor_{tv}_{month}_{typ_f}_{cat_f}_{q}", hide_index=True, width="stretch",
                               num_rows="fixed", disabled=["Source"], height=min(38+35*len(rows), 560),
                               column_config={
                                   "Date": st.column_config.DateColumn(format="DD/MM/YYYY", width="small"),
                                   "Description": st.column_config.TextColumn(width="large"),
                                   "Amount": st.column_config.NumberColumn(min_value=0.0, format="€ %.2f", width="small"),
                                   "Type": st.column_config.SelectboxColumn(options=["Expense","Income"], width="small"),
                                   "Category": st.column_config.SelectboxColumn(options=CATEGORIES, width="medium"),
                                   "Source": st.column_config.TextColumn(width="small"),
                                   "Delete": st.column_config.CheckboxColumn("🗑️", width="small"),
                               })
            st.caption("Edit any cell directly, tick 🗑️ to delete, then save.")
            c1, c2=st.columns([3, 1])
            if c1.button("💾 Save changes", type="primary", key="tx_save", width="stretch"):
                upd=dele=0
                for rid, r in ted.iterrows():
                    o=tdf.loc[rid]
                    if r["Delete"]:
                        delete_transaction(int(rid)); dele+=1
                    elif any(r[k]!=o[k] for k in ("Date","Description","Amount","Type","Category")):
                        typ=r["Type"]; cat=r["Category"]
                        if cat not in categories_for(typ): cat=auto_categorize(r["Description"], typ)
                        update_transaction(int(rid), as_date(r["Date"]), float(r["Amount"]), cat, typ, r["Description"])
                        if cat!=o["Category"]: learn_category(r["Description"], cat)
                        upd+=1
                st.session_state.tx_ver=tv+1
                st.toast(f"Updated {upd} · deleted {dele} ✅" if upd or dele else "Nothing changed")
                st.rerun()
            c2.download_button("⬇️ Export CSV", tdf.drop(columns=["Delete"]).to_csv().encode("utf-8"),
                               file_name=f"transactions_{'all' if month=='All' else f'{month:%Y-%m}'}.csv",
                               mime="text/csv", width="stretch")

# =====================================================================
# REPORTS
# =====================================================================
elif page=="📊 Reports":
    st.title("📊 Reports")
    c1, c2, _=st.columns([1, 1.4, 2])
    mode=c1.segmented_control("Period", ["Month", "Year"], default="Month", key="rp_mode") or "Month"
    if mode=="Month":
        sel=c2.selectbox("Month", tx_months(), format_func=lambda v: f"{v:%B %Y}", key="rp_month")
        start, end=sel, add_months(sel, 1)
        pstart=add_months(sel, -1); plabel=f"{pstart:%B}"
        title=f"{sel:%B %Y}"
    else:
        ys=sorted({m.year for m in tx_months()}, reverse=True)
        yr=c2.selectbox("Year", ys, key="rp_year")
        start, end=date(yr, 1, 1), date(yr+1, 1, 1)
        pstart=date(yr-1, 1, 1); plabel=str(yr-1)
        title=str(yr)
    cur=tx_totals(start, end); prev=tx_totals(pstart, start)
    k1,k2,k3,k4,k5=st.columns(5)
    kpi(k1, "💵 Income", cur["income"], cur["income"]-prev["income"], pct_change(cur["income"], prev["income"]),
        f"vs {plabel}", "#16a34a")
    kpi(k2, "💳 Spending", cur["expenses"], cur["expenses"]-prev["expenses"], pct_change(cur["expenses"], prev["expenses"]),
        f"Excludes investing · vs {plabel}", "#dc2626", invert=True)
    kpi(k3, "📈 Invested", cur["invested"], cur["invested"]-prev["invested"], None,
        f"{cur['invest_rate']:.0f}% of income" if cur["invest_rate"] is not None else f"vs {plabel}", "#7c3aed")
    kpi(k4, "🔄 Saved", cur["net"], cur["net"]-prev["net"], None,
        f"Income − spending · {euro(cur['cash_left'])} kept as cash", "#2563eb")
    rate, prate=cur["rate"], prev["rate"]
    kpi(k5, "🐷 Savings rate", 0, None if rate is None or prate is None else rate-prate, None,
        f"{cur['count']} transactions", "#0f172a",
        text="—" if rate is None else f"{rate:.1f}%",
        delta_text=None if rate is None or prate is None else f"{rate-prate:+.1f} pp")
    st.write("")

    cats=cat_totals(start, end); pcats=cat_totals(pstart, start)
    cats_all=cat_totals(start, end, include_investing=True)
    t_sp, t_in, t_tr, t_ins=st.tabs(["🧾 Spending", "💵 Income", "📈 Trends", "🔎 Insights"])

    with t_sp:
        if not cats:
            st.info(f"No spending in {title}.")
        else:
            left, right=st.columns([1.1, 1], gap="large")
            with left:
                card("By category")
                if cur["invested"]:
                    st.caption(f"Spending only. You also invested **{euro(cur['invested'])}** in this period, counted as saving.")
                items=list(cats.items())[::-1]
                fig=go.Figure(go.Bar(x=[v for _,v in items], y=[c for c,_ in items], orientation="h",
                                     marker_color=[PIE_COLORS[i % len(PIE_COLORS)] for i in range(len(items))][::-1],
                                     text=[f"{euro(v)} · {v/cur['expenses']*100:.0f}%" for _,v in items], textposition="auto",
                                     hovertemplate="%{y}: € %{x:,.2f}<extra></extra>"))
                style_chart(fig, max(260, 34*len(items)+40))
                fig.update_layout(margin=dict(t=10, l=10, r=10, b=10), showlegend=False)
                fig.update_xaxes(visible=False); fig.update_yaxes(gridcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
            with right:
                card(f"Compared with {plabel}")
                cmp=pd.DataFrame([{"Category": c, "Amount": v, "Share": v/cur["expenses"]*100,
                                   "Change": v-pcats.get(c, 0)} for c, v in cats.items()])
                def col_change(v):
                    return "color:#dc2626;font-weight:600" if v>0 else "color:#16a34a;font-weight:600" if v<0 else ""
                st.dataframe(cmp.style.format({"Amount": "€ {:,.2f}", "Share": "{:.1f}%", "Change": signed_euro})
                             .map(col_change, subset=["Change"]), hide_index=True, width="stretch")
                budgets=get_budgets()
                if mode=="Month" and budgets:
                    card("🎯 Budgets")
                    st.markdown(budget_bars(cats, budgets), unsafe_allow_html=True)
            if mode=="Month":
                card(f"📆 Spending through the month · {sel:%B} vs {plabel}")
                def cumulative(s):
                    e=add_months(s, 1); n=(e-s).days
                    daily={as_date(d): float(v) for d, v in _q(f"""SELECT transaction_date, SUM(amount) FROM transactions
                        WHERE transaction_type='Expense' AND category NOT IN {_NS_SQL}
                        AND transaction_date>=? AND transaction_date<? GROUP BY 1""", [s, e])}
                    out, run=[], 0.0
                    for i in range(n):
                        run+=daily.get(s+timedelta(days=i), 0.0); out.append(run)
                    return out
                cc, pc=cumulative(sel), cumulative(pstart)
                if sel==MONTH_START: cc=cc[:TODAY.day]
                fig=go.Figure()
                fig.add_trace(go.Scatter(name=plabel, x=list(range(1, len(pc)+1)), y=pc, mode="lines",
                                         line=dict(color="#94a3b8", width=2, dash="dot"),
                                         hovertemplate=f"{plabel} day %{{x}}: € %{{y:,.0f}}<extra></extra>"))
                fig.add_trace(go.Scatter(name=f"{sel:%B}", x=list(range(1, len(cc)+1)), y=cc, mode="lines",
                                         line=dict(color="#dc2626", width=3), fill="tozeroy", fillcolor="rgba(220,38,38,.08)",
                                         hovertemplate=f"{sel:%B} day %{{x}}: € %{{y:,.0f}}<extra></extra>"))
                style_chart(fig, 340)
                fig.update_layout(margin=dict(t=10), xaxis_title="Day of month")
                fig.update_yaxes(tickprefix="€ ", separatethousands=True)
                st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
            else:
                card("📆 Spending by month")
                rows=_q("""SELECT DATE_TRUNC('month',transaction_date)::DATE, category, SUM(amount) FROM transactions
                           WHERE transaction_type='Expense' AND transaction_date>=? AND transaction_date<? GROUP BY 1,2""", [start, end])
                months=[date(yr, m, 1) for m in range(1, 13)]
                data={}
                for m, c, v in rows: data.setdefault(c, {})[as_date(m)]=float(v)
                fig=go.Figure()
                for i, c in enumerate(cats):
                    fig.add_trace(go.Bar(name=c, x=months, y=[data.get(c, {}).get(m, 0) for m in months],
                                         marker_color=PIE_COLORS[i % len(PIE_COLORS)],
                                         hovertemplate=f"{c}: € %{{y:,.0f}}<extra></extra>"))
                fig.update_layout(barmode="stack", hovermode="x unified")
                style_chart(fig, 420)
                fig.update_layout(margin=dict(t=10))
                fig.update_xaxes(dtick="M1", tickformat="%b")
                fig.update_yaxes(tickprefix="€ ", separatethousands=True)
                st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)

    with t_in:
        inc=cat_totals(start, end, "Income")
        if not inc:
            st.info(f"No income in {title}.")
        else:
            left, right=st.columns(2, gap="large")
            with left:
                card("By type")
                fig=go.Figure(go.Pie(labels=list(inc), values=list(inc.values()), hole=.6,
                                     marker=dict(colors=["#16a34a", "#86efac", "#2563eb"]),
                                     hovertemplate="%{label}: € %{value:,.2f} (%{percent})<extra></extra>"))
                fig.update_layout(annotations=[dict(text=f"<b>{euro(sum(inc.values()))}</b>", showarrow=False, font_size=16)])
                style_chart(fig, 320); fig.update_layout(margin=dict(t=10, b=10))
                st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
            with right:
                card("Sources")
                src=_q("""SELECT description, category, SUM(amount), COUNT(*) FROM transactions
                          WHERE transaction_type='Income' AND transaction_date>=? AND transaction_date<?
                          GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12""", [start, end])
                st.dataframe(pd.DataFrame([{"Source": d or "—", "Category": c, "Total": float(v), "Times": int(n)} for d, c, v, n in src]),
                             hide_index=True, width="stretch",
                             column_config={"Total": st.column_config.NumberColumn(format="€ %.2f")})

    with t_tr:
        tend=end if mode=="Year" else add_months(sel, 1)
        tstart=start if mode=="Year" else add_months(sel, -11)
        flow=monthly_flow(tstart, tend)
        card("Income, spending, investing" + (f" · {yr}" if mode=="Year" else " · 12 months to " + f"{sel:%b %Y}"))
        st.plotly_chart(flow_chart(flow, 400), width="stretch", config=PLOT_CONFIG)
        card("Savings rate & investing rate")
        st.caption("Savings rate = (income − spending) ÷ income. Investing counts as saving, not spending.")
        sr=[(m, (i-e)/i*100, v/i*100) for m, i, e, v in flow if i]
        if sr:
            fig=go.Figure()
            fig.add_trace(go.Scatter(name="Savings rate", x=[m for m,_,_ in sr], y=[v for _,v,_ in sr], mode="lines+markers",
                                     line=dict(color="#2563eb", width=3, shape="spline"), fill="tozeroy",
                                     fillcolor="rgba(37,99,235,.08)", hovertemplate="Savings rate %{y:.1f}%<extra></extra>"))
            fig.add_trace(go.Scatter(name="Invested (% of income)", x=[m for m,_,_ in sr], y=[v for _,_,v in sr],
                                     mode="lines+markers", line=dict(color="#7c3aed", width=2, dash="dot"),
                                     hovertemplate="Invested %{y:.1f}%<extra></extra>"))
            style_chart(fig, 300); fig.update_layout(margin=dict(t=10), hovermode="x unified")
            fig.update_xaxes(dtick="M1", tickformat="%b\n%Y"); fig.update_yaxes(ticksuffix="%")
            st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
        else:
            st.caption("No income in this range to calculate a savings rate.")
        yr_rows=_q(f"""SELECT EXTRACT(YEAR FROM transaction_date)::INTEGER,
                   SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),
                   SUM(CASE WHEN transaction_type='Expense' AND category NOT IN {_NS_SQL} THEN amount ELSE 0 END),
                   SUM(CASE WHEN transaction_type='Expense' AND category IN {_NS_SQL} THEN amount ELSE 0 END)
               FROM transactions GROUP BY 1 ORDER BY 1 DESC""")
        if len(yr_rows)>1:
            card("Year by year")
            st.dataframe(pd.DataFrame([{"Year": str(y), "Income": float(i), "Spending": float(e), "Invested": float(v),
                                        "Saved": float(i)-float(e),
                                        "Savings rate": (float(i)-float(e))/float(i)*100 if i else None}
                                       for y, i, e, v in yr_rows]),
                         hide_index=True, width="stretch",
                         column_config={**{k: st.column_config.NumberColumn(format="€ %.2f") for k in ("Income","Spending","Invested","Saved")},
                                        "Savings rate": st.column_config.NumberColumn(format="%.1f%%")})

    with t_ins:
        left, right=st.columns(2, gap="large")
        with left:
            card("📐 30 / 20 / 50")
            st.caption("Needs 30% · Wants 20% · Savings 50% of spending.")
            needs_set={"Housing","Utilities","Groceries","Healthcare","Insurance","Transport","Taxes","Debt"}
            wants_set={"Dining Out","Entertainment","Shopping","Travel","Personal","Subscriptions"}
            n=sum(v for c,v in cats_all.items() if c in needs_set); w=sum(v for c,v in cats_all.items() if c in wants_set)
            sv=sum(v for c,v in cats_all.items() if c in {"Savings","Investing"}); oth=sum(cats_all.values())-n-w-sv
            tot=n+w+sv+oth
            if tot:
                labels=["Needs","Wants","Savings/Investing","Other"]; vals=[n,w,sv,oth]; targets=[30,20,50,None]
                fig=go.Figure()
                fig.add_trace(go.Bar(name="Actual", y=labels, x=[v/tot*100 for v in vals], orientation="h",
                                     marker_color=["#2563eb","#f59e0b","#16a34a","#94a3b8"],
                                     text=[f"{v/tot*100:.0f}% · {euro(v)}" for v in vals], textposition="auto",
                                     hovertemplate="%{y}: %{x:.1f}%<extra></extra>"))
                fig.add_trace(go.Scatter(name="Target", y=labels[:3], x=targets[:3], mode="markers",
                                         marker=dict(symbol="line-ns", size=26, line=dict(width=3, color="#0f172a")),
                                         hovertemplate="Target %{x}%<extra></extra>"))
                style_chart(fig, 260); fig.update_layout(margin=dict(t=10, l=10), yaxis=dict(autorange="reversed"))
                fig.update_xaxes(ticksuffix="%", range=[0, 100])
                st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
            else:
                st.caption("No spending in this period.")
            if mode=="Month":
                card("🚨 Unusual spending")
                if get_historical_month_count()<2:
                    st.caption("Needs at least 2 months of history.")
                else:
                    an=spending_anomalies(sel.year, sel.month)
                    if an is None: st.caption("Couldn't check for unusual spending right now.")
                    elif not an: st.success("Nothing unusual — every category is within its normal range.")
                    for c, curv, avg, _ in an:
                        st.warning(f"**{c}**: {euro(curv)} vs your usual {euro(avg)} (+{(float(curv)/float(avg)-1)*100:.0f}%)")
        with right:
            card("🏪 Top merchants")
            exp_rows=_q(f"""SELECT description, amount FROM transactions WHERE transaction_type='Expense'
                           AND category NOT IN {_NS_SQL} AND transaction_date>=? AND transaction_date<?""", [start, end])
            agg={}
            for d, v in exp_rows:
                k=merchant_keys(d)[0] or "(no description)"
                a=agg.setdefault(k, [0.0, 0]); a[0]+=float(v); a[1]+=1
            top=sorted(agg.items(), key=lambda x: -x[1][0])[:10]
            if top:
                st.dataframe(pd.DataFrame([{"Merchant": k.title(), "Total": v[0], "Visits": v[1]} for k, v in top]),
                             hide_index=True, width="stretch",
                             column_config={"Total": st.column_config.NumberColumn(format="€ %.2f")})
            card("💸 Biggest expenses")
            big=_q(f"""SELECT transaction_date, amount, category, transaction_type, description FROM transactions
                      WHERE transaction_type='Expense' AND category NOT IN {_NS_SQL} AND transaction_date>=? AND transaction_date<?
                      ORDER BY amount DESC LIMIT 5""", [start, end])
            st.markdown(tx_rows_html(big), unsafe_allow_html=True)
# =====================================================================
# WEALTH
# =====================================================================
elif page=="💎 Wealth & Strategy":
    st.title("💎 Wealth")
    types=get_account_types()
    records=get_net_worth_records()
    dates, series, kinds=build_series(types)
    nw_vals=networth_values()
    cash_vals=group_values(dates, series, kinds, "Cash")
    inv_vals=group_values(dates, series, kinds, "Investment")

    # ---------- headline cards ----------
    if records:
        last=dates[-1] if dates else None
        prev=dates[-2] if len(dates)>1 else None
        nw_now, nw_prev=float(records[0][4]), (float(records[1][4]) if len(records)>1 else None)
        c_now, i_now=cash_vals.get(last, 0), inv_vals.get(last, 0)
        k1,k2,k3,k4=st.columns(4)
        kpi(k1, "Net worth", nw_now,
            None if nw_prev is None else nw_now-nw_prev, pct_change(nw_now, nw_prev),
            f"Snapshot {as_date(records[0][1]):%d %b %Y}", "#0f172a")
        kpi(k2, "💶 Cash", c_now,
            None if prev is None else c_now-cash_vals[prev], None if prev is None else pct_change(c_now, cash_vals[prev]),
            "Everything except investments", CASH_COLORS[0])
        kpi(k3, "📈 Investments", i_now,
            None if prev is None else i_now-inv_vals[prev], None if prev is None else pct_change(i_now, inv_vals[prev]),
            "Your Investments account", INV_COLORS[0])
        total=c_now+i_now
        k4.markdown(f'<div class="kpi" style="--accent:#7c3aed"><div class="kpi-label">Allocation</div>'
                    f'<div class="kpi-value">{(i_now/total*100 if total else 0):.0f}% <span style="font-size:1rem;color:#64748b">invested</span></div>'
                    f'<div style="display:flex;height:10px;border-radius:6px;overflow:hidden;margin-top:12px">'
                    f'<div style="width:{(c_now/total*100 if total else 0):.1f}%;background:{CASH_COLORS[0]}"></div>'
                    f'<div style="width:{(i_now/total*100 if total else 0):.1f}%;background:{INV_COLORS[0]}"></div></div>'
                    f'<div class="kpi-sub" style="margin-top:8px">{(c_now/total*100 if total else 0):.0f}% cash · '
                    f'{(i_now/total*100 if total else 0):.0f}% investments</div></div>', unsafe_allow_html=True)
        st.write("")

    tab_growth, tab_change, tab_new, tab_edit, tab_router=st.tabs(
        ["📈 Growth", "🔍 Monthly change", "➕ New snapshot", "✏️ Edit snapshot", "💸 Paycheck router"])

    # ---------- growth ----------
    with tab_growth:
        @st.fragment
        def growth_view():
            if not dates:
                st.info("No snapshots yet. Add your first one in the **➕ New snapshot** tab.")
                return
            c1, c2=st.columns([2, 1])
            view=c1.segmented_control("View", ["By account", "Cash vs Investments"], default="By account", key="nw_view") or "By account"
            rng=c2.segmented_control("Range", ["6M", "1Y", "All"], default="All", key="nw_range") or "All"
            start=None
            if rng!="All":
                months=6 if rng=="6M" else 12
                t=date.today(); total_m=t.year*12+t.month-1-months
                start=date(total_m//12, total_m%12+1, 1)
            ds=[d for d in dates if start is None or d>=start]
            def ordered(kind):
                return sorted([n for n in series if kinds[n]==kind], key=lambda n: -sum(series[n].values()))
            fig=go.Figure()
            hover="%{fullData.name}: € %{y:,.0f}<extra></extra>"
            if view=="By account":
                for gi,(kind, title, colors) in enumerate((("Cash","Cash",CASH_COLORS),("Investment","Investments",INV_COLORS))):
                    for i, name in enumerate(ordered(kind)):
                        fig.add_trace(go.Bar(name=name, x=ds, y=[series[name].get(d, 0) for d in ds],
                                             marker_color=colors[i % len(colors)], legendgroup=kind,
                                             legendgrouptitle_text=title if i==0 else None, hovertemplate=hover))
            else:
                fig.add_trace(go.Bar(name="Cash", x=ds, y=[cash_vals[d] for d in ds], marker_color=CASH_COLORS[0], hovertemplate=hover))
                fig.add_trace(go.Bar(name="Investments", x=ds, y=[inv_vals[d] for d in ds], marker_color=INV_COLORS[0], hovertemplate=hover))
            nw_ds=[d for d in sorted(nw_vals) if start is None or d>=start]
            fig.add_trace(go.Scatter(name="Net worth", x=nw_ds, y=[nw_vals[d] for d in nw_ds], mode="lines+markers",
                                     line=dict(color="#0f172a", width=3, shape="spline"), marker=dict(size=7),
                                     hovertemplate=hover))
            fig.update_layout(barmode="stack", yaxis_title="€", hovermode="x unified", bargap=0.28)
            style_chart(fig, 520)
            fig.update_layout(margin=dict(t=16), legend=dict(traceorder="grouped+reversed" if view=="By account" else "reversed"))
            fig.update_yaxes(tickprefix="€ ", separatethousands=True)
            st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)
            with st.expander("All snapshots"):
                st.dataframe(pd.DataFrame([{
                    "Date": as_date(r[1]), "Assets": float(r[2] or 0),
                    "Liabilities": float(r[3] or 0), "Net worth": float(r[4] or 0)} for r in records]),
                    width="stretch", hide_index=True,
                    column_config={"Date": st.column_config.DateColumn(format="DD/MM/YYYY"),
                                   **{k: st.column_config.NumberColumn(format="€ %.2f") for k in ("Assets","Liabilities","Net worth")}})
        growth_view()

    # ---------- monthly change ----------
    with tab_change:
        @st.fragment
        def change_view():
            if len(dates)<2:
                st.info("You need snapshots from at least two different months to see monthly changes.")
                return
            opts={"💶 Cash": cash_vals, "📈 Investments": inv_vals, "💎 Net worth": nw_vals}
            for kind, icon in (("Cash","🏦"),("Investment","📊")):
                for n in sorted([n for n in series if kinds[n]==kind], key=str.lower):
                    opts[f"{icon} {n}"]={d: series[n].get(d, 0) for d in dates}
            c1, c2=st.columns([2, 1])
            sel=c1.selectbox("What do you want to look at?", list(opts), key="mc_sel",
                             help="Cash = every account except investments. You can also pick a single account.")
            rows=monthly_series(opts[sel])
            if rng_rows:=c2.segmented_control("Range", ["6M", "1Y", "All"], default="1Y", key="mc_range"):
                if rng_rows!="All":
                    rows=rows[-(int(rng_rows[:-1])*(12 if rng_rows.endswith("Y") else 1))-1:]
            ch=[r for r in rows if r["change"] is not None]
            if not ch:
                st.info("Not enough months in this range yet.")
                return
            lastr=ch[-1]
            first=rows[0]
            avg=sum(r["change"] for r in ch)/len(ch)
            best=max(ch, key=lambda r: r["change"]); worst=min(ch, key=lambda r: r["change"])
            m1,m2,m3,m4=st.columns(4)
            m1.metric(f"Last month · {lastr['month']:%b %Y}", signed_euro(lastr["change"]),
                      f"{lastr['pct']:+.1f}%" if lastr["pct"] is not None else None)
            m2.metric("Average per month", signed_euro(avg),
                      f"{sum(r['pct'] for r in ch if r['pct'] is not None)/max(1,len([r for r in ch if r['pct'] is not None])):+.1f}%")
            tot=rows[-1]["value"]-first["value"]
            tp=pct_change(rows[-1]["value"], first["value"])
            m3.metric(f"Total since {first['month']:%b %Y}", signed_euro(tot), f"{tp:+.1f}%" if tp is not None else None)
            m4.metric("Best / worst month", f"{best['month']:%b} {signed_euro(best['change'])}",
                      f"{worst['month']:%b %Y}: {signed_euro(worst['change'])}", delta_color="off")

            fig=make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(go.Bar(name="Change (€)", x=[r["month"] for r in ch], y=[r["change"] for r in ch],
                                 marker_color=["#16a34a" if r["change"]>=0 else "#dc2626" for r in ch],
                                 text=[f"{r['pct']:+.1f}%" if r["pct"] is not None else "new" for r in ch],
                                 textposition="outside", cliponaxis=False,
                                 customdata=[[signed_euro(r["change"]), f"{r['pct']:+.2f}%" if r["pct"] is not None else "—"] for r in ch],
                                 hovertemplate="%{x|%b %Y}<br>%{customdata[0]} (%{customdata[1]})<extra></extra>"),
                          secondary_y=False)
            fig.add_trace(go.Scatter(name="Balance", x=[r["month"] for r in ch], y=[r["value"] for r in ch],
                                     mode="lines+markers", line=dict(color="#0f172a", width=2.5, shape="spline"),
                                     hovertemplate="Balance € %{y:,.0f}<extra></extra>"), secondary_y=True)
            style_chart(fig, 460)
            fig.update_layout(margin=dict(t=24), hovermode="x unified", bargap=0.35)
            fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
            fig.update_yaxes(title_text="Monthly change (€)", tickprefix="€ ", separatethousands=True, secondary_y=False)
            fig.update_yaxes(title_text="Balance (€)", tickprefix="€ ", separatethousands=True, showgrid=False, secondary_y=True)
            st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG)

            table=pd.DataFrame([{"Month": r["month"].strftime("%b %Y"), "Balance": r["value"],
                                 "Change €": r["change"], "Change %": r["pct"]} for r in reversed(rows)])
            def color(v):
                if v is None or pd.isna(v): return "color:#94a3b8"
                return "color:#16a34a;font-weight:600" if v>0 else "color:#dc2626;font-weight:600" if v<0 else ""
            styled=(table.style
                    .format({"Balance": "€ {:,.2f}",
                             "Change €": lambda v: "—" if pd.isna(v) else signed_euro(v),
                             "Change %": lambda v: "—" if pd.isna(v) else f"{v:+.2f}%"})
                    .map(color, subset=["Change €", "Change %"]))
            st.dataframe(styled, width="stretch", hide_index=True)
        change_view()

    # ---------- new snapshot ----------
    with tab_new:
        @st.fragment
        def new_snapshot():
            ver=st.session_state.setdefault("nw_new_ver", 0)
            base_key=f"nw_new_base_{ver}"
            if base_key not in st.session_state:
                a, l, src=default_lines()
                st.session_state[base_key]=(lines_df(a, True), lines_df(l, False), src)
            adf, ldf, src=st.session_state[base_key]
            if src:
                st.caption(f"Pre-filled with your accounts and balances from **{src:%d/%m/%Y}**. "
                           "Just update the amounts and save. Add a row at the bottom of the table, or select a row and delete it.")
            else:
                st.caption("One row per account. Mark each one as Cash or Investment — next month they'll be pre-filled for you.")
            d=st.date_input("Snapshot date", date.today(), key="nw_new_date", format="DD/MM/YYYY")
            if any(as_date(r[1]).year==d.year and as_date(r[1]).month==d.month for r in records):
                st.warning(f"You already have a snapshot for {d:%B %Y}. Saving adds a second one — "
                           "to change the existing one, use **✏️ Edit snapshot**.")
            st.markdown("**Assets**")
            ea=lines_editor(adf, f"nw_new_a_{ver}", True)
            with st.expander("Liabilities (loans, credit cards)", expanded=not ldf.empty):
                el=lines_editor(ldf, f"nw_new_l_{ver}", False)
            assets=items_from_df(ea, "Asset"); liabs=items_from_df(el, "Liability")
            if assets is not None and liabs is not None:
                cash=sum(i["amount"] for i in assets if i["group"]=="Cash")
                inv=sum(i["amount"] for i in assets if i["group"]=="Investment")
                debt=sum(i["amount"] for i in liabs)
                p1,p2,p3=st.columns(3)
                nw_prev=float(records[0][4]) if records else None
                p1.metric("Cash", euro(cash)); p2.metric("Investments", euro(inv))
                p3.metric("Net worth", euro(cash+inv-debt),
                          None if nw_prev is None else f"{signed_euro(cash+inv-debt-nw_prev)} vs last")
            if st.button("Save snapshot", type="primary", key="nw_save_new", width="stretch"):
                if assets is None or liabs is None:
                    st.error("Give every amount an account name.")
                    return
                items=merge_items(assets+liabs)
                if not items:
                    st.warning("Enter at least one amount.")
                    return
                save_snapshot(d, items)
                st.session_state.nw_new_ver=ver+1
                st.session_state.nw_edit_ver=st.session_state.get("nw_edit_ver", 0)+1
                st.toast("Snapshot saved ✅")
                st.rerun()
        new_snapshot()

    # ---------- edit snapshot ----------
    with tab_edit:
        @st.fragment
        def edit_snapshot():
            if not records:
                st.info("Nothing to edit yet.")
                return
            ids=[r[0] for r in records]
            labels={r[0]: f"{as_date(r[1]):%d/%m/%Y} · {euro(r[4])}" for r in records}
            if st.session_state.get("nw_pick") not in ids:
                st.session_state.nw_pick=ids[0]
            pick=st.selectbox("Snapshot", ids, format_func=lambda i: labels[i], key="nw_pick")
            chosen=next(r for r in records if r[0]==pick)
            ver=st.session_state.setdefault("nw_edit_ver", 0)
            base_key=f"nw_edit_base_{pick}_{ver}"
            if base_key not in st.session_state:
                a, l=lines_from_snapshot(pick, types)
                st.session_state[base_key]=(lines_df(a, True), lines_df(l, False))
            adf, ldf=st.session_state[base_key]
            nd=st.date_input("Snapshot date", as_date(chosen[1]), key=f"nw_edit_d_{pick}_{ver}", format="DD/MM/YYYY")
            st.markdown("**Assets**")
            ea=lines_editor(adf, f"nw_edit_a_{pick}_{ver}", True)
            with st.expander("Liabilities", expanded=not ldf.empty):
                el=lines_editor(ldf, f"nw_edit_l_{pick}_{ver}", False)
            b1, b2=st.columns([3, 1])
            if b1.button("Save changes", type="primary", key="nw_edit_save", width="stretch"):
                assets=items_from_df(ea, "Asset"); liabs=items_from_df(el, "Liability")
                if assets is None or liabs is None:
                    st.error("Give every amount an account name.")
                    return
                items=merge_items(assets+liabs)
                if not items:
                    st.warning("Enter at least one amount, or delete the snapshot.")
                    return
                replace_snapshot(pick, nd, items)
                st.session_state.nw_edit_ver=ver+1
                st.session_state.nw_new_ver=st.session_state.get("nw_new_ver", 0)+1
                st.toast("Snapshot updated ✅")
                st.rerun()
            with b2.popover("🗑️ Delete", width="stretch"):
                st.write(f"Delete the snapshot from **{as_date(chosen[1]):%d/%m/%Y}**? This can't be undone.")
                if st.button("Yes, delete it", type="primary", key="nw_edit_delete"):
                    remove_snapshot(pick)
                    st.session_state.nw_edit_ver=ver+1
                    st.session_state.nw_new_ver=st.session_state.get("nw_new_ver", 0)+1
                    st.toast("Snapshot deleted")
                    st.rerun()
        edit_snapshot()

    # ---------- paycheck router ----------
    with tab_router:
        existing={r[0]:float(r[1]) for r in fetch_all("SELECT account_name,percentage FROM routing_rules")}
        st.caption("Split each paycheck automatically. The three percentages must add up to 100%.")
        with st.form("routing"):
            c1,c2,c3=st.columns(3)
            p1=c1.number_input("Checking %",0.,100.,existing.get("Checking",50.),1.)
            p2=c2.number_input("Savings %",0.,100.,existing.get("Savings",30.),1.)
            p3=c3.number_input("Investments %",0.,100.,existing.get("Investments",20.),1.)
            if st.form_submit_button("Save rules"):
                if abs(p1+p2+p3-100)>1e-9: st.error("They must add up to exactly 100%.")
                else:
                    for name,p in [("Checking",p1),("Savings",p2),("Investments",p3)]:
                        execute("INSERT INTO routing_rules(account_name,percentage) VALUES(?,?) ON CONFLICT(account_name) DO UPDATE SET percentage=excluded.percentage",[name,p])
                    st.toast("Rules saved ✅"); st.rerun()
        paycheck=st.number_input("Net paycheck (€)",min_value=0.,step=100.,format="%.2f")
        if abs(p1+p2+p3-100)<1e-9:
            c1,c2,c3=st.columns(3)
            c1.metric("Checking",euro(paycheck*p1/100),f"{p1:.1f}%",delta_color="off")
            c2.metric("Savings",euro(paycheck*p2/100),f"{p2:.1f}%",delta_color="off")
            c3.metric("Investments",euro(paycheck*p3/100),f"{p3:.1f}%",delta_color="off")
# =====================================================================
# AI
# =====================================================================
else:
    st.title("🤖 AI Insights")
    st.caption("Ask anything about your money. Answers use only your own data, read-only.")
    if "ai_messages" not in st.session_state: st.session_state.ai_messages=[]
    with st.sidebar:
        if get_secret("GEMINI_API_KEY"): st.success("Gemini connected")
        else: st.warning("Gemini not configured — add GEMINI_API_KEY")
        if st.button("🧹 Clear chat", width="stretch"): st.session_state.ai_messages=[]; st.rerun()
    def context():
        ensure_net_worth_items()
        types=get_account_types()
        dates_, series_, kinds_=build_series(types)
        p={"today": str(TODAY),
           "rules": ("Cash = every account except the Investments account. Amounts in EUR. "
                     "'expenses' means real spending and EXCLUDES the Investing category; 'invested' is money put into "
                     "investments and counts as saving. Savings rate = (income - spending) / income."),
           "this_month": tx_totals(MONTH_START, add_months(MONTH_START, 1)),
           "last_month": tx_totals(add_months(MONTH_START, -1), MONTH_START),
           "spend_by_category_last_6_months": [(str(m), c, float(v)) for m, c, v in fetch_all("""
                SELECT DATE_TRUNC('month',transaction_date)::DATE, category, SUM(amount) FROM transactions
                WHERE transaction_type='Expense' AND transaction_date>=? GROUP BY 1,2 ORDER BY 1 DESC, 3 DESC""",
                [add_months(MONTH_START, -5)])],
           "cashflow_by_month_income_spending_invested": [(str(m), i, e, v) for m, i, e, v in monthly_flow(add_months(MONTH_START, -11), add_months(MONTH_START, 1))],
           "recent_transactions": fetch_all("""SELECT transaction_date,amount,category,transaction_type,description
                FROM transactions ORDER BY transaction_date DESC,id DESC LIMIT 40"""),
           "recurring": fetch_all("SELECT name,amount,category,frequency,next_due_date,active FROM recurring_expenses"),
           "budgets": get_budgets(),
           "wealth_by_snapshot": [{"date": str(d),
                                   "cash": sum(v.get(d,0) for n,v in series_.items() if kinds_[n]=="Cash"),
                                   "investments": sum(v.get(d,0) for n,v in series_.items() if kinds_[n]=="Investment"),
                                   "accounts": {n: v.get(d,0) for n,v in series_.items() if v.get(d)}}
                                  for d in dates_[-12:]],
           "net_worth_history": [(str(d), float(v)) for d, v in get_net_worth_history()][-24:]}
        return json.dumps(p, default=str, ensure_ascii=False)
    pending=None
    if not st.session_state.ai_messages:
        st.write("")
        st.markdown("**Try asking:**")
        ideas=["Where did I spend the most this month?", "How does this month compare with last month?",
               "How have my cash and investments changed recently?", "Which recurring costs or subscriptions could I cut?"]
        cols=st.columns(2)
        for i, idea in enumerate(ideas):
            if cols[i%2].button(idea, key=f"idea_{i}", width="stretch"):
                pending=idea
    for msg in st.session_state.ai_messages:
        with st.chat_message(msg["role"], avatar="🧑" if msg["role"]=="user" else "🤖"): st.markdown(msg["content"])
    q=st.chat_input("e.g. Where did I spend the most money this week?") or pending
    if q:
        st.session_state.ai_messages.append({"role":"user","content":q})
        with st.chat_message("user", avatar="🧑"): st.markdown(q)
        with st.chat_message("assistant", avatar="🤖"):
            with st.spinner("Analysing your data…"):
                history="\n".join(f"{m['role']}: {m['content']}" for m in st.session_state.ai_messages[-6:-1])
                ans=ask_gemini((f"Previous conversation:\n{history}\n\nNew question: " if history else "")+q, context())
            st.markdown(ans)
        st.session_state.ai_messages.append({"role":"assistant","content":ans})
