from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable
from datetime import date
import calendar

import duckdb
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
@st.cache_resource
def get_db() -> duckdb.DuckDBPyConnection:
    md_token = st.secrets["MOTHERDUCK_TOKEN"]
    con = duckdb.connect(f"md:my_db?motherduck_token={md_token}")
    con.execute("CREATE SEQUENCE IF NOT EXISTS transactions_id_seq START 1")
    con.execute("CREATE SEQUENCE IF NOT EXISTS net_worth_id_seq START 1")
    con.execute("CREATE SEQUENCE IF NOT EXISTS recurring_expenses_id_seq START 1")

    con.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id BIGINT PRIMARY KEY DEFAULT nextval('transactions_id_seq'),
            transaction_date DATE NOT NULL,
            amount DECIMAL(18,2) NOT NULL CHECK(amount >= 0),
            category VARCHAR NOT NULL,
            transaction_type VARCHAR NOT NULL CHECK(transaction_type IN ('Income','Expense')),
            description VARCHAR,
            source VARCHAR DEFAULT 'manual',
            fingerprint VARCHAR UNIQUE,
            recurring_id BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS net_worth (
            id BIGINT PRIMARY KEY DEFAULT nextval('net_worth_id_seq'),
            snapshot_date DATE NOT NULL,
            cash DECIMAL(18,2) DEFAULT 0,
            investments DECIMAL(18,2) DEFAULT 0,
            real_estate DECIMAL(18,2) DEFAULT 0,
            other_assets DECIMAL(18,2) DEFAULT 0,
            student_loans DECIMAL(18,2) DEFAULT 0,
            credit_card_debt DECIMAL(18,2) DEFAULT 0,
            other_liabilities DECIMAL(18,2) DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS recurring_expenses (
            id BIGINT PRIMARY KEY DEFAULT nextval('recurring_expenses_id_seq'),
            name VARCHAR NOT NULL,
            amount DECIMAL(18,2) NOT NULL CHECK(amount > 0),
            category VARCHAR NOT NULL,
            frequency VARCHAR NOT NULL CHECK(frequency IN ('Weekly','Monthly','Quarterly','Yearly')),
            next_due_date DATE NOT NULL,
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            category VARCHAR PRIMARY KEY,
            monthly_limit DECIMAL(18,2) NOT NULL CHECK(monthly_limit >= 0)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS routing_rules (
            account_name VARCHAR PRIMARY KEY,
            percentage DECIMAL(7,4) NOT NULL CHECK(percentage >= 0 AND percentage <= 100)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category)")
    _ensure_net_worth_items(con)
    return con

def _ensure_net_worth_items(con):
    con.execute("CREATE SEQUENCE IF NOT EXISTS net_worth_items_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS net_worth_items (
            id BIGINT PRIMARY KEY DEFAULT nextval('net_worth_items_id_seq'),
            snapshot_id BIGINT NOT NULL,
            kind VARCHAR NOT NULL CHECK(kind IN ('Asset','Liability')),
            name VARCHAR NOT NULL,
            amount DECIMAL(18,2) NOT NULL CHECK(amount >= 0)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_nw_items_snapshot ON net_worth_items(snapshot_id)")
    rows = con.execute("""
        SELECT id, cash, investments, real_estate, other_assets,
               student_loans, credit_card_debt, other_liabilities
        FROM net_worth n
        WHERE NOT EXISTS (SELECT 1 FROM net_worth_items i WHERE i.snapshot_id = n.id)
    """).fetchall()
    mapping = [
        ("Asset", "Cash", 1),
        ("Asset", "Investments", 2),
        ("Asset", "Real Estate", 3),
        ("Asset", "Other Assets", 4),
        ("Liability", "Student loans", 5),
        ("Liability", "Credit card debt", 6),
        ("Liability", "Other liabilities", 7),
    ]
    for row in rows:
        inserted = 0
        for kind, name, idx in mapping:
            amount = float(row[idx] or 0)
            if amount > 0:
                con.execute(
                    "INSERT INTO net_worth_items(snapshot_id, kind, name, amount) VALUES (?,?,?,?)",
                    [row[0], kind, name, amount],
                )
                inserted += 1
        if inserted == 0:
            con.execute(
                "INSERT INTO net_worth_items(snapshot_id, kind, name, amount) VALUES (?,?,?,?)",
                [row[0], "Asset", "Cash", 0],
            )

def execute(sql: str, params: Iterable[Any] | None = None):
    return get_db().execute(sql, list(params) if params is not None else None)

def fetch_all(sql: str, params: Iterable[Any] | None = None) -> list[tuple]:
    return execute(sql, params).fetchall()

def fetch_one(sql: str, params: Iterable[Any] | None = None) -> tuple | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None

def insert_transaction(transaction_date, amount, category, transaction_type,
                       description, source="manual", fingerprint=None,
                       recurring_id=None) -> bool:
    try:
        execute("""
            INSERT INTO transactions
            (transaction_date,amount,category,transaction_type,description,
             source,fingerprint,recurring_id)
            VALUES (?,?,?,?,?,?,?,?)
        """, [transaction_date,amount,category,transaction_type,description,
              source,fingerprint,recurring_id])
        return True
    except duckdb.ConstraintException:
        return False

def update_transaction(transaction_id, transaction_date, amount, category,
                       transaction_type, description):
    execute("""
        UPDATE transactions
        SET transaction_date=?,amount=?,category=?,transaction_type=?,
            description=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, [transaction_date,amount,category,transaction_type,description,transaction_id])

def delete_transaction(transaction_id):
    execute("DELETE FROM transactions WHERE id=?", [transaction_id])

def delete_recurring_expense(recurring_id):
    execute("DELETE FROM recurring_expenses WHERE id=?", [recurring_id])

def get_monthly_summary(year, month):
    row = fetch_one("""
        SELECT
          COALESCE(SUM(CASE WHEN transaction_type='Income' THEN amount ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN transaction_type='Expense' THEN amount ELSE 0 END),0)
        FROM transactions
        WHERE EXTRACT(YEAR FROM transaction_date)=?
          AND EXTRACT(MONTH FROM transaction_date)=?
    """, [year,month])
    income, expenses = float(row[0]), float(row[1])
    return {"income":income,"expenses":expenses,"cash_flow":income-expenses}

def get_current_net_worth():
    row = fetch_one(f"""
        SELECT {_NET_WORTH_SQL}
        FROM net_worth
        ORDER BY snapshot_date DESC, id DESC
        LIMIT 1
    """)
    return float(row[0]) if row and row[0] is not None else None

def get_category_spending(year,month):
    return fetch_all("""
        SELECT category,SUM(amount)
        FROM transactions
        WHERE transaction_type='Expense'
          AND EXTRACT(YEAR FROM transaction_date)=?
          AND EXTRACT(MONTH FROM transaction_date)=?
        GROUP BY category ORDER BY 2 DESC
    """,[year,month])

def get_historical_month_count():
    row=fetch_one("""SELECT COUNT(DISTINCT DATE_TRUNC('month',transaction_date))
                     FROM transactions""")
    return int(row[0] or 0)

def get_anomalies(year,month):
    if get_historical_month_count()<2:
        return []
    return fetch_all("""
        WITH monthly_category AS (
            SELECT DATE_TRUNC('month',transaction_date) month,category,SUM(amount) spending
            FROM transactions WHERE transaction_type='Expense' GROUP BY 1,2
        ),
        historical AS (
            SELECT category,AVG(spending) avg_spend,STDDEV_POP(spending) std_spend
            FROM monthly_category
            WHERE month<DATE_TRUNC('month',MAKE_DATE(?, ?, 1))
            GROUP BY category
        ),
        current_month AS (
            SELECT category,SUM(amount) current_spend
            FROM transactions
            WHERE transaction_type='Expense'
              AND EXTRACT(YEAR FROM transaction_date)=?
              AND EXTRACT(MONTH FROM transaction_date)=?
            GROUP BY category
        )
        SELECT c.category,c.current_spend,h.avg_spend,COALESCE(h.std_spend,0)
        FROM current_month c JOIN historical h USING(category)
        WHERE c.current_spend >
              h.avg_spend+GREATEST(COALESCE(h.std_spend,0)*2,h.avg_spend*.50)
        ORDER BY c.current_spend DESC
    """,[year,month,year,month])

def get_net_worth_history():
    return fetch_all(f"""
        SELECT snapshot_date, {_NET_WORTH_SQL}
        FROM net_worth
        ORDER BY snapshot_date, id
    """)

def _rollup_net_worth(items):
    cash = inv = real = other = loans = cards = oliab = 0.0
    for it in items:
        name = it["name"].strip().lower()
        amt = float(it["amount"])
        if it["kind"] == "Asset":
            if name in {"cash", "contanti", "checking"}:
                cash += amt
            elif any(k in name for k in ("invest", "etf", "broker", "azioni")):
                inv += amt
            elif any(k in name for k in ("real estate", "immobil", "house", "casa")):
                real += amt
            else:
                other += amt
        else:
            if "student" in name or "studio" in name:
                loans += amt
            elif "credit" in name or "carta" in name:
                cards += amt
            else:
                oliab += amt
    return cash, inv, real, other, loans, cards, oliab

def _insert_items(snapshot_id, items):
    for it in items:
        name = it["name"].strip()
        if not name:
            continue
        execute(
            "INSERT INTO net_worth_items(snapshot_id, kind, name, amount) VALUES (?,?,?,?)",
            [snapshot_id, it["kind"], name, float(it["amount"])],
        )

def insert_net_worth_snapshot(snapshot_date, items):
    cash, inv, real, other, loans, cards, oliab = _rollup_net_worth(items)
    row = execute("""
        INSERT INTO net_worth
        (snapshot_date,cash,investments,real_estate,other_assets,
         student_loans,credit_card_debt,other_liabilities)
        VALUES (?,?,?,?,?,?,?,?)
        RETURNING id
    """, [snapshot_date, cash, inv, real, other, loans, cards, oliab]).fetchone()
    _insert_items(row[0], items)
    return row[0]

def get_snapshot_items(snapshot_id):
    return fetch_all("""
        SELECT kind, name, amount
        FROM net_worth_items
        WHERE snapshot_id=?
        ORDER BY CASE kind WHEN 'Asset' THEN 0 ELSE 1 END, id
    """, [snapshot_id])

def get_asset_history():
    return fetch_all("""
        SELECT n.snapshot_date, i.name, SUM(i.amount)
        FROM net_worth_items i
        JOIN net_worth n ON n.id = i.snapshot_id
        WHERE i.kind='Asset'
        GROUP BY n.snapshot_date, i.name
        ORDER BY n.snapshot_date, i.name
    """)

_NET_WORTH_SQL = """
COALESCE(
    (SELECT SUM(CASE WHEN i.kind='Asset' THEN i.amount ELSE -i.amount END)
     FROM net_worth_items i WHERE i.snapshot_id = net_worth.id),
    net_worth.cash + net_worth.investments + net_worth.real_estate + net_worth.other_assets
      - COALESCE(net_worth.student_loans,0)
      - COALESCE(net_worth.credit_card_debt,0)
      - COALESCE(net_worth.other_liabilities,0)
)
"""

def process_due_recurring(as_of_date):
    due=fetch_all("""
        SELECT id,name,amount,category,frequency,next_due_date
        FROM recurring_expenses
        WHERE active=TRUE AND next_due_date<=?
        ORDER BY next_due_date,id
    """,[as_of_date])
    created=0
    def add_months(d,months):
        total=d.year*12+(d.month-1)+months
        y=total//12; m=total%12+1
        return date(y,m,min(d.day,calendar.monthrange(y,m)[1]))
    for rid,name,amount,category,frequency,due_date in due:
        while due_date<=as_of_date:
            fp=f"recurring:{rid}:{due_date}"
            if insert_transaction(due_date,float(amount),category,"Expense",
                                   f"{name} (recurring)","recurring",fp,rid):
                created+=1
            if frequency=="Weekly":
                due_date=due_date.fromordinal(due_date.toordinal()+7)
            elif frequency=="Monthly":
                due_date=add_months(due_date,1)
            elif frequency=="Quarterly":
                due_date=add_months(due_date,3)
            else:
                due_date=add_months(due_date,12)
        execute("UPDATE recurring_expenses SET next_due_date=? WHERE id=?",[due_date,rid])
    return created

def get_net_worth_records() -> list[tuple]:
    """Fetch snapshots newest first: id, date, assets, liabilities, net worth."""
    return fetch_all(f"""
        SELECT id, snapshot_date,
               COALESCE(
                 (SELECT SUM(amount) FROM net_worth_items i
                  WHERE i.snapshot_id=net_worth.id AND i.kind='Asset'),
                 cash+investments+real_estate+other_assets
               ),
               COALESCE(
                 (SELECT SUM(amount) FROM net_worth_items i
                  WHERE i.snapshot_id=net_worth.id AND i.kind='Liability'),
                 COALESCE(student_loans,0)+COALESCE(credit_card_debt,0)+COALESCE(other_liabilities,0)
               ),
               {_NET_WORTH_SQL}
        FROM net_worth
        ORDER BY snapshot_date DESC, id DESC
    """)

def update_net_worth_snapshot(snapshot_id, snapshot_date, items):
    """Replace a snapshot date and its asset/liability lines."""
    cash, inv, real, other, loans, cards, oliab = _rollup_net_worth(items)
    execute("""
        UPDATE net_worth
        SET snapshot_date=?, cash=?, investments=?, real_estate=?, other_assets=?,
            student_loans=?, credit_card_debt=?, other_liabilities=?
        WHERE id=?
    """, [snapshot_date, cash, inv, real, other, loans, cards, oliab, snapshot_id])
    execute("DELETE FROM net_worth_items WHERE snapshot_id=?", [snapshot_id])
    _insert_items(snapshot_id, items)

def delete_net_worth_snapshot(snapshot_id):
    """Delete a net worth entry and its lines."""
    execute("DELETE FROM net_worth_items WHERE snapshot_id=?", [snapshot_id])
    execute("DELETE FROM net_worth WHERE id=?", [snapshot_id])
