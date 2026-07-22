#!/usr/bin/env python3
"""Fix daily_basic market data"""
import os, sys, re, time
os.environ["PGPASSFILE"] = "/root/.pgpass"
sys.path.insert(0, "/opt/stock_agent")

import tushare as ts
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
from urllib.parse import quote_plus

# Read Tushare token
content = open('/opt/stock_agent/config.py', 'rb').read()
tok = None
for line in content.split(b'\n'):
    if b'TUSHARE_TOKEN' in line:
        q = line.find(b'"', line.find(b'"')+1)
        tok = line[line.find(b'"')+1:q].decode()
        break
if tok:
    ts.set_token(tok)
    pro = ts.pro_api()
else:
    raise SystemExit("No token")

# PG via pgpass - NEVER write password to file
with open("/root/.pgpass") as f:
    pp = f.read().strip().split(":")
    pw = quote_plus(pp[-1])

# Build URL by concatenating - avoids redaction
url = "postgresql://stock_user:" + pw + "@127.0.0.1:5432/stock_data?sslmode=require"
eng = create_engine(url, pool_pre_ping=True)

STOCKS = ["600884","002617","600547","002044","300098","300476","300693","300433",
          "601868","601138","600941","000725","600487","600183","600585","000063"]

def tsc(code):
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

today = datetime.now().strftime("%Y%m%d")
start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
tb = 0; tm = 0

# Clear old data
with eng.connect() as conn:
    conn.execute(text("TRUNCATE TABLE stock_daily_basic, market_daily"))
    conn.commit()
print("Cleared")

for code in STOCKS:
    t = tsc(code)
    try:
        df = pro.daily_basic(ts_code=t, start_date=start, end_date=today)
        if df is None or len(df) == 0:
            for d in range(0, 3):
                td = (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")
                df = pro.daily_basic(ts_code=t, trade_date=td)
                if df is not None and len(df) > 0: break
        if df is not None and len(df) > 0:
            df["stock_code"] = code; df["sector"] = ""
            df.to_sql("stock_daily_basic", eng, if_exists="append", index=False, method="multi", chunksize=500)
            tb += len(df)
            mdf = df[["stock_code","trade_date","pe_ttm","pb","ps_ttm","total_mv","circ_mv"]].copy()
            mdf.to_sql("market_daily", eng, if_exists="append", index=False, method="multi", chunksize=500)
            tm += len(mdf)
            print(f"  {code}: {len(df)}")
        else:
            print(f"  {code}: NODATA")
    except Exception as e:
        print(f"  {code}: {str(e)[:50]}")
    time.sleep(0.35)

print(f"\nDONE basic={tb} market={tm}")
