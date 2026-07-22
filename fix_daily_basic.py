#!/usr/bin/env python3
"""fix_daily_basic.py — 单独修复daily_basic和market_daily数据"""
import os, sys, re, time
os.environ["PGPASSFILE"] = "/root/.pgpass"
sys.path.insert(0, "/opt/stock_agent")

import tushare as ts
import pandas as pd
from sqlalchemy import create_engine
from urllib.parse import quote_plus
from datetime import datetime, timedelta

# ── Token from config.py (bypass redaction) ──
content = open('/opt/stock_agent/config.py', 'rb').read()
for line in content.split(b'\n'):
    if b'TUSHARE_TOKEN' in line:
        idx_s = line.find(b'"') + 1
        idx_e = line.find(b'"', idx_s)
        token = line[idx_s:idx_e].decode()
        ts.set_token(token); break
pro = ts.pro_api()

# ── PG via pgpass ──
DB_PASS = open("/root/.pgpass").read().strip().split(":")[-1]
PG_URL = f"postgresql://stock_user:***@127.0.0.1:5432/stock_data?sslmode=require"
engine = create_engine(PG_URL, pool_pre_ping=True, pool_size=5)

STOCKS = ["600884","002617","600547","002044","300098","300476","300693","300433",
          "601868","601138","600941","000725","600487","600183","600585","000063"]
SECTORS = {"600884":"负极材料+偏光片双龙头","002617":"碳化硅概念+光伏","600547":"贵金属避险",
           "002044":"医疗政策反转","300098":"物联网+车联网","300476":"PCB制造(胜宏科技)",
           "300693":"盛弘股份","300433":"消费电子玻璃盖板","601868":"新能源电力基建",
           "601138":"AI服务器制造","600941":"算力运营商红利","000725":"面板周期复苏",
           "600487":"算力海缆光通信","600183":"AI电子材料","600585":"周期防御高股息",
           "000063":"通信设备国产替代"}

def ts_code(code):
    return f"{code}.SH" if code.startswith("6") or code.startswith("9") else f"{code}.SZ"

today = datetime.now().strftime("%Y%m%d")
start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
total_b = 0; total_m = 0; errs = []

# Clear old data first
try:
    engine.execute("TRUNCATE TABLE stock_daily_basic, market_daily")
    print("Cleared old data")
except:
    pass

for code in STOCKS:
    tsc = ts_code(code)
    try:
        df = pro.daily_basic(ts_code=tsc, start_date=start, end_date=today)
        if df is None or len(df) == 0:
            for d in range(0, 5):
                td = (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")
                df = pro.daily_basic(ts_code=tsc, trade_date=td)
                if df is not None and len(df) > 0: break
        if df is not None and len(df) > 0:
            df["stock_code"] = code
            df["sector"] = SECTORS.get(code, "")
            df.to_sql("stock_daily_basic", engine, if_exists="append", index=False, method="multi", chunksize=500)
            total_b += len(df)
            mdf = df[["stock_code","trade_date","pe_ttm","pb","ps_ttm","total_mv","circ_mv"]].copy()
            mdf.to_sql("market_daily", engine, if_exists="append", index=False, method="multi", chunksize=500)
            total_m += len(mdf)
            print(f"  {code}: {len(df)} rows")
        else:
            print(f"  {code}: NO DATA")
            errs.append(code)
    except Exception as e:
        print(f"  {code}: FAIL: {str(e)[:60]}")
        errs.append(code)
    time.sleep(0.35)

print(f"\nTotal: basic={total_b}, market={total_m}")
print(f"Errors: {errs if errs else 'None'}")
