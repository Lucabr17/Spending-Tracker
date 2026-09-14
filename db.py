from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable
from datetime import date
import calendar

import duckdb
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("FINANCE_DB_PATH", str(BASE_DIR / "finance.duckdb"))

@st.cache_resource
def get_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(DB_PATH)
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
    return con

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
    row = fetch_one("""
        SELECT cash+investments+real_estate+other_assets
        FROM net_worth ORDER BY snapshot_date DESC,id DESC LIMIT 1
    """)
    return float(row[0]) if row else None

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
    return fetch_all("""
        SELECT snapshot_date,
               cash+investments+real_estate+other_assets AS net_worth
        FROM net_worth ORDER BY snapshot_date
    """)

def insert_net_worth_snapshot(snapshot_date,cash,investments,real_estate,
                              other_assets,student_loans,credit_card_debt,
                              other_liabilities):
    execute("""
        INSERT INTO net_worth
        (snapshot_date,cash,investments,real_estate,other_assets,
         student_loans,credit_card_debt,other_liabilities)
        VALUES (?,?,?,?,?,?,?,?)
    """,[snapshot_date,cash,investments,real_estate,other_assets,
         student_loans,credit_card_debt,other_liabilities])

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
