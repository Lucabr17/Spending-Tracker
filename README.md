# Personal Finance OS V4

Streamlit + DuckDB personal finance dashboard.

Fixes in V4: DuckDB dashboard chart SQL, natural-language dates, bundled OCR + Gemini Vision fallback, scanned/text PDF statements, Month/Year Command Center, assets-only Wealth page, and true stacked asset-category columns.

Run:
```bash
pip install -r requirements.txt
streamlit run app.py
```


## Validation
Python syntax-checked for app.py, db.py and utils.py.

- Recurring expenses use safe Python date arithmetic for weekly/monthly/quarterly/yearly schedules.
- Net worth is asset-only in all displayed calculations.
- OCR uses dark-panel detection and multiple Tesseract passes.
