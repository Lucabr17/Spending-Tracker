from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import (
    delete_recurring_expense, delete_transaction, execute, fetch_all, fetch_one,
    get_category_spending, get_current_net_worth, get_db, get_historical_month_count,
    get_monthly_summary, get_net_worth_history, get_anomalies,
    insert_net_worth_snapshot, insert_transaction, process_due_recurring,
    update_transaction,
)
from utils import (
    CATEGORIES, ask_gemini, auto_categorize, extract_image_transactions,
    get_gemini_client, get_secret, parse_amount, parse_date,
    parse_natural_language, transaction_fingerprint,
)

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
</style>
""",unsafe_allow_html=True)

def euro(v): return f"€ {float(v):,.2f}"

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

def parse_pdf_statement(data):
    try: import fitz
    except ImportError: raise RuntimeError('Installa PyMuPDF: pip install pymupdf')
    doc=fitz.open(stream=data,filetype='pdf'); text='\n'.join(p.get_text('text') for p in doc); rows=[]
    if text.strip():
        date_re=re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b")
        amount_re=re.compile(r"(?<!\d)([-+]?(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:[,.]\d{2}))(?!\d)")
        current_date=None
        for line in [' '.join(x.split()) for x in text.splitlines() if x.strip()]:
            low=line.lower()
            if any(x in low for x in ['saldo iniziale','saldo finale','opening balance','closing balance']): continue
            dm=date_re.search(line)
            if dm: current_date=parse_date(dm.group())
            amounts=amount_re.findall(line)
            if current_date and amounts:
                raw=amounts[-1].replace(' ','')
                if ',' in raw and '.' in raw: raw=raw.replace('.','').replace(',','.') if raw.rfind(',')>raw.rfind('.') else raw.replace(',','')
                elif ',' in raw: raw=raw.replace(',','.')
                try: amount=float(raw)
                except ValueError: continue
                typ='Income' if any(x in low for x in ['accredito','entrata','credit','versamento','stipendio','salary']) else 'Expense'
                desc=amount_re.sub('',date_re.sub('',line,count=1)).strip(' -|') or 'Bank statement transaction'
                cat=auto_categorize(desc)
                if typ=='Income' and cat!='Wage': cat='Other Income'
                rows.append({'transaction_date':current_date,'amount':abs(amount),'category':cat,'transaction_type':typ,'description':desc})
    if rows: return rows
    scanned=[]
    from PIL import Image
    for page in doc:
        pix=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
        image=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
        page_rows,_=extract_image_transactions(image); scanned.extend(page_rows)
    if scanned: return scanned
    raise RuntimeError('Non ho trovato transazioni nel PDF. Per un PDF scansionato, configura GEMINI_API_KEY oppure installa rapidocr_onnxruntime.')

with st.sidebar:
    st.title("💰 Finance OS")
    page=st.radio("Navigation",["🏠 Dashboard","📝 Data Entry","📊 Command Center","💎 Wealth & Strategy","🤖 AI Insights"])
    st.divider()
    st.caption(f"DuckDB · {date.today():%d/%m/%Y}")

# =====================================================================
# DASHBOARD
# =====================================================================
if page=="🏠 Dashboard":
    st.title("🏠 Financial Dashboard")
    t=date.today(); s=get_monthly_summary(t.year,t.month); nw=get_current_net_worth()
    st.subheader("Current balances")
    c1,c2=st.columns(2)
    c1.markdown(f'<div class="card income"><b>💵 MONEY IN — THIS MONTH</b><div class="big">{euro(s["income"])}</div></div>',unsafe_allow_html=True)
    c2.markdown(f'<div class="card expense"><b>💳 MONEY OUT — THIS MONTH</b><div class="big">{euro(s["expenses"])}</div></div>',unsafe_allow_html=True)
    st.write("")
    c1,c2,c3=st.columns(3)
    c1.metric("Net Cash Flow",euro(s["cash_flow"]))
    c2.metric("Savings Rate",f"{s['cash_flow']/s['income']*100:.1f}%" if s["income"] else "0.0%")
    c3.metric("Net Worth",euro(nw) if nw is not None else "—")
    st.divider()
    st.header("📈 Income vs Expenses")
    y=st.selectbox("Select year",years(),key="dash_y")
    r=monthly_rows(y)
    fig=go.Figure()
    fig.add_trace(go.Bar(x=[month_name(x[0]) for x in r],y=[float(x[1]) for x in r],name="Income"))
    fig.add_trace(go.Bar(x=[month_name(x[0]) for x in r],y=[float(x[2]) for x in r],name="Expenses"))
    fig.update_layout(barmode="group",title=f"Monthly Income vs Expenses — {y}",yaxis_title="€",height=500)
    st.plotly_chart(fig,use_container_width=True)
    yr=yearly_rows()
    if yr:
        yf=go.Figure()
        yf.add_trace(go.Bar(x=[str(x[0]) for x in yr],y=[float(x[1]) for x in yr],name="Income"))
        yf.add_trace(go.Bar(x=[str(x[0]) for x in yr],y=[float(x[2]) for x in yr],name="Expenses"))
        yf.update_layout(barmode="group",title="Overall Yearly Income vs Expenses",yaxis_title="€",height=450)
        st.plotly_chart(yf,use_container_width=True)

# =====================================================================
# DATA ENTRY
# =====================================================================
elif page=="📝 Data Entry":
    st.title("📝 Data Entry & Automation")
    st.header("1. Standard Entry")
    with st.form("manual",clear_on_submit=True):
        c1,c2,c3=st.columns(3)
        d=c1.date_input("Date",date.today()); a=c2.number_input("Amount",min_value=.01,step=.01)
        typ=c3.selectbox("Type",["Expense","Income"])
        c1,c2=st.columns(2)
        cat=c1.selectbox("Category",CATEGORIES); desc=c2.text_input("Description")
        if st.form_submit_button("💾 Save",use_container_width=True):
            fp=transaction_fingerprint(d,a,cat,typ,desc)
            ok=insert_transaction(d,a,cat,typ,desc,"manual",fp)
            st.success("Salvata.") if ok else st.warning("Transazione duplicata.")
    st.divider()
    st.header("2. Natural Language")
    nl=st.text_input("Describe the transaction",placeholder="Received €2,000 salary today")
    if nl:
        try:
            p=parse_natural_language(nl)
            c1,c2,c3,c4=st.columns(4)
            c1.metric("Amount",euro(p["amount"]));c2.metric("Type",p["transaction_type"])
            c3.metric("Category",p["category"]);c4.metric("Date",p["transaction_date"].strftime("%d/%m/%Y"))
            if st.button("💾 Save parsed",key="nl_save"):
                fp=transaction_fingerprint(p["transaction_date"],p["amount"],p["category"],p["transaction_type"],p["description"])
                st.success("Salvata.") if insert_transaction(p["transaction_date"],p["amount"],p["category"],p["transaction_type"],p["description"],"natural_language",fp) else st.warning("Duplicata.")
        except ValueError as e:st.warning(str(e))
    st.divider()
    st.header("3. 🏦 Bank Statement PDF")
    pdf=st.file_uploader("Upload bank statement PDF",type=["pdf"])
    if pdf and st.button("🔍 Read PDF",type="primary"):
        try:st.session_state.pdf_rows=parse_pdf_statement(pdf.getvalue())
        except Exception as e:st.error(str(e))
    if "pdf_rows" in st.session_state:
        rows=st.session_state.pdf_rows
        st.success(f"{len(rows)} transazioni riconosciute. Controllale prima dell'import.")
        st.dataframe(rows,use_container_width=True)
        if st.button("🚀 Import PDF transactions"):
            ins=dup=0
            for x in rows:
                fp=transaction_fingerprint(x["transaction_date"],x["amount"],x["category"],x["transaction_type"],x["description"])
                if insert_transaction(x["transaction_date"],x["amount"],x["category"],x["transaction_type"],x["description"],"pdf",fp):ins+=1
                else:dup+=1
            st.success(f"Inserite {ins}; duplicate ignorate {dup}.")
            del st.session_state.pdf_rows
    st.divider()
    st.header("4. 🧾 Receipt / Bank Screenshot Scanner")
    imgf=st.file_uploader("Upload receipt or bank/card screenshot",type=["png","jpg","jpeg","webp"],key="img_upload")
    if imgf:
        from PIL import Image
        im=Image.open(imgf).convert("RGB"); st.image(im,width=550)
        if st.button("🔍 Scan image",type="primary",key="scan_img"):
            rows,raw=extract_image_transactions(im); st.session_state.ocr_rows=rows; st.session_state.ocr_raw=raw
            if rows: st.success(f"{len(rows)} transaction(s) recognized. Check dates and amounts before saving.")
            else: st.warning("Nessuna transazione riconosciuta. Lo scanner usa RapidOCR e, se configurato, Gemini Vision. Puoi inserirla manualmente.")
    if st.session_state.get("ocr_rows"):
        st.subheader("Review scanned transactions"); cleaned=[]
        for i,x in enumerate(st.session_state.ocr_rows):
            c1,c2,c3,c4=st.columns(4)
            d=c1.date_input("Date",x["transaction_date"],key=f"ocr_d_{i}"); amount=c2.number_input("Amount",min_value=.01,value=float(x["amount"]),step=.01,key=f"ocr_a_{i}")
            typ=c3.selectbox("Type",["Expense","Income"],index=0 if x.get("transaction_type")=="Expense" else 1,key=f"ocr_t_{i}"); desc=c4.text_input("Description",x.get("description","Image OCR"),key=f"ocr_x_{i}")
            cat=st.selectbox("Category",CATEGORIES,index=CATEGORIES.index(x["category"]) if x.get("category") in CATEGORIES else len(CATEGORIES)-1,key=f"ocr_c_{i}"); cleaned.append((d,amount,cat,typ,desc))
        if st.button("🚀 Import scanned transactions",key="import_ocr"):
            ins=dup=0
            for d,amount,cat,typ,desc in cleaned:
                fp=transaction_fingerprint(d,amount,cat,typ,desc)
                if insert_transaction(d,amount,cat,typ,desc,"image_ocr",fp): ins+=1
                else: dup+=1
            st.success(f"Inserite {ins}; duplicate ignorate {dup}."); st.session_state.ocr_rows=[]
    st.divider()
    st.header("5. 🔄 Recurring Expenses")
    if st.button("🔄 Process due recurring expenses"):
        n=process_due_recurring(date.today());st.success(f"{n} transazioni create.");st.rerun()
    t1,t2=st.tabs(["➕ Add","⚙️ Manage"])
    with t1:
        with st.form("rec"):
            c1,c2=st.columns(2);name=c1.text_input("Name");ra=c1.number_input("Amount",min_value=.01,step=.01)
            rc=c1.selectbox("Category",CATEGORIES,key="rc");freq=c2.selectbox("Frequency",["Weekly","Monthly","Quarterly","Yearly"]);nd=c2.date_input("Next due",date.today())
            if st.form_submit_button("Add"):
                execute("INSERT INTO recurring_expenses(name,amount,category,frequency,next_due_date) VALUES(?,?,?,?,?)",[name,ra,rc,freq,nd]);st.success("Aggiunta.")
    with t2:
        for rid,name,a,cat,freq,nd,active in fetch_all("SELECT id,name,amount,category,frequency,next_due_date,active FROM recurring_expenses ORDER BY next_due_date"):
            c1,c2,c3,c4,c5=st.columns([2,1,1.4,1.4,.5]);c1.write(name);c2.write(euro(a));c3.write(cat);c4.write(f"{freq} · {nd}")
            if c5.button("🗑️",key=f"rd{rid}"):delete_recurring_expense(rid);st.rerun()
    st.divider()
    st.header("6. 🔎 Transaction Manager")
    q=st.text_input("Search")
    rows=fetch_all("""SELECT id,transaction_date,amount,category,transaction_type,description,source FROM transactions
                      WHERE ?='' OR lower(coalesce(description,'')) LIKE '%'||lower(?)||'%' OR lower(category) LIKE '%'||lower(?)||'%'
                      ORDER BY transaction_date DESC,id DESC LIMIT 250""",[q,q,q])
    for rid,td,a,cat,typ,desc,src in rows:
        with st.expander(f"{td} · {euro(a)} · {cat} · {typ}"):
            c1,c2,c3=st.columns(3);nd=c1.date_input("Date",td,key=f"d{rid}");na=c2.number_input("Amount",min_value=0.,value=float(a),key=f"a{rid}")
            nc=c3.selectbox("Category",CATEGORIES,index=CATEGORIES.index(cat) if cat in CATEGORIES else len(CATEGORIES)-1,key=f"c{rid}")
            nt=st.selectbox("Type",["Expense","Income"],index=0 if typ=="Expense" else 1,key=f"t{rid}");nx=st.text_input("Description",desc or "",key=f"x{rid}")
            b1,b2=st.columns(2)
            if b1.button("Save",key=f"s{rid}"):update_transaction(rid,nd,na,nc,nt,nx);st.success("Aggiornata.")
            if b2.button("Delete",key=f"del{rid}"):delete_transaction(rid);st.rerun()

# =====================================================================
# COMMAND CENTER
# =====================================================================
elif page=="📊 Command Center":
    st.title("📊 Command Center")
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
        if w+o>0: p1.plotly_chart(px.pie(names=["Wage","Other Income"],values=[w,o],hole=.5,title="Income split"),use_container_width=True)
        if cats: p2.plotly_chart(px.pie(names=[x[0] for x in cats],values=[float(x[1]) for x in cats],hole=.5,title="Spending by category"),use_container_width=True)
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
        monthly=monthly_rows(y); fig=go.Figure(); fig.add_trace(go.Bar(x=[month_name(x[0]) for x in monthly],y=[float(x[1]) for x in monthly],name="Income")); fig.add_trace(go.Bar(x=[month_name(x[0]) for x in monthly],y=[float(x[2]) for x in monthly],name="Expenses")); fig.update_layout(barmode="group",title=f"Monthly overview — {y}",yaxis_title="€",height=430); st.plotly_chart(fig,use_container_width=True)
        st.divider(); st.header("🥧 Yearly Spending & Income"); cats=fetch_all("SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND EXTRACT(YEAR FROM transaction_date)=? GROUP BY category ORDER BY 2 DESC",[y]); p1,p2=st.columns(2)
        if inc>0: p1.plotly_chart(px.pie(names=["Wage","Other Income"],values=[wage,other],hole=.5,title="Income split"),use_container_width=True)
        if cats: p2.plotly_chart(px.pie(names=[x[0] for x in cats],values=[float(x[1]) for x in cats],hole=.5,title="Spending by category"),use_container_width=True)
        st.divider(); st.header("💳 Annual Spending by Category")
        if cats:
            cols=st.columns(3)
            for i,(cat,total) in enumerate(cats): cols[i%3].metric(cat,euro(total),f"{float(total)/exp*100:.1f}%" if exp else "0%")
    st.divider(); st.header("🎯 Budget Progress")
    if mode=="Month":
        for cat,limit,spent in fetch_all("""SELECT b.category,b.monthly_limit,COALESCE(SUM(CASE WHEN t.transaction_type='Expense' THEN t.amount ELSE 0 END),0) FROM budgets b LEFT JOIN transactions t ON t.category=b.category AND EXTRACT(YEAR FROM t.transaction_date)=? AND EXTRACT(MONTH FROM t.transaction_date)=? GROUP BY b.category,b.monthly_limit ORDER BY b.category""",[y,m]):
            ratio=float(spent)/float(limit) if limit else 0; c1,c2=st.columns([5,1]); c1.write(f"**{cat}** — {euro(spent)} / {euro(limit)}"); c1.progress(min(max(ratio,0),1)); c2.write("🔴" if ratio>=1 else "🟡" if ratio>=.8 else "🟢")
        st.divider(); st.header("📐 50 / 30 / 20"); needs_set={"Housing","Utilities","Groceries","Healthcare","Insurance","Transport","Taxes","Debt"}; wants_set={"Dining Out","Entertainment","Shopping","Travel","Personal","Subscriptions"}; n=wnt=sv=oth=0.
        for cat,total in cats:
            if cat in needs_set:n+=float(total)
            elif cat in wants_set:wnt+=float(total)
            elif cat in {"Savings","Investing"}:sv+=float(total)
            else:oth+=float(total)
        total=n+wnt+sv+oth; c1,c2,c3,c4=st.columns(4); c1.metric("Needs",euro(n),f"{n/total*100:.1f}% vs 50%" if total else "0%"); c2.metric("Wants",euro(wnt),f"{wnt/total*100:.1f}% vs 30%" if total else "0%"); c3.metric("Savings/Investing",euro(sv),f"{sv/total*100:.1f}% vs 20%" if total else "0%"); c4.metric("Other",euro(oth))
        st.divider(); st.header("🚨 Spending Anomalies")
        if get_historical_month_count()<2: st.info("Servono almeno 2 mesi distinti.")
        else:
            anomalies=get_anomalies(y,m)
            if not anomalies: st.success("Nessuna anomalia significativa.")
            for cat,current,avg,std in anomalies: st.warning(f"**{cat}** — {euro(current)} vs media {euro(avg)}.")
    else: st.caption("Budgets are monthly. Switch to Month view to inspect budget progress.")
# =====================================================================
# WEALTH
# =====================================================================
elif page=="💎 Wealth & Strategy":
    st.title("💎 Wealth Building & Strategy")
    with st.form("nw",clear_on_submit=True):
        d=st.date_input("Snapshot date",date.today()); st.subheader("Assets"); c1,c2,c3,c4=st.columns(4)
        cash=c1.number_input("Cash",min_value=0.,step=100.); inv=c2.number_input("Investments",min_value=0.,step=100.); real=c3.number_input("Real Estate",min_value=0.,step=1000.); other=c4.number_input("Other Assets",min_value=0.,step=100.)
        if st.form_submit_button("💾 Save Snapshot",use_container_width=True): insert_net_worth_snapshot(d,cash,inv,real,other,0,0,0); st.success("Snapshot salvato.")
    h=get_net_worth_history()
    if h:
        st.metric("Latest Net Worth",euro(h[-1][1])); st.header("📊 Net Worth Growth — Stacked Asset Columns")
        rows=fetch_all("SELECT snapshot_date,cash,investments,real_estate,other_assets FROM net_worth ORDER BY snapshot_date"); fig=go.Figure()
        for idx,label in enumerate(["Cash","Investments","Real Estate","Other Assets"],1): fig.add_trace(go.Bar(x=[r[0] for r in rows],y=[float(r[idx]) for r in rows],name=label))
        fig.update_layout(barmode="stack",title="Net Worth Growth by Asset Category",yaxis_title="€",height=520,legend_title="Asset category"); st.plotly_chart(fig,use_container_width=True)
    st.divider(); st.header("3. Paycheck Router"); existing={r[0]:float(r[1]) for r in fetch_all("SELECT account_name,percentage FROM routing_rules")}
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
        p={}
        p["weekly"]=fetch_all("""SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND transaction_date>=CURRENT_DATE-INTERVAL 7 DAY GROUP BY category ORDER BY 2 DESC""")
        p["monthly"]=fetch_all("""SELECT category,SUM(amount) FROM transactions WHERE transaction_type='Expense' AND DATE_TRUNC('month',transaction_date)=DATE_TRUNC('month',CURRENT_DATE) GROUP BY category ORDER BY 2 DESC""")
        p["recent"]=fetch_all("""SELECT transaction_date,amount,category,transaction_type,description FROM transactions ORDER BY transaction_date DESC,id DESC LIMIT 25""")
        p["cashflow"]=fetch_all("""SELECT DATE_TRUNC('month',transaction_date),SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),SUM(CASE WHEN transaction_type='Expense' THEN amount ELSE 0 END) FROM transactions GROUP BY 1 ORDER BY 1 DESC LIMIT 12""")
        p["net_worth"]=fetch_all("""SELECT snapshot_date,cash+investments+real_estate+other_assets FROM net_worth ORDER BY snapshot_date DESC LIMIT 24""")
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
