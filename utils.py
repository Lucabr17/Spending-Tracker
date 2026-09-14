from __future__ import annotations
import csv, hashlib, io, os, re, json
from datetime import date, datetime, timedelta
from typing import Optional
import streamlit as st
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract=None

CATEGORIES=["Housing","Utilities","Groceries","Dining Out","Transport","Travel",
"Healthcare","Insurance","Entertainment","Shopping","Education","Subscriptions",
"Personal","Taxes","Debt","Savings","Investing","Wage","Other Income","Other"]

KEYWORD_CATEGORIES={
"uber eats":"Dining Out","deliveroo":"Dining Out","just eat":"Dining Out",
"starbucks":"Dining Out","mcdonald":"Dining Out","restaurant":"Dining Out","ristorante":"Dining Out",
"uber":"Transport","lyft":"Transport","taxi":"Transport","shell":"Transport","esso":"Transport",
"eni":"Transport","q8":"Transport","tamoil":"Transport","fuel":"Transport","benzina":"Transport",
"netflix":"Subscriptions","spotify":"Subscriptions","amazon prime":"Subscriptions",
"cinema":"Entertainment","theatre":"Entertainment","teatro":"Entertainment",
"amazon":"Shopping","ikea":"Shopping","zara":"Shopping","decathlon":"Shopping",
"airbnb":"Travel","booking":"Travel","hotel":"Travel","ryanair":"Travel","easyjet":"Travel",
"enel":"Utilities","a2a":"Utilities","gas":"Utilities","electricity":"Utilities","internet":"Utilities",
"tim":"Utilities","vodafone":"Utilities","fastweb":"Utilities",
"esselunga":"Groceries","carrefour":"Groceries","conad":"Groceries","lidl":"Groceries","aldi":"Groceries",
"pharmacy":"Healthcare","farmacia":"Healthcare","doctor":"Healthcare","medical":"Healthcare",
"rent":"Housing","affitto":"Housing","mortgage":"Housing","mutuo":"Housing",
"insurance":"Insurance","assicurazione":"Insurance",
"salary":"Wage","stipendio":"Wage","payroll":"Wage","wage":"Wage",
}

def auto_categorize(text):
    t=(text or "").lower()
    for k in sorted(KEYWORD_CATEGORIES,key=len,reverse=True):
        if k in t:return KEYWORD_CATEGORIES[k]
    return "Other"

def parse_amount(raw):
    if not raw:return None
    v=re.sub(r"[^\d,.\-]","",str(raw).strip())
    if not v:return None
    if "," in v and "." in v:
        v=v.replace(".","").replace(",",".") if v.rfind(",")>v.rfind(".") else v.replace(",","")
    elif "," in v:v=v.replace(",",".")
    try:return float(v)
    except ValueError:return None

def parse_date(value):
    if value is None: return None
    value=str(value).strip()
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%m/%d/%Y","%Y/%m/%d","%d.%m.%Y","%d %B %Y","%d %b %Y","%B %d %Y","%b %d %Y","%d %B","%d %b","%B %d","%b %d"):
        try:
            d=datetime.strptime(value,fmt).date()
            return d if "%Y" in fmt else d.replace(year=date.today().year)
        except ValueError: pass
    try: return datetime.fromisoformat(value).date()
    except ValueError: return None

def extract_explicit_date(text):
    s=(text or '').lower()
    months={"january":1,"jan":1,"gennaio":1,"gen":1,"february":2,"feb":2,"febbraio":2,"march":3,"mar":3,"marzo":3,"april":4,"apr":4,"aprile":4,"may":5,"maggio":5,"june":6,"jun":6,"giugno":6,"giu":6,"july":7,"jul":7,"luglio":7,"lug":7,"august":8,"aug":8,"agosto":8,"ago":8,"september":9,"sep":9,"sept":9,"settembre":9,"set":9,"october":10,"oct":10,"ottobre":10,"ott":10,"november":11,"nov":11,"novembre":11,"december":12,"dec":12,"dicembre":12,"dic":12}
    for pat in (r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b',r'\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b',r'\b(\d{1,2})[/-](\d{1,2})\b'):
        m=re.search(pat,s)
        if m:
            try:
                if len(m.group(1))==4: return date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
                y=int(m.group(3)) if len(m.groups())==3 else date.today().year
                if y<100: y+=2000
                return date(y,int(m.group(2)),int(m.group(1)))
            except ValueError: pass
    names='|'.join(sorted(months,key=len,reverse=True))
    for pat in (rf'\b(\d{{1,2}})\s+(?:di\s+)?({names})(?:\s+(\d{{4}}))?\b',rf'\b({names})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b'):
        m=re.search(pat,s)
        if m:
            try:
                if m.group(1).isdigit(): day,key=int(m.group(1)),m.group(2)
                else: key,day=m.group(1),int(m.group(2))
                return date(int(m.group(3)) if m.group(3) else date.today().year,months[key],day)
            except (ValueError,KeyError): pass
    if re.search(r'\b(today|oggi)\b',s): return date.today()
    if re.search(r'\b(yesterday|ieri)\b',s): return date.today()-timedelta(days=1)
    return None

def parse_natural_language(text):
    s=text.strip()
    if not s: raise ValueError('Inserisci una descrizione.')
    m=re.search(r'(?:[$€£]\s*)?(-?\d+(?:[.,]\d{1,2})?)',s)
    amount=parse_amount(m.group(1)) if m else None
    if amount is None: raise ValueError('Non trovo un importo.')
    low=s.lower()
    tx='Income' if any(w in low for w in ['salary','income','stipendio','received','ricevuto','accredito','payroll']) else 'Expense'
    d=extract_explicit_date(s) or date.today()
    category=auto_categorize(s)
    if tx=='Income' and category!='Wage': category='Other Income'
    return {'transaction_date':d,'amount':abs(amount),'category':category,'transaction_type':tx,'description':s}

def infer_type(amount,raw_type):
    t=(raw_type or "").lower()
    if any(w in t for w in ["income","credit","deposit","entrata","accredito"]):return "Income"
    if any(w in t for w in ["expense","debit","payment","uscita","spesa"]):return "Expense"
    return "Income" if amount<0 else "Expense"

def transaction_fingerprint(transaction_date,amount,category,transaction_type,description):
    raw="|".join([str(transaction_date),f"{float(amount):.2f}",category.strip().lower(),
                  transaction_type,(description or "").strip().lower()])
    return hashlib.sha256(raw.encode()).hexdigest()

def parse_csv_transactions(file_bytes):
    text=file_bytes.decode("utf-8-sig",errors="replace")
    try:dialect=csv.Sniffer().sniff(text[:5000],delimiters=",;\t")
    except csv.Error:dialect=csv.excel
    reader=csv.DictReader(io.StringIO(text),dialect=dialect)
    headers={h.strip().lower():h for h in (reader.fieldnames or []) if h}
    def find(names):
        for n in names:
            if n in headers:return headers[n]
        for n,h in headers.items():
            if any(x in n for x in names):return h
        return None
    dc=find(["date","transaction_date","data"])
    ac=find(["amount","value","importo"])
    xc=find(["description","merchant","memo","descrizione","details","payee"])
    cc=find(["category","categoria"])
    tc=find(["type","transaction_type","tipo"])
    if not dc or not ac:raise ValueError("Il file deve contenere data e importo.")
    out=[]
    for row in reader:
        d=parse_date((row.get(dc) or "").strip()); a=parse_amount(row.get(ac) or "")
        if not d or a is None:continue
        desc=(row.get(xc) or "").strip() if xc else ""
        cat=(row.get(cc) or "").strip() if cc else ""
        cat=cat if cat in CATEGORIES else auto_categorize(f"{cat} {desc}")
        typ=infer_type(a,(row.get(tc) or "").strip() if tc else "")
        if typ=="Income" and cat not in {"Wage","Other Income"}:cat="Other Income"
        out.append({"transaction_date":d,"amount":abs(a),"category":cat,
                    "transaction_type":typ,"description":desc})
    return out

def _ocr_text(image):
    # RapidOCR is the first choice because it does not require a system binary.
    try:
        from rapidocr_onnxruntime import RapidOCR
        import numpy as np
        result,_=RapidOCR()(np.array(image.convert('RGB')))
        if result:
            text='\n'.join(str(x[1]) for x in result if len(x)>=2)
            if any(ch.isdigit() for ch in text):
                return text
    except Exception:
        pass
    if pytesseract is not None:
        try:
            # Financial screenshots often have white text on a dark card.
            # Upscale + grayscale/threshold makes amounts much easier to read.
            img=image.convert('L')
            w,h=img.size
            # Detect a dark bank-app/card panel and crop away the surrounding UI.
            # This dramatically improves OCR of white amounts on black backgrounds.
            try:
                import numpy as np
                arr=np.array(img)
                mask=arr < 55
                ys,xs=np.where(mask)
                if len(xs) > 0.20*w*h:
                    x0,x1=int(xs.min()),int(xs.max())
                    y0,y1=int(ys.min()),int(ys.max())
                    if (x1-x0) > 0.40*w and (y1-y0) > 0.40*h:
                        img=img.crop((max(0,x0-5),max(0,y0-5),min(w,x1+6),min(h,y1+6)))
            except Exception:
                pass
            w,h=img.size
            if w < 1200:
                img=img.resize((w*2,h*2))
            candidates=[]
            for psm in (6,11,12):
                txt=pytesseract.image_to_string(img,config=f'--psm {psm}')
                if txt: candidates.append(txt)
            # Prefer the OCR pass that contains the most currency-like amounts.
            amount_re=re.compile(r'(?<!\d)(?:[-+€$£]\s*)?\d{1,6}[.,]\d{2}(?!\d)')
            return max(candidates,key=lambda t:len(amount_re.findall(t))) if candidates else ''
        except Exception:
            pass
    return ''

def _parse_ocr_transactions(text):
    lines=[re.sub(r'\s+',' ',x).strip() for x in (text or '').splitlines() if x.strip()]
    out=[]; current_date=None; pending_desc=None
    amount_re=re.compile(r'(?<!\d)(?:[-+€$£]\s*)?(\d{1,6}(?:[.,]\d{2}))(?!\d)')
    for i,line in enumerate(lines):
        low=line.lower()
        if any(k in low for k in ['reverted','reversed','cancelled','canceled','stornato','revocato']):
            pending_desc=None
            continue
        d=extract_explicit_date(line)
        if d:
            current_date=d
            pending_desc=None
        # Ignore time-only OCR lines.
        if re.fullmatch(r'[\W_]*(?:\d{1,2})[:.]\d{2}[\W_]*',line):
            continue
        amounts=amount_re.findall(line)
        desc=amount_re.sub('',line).strip(' -|·')
        if not amounts:
            if current_date and desc and not extract_explicit_date(line):
                pending_desc=desc
            continue
        amount=parse_amount(amounts[-1])
        if amount is None or not current_date:
            continue
        # If this line is just an amount, use the nearest merchant description.
        if not desc or re.fullmatch(r'[\W_]*',desc):
            desc=pending_desc or 'Bank/card transaction'
        # A row followed immediately by a REVERTED marker should not be imported.
        following=' '.join(lines[i+1:i+3]).lower()
        if any(k in following for k in ['reverted','reversed','cancelled','canceled','stornato','revocato']):
            pending_desc=None
            continue
        pending_desc=None
        typ='Income' if any(k in low for k in ['credit','deposit','accredito','entrata','versamento','stipendio','salary']) else 'Expense'
        cat=auto_categorize(desc)
        if typ=='Income' and cat!='Wage': cat='Other Income'
        out.append({'transaction_date':current_date,'amount':abs(amount),'category':cat,
                    'transaction_type':typ,'description':desc})
    return out

def _gemini_image_transactions(image):
    client=get_gemini_client()
    if not client: return []
    model=get_secret('GEMINI_MODEL') or 'gemini-3.6-flash'
    prompt="Read this receipt or bank/card screenshot and return ONLY JSON: {\"transactions\":[{\"date\":\"YYYY-MM-DD\",\"amount\":12.34,\"description\":\"merchant\",\"type\":\"Expense\"}]} Extract every visible transaction. Use the date shown in the image, never today. Ignore rows marked REVERTED, REVERSED, CANCELLED or STORNATO. Do not invent missing values."
    try:
        response=client.models.generate_content(model=model,contents=[prompt,image])
        raw=re.sub(r'^```(?:json)?\s*|\s*```$','',(response.text or '').strip(),flags=re.I|re.S)
        data=json.loads(raw); data=data.get('transactions',[]) if isinstance(data,dict) else data
        out=[]
        for row in data if isinstance(data,list) else []:
            d=parse_date(row.get('date')); amount=parse_amount(row.get('amount')); desc=str(row.get('description') or '').strip()
            if d and amount is not None and desc:
                typ='Income' if str(row.get('type','Expense')).lower().startswith('inc') else 'Expense'
                cat=auto_categorize(desc)
                if typ=='Income' and cat!='Wage': cat='Other Income'
                out.append({'transaction_date':d,'amount':abs(amount),'category':cat,'transaction_type':typ,'description':desc})
        return out
    except Exception: return []

def extract_image_transactions(image):
    text=_ocr_text(image); rows=_parse_ocr_transactions(text)
    if not rows:
        vision=_gemini_image_transactions(image)
        if vision: return vision,text
    return rows,text

def extract_receipt_data(image):
    rows,raw_text=extract_image_transactions(image)
    if not rows: raise RuntimeError('Nessuna transazione riconosciuta.')
    first=rows[0]
    return {'raw_text':raw_text,'amount':first['amount'],'date':first['transaction_date'],'category':first['category'],'description':first['description'],'transaction_type':first['transaction_type'],'transactions':rows}

def get_secret(name):
    value=os.getenv(name)
    if value:return value
    try:return st.secrets.get(name)
    except Exception:return None

def get_gemini_client():
    key=get_secret("GEMINI_API_KEY")
    if not key:return None
    from google import genai
    return genai.Client(api_key=key)

def ask_gemini(question,context):
    client=get_gemini_client()
    if not client:return "Gemini non è configurato. Imposta GEMINI_API_KEY."
    model=get_secret("GEMINI_MODEL") or "gemini-3.6-flash"
    prompt=f"""You are a personal finance assistant. Use ONLY this read-only financial context.
Do not invent data. Be concise and practical.
CONTEXT:
{context}
QUESTION:
{question}"""
    try:
        r=client.models.generate_content(model=model,contents=prompt)
        return r.text or "Nessuna risposta."
    except Exception as e:return f"Errore Gemini: {e}"

def authenticate_user():
    password=get_secret("APP_PASSWORD")
    if not password:return True
    if st.session_state.get("authenticated"):
        with st.sidebar:
            if st.button("Log out"):st.session_state.authenticated=False;st.rerun()
        return True
    st.title("🔐 Personal Finance OS")
    entered=st.text_input("Password",type="password")
    if st.button("Sign in",type="primary"):
        if entered==password:st.session_state.authenticated=True;st.rerun()
        else:st.error("Password non valida.")
    return False
