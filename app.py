from __future__ import annotations

import json
import uuid
from datetime import date, datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from db import (
    delete_recurring_expense, delete_transaction,
    execute, fetch_all, fetch_one, get_anomalies,
    get_category_spending, get_db,
    get_historical_month_count, get_monthly_summary,
    insert_transaction, process_due_recurring,
    update_transaction,
)
try:
    from utils import (
        CATEGORIES, EXPENSE_CATEGORIES, ask_gemini, categories_for,
        extract_image_transactions, get_secret, parse_natural_language,
        transaction_fingerprint, auto_categorize, merchant_keys,
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
INVEST_KEYS=("invest","etf","broker","azioni","trading 212","trading212","t212","mexem",
             "degiro","scalable","trade republic","ibkr","interactive brokers","directa",
             "stock","shares","crypto","fondo","fund","pension","super","bitcoin")
CASH_COLORS=["#1d4ed8","#3b82f6","#60a5fa","#93c5fd","#0e7490","#06b6d4","#67e8f9","#bfdbfe","#1e3a8a","#0284c7"]
INV_COLORS=["#ea580c","#f97316","#fb923c","#fdba74","#c2410c","#f59e0b","#fbbf24","#fed7aa","#9a3412","#d97706"]

def guess_kind(name):
    n=(name or "").lower()
    return "Investment" if any(k in n for k in INVEST_KEYS) else "Cash"

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

def kpi(col, label, value, delta=None, pct=None, sub=None, accent="#2563eb"):
    d=""
    if delta is not None:
        color="#16a34a" if delta>0 else "#dc2626" if delta<0 else "#64748b"
        arrow="▲" if delta>0 else "▼" if delta<0 else "■"
        p=f" · {pct:+.1f}%" if pct is not None else ""
        d=f'<div class="kpi-delta" style="color:{color}">{arrow} {signed_euro(delta)}{p}</div>'
    s=f'<div class="kpi-sub">{sub}</div>' if sub else ""
    col.markdown(f'<div class="kpi" style="--accent:{accent}"><div class="kpi-label">{label}</div>'
                 f'<div class="kpi-value">{euro(value)}</div>{d}{s}</div>', unsafe_allow_html=True)

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
                                                     help="Leave empty on a new row and it is detected from the name")
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
    st.title("💰 Finance OS")
    page=st.radio("Navigation",["🏠 Dashboard","📝 Data Entry","📊 Reports","💎 Wealth & Strategy","🤖 AI Insights"])
    st.divider()
    st.caption(f"DuckDB · {date.today():%d/%m/%Y}")

# =====================================================================
# DASHBOARD
# =====================================================================
if page=="🏠 Dashboard":
    st.title("🏠 Financial Dashboard")
    t=date.today(); s=get_monthly_summary(t.year,t.month); ps=get_monthly_summary(*previous_month(t).timetuple()[:2])
    hist=get_net_worth_history(); nw=float(hist[-1][1]) if hist else None
    nw_delta=round(float(hist[-1][1])-float(hist[-2][1]),2) if len(hist)>=2 else None
    st.subheader("Current balances")
    st.caption(f"Compared with {month_name(previous_month(t).month)} {previous_month(t).year}.")
    c1,c2=st.columns(2)
    c1.markdown(f'<div class="card income"><b>💵 MONEY IN — THIS MONTH</b><div class="big">{euro(s["income"])}</div>{delta_line(s["income"],ps["income"])}</div>',unsafe_allow_html=True)
    c2.markdown(f'<div class="card expense"><b>💳 MONEY OUT — THIS MONTH</b><div class="big">{euro(s["expenses"])}</div>{delta_line(s["expenses"],ps["expenses"],invert=True)}</div>',unsafe_allow_html=True)
    st.write("")
    c1,c2,c3=st.columns(3)
    c1.metric("Net Cash Flow",euro(s["cash_flow"]),delta=round(s["cash_flow"]-ps["cash_flow"],2))
    sr=(s["cash_flow"]/s["income"]*100) if s["income"] else 0.0
    psr=(ps["cash_flow"]/ps["income"]*100) if ps["income"] else 0.0
    c2.metric("Savings Rate",f"{sr:.1f}%",delta=f"{sr-psr:+.1f} pp")
    c3.metric("Net Worth",euro(nw) if nw is not None else "—",delta=nw_delta,help="Change versus the previous snapshot" if nw_delta is not None else None)
    st.divider()
    st.header("📈 Income vs Expenses")
    y=st.selectbox("Select year",years(),key="dash_y")
    r=monthly_rows(y)
    fig=go.Figure()
    fig.add_trace(go.Bar(x=[month_name(x[0]) for x in r],y=[float(x[1]) for x in r],name="Income",marker_color=INCOME_COLOR))
    fig.add_trace(go.Bar(x=[month_name(x[0]) for x in r],y=[float(x[2]) for x in r],name="Expenses",marker_color=EXPENSE_COLOR))
    fig.update_layout(barmode="group",title=f"Monthly Income vs Expenses — {y}",yaxis_title="€")
    st.plotly_chart(style_chart(fig,500),width="stretch")
    yr=yearly_rows()
    if yr:
        yf=go.Figure()
        yf.add_trace(go.Bar(x=[str(x[0]) for x in yr],y=[float(x[1]) for x in yr],name="Income",marker_color=INCOME_COLOR))
        yf.add_trace(go.Bar(x=[str(x[0]) for x in yr],y=[float(x[2]) for x in yr],name="Expenses",marker_color=EXPENSE_COLOR))
        yf.update_layout(barmode="group",title="Overall Yearly Income vs Expenses",yaxis_title="€")
        st.plotly_chart(style_chart(yf,450),width="stretch")

# =====================================================================
# DATA ENTRY
# =====================================================================
elif page=="📝 Data Entry":
    st.title("📝 Data Entry")
    tab_quick, tab_import, tab_rec, tab_tx = st.tabs(["Quick add", "Import", "Recurring", "Transactions"])

    with tab_quick:
        st.subheader("Natural language")
        st.caption("Write it the way you would say it. The category is a guess — change it before saving if it is wrong.")
        if st.session_state.pop("nl_clear", False):
            st.session_state.nl_text=""
            st.session_state.nl_sig=""
        nl=st.text_input("Describe the transaction", key="nl_text", placeholder="Esselunga 42,50 ieri   ·   Stipendio 2.000 oggi")
        parsed=None
        if nl.strip():
            try:
                parsed=parse_natural_language(nl)
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
                guess=categories_for(parsed["transaction_type"])
                st.session_state.nl_cat=parsed["category"] if parsed["category"] in guess else guess[0]
            c1,c2,c3=st.columns(3)
            d=c1.date_input("Date", key="nl_date")
            a=c2.number_input("Amount", min_value=0.01, step=0.01, key="nl_amount")
            typ=c3.selectbox("Type", ["Expense","Income"], key="nl_type")
            cats=categories_for(typ)
            if st.session_state.get("nl_cat") not in cats:
                st.session_state.nl_cat=cats[0]
            cat=st.selectbox("Category", cats, key="nl_cat")
            desc=st.text_input("Description", key="nl_desc")
            if st.button("Save", type="primary", key="nl_save"):
                fp=transaction_fingerprint(d,a,cat,typ,desc)
                if insert_transaction(d,a,cat,typ,desc,"natural_language",fp):
                    st.session_state.nl_clear=True
                    st.toast("Salvata")
                    st.rerun()
                else:
                    st.toast("Transazione duplicata")
        st.divider()
        st.subheader("Manual entry")
        typ=st.radio("Type", ["Expense","Income"], horizontal=True, key="manual_typ")
        cats=categories_for(typ)
        if "manual_cat" in st.session_state and st.session_state.manual_cat not in cats:
            st.session_state.manual_cat=cats[0]
        with st.form("manual", clear_on_submit=True):
            c1,c2=st.columns(2)
            d=c1.date_input("Date", date.today())
            a=c2.number_input("Amount", min_value=0.01, step=0.01)
            cat=st.selectbox("Category", cats, key="manual_cat")
            desc=st.text_input("Description", placeholder="Esselunga")
            if st.form_submit_button("Save", width="stretch"):
                fp=transaction_fingerprint(d,a,cat,typ,desc)
                if insert_transaction(d,a,cat,typ,desc,"manual",fp):
                    st.toast("Salvata")
                else:
                    st.toast("Transazione duplicata")

    with tab_import:
        st.subheader("📸 Bank or receipt screenshot")
        st.caption("Upload a screenshot from your banking app or a photo of a receipt. Every transaction is read and "
                   "categorised automatically. Fix anything that's wrong before importing — the app remembers your "
                   "choice for that merchant next time.")
        imgf=st.file_uploader("Upload screenshot", type=["png","jpg","jpeg","webp"], key="img_upload",
                              label_visibility="collapsed")
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
                        for r in rows:
                            r["category"], r["how"]=smart_category(r["description"], r["transaction_type"], r.get("category"))
                    st.session_state.ocr_rows=rows
                    st.session_state.ocr_raw=raw
                    st.session_state.ocr_ver=st.session_state.get("ocr_ver", 0)+1
                    if rows:
                        st.toast(f"{len(rows)} transaction(s) found")
                    else:
                        st.warning("No transactions recognised. Try a sharper screenshot, or add it from Quick add.")
        if st.session_state.get("ocr_rows"):
            st.subheader("Review & import")
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
            exp_total=chosen.loc[chosen["Type"]=="Expense","Amount"].sum()
            inc_total=chosen.loc[chosen["Type"]=="Income","Amount"].sum()
            st.caption(f"{len(chosen)} selected · expenses {euro(exp_total)} · income {euro(inc_total)}")
            b1, b2=st.columns([3, 1])
            if b1.button("✅ Import selected", type="primary", key="import_ocr", width="stretch"):
                ins=dup=0
                for r in chosen.to_dict("records"):
                    d=as_date(r["Date"]); amount=float(r["Amount"]); typ=r["Type"] or "Expense"
                    desc=str(r["Description"] or "").strip()
                    cat=r["Category"] if r["Category"] in categories_for(typ) else auto_categorize(desc, typ)
                    fp=transaction_fingerprint(d, amount, cat, typ, desc)
                    if insert_transaction(d, amount, cat, typ, desc, "image_ocr", fp): ins+=1
                    else: dup+=1
                    learn_category(desc, cat)
                st.session_state.ocr_rows=[]
                st.toast(f"Imported {ins}" + (f" · skipped {dup} duplicate(s)" if dup else "") + " ✅")
                st.rerun()
            if b2.button("Discard", key="discard_ocr", width="stretch"):
                st.session_state.ocr_rows=[]
                st.rerun()

    with tab_rec:
        st.subheader("Recurring expenses")
        if st.button("Process due recurring expenses"):
            n=process_due_recurring(date.today())
            st.toast(f"{n} transazioni create")
            st.rerun()
        t1,t2=st.tabs(["Add","Manage"])
        with t1:
            with st.form("rec"):
                c1,c2=st.columns(2)
                name=c1.text_input("Name")
                ra=c2.number_input("Amount", min_value=0.01, step=0.01)
                rc=c1.selectbox("Category", EXPENSE_CATEGORIES, key="rc")
                freq=c2.selectbox("Frequency", ["Weekly","Monthly","Quarterly","Yearly"])
                nd=st.date_input("Next due", date.today())
                if st.form_submit_button("Add"):
                    execute("INSERT INTO recurring_expenses(name,amount,category,frequency,next_due_date) VALUES(?,?,?,?,?)",[name,ra,rc,freq,nd])
                    st.toast("Aggiunta")
        with t2:
            recs=fetch_all("SELECT id,name,amount,category,frequency,next_due_date,active FROM recurring_expenses ORDER BY next_due_date")
            if not recs:
                st.info("No recurring expenses yet.")
            for rid,name,a,cat,freq,nd,active in recs:
                c1,c2,c3,c4,c5=st.columns([2,1,1.4,1.4,.5])
                c1.write(name); c2.write(euro(a)); c3.write(cat); c4.write(f"{freq} · {nd}")
                if c5.button("🗑️", key=f"rd{rid}"):
                    delete_recurring_expense(rid)
                    st.rerun()

    with tab_tx:
        st.subheader("Transactions")
        month_rows=fetch_all("SELECT DISTINCT EXTRACT(YEAR FROM transaction_date)::INTEGER, EXTRACT(MONTH FROM transaction_date)::INTEGER FROM transactions ORDER BY 1 DESC, 2 DESC")
        month_opts=["All"]+[f"{int(y):04d}-{int(m):02d}" for y,m in month_rows]
        f1,f2,f3,f4=st.columns(4)
        month=f1.selectbox("Month", month_opts, format_func=lambda v: "All months" if v=="All" else f"{month_name(int(v[5:7]))} {v[:4]}")
        typ_f=f2.selectbox("Type", ["All","Expense","Income"])
        cat_f=f3.selectbox("Category", ["All"]+CATEGORIES)
        q=f4.text_input("Search", placeholder="Esselunga, rent, stipendio")
        year=int(month[:4]) if month!="All" else None
        mon=int(month[5:7]) if month!="All" else None
        tx_type=None if typ_f=="All" else typ_f
        tx_cat=None if cat_f=="All" else cat_f
        rows=fetch_all("""
            SELECT id, transaction_date, amount, category, transaction_type, description, source
            FROM transactions
            WHERE (? IS NULL OR EXTRACT(YEAR FROM transaction_date)=?)
              AND (? IS NULL OR EXTRACT(MONTH FROM transaction_date)=?)
              AND (? IS NULL OR transaction_type=?)
              AND (? IS NULL OR category=?)
              AND (?='' OR lower(coalesce(description,'')) LIKE '%'||lower(?)||'%'
                   OR lower(category) LIKE '%'||lower(?)||'%')
            ORDER BY transaction_date DESC, id DESC
            LIMIT 400
        """, [year, year, mon, mon, tx_type, tx_type, tx_cat, tx_cat, q or "", q or "", q or ""])
        st.caption(f"{len(rows)} transaction" + ("s" if len(rows)!=1 else "") + (" — latest 400" if len(rows)==400 else ""))
        if not rows:
            st.info("No transactions for these filters.")
        else:
            st.dataframe([{
                "Date": as_date(r[1]).strftime("%d/%m/%Y"),
                "Amount": float(r[2]),
                "Category": r[3],
                "Type": r[4],
                "Description": r[5] or "",
                "Source": r[6] or "",
            } for r in rows], width="stretch", hide_index=True)
            labels={r[0]: f"{as_date(r[1]):%d/%m/%Y} · {euro(r[2])} · {r[3]} · {r[5] or '—'}" for r in rows}
            ids=[r[0] for r in rows]
            if st.session_state.get("tx_edit_id") not in ids:
                st.session_state.tx_edit_id=ids[0]
            rid=st.selectbox("Edit one transaction", ids, format_func=lambda i: labels[i], key="tx_edit_id")
            row=next(r for r in rows if r[0]==rid)
            if st.session_state.get("tx_loaded")!=rid:
                st.session_state.tx_loaded=rid
                st.session_state.tx_date=as_date(row[1])
                st.session_state.tx_amount=float(row[2])
                st.session_state.tx_type=row[4] if row[4] in ("Expense","Income") else "Expense"
                st.session_state.tx_desc=row[5] or ""
                guess=categories_for(st.session_state.tx_type)
                st.session_state.tx_cat=row[3] if row[3] in guess else guess[0]
            c1,c2,c3=st.columns(3)
            nd=c1.date_input("Date", key="tx_date")
            na=c2.number_input("Amount", min_value=0.0, step=0.01, key="tx_amount")
            nt=c3.selectbox("Type", ["Expense","Income"], key="tx_type")
            cats=categories_for(nt)
            if st.session_state.get("tx_cat") not in cats:
                st.session_state.tx_cat=cats[0]
            nc=st.selectbox("Category", cats, key="tx_cat")
            nx=st.text_input("Description", key="tx_desc")
            b1,b2=st.columns(2)
            if b1.button("Save changes", key="tx_save"):
                update_transaction(rid, nd, na, nc, nt, nx)
                if nc!=row[3]:
                    learn_category(nx, nc)
                st.session_state.tx_loaded=None
                st.toast("Aggiornata")
                st.rerun()
            if b2.button("Delete", key="tx_delete"):
                delete_transaction(rid)
                st.session_state.tx_loaded=None
                st.toast("Eliminata")
                st.rerun()

# =====================================================================
# REPORTS
# =====================================================================
elif page=="📊 Reports":
    st.title("📊 Reports")
    mode=st.radio("View",["Month","Year"],horizontal=True,key="cc_mode")
    if mode=="Month":
        months=fetch_all("SELECT DISTINCT EXTRACT(YEAR FROM transaction_date)::INTEGER,EXTRACT(MONTH FROM transaction_date)::INTEGER FROM transactions ORDER BY 1 DESC,2 DESC")
        opts=[(int(y),int(m)) for y,m in months] or [(date.today().year,date.today().month)]
        y,m=st.selectbox("Select month",opts,format_func=lambda x:f"{month_name(x[1])} {x[0]}")
        s=get_monthly_summary(y,m);w,o=income_split(y,m)
        st.subheader(f"Financial picture — {month_name(m)} {y}")
        c1,c2=st.columns(2); c1.markdown(f'<div class="card income"><b>💵 TOTAL INCOME</b><div class="big">{euro(s["income"])}</div></div>',unsafe_allow_html=True); c2.markdown(f'<div class="card expense"><b>💳 TOTAL EXPENSES</b><div class="big">{euro(s["expenses"])}</div></div>',unsafe_allow_html=True)
        c1,c2,c3=st.columns(3); c1.metric("Wage",euro(w)); c2.metric("Other Income",euro(o)); c3.metric("Net Cash Flow",euro(s["cash_flow"]))
        st.divider(); st.header("🥧 Income & Spending"); cats=get_category_spending(y,m); p1,p2=st.columns(2)
        if w+o>0: p1.plotly_chart(px.pie(names=["Wage","Other Income"],values=[w,o],hole=.5,color_discrete_sequence=PIE_COLORS,title="Income split"),width="stretch")
        if cats: p2.plotly_chart(px.pie(names=[x[0] for x in cats],values=[float(x[1]) for x in cats],hole=.5,color_discrete_sequence=PIE_COLORS,title="Spending by category"),width="stretch")
        st.divider(); st.header("💳 Spending by Category")
        if cats:
            cols=st.columns(3)
            for i,(cat,total) in enumerate(cats): cols[i%3].metric(cat,euro(total),f"{float(total)/s['expenses']*100:.1f}%" if s['expenses'] else "0%")
    else:
        y=st.selectbox("Select year",years(),key="cc_year")
        inc,exp=map(float,fetch_one("SELECT COALESCE(SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),0),COALESCE(SUM(CASE WHEN transaction_type='Expense' THEN amount ELSE 0 END),0) FROM transactions WHERE EXTRACT(YEAR FROM transaction_date)=?",[y]))
        wage=float(fetch_one("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE transaction_type='Income' AND EXTRACT(YEAR FROM transaction_date)=? AND (category='Wage' OR lower(coalesce(description,'')) LIKE '%salary%' OR lower(coalesce(description,'')) LIKE '%stipendio%' OR lower(coalesce(description,'')) LIKE '%wage%' OR lower(coalesce(description,'')) LIKE '%payroll%')",[y])[0] or 0); other=inc-wage
        st.subheader(f"Financial overview — {y}"); c1,c2=st.columns(2); c1.markdown(f'<div class="card income"><b>💵 TOTAL INCOME</b><div class="big">{euro(inc)}</div></div>',unsafe_allow_html=True); c2.markdown(f'<div class="card expense"><b>💳 TOTAL EXPENSES</b><div class="big">{euro(exp)}</div></div>',unsafe_allow_html=True)
        c1,c2,c3=st.columns(3); c1.metric("Wage",euro(wage)); c2.metric("Other Income",euro(other)); c3.metric("Net Cash Flow",euro(inc-exp))
        monthly=monthly_rows(y); fig=go.Figure(); fig.add_trace(go.Bar(x=[month_name(x[0]) for x in monthly],y=[float(x[1]) for x in monthly],name="Income",marker_color="#16a34a")); fig.add_trace(go.Bar(x=[month_name(x[0]) for x in monthly],y=[float(x[2]) for x in monthly],name="Expenses",marker_color="#dc2626")); fig.update_layout(barmode="group",title=f"Monthly overview — {y}",yaxis_title="€"); st.plotly_chart(style_chart(fig,430),width="stretch")
        st.divider(); st.header("🥧 Yearly Spending & Income"); cats=fetch_all("SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND EXTRACT(YEAR FROM transaction_date)=? GROUP BY category ORDER BY 2 DESC",[y]); p1,p2=st.columns(2)
        if inc>0: p1.plotly_chart(px.pie(names=["Wage","Other Income"],values=[wage,other],hole=.5,color_discrete_sequence=PIE_COLORS,title="Income split"),width="stretch")
        if cats: p2.plotly_chart(px.pie(names=[x[0] for x in cats],values=[float(x[1]) for x in cats],hole=.5,color_discrete_sequence=PIE_COLORS,title="Spending by category"),width="stretch")
        st.divider(); st.header("💳 Annual Spending by Category")
        if cats:
            cols=st.columns(3)
            for i,(cat,total) in enumerate(cats): cols[i%3].metric(cat,euro(total),f"{float(total)/exp*100:.1f}%" if exp else "0%")
    st.divider(); st.header("📐 30 / 20 / 50"); st.caption("Needs 30% · Wants 20% · Savings 50% of spending.")
    needs_set={"Housing","Utilities","Groceries","Healthcare","Insurance","Transport","Taxes","Debt"}; wants_set={"Dining Out","Entertainment","Shopping","Travel","Personal","Subscriptions"}; n=wnt=sv=oth=0.
    for cat,total in cats:
        if cat in needs_set:n+=float(total)
        elif cat in wants_set:wnt+=float(total)
        elif cat in {"Savings","Investing"}:sv+=float(total)
        else:oth+=float(total)
    total=n+wnt+sv+oth or 0
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Needs",euro(n),f"{n/total*100:.1f}% vs 30%" if total else "0%")
    c2.metric("Wants",euro(wnt),f"{wnt/total*100:.1f}% vs 20%" if total else "0%")
    c3.metric("Savings/Investing",euro(sv),f"{sv/total*100:.1f}% vs 50%" if total else "0%")
    c4.metric("Other",euro(oth),f"{oth/total*100:.1f}%" if total else "0%")
    if mode=="Month":
        st.divider(); st.header("🚨 Spending Anomalies")
        if get_historical_month_count()<2: st.info("Servono almeno 2 mesi distinti.")
        else:
            anomalies=get_anomalies(y,m)
            if not anomalies: st.success("Nessuna anomalia significativa.")
            for cat,current,avg,std in anomalies: st.warning(f"**{cat}** — {euro(current)} vs media {euro(avg)}.")
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
            "Brokers & funds", INV_COLORS[0])
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
    st.caption("Natural-language questions over a controlled, read-only financial context.")
    if "ai_messages" not in st.session_state:st.session_state.ai_messages=[]
    with st.sidebar:
        if get_secret("GEMINI_API_KEY"):st.success("Gemini configured")
        else:st.warning("Gemini not configured")
        if st.button("🧹 Clear Chat"):st.session_state.ai_messages=[];st.rerun()
    def context():
        ensure_net_worth_items()
        p={}
        p["weekly"]=fetch_all("""SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND transaction_date>=CURRENT_DATE-INTERVAL 7 DAY GROUP BY category ORDER BY 2 DESC""")
        p["monthly"]=fetch_all("""SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND DATE_TRUNC('month',transaction_date)=DATE_TRUNC('month',CURRENT_DATE) GROUP BY category ORDER BY 2 DESC""")
        p["recent"]=fetch_all("""SELECT transaction_date,amount,category,transaction_type,description FROM transactions ORDER BY transaction_date DESC,id DESC LIMIT 25""")
        p["cashflow"]=fetch_all("""SELECT DATE_TRUNC('month',transaction_date),SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),SUM(CASE WHEN transaction_type='Expense' THEN amount ELSE 0 END) FROM transactions GROUP BY 1 ORDER BY 1 DESC LIMIT 12""")
        p["net_worth"]=fetch_all("""
            SELECT n.snapshot_date,
              COALESCE((SELECT SUM(CASE WHEN i.kind='Asset' THEN i.amount ELSE -i.amount END) FROM net_worth_items i WHERE i.snapshot_id=n.id),
                n.cash+n.investments+n.real_estate+n.other_assets-COALESCE(n.student_loans,0)-COALESCE(n.credit_card_debt,0)-COALESCE(n.other_liabilities,0))
            FROM net_worth n ORDER BY 1 DESC LIMIT 24""")
        p["net_worth_items"]=fetch_all("""
            SELECT n.snapshot_date,i.kind,i.name,i.amount
            FROM net_worth_items i JOIN net_worth n ON n.id=i.snapshot_id
            ORDER BY n.snapshot_date DESC,i.kind,i.name LIMIT 80""")
        return json.dumps(p,default=str,ensure_ascii=False)
    for msg in st.session_state.ai_messages:
        with st.chat_message(msg["role"]):st.markdown(msg["content"])
    q=st.chat_input("Es. Where did I spend the most money this week?")
    if q:
        st.session_state.ai_messages.append({"role":"user","content":q})
        with st.chat_message("user"):st.markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("Analysing..."):ans=ask_gemini(q,context())
            st.markdown(ans)
        st.session_state.ai_messages.append({"role":"assistant","content":ans})
