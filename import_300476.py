#!/usr/bin/env python3
"""300476 胜宏科技 — 全量入库脚本"""
import sys, os, time, json, logging
from datetime import datetime, timedelta
sys.path.insert(0, '/opt/stock_agent')

import tushare as ts
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ── PGD via pgpass ──
os.environ["PGPASSFILE"] = "/root/.pgpass"
DB_PASS = open("/root/.pgpass").read().strip().split(":")[-1]
PG_USER = "stock_user"
PG_PASS = quote_plus(DB_PASS)
PG_HOST = "127.0.0.1"; PG_PORT = "5432"; PG_DB = "stock_data"
PG_URL = f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}?sslmode=require"
engine = create_engine(PG_URL, pool_pre_ping=True, pool_size=5)

# ── Tushare ──
TUSHARE_TOKEN = "8f106090fcf57ae1d0d86f330acf03b35b95ec3df5064ea25a768860"
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# ── 标的定义 ──
STOCKS = {
    "600884": {"name": "杉杉股份", "sector": "负极材料+偏光片双龙头", "group": "新能源"},
    "002617": {"name": "露笑科技", "sector": "碳化硅概念+光伏", "group": "新能源"},
    "600547": {"name": "山东黄金", "sector": "贵金属避险", "group": "贵金属"},
    "002044": {"name": "美年健康", "sector": "医疗政策反转", "group": "医疗"},
    "300098": {"name": "高新兴", "sector": "物联网+车联网", "group": "科技成长"},
    "300476": {"name": "胜宏科技", "sector": "PCB制造", "group": "PCB制造"},
    "300693": {"name": "盛弘股份", "sector": "消费电子", "group": "消费电子"},
    "300433": {"name": "蓝思科技", "sector": "消费电子玻璃盖板", "group": "消费电子"},
    "601868": {"name": "中国能建", "sector": "新能源电力基建", "group": "新能源"},
    "601138": {"name": "工业富联", "sector": "AI服务器制造", "group": "AI科技"},
    "600941": {"name": "中国移动", "sector": "算力运营商红利", "group": "周期防御"},
    "000725": {"name": "京东方A", "sector": "面板周期复苏", "group": "消费电子"},
    "600487": {"name": "亨通光电", "sector": "算力海缆光通信", "group": "AI科技"},
    "600183": {"name": "生益科技", "sector": "AI电子材料", "group": "AI科技"},
    "600585": {"name": "海螺水泥", "sector": "周期防御高股息", "group": "周期防御"},
    "000063": {"name": "中兴通讯", "sector": "通信设备国产替代", "group": "AI科技"},
}

TAGS = {
    "中线布局": {"type": "strategy", "desc": "中期持有（3-6个月），基本面优先"},
    "短线跟踪": {"type": "strategy", "desc": "短线交易（1-4周），技术面+资金流优先"},
    "风险避雷": {"type": "risk", "desc": "高风险规避，仅观察不持仓"},
    "持仓": {"type": "position", "desc": "当前实盘持仓"},
}

STOCK_TAGS = {
    "600884": "中线布局", "002617": "短线跟踪", "600547": "中线布局",
    "002044": "短线跟踪", "300098": "短线跟踪", "300476": "中线布局",
    "300693": "中线布局", "300433": "短线跟踪", "601868": "中线布局",
    "601138": "短线跟踪", "600941": "中线布局", "000725": "短线跟踪",
    "600487": "短线跟踪", "600183": "中线布局", "600585": "中线布局",
    "000063": "短线跟踪",
}

logger = logging.getLogger("Import300476")
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

def ts_code(code):
    return f"{code}.SH" if code.startswith("6") or code.startswith("9") else f"{code}.SZ"

def step(name, fn):
    logger.info(f"\n{'='*60}\n[{name}]\n{'='*60}")
    t0 = time.time()
    try:
        ret = fn()
        logger.info(f"OK {name} done ({time.time()-t0:.1f}s)")
        return ret
    except Exception as e:
        logger.error(f"FAIL {name}: {e}")
        raise

def insert_dim_stock():
    rows = []
    for code, info in STOCKS.items():
        tsc = ts_code(code)
        exchange = "SH" if tsc.endswith(".SH") else "SZ"
        rows.append({"stock_code": code, "stock_name": info["name"],
            "sector": info["sector"], "sector_group": info["group"],
            "exchange": exchange, "is_active": True})
    pd.DataFrame(rows).to_sql("dim_stock", engine, if_exists="append", index=False, method="multi")
    return len(rows)

def insert_dim_concept():
    rows = [{"concept_name": k, "concept_type": v["type"], "description": v["desc"]} for k, v in TAGS.items()]
    pd.DataFrame(rows).to_sql("dim_concept", engine, if_exists="append", index=False, method="multi")
    return len(rows)

def insert_dim_stock_concept():
    sql = text("SELECT concept_id, concept_name FROM dim_concept")
    concepts = pd.read_sql(sql, engine).set_index("concept_name")["concept_id"].to_dict()
    rows = [{"stock_code": code, "concept_id": concepts[tag], "is_primary": True} for code, tag in STOCK_TAGS.items()]
    pd.DataFrame(rows).to_sql("dim_stock_concept", engine, if_exists="append", index=False, method="multi")
    return len(rows)

def insert_watchlist():
    pmap = {"中线布局": 3, "短线跟踪": 5, "风险避雷": 8, "持仓": 1}
    rows = [{"stock_code": code, "tag": tag, "priority": pmap.get(tag, 5),
             "remark": STOCKS[code]["name"], "is_active": True} for code, tag in STOCK_TAGS.items()]
    pd.DataFrame(rows).to_sql("watchlist", engine, if_exists="append", index=False, method="multi")
    return len(rows)

def import_stock_daily():
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    total = 0
    for code, info in STOCKS.items():
        tsc = ts_code(code)
        try:
            df = pro.daily(ts_code=tsc, start_date=start, end_date=today)
            if df is not None and len(df) > 0:
                df["stock_code"] = code
                df["sector"] = info["sector"]
                for col in ["ma5", "ma10", "ma20", "amplitude"]:
                    if col not in df.columns: df[col] = 0.0
                df.to_sql("stock_daily", engine, if_exists="append", index=False, method="multi", chunksize=500)
                total += len(df)
                logger.info(f"  {code} daily: {len(df)} rows")
        except Exception as e:
            logger.warning(f"  {code} daily fail: {e}")
        time.sleep(0.35)
    return total

def import_money_flow():
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    total = 0
    for code, info in STOCKS.items():
        tsc = ts_code(code)
        try:
            df = pro.moneyflow(ts_code=tsc, start_date=start, end_date=today)
            if df is not None and len(df) > 0:
                df["stock_code"] = code
                df["sector"] = info["sector"]
                df.to_sql("stock_money_flow", engine, if_exists="append", index=False, method="multi", chunksize=500)
                total += len(df)
                logger.info(f"  {code} moneyflow: {len(df)} rows")
        except Exception as e:
            logger.warning(f"  {code} moneyflow fail: {e}")
        time.sleep(0.35)
    return total

def import_daily_basic():
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    total_b = 0; total_m = 0
    for code, info in STOCKS.items():
        tsc = ts_code(code)
        try:
            df = pro.daily_basic(ts_code=tsc, start_date=start, end_date=today)
            if df is None or len(df) == 0:
                df = pro.daily_basic(ts_code=tsc, trade_date=today)
            if df is not None and len(df) > 0:
                df["stock_code"] = code
                df["sector"] = info["sector"]
                df.to_sql("stock_daily_basic", engine, if_exists="append", index=False, method="multi", chunksize=500)
                total_b += len(df)
                mdf = df[["stock_code", "trade_date", "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv"]].copy()
                mdf.to_sql("market_daily", engine, if_exists="append", index=False, method="multi", chunksize=500)
                total_m += len(mdf)
                logger.info(f"  {code} basic: {len(df)} rows")
        except Exception as e:
            logger.warning(f"  {code} basic fail: {e}")
        time.sleep(0.35)
    return total_b, total_m

if __name__ == "__main__":
    logger.info(f"===== Full Import Start ({datetime.now()}) =====")
    r1 = step("Step1: dim_stock", insert_dim_stock)
    r2 = step("Step2: dim_concept", insert_dim_concept)
    r3 = step("Step3: dim_stock_concept", insert_dim_stock_concept)
    r4 = step("Step4: watchlist", insert_watchlist)
    r5 = step("Step5: stock_daily", import_stock_daily)
    r6 = step("Step6: money_flow", import_money_flow)
    r7 = step("Step7: daily_basic", import_daily_basic)
    logger.info(f"\n{'='*60}\nDONE\n{'='*60}")
    logger.info(f"  dim_stock:{r1} concept:{r2} mapping:{r3} watchlist:{r4}")
    logger.info(f"  daily:{r5} money:{r6} basic:{r7[0]} market:{r7[1]}")
