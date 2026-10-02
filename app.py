from __future__ import annotations

import json
import uuid
from datetime import date, datetime

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

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
        transaction_fingerprint,
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
.block-container{max-width:1500px;padding-top:1.4rem}
div[data-testid="stMetric"]{border:1px solid rgba(100,116,139,.20);border-radius:16px;padding:18px;min-height:110px;background:#fff}
div[data-testid="stMetricValue"]{font-size:1.85rem;font-weight:750}
.big{font-size:2rem;font-weight:800}
.card{padding:16px;border-radius:14px;background:white;border:1px solid rgba(100,116,139,.18)}
.income{border-left:5px solid #16a34a;background:#f0fdf4}
.expense{border-left:5px solid #dc2626;background:#fef2f2}
.sub{font-size:.92rem;font-weight:650;margin-top:.35rem}
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

def new_line(name="", amount=0.0):
    return {"uid": uuid.uuid4().hex[:8], "name": name, "amount": float(amount or 0)}

def clear_prefix(prefix):
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefix):
            del st.session_state[key]

def edit_lines(rows, prefix):
    remove_uid=None
    for i, row in enumerate(rows):
        nk, ak=f"{prefix}n{row['uid']}", f"{prefix}a{row['uid']}"
        c1,c2,c3=st.columns([3,2,.8])
        vis="visible" if i==0 else "collapsed"
        if nk not in st.session_state: st.session_state[nk]=row["name"]
        if ak not in st.session_state: st.session_state[ak]=float(row["amount"])
        row["name"]=c1.text_input("Name", key=nk, label_visibility=vis)
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
            items.append({"kind": kind, "name": name, "amount": amount})
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

def _rollup(items):
    cash=inv=real=other=loans=cards=oliab=0.0
    for it in items:
        name=it["name"].strip().lower(); amt=float(it["amount"])
        if it["kind"]=="Asset":
            if name in {"cash","contanti","checking"}: cash+=amt
            elif any(k in name for k in ("invest","etf","broker","azioni")): inv+=amt
            elif any(k in name for k in ("real estate","immobil","house","casa")): real+=amt
            else: other+=amt
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
    execute("""
        INSERT INTO net_worth
        (snapshot_date,cash,investments,real_estate,other_assets,student_loans,credit_card_debt,other_liabilities)
        VALUES (?,?,?,?,?,?,?,?)
    """,[snapshot_date,cash,inv,real,other,loans,cards,oliab])
    sid=fetch_one("SELECT id FROM net_worth ORDER BY id DESC LIMIT 1")[0]
    _insert_items(sid, items)

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

def remove_snapshot(snapshot_id):
    ensure_net_worth_items()
    execute("DELETE FROM net_worth_items WHERE snapshot_id=?",[snapshot_id])
    execute("DELETE FROM net_worth WHERE id=?",[snapshot_id])

def get_snapshot_items(snapshot_id):
    ensure_net_worth_items()
    return fetch_all("""
        SELECT kind,name,amount FROM net_worth_items
        WHERE snapshot_id=?
        ORDER BY CASE kind WHEN 'Asset' THEN 0 ELSE 1 END, id
    """,[snapshot_id])

def get_asset_history():
    ensure_net_worth_items()
    return fetch_all("""
        SELECT n.snapshot_date,i.name,SUM(i.amount)
        FROM net_worth_items i JOIN net_worth n ON n.id=i.snapshot_id
        WHERE i.kind='Asset'
        GROUP BY n.snapshot_date,i.name
        ORDER BY n.snapshot_date,i.name
    """)

def get_net_worth_history():
    ensure_net_worth_items()
    return fetch_all("""
        SELECT n.snapshot_date,
          COALESCE((SELECT SUM(CASE WHEN i.kind='Asset' THEN i.amount ELSE -i.amount END)
                    FROM net_worth_items i WHERE i.snapshot_id=n.id),
                   n.cash+n.investments+n.real_estate+n.other_assets
                   -COALESCE(n.student_loans,0)-COALESCE(n.credit_card_debt,0)-COALESCE(n.other_liabilities,0))
        FROM net_worth n ORDER BY n.snapshot_date,n.id
    """)

def get_net_worth_records():
    ensure_net_worth_items()
    return fetch_all("""
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
    st.plotly_chart(style_chart(fig,500),use_container_width=True)
    yr=yearly_rows()
    if yr:
        yf=go.Figure()
        yf.add_trace(go.Bar(x=[str(x[0]) for x in yr],y=[float(x[1]) for x in yr],name="Income",marker_color=INCOME_COLOR))
        yf.add_trace(go.Bar(x=[str(x[0]) for x in yr],y=[float(x[2]) for x in yr],name="Expenses",marker_color=EXPENSE_COLOR))
        yf.update_layout(barmode="group",title="Overall Yearly Income vs Expenses",yaxis_title="€")
        st.plotly_chart(style_chart(yf,450),use_container_width=True)

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
            if st.form_submit_button("Save", use_container_width=True):
                fp=transaction_fingerprint(d,a,cat,typ,desc)
                if insert_transaction(d,a,cat,typ,desc,"manual",fp):
                    st.toast("Salvata")
                else:
                    st.toast("Transazione duplicata")

    with tab_import:
        st.subheader("Receipt or bank screenshot")
        st.caption("Upload a photo or a screenshot. PDF statements are no longer imported.")
        imgf=st.file_uploader("Upload receipt or bank/card screenshot", type=["png","jpg","jpeg","webp"], key="img_upload")
        if imgf:
            from PIL import Image
            im=Image.open(imgf).convert("RGB")
            st.image(im, width=550)
            if st.button("Scan image", type="primary", key="scan_img"):
                rows, raw=extract_image_transactions(im)
                st.session_state.ocr_rows=rows
                st.session_state.ocr_raw=raw
                if rows:
                    st.toast(f"{len(rows)} transaction(s) found")
                else:
                    st.warning("Nessuna transazione riconosciuta. Lo scanner usa RapidOCR e, se configurato, Gemini Vision. Puoi inserirla da Quick add.")
        if st.session_state.get("ocr_rows"):
            st.subheader("Review")
            cleaned=[]
            for i,x in enumerate(st.session_state.ocr_rows):
                c1,c2,c3,c4=st.columns(4)
                d=c1.date_input("Date", x["transaction_date"], key=f"ocr_d_{i}")
                amount=c2.number_input("Amount", min_value=0.01, value=float(x["amount"]), step=0.01, key=f"ocr_a_{i}")
                typ=c3.selectbox("Type", ["Expense","Income"], index=0 if x.get("transaction_type")=="Expense" else 1, key=f"ocr_t_{i}")
                desc=c4.text_input("Description", x.get("description","Image OCR"), key=f"ocr_x_{i}")
                cats=categories_for(typ)
                ck=f"ocr_c_{i}"
                if ck not in st.session_state:
                    st.session_state[ck]=x.get("category") if x.get("category") in cats else cats[0]
                elif st.session_state[ck] not in cats:
                    st.session_state[ck]=cats[0]
                cat=st.selectbox("Category", cats, key=ck)
                cleaned.append((d,amount,cat,typ,desc))
            if st.button("Import scanned transactions", type="primary", key="import_ocr"):
                ins=dup=0
                for d,amount,cat,typ,desc in cleaned:
                    fp=transaction_fingerprint(d,amount,cat,typ,desc)
                    if insert_transaction(d,amount,cat,typ,desc,"image_ocr",fp): ins+=1
                    else: dup+=1
                st.session_state.ocr_rows=[]
                st.toast(f"Inserite {ins}. Duplicate ignorate {dup}.")
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
            } for r in rows], use_container_width=True, hide_index=True)
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
        if w+o>0: p1.plotly_chart(px.pie(names=["Wage","Other Income"],values=[w,o],hole=.5,color_discrete_sequence=PIE_COLORS,title="Income split"),use_container_width=True)
        if cats: p2.plotly_chart(px.pie(names=[x[0] for x in cats],values=[float(x[1]) for x in cats],hole=.5,color_discrete_sequence=PIE_COLORS,title="Spending by category"),use_container_width=True)
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
        monthly=monthly_rows(y); fig=go.Figure(); fig.add_trace(go.Bar(x=[month_name(x[0]) for x in monthly],y=[float(x[1]) for x in monthly],name="Income",marker_color="#16a34a")); fig.add_trace(go.Bar(x=[month_name(x[0]) for x in monthly],y=[float(x[2]) for x in monthly],name="Expenses",marker_color="#dc2626")); fig.update_layout(barmode="group",title=f"Monthly overview — {y}",yaxis_title="€"); st.plotly_chart(style_chart(fig,430),use_container_width=True)
        st.divider(); st.header("🥧 Yearly Spending & Income"); cats=fetch_all("SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND EXTRACT(YEAR FROM transaction_date)=? GROUP BY category ORDER BY 2 DESC",[y]); p1,p2=st.columns(2)
        if inc>0: p1.plotly_chart(px.pie(names=["Wage","Other Income"],values=[wage,other],hole=.5,color_discrete_sequence=PIE_COLORS,title="Income split"),use_container_width=True)
        if cats: p2.plotly_chart(px.pie(names=[x[0] for x in cats],values=[float(x[1]) for x in cats],hole=.5,color_discrete_sequence=PIE_COLORS,title="Spending by category"),use_container_width=True)
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
    st.title("💎 Wealth Building & Strategy")
    st.header("1. Add a snapshot")
    st.caption("One line per account. Rename Cash, add another bank, or remove what you don't use. A car or a house can be added the same way later.")
    if st.session_state.pop("nw_reset_new", False):
        clear_prefix("nwnewa")
        clear_prefix("nwnewl")
        st.session_state.nw_new_assets=[new_line("Cash"), new_line("Investments")]
        st.session_state.nw_new_liab=[]
    if "nw_new_assets" not in st.session_state:
        st.session_state.nw_new_assets=[new_line("Cash"), new_line("Investments")]
    if "nw_new_liab" not in st.session_state:
        st.session_state.nw_new_liab=[]
    d=st.date_input("Snapshot date", date.today(), key="nw_new_date")
    st.subheader("Assets")
    edit_lines(st.session_state.nw_new_assets, "nwnewa")
    if st.button("Add asset", key="nw_add_asset"):
        st.session_state.nw_new_assets.append(new_line())
        st.rerun()
    st.subheader("Liabilities")
    st.caption("Leave this empty if you have none.")
    edit_lines(st.session_state.nw_new_liab, "nwnewl")
    if st.button("Add liability", key="nw_add_liab"):
        st.session_state.nw_new_liab.append(new_line())
        st.rerun()
    if st.button("Save snapshot", type="primary", key="nw_save_new"):
        assets=collect_lines(st.session_state.nw_new_assets, "Asset")
        liabs=collect_lines(st.session_state.nw_new_liab, "Liability")
        if assets is None or liabs is None:
            st.error("Give every amount a name.")
        else:
            items=merge_items(assets+liabs)
            if not items:
                st.warning("Enter at least one amount.")
            else:
                save_snapshot(d, items)
                st.session_state.nw_reset_new=True
                st.toast("Snapshot salvato")
                st.rerun()
    records=get_net_worth_records()
    if not records:
        st.info("No snapshots yet. Save the first one above and it will show up here.")
    else:
        latest=records[0]
        delta=None
        help_txt=None
        if len(records)>1:
            delta=round(float(latest[4])-float(records[1][4]), 2)
            help_txt=f"Versus {as_date(records[1][1]):%d/%m/%Y}"
        st.metric("Latest net worth", euro(latest[4]), delta=delta, help=help_txt)
        st.header("2. Growth")
        fig=go.Figure()
        dates, series=[], {}
        for dt, name, amt in get_asset_history():
            if dt not in dates: dates.append(dt)
            series.setdefault(name, {})[dt]=float(amt or 0)
        for name in sorted(series):
            fig.add_trace(go.Bar(name=name, x=dates, y=[series[name].get(dt, 0) for dt in dates]))
        hist=get_net_worth_history()
        if hist:
            fig.add_trace(go.Scatter(name="Net worth", x=[r[0] for r in hist], y=[float(r[1]) for r in hist], mode="lines+markers", line=dict(color="#0f172a", width=3)))
        fig.update_layout(barmode="stack", title="Assets and net worth", yaxis_title="€")
        st.plotly_chart(style_chart(fig, 520), use_container_width=True)
        st.dataframe([{
            "Date": as_date(r[1]).strftime("%d/%m/%Y"),
            "Assets": float(r[2] or 0),
            "Liabilities": float(r[3] or 0),
            "Net worth": float(r[4] or 0),
        } for r in records], use_container_width=True, hide_index=True)
        st.divider()
        st.header("3. Correct a snapshot")
        st.caption("Pick one snapshot. Add, rename, or remove lines, then save. This stays one form, not a long list.")
        ids=[r[0] for r in records]
        labels={r[0]: f"{as_date(r[1]):%d/%m/%Y} · {euro(r[4])}" for r in records}
        if st.session_state.get("nw_pick") not in ids:
            st.session_state.nw_pick=ids[0]
        pick=st.selectbox("Snapshot", ids, format_func=lambda i: labels[i], key="nw_pick")
        chosen=next(r for r in records if r[0]==pick)
        if st.session_state.get("nw_edit_id")!=pick:
            clear_prefix("nwedita")
            clear_prefix("nweditl")
            if "nweditd" in st.session_state:
                del st.session_state["nweditd"]
            items=get_snapshot_items(pick)
            assets=[new_line(n, a) for k,n,a in items if k=="Asset"]
            liabs=[new_line(n, a) for k,n,a in items if k=="Liability"]
            if not assets:
                assets=[new_line("Cash"), new_line("Investments")]
            st.session_state.nw_edit_assets=[x for x in assets]
            st.session_state.nw_edit_liab=[x for x in liabs]
            st.session_state.nweditd=as_date(chosen[1])
            st.session_state.nw_edit_id=pick
        nd=st.date_input("Snapshot date", key="nweditd")
        st.subheader("Assets")
        edit_lines(st.session_state.nw_edit_assets, "nwedita")
        if st.button("Add asset", key="nw_edit_add_asset"):
            st.session_state.nw_edit_assets.append(new_line())
            st.rerun()
        st.subheader("Liabilities")
        edit_lines(st.session_state.nw_edit_liab, "nweditl")
        if st.button("Add liability", key="nw_edit_add_liab"):
            st.session_state.nw_edit_liab.append(new_line())
            st.rerun()
        b1,b2=st.columns(2)
        if b1.button("Save changes", type="primary", key="nw_edit_save"):
            assets=collect_lines(st.session_state.nw_edit_assets, "Asset")
            liabs=collect_lines(st.session_state.nw_edit_liab, "Liability")
            if assets is None or liabs is None:
                st.error("Give every amount a name.")
            else:
                items=merge_items(assets+liabs)
                if not items:
                    st.warning("Enter at least one amount, or delete the snapshot.")
                else:
                    replace_snapshot(pick, nd, items)
                    st.session_state.nw_edit_id=None
                    st.toast("Snapshot aggiornato")
                    st.rerun()
        if b2.button("Delete this snapshot", key="nw_edit_delete"):
            remove_snapshot(pick)
            st.session_state.nw_edit_id=None
            st.toast("Snapshot eliminato")
            st.rerun()
    st.divider()
    st.header("4. Paycheck Router")
    existing={r[0]:float(r[1]) for r in fetch_all("SELECT account_name,percentage FROM routing_rules")}
    with st.form("routing"):
        c1,c2,c3=st.columns(3); p1=c1.number_input("Checking %",0.,100.,existing.get("Checking",50.),1.); p2=c2.number_input("Savings %",0.,100.,existing.get("Savings",30.),1.); p3=c3.number_input("Investments %",0.,100.,existing.get("Investments",20.),1.)
        if st.form_submit_button("Save rules"):
            if abs(p1+p2+p3-100)>1e-9: st.error("Devono essere esattamente 100%.")
            else:
                for name,p in [("Checking",p1),("Savings",p2),("Investments",p3)]: execute("INSERT INTO routing_rules(account_name,percentage) VALUES(?,?) ON CONFLICT(account_name) DO UPDATE SET percentage=excluded.percentage",[name,p])
                st.success("Regole salvate."); st.rerun()
    paycheck=st.number_input("Net paycheck",min_value=0.,step=100.,format="%.2f")
    if abs(p1+p2+p3-100)<1e-9:
        c1,c2,c3=st.columns(3); c1.metric("Checking",euro(paycheck*p1/100),f"{p1:.1f}%"); c2.metric("Savings",euro(paycheck*p2/100),f"{p2:.1f}%"); c3.metric("Investments",euro(paycheck*p3/100),f"{p3:.1f}%")
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
