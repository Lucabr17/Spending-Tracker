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
INCOME_CATEGORIES=["Wage","Other Income"]
EXPENSE_CATEGORIES=[c for c in CATEGORIES if c not in {"Wage","Other Income"}]

def categories_for(transaction_type):
    return INCOME_CATEGORIES if transaction_type=="Income" else EXPENSE_CATEGORIES

KEYWORD_CATEGORIES={
"uber eats":"Dining Out","just eat":"Dining Out","deliveroo":"Dining Out","glovo":"Dining Out",
"starbucks":"Dining Out","mcdonald":"Dining Out","burger king":"Dining Out",
"ristorante":"Dining Out","pizzeria":"Dining Out","trattoria":"Dining Out","osteria":"Dining Out",
"restaurant":"Dining Out","caffè":"Dining Out","caffe":"Dining Out","caffé":"Dining Out",
"colazione":"Dining Out","pranzo":"Dining Out","cena":"Dining Out","aperitivo":"Dining Out",
"gelato":"Dining Out","sushi":"Dining Out","pizza":"Dining Out","bar":"Dining Out",
"amazon prime":"Subscriptions","prime video":"Subscriptions","disney+":"Subscriptions","disney plus":"Subscriptions",
"netflix":"Subscriptions","spotify":"Subscriptions","dazn":"Subscriptions","chatgpt":"Subscriptions",
"icloud":"Subscriptions","youtube premium":"Subscriptions","abbonamento":"Subscriptions",
"apple music":"Subscriptions","google one":"Subscriptions",
"trenitalia":"Transport","italo":"Transport","telepass":"Transport","autostrada":"Transport",
"parcheggio":"Transport","benzina":"Transport","gasolio":"Transport","carburante":"Transport",
"uber":"Transport","lyft":"Transport","taxi":"Transport","shell":"Transport","esso":"Transport",
"eni":"Transport","q8":"Transport","tamoil":"Transport","fuel":"Transport","enjoy":"Transport",
"treno":"Transport","metro":"Transport","autobus":"Transport","atm":"Transport",
"esselunga":"Groceries","carrefour":"Groceries","conad":"Groceries","lidl":"Groceries","aldi":"Groceries",
"eurospin":"Groceries","coop":"Groceries","pam":"Groceries","iper":"Groceries","unes":"Groceries",
"supermercato":"Groceries","spesa":"Groceries","groceries":"Groceries","grocery":"Groceries","market":"Groceries",
"amazon":"Shopping","ikea":"Shopping","zara":"Shopping","decathlon":"Shopping","zalando":"Shopping",
"shein":"Shopping","mediaworld":"Shopping","unieuro":"Shopping","shopping":"Shopping",
"airbnb":"Travel","booking":"Travel","ryanair":"Travel","easyjet":"Travel","wizz":"Travel",
"hotel":"Travel","ostello":"Travel","volo":"Travel","vacanza":"Travel",
"eni gas":"Utilities","enel":"Utilities","a2a":"Utilities","hera":"Utilities","iren":"Utilities",
"bolletta":"Utilities","electricity":"Utilities","internet":"Utilities","fastweb":"Utilities",
"vodafone":"Utilities","windtre":"Utilities","iliad":"Utilities","tim":"Utilities","wifi":"Utilities",
"luce":"Utilities","acqua":"Utilities","gas":"Utilities",
"farmacia":"Healthcare","pharmacy":"Healthcare","dentista":"Healthcare","ospedale":"Healthcare",
"medico":"Healthcare","doctor":"Healthcare","medical":"Healthcare","visita":"Healthcare",
"assicurazione":"Insurance","polizza":"Insurance","insurance":"Insurance",
"affitto":"Housing","condominio":"Housing","mutuo":"Housing","rent":"Housing","mortgage":"Housing",
"università":"Education","universita":"Education","retta":"Education","udemy":"Education","corso":"Education",
"tasse":"Taxes","f24":"Taxes","inps":"Taxes","imu":"Taxes","irpef":"Taxes","agenzia entrate":"Taxes",
"finanziamento":"Debt","prestito":"Debt",
"risparmi":"Savings","salvadanaio":"Savings",
"trade republic":"Investing","degiro":"Investing","scalable":"Investing","investimenti":"Investing",
"investimento":"Investing","azioni":"Investing","etf":"Investing",
"busta paga":"Wage","cedolino":"Wage","stipendio":"Wage","salary":"Wage","payroll":"Wage","wage":"Wage",
"rimborso":"Other Income","dividendi":"Other Income","dividendo":"Other Income","cashback":"Other Income",
"interessi":"Other Income",
"cinema":"Entertainment","teatro":"Entertainment","theatre":"Entertainment","concerto":"Entertainment",
"steam":"Entertainment","playstation":"Entertainment",
"parrucchiere":"Personal","barbiere":"Personal","palestra":"Personal","gym":"Personal",
}

def _has_keyword(text, keyword):
    k=(keyword or "").lower().strip()
    if not k: return False
    return re.search(rf"(?<![a-z0-9àèéìòù]){re.escape(k)}(?![a-z0-9àèéìòù])", (text or "").lower()) is not None

def auto_categorize(text, transaction_type=None):
    t=(text or "").lower()
    pool=categories_for(transaction_type) if transaction_type else CATEGORIES
    named, named_len=None, -1
    for c in sorted(pool, key=len, reverse=True):
        if _has_keyword(t, c.lower()) and len(c)>named_len:
            named, named_len=c, len(c)
    best, best_len=None, -1
    for k, cat in KEYWORD_CATEGORIES.items():
        if _has_keyword(t, k) and len(k)>best_len:
            best, best_len=cat, len(k)
    if named and (best is None or named_len>=best_len):
        choice=named
    else:
        choice=best or ("Other Income" if transaction_type=="Income" else "Other")
    if transaction_type=="Income" and choice not in INCOME_CATEGORIES:
        choice="Other Income"
    if transaction_type=="Expense" and choice not in EXPENSE_CATEGORIES:
        choice="Other"
    return choice

def parse_amount(raw):
    if raw is None: return None
    v=re.sub(r"[^\d,.\-]","",str(raw).strip())
    if not v or v in {"-",".",",","-.","-,"}: return None
    if "," in v and "." in v:
        v=v.replace(".","").replace(",",".") if v.rfind(",")>v.rfind(".") else v.replace(",","")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", v):
        v=v.replace(".","")
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+", v):
        v=v.replace(",","")
    elif "," in v:
        v=v.replace(",",".")
    try: return float(v)
    except ValueError: return None

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

def _strip_dates(text):
    s=text or ""
    s=re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"," ",s)
    s=re.sub(r"\b\d{4}[./-]\d{1,2}[./-]\d{1,2}\b"," ",s)
    names="|".join(sorted({
        "january","jan","gennaio","gen","february","feb","febbraio","march","mar","marzo",
        "april","apr","aprile","may","maggio","june","jun","giugno","giu","july","jul","luglio","lug",
        "august","aug","agosto","ago","september","sep","sept","settembre","set","october","oct","ottobre","ott",
        "november","nov","novembre","december","dec","dicembre","dic",
    }, key=len, reverse=True))
    s=re.sub(rf"\b\d{{1,2}}\s+(?:di\s+)?(?:{names})(?:\s+\d{{4}})?\b"," ",s,flags=re.I)
    s=re.sub(rf"\b(?:{names})\s+\d{{1,2}}(?:,?\s+\d{{4}})?\b"," ",s,flags=re.I)
    s=re.sub(r"\b20\d{2}\b"," ",s)
    s=re.sub(r"\b(today|oggi|yesterday|ieri)\b"," ",s,flags=re.I)
    return s

def extract_amount(text):
    s=_strip_dates(text)
    patterns=[
        r"(?:€|eur)\s*(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?|\d+)",
        r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?|\d+)\s*(?:€|eur)",
        r"(?<!\d)(\d{1,3}(?:\.\d{3})+(?:,\d{2})?)(?!\d)",
        r"(?<!\d)(\d+,\d{2})(?!\d)",
        r"(?<!\d)(\d+\.\d{2})(?!\d)",
        r"(?<!\d)(\d+)(?!\d)",
    ]
    for pat in patterns:
        found=re.findall(pat, s, flags=re.I)
        for token in reversed(found):
            amount=parse_amount(token)
            if amount not in (None, 0):
                return abs(amount)
    return None

def parse_natural_language(text):
    s=(text or "").strip()
    if not s: raise ValueError("Inserisci una descrizione.")
    amount=extract_amount(s)
    if amount is None: raise ValueError("Non trovo un importo.")
    low=s.lower()
    income_words=["busta paga","cedolino","stipendio","salary","payroll","wage","rimborso","dividendo","dividendi","cashback","accredito","interessi"]
    tx="Income" if any(_has_keyword(low, w) for w in income_words) else "Expense"
    category=auto_categorize(s, tx)
    d=extract_explicit_date(s) or date.today()
    return {"transaction_date":d,"amount":abs(amount),"category":category,"transaction_type":tx,"description":s}

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
        raw_cat=(row.get(cc) or "").strip() if cc else ""
        typ=infer_type(a,(row.get(tc) or "").strip() if tc else "")
        cat=raw_cat if raw_cat in categories_for(typ) else auto_categorize(f"{raw_cat} {desc}", typ)
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
        cat=auto_categorize(desc, typ)
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
                cat=auto_categorize(desc, typ)
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
