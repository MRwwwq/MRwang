#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_import_agent.py — 标的入库采集代理
每日定时调度: 采集日线/资金流/PE/PB/ROE基本面/公告 → 写入PostgreSQL

调度: 每日收盘后17:30自动执行(run_daily.sh)
"""

import sys, os, time, json, logging, random
from datetime import datetime, timedelta

import tushare as ts
import pandas as pd
import numpy as np
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(__file__))
from config import pg_engine, TARGET_CODES, SECTOR_LABELS, TUSHARE_TOKEN, REQUEST_TIMEOUT, BATCH_SIZE, HISTORY_DAYS, SECONDARY_SOURCE

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("ImportAgent")

today = datetime.now().strftime("%Y%m%d")
start_date = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")

# ── Sina备用数据源 (akshare) ──
_SINA_ACTIVE = True  # 2026-07-16 已恢复
try:
    import akshare as _ak
    SINA_AVAILABLE = True
except ImportError:
    _ak = None
    SINA_AVAILABLE = False
    logger.warning("akshare未安装, Sina备用源不可用")

def _fetch_sina_daily(code):
    """通过AkShare Sina拉取日线作为Tushare备用"""
    if not SINA_AVAILABLE or not _SINA_ACTIVE:
        return None
    prefix = "sh" if code.startswith("6") or code.startswith("9") else "sz"
    symbol = f"{prefix}{code}"
    for attempt in range(3):
        try:
            time.sleep(random.uniform(0.3, 0.8))
            df = _ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
            if df is not None and len(df) > 0:
                df = df.rename(columns={
                    "date": "trade_date", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "vol",
                    "amount": "amount"
                })
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
                df["pct_chg"] = df["close"].pct_change() * 100
                df["ts_code"] = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                df["stock_code"] = code
                return df
        except Exception as e:
            if attempt < 2:
                time.sleep(random.uniform(1.0, 2.0))
            continue
    return None


def import_stock(code):
    """单只标的全量入库: daily + moneyflow + daily_basic"""
    tsc = f"{code}.SH" if code.startswith("6") or code.startswith("9") else f"{code}.SZ"
    results = {"code": code, "ts_code": tsc, "daily": 0, "moneyflow": 0, "basic": 0}

    # 1. 日线 (Tushare主源 → Sina备用)
    try:
        df = pro.daily(ts_code=tsc, start_date=start_date, end_date=today)
        if df is not None and len(df) > 0:
            df["stock_code"] = code
            df["sector"] = SECTOR_LABELS.get(code, "")
            df.to_sql("stock_daily", pg_engine, if_exists="append", index=False, method="multi", chunksize=500)
            results["daily"] = len(df)
            logger.info(f"  {code} daily(Tushare): {len(df)}条")
        else:
            raise ValueError("Tushare返回空数据, 切换到Sina")
    except Exception as e:
        logger.warning(f"  {code} Tushare失败: {str(e)[:60]}")
        # 备用: Sina (akshare)
        sina_df = _fetch_sina_daily(code)
        if sina_df is not None:
            sina_df["sector"] = SECTOR_LABELS.get(code, "")
            col_map = {
                "trade_date":"trade_date","open":"open","high":"high","low":"low",
                "close":"close","vol":"vol","amount":"amount","pct_chg":"pct_chg",
                "ts_code":"ts_code","stock_code":"stock_code"
            }
            save_df = sina_df[[c for c in col_map if c in sina_df.columns]].copy()
            save_df["sector"] = SECTOR_LABELS.get(code, "")
            save_df.to_sql("stock_daily", pg_engine, if_exists="append", index=False, method="multi", chunksize=500)
            results["daily"] = len(save_df)
            logger.info(f"  {code} daily(Sina备用✅): {len(save_df)}条")
        else:
            logger.error(f"  {code} Sina备用也失败, 日线缺失")

    # 2. 资金流
    try:
        df = pro.moneyflow(ts_code=tsc, start_date=start_date, end_date=today)
        if df is not None and len(df) > 0:
            df["stock_code"] = code
            df["sector"] = SECTOR_LABELS.get(code, "")
            df.to_sql("stock_money_flow", pg_engine, if_exists="append", index=False, method="multi", chunksize=500)
            results["moneyflow"] = len(df)
            logger.info(f"  {code} moneyflow: {len(df)}条")
    except Exception as e:
        logger.warning(f"  {code} moneyflow失败: {e}")

    # 3. PE/PB/ROE基本面快照
    try:
        df = pro.daily_basic(ts_code=tsc, trade_date=today)
        if df is None or len(df) == 0:
            df = pro.daily_basic(ts_code=tsc, start_date=start_date, end_date=today)
        if df is not None and len(df) > 0:
            df["stock_code"] = code
            df["sector"] = SECTOR_LABELS.get(code, "")
            df.to_sql("stock_daily_basic", pg_engine, if_exists="append", index=False, method="multi", chunksize=500)
            results["basic"] = len(df)
            logger.info(f"  {code} daily_basic: {len(df)}条")
    except Exception as e:
        logger.warning(f"  {code} daily_basic失败: {e}")

    return results


def import_all():
    """全量标的入库"""
    logger.info(f"===== 标的入库采集 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) =====")
    logger.info(f"目标标的: {len(TARGET_CODES)}只: {TARGET_CODES}")

    summary = {"total": len(TARGET_CODES), "success": 0, "failed": 0, "details": []}

    for i, code in enumerate(TARGET_CODES):
        logger.info(f"[{i+1}/{len(TARGET_CODES)}] 采集 {code} {SECTOR_LABELS.get(code,'')}")
        try:
            res = import_stock(code)
            summary["details"].append(res)
            if res["daily"] > 0:
                summary["success"] += 1
            else:
                summary["failed"] += 1
            # 节流
            if i < len(TARGET_CODES) - 1:
                time.sleep(0.5)
        except Exception as e:
            logger.error(f"  {code} 采集异常: {e}")
            summary["failed"] += 1

    logger.info(f"\n===== 入库完成 =====")
    logger.info(f"成功: {summary['success']}/{summary['total']} | 失败: {summary['failed']}")
    logger.info(f"总日线: {sum(r['daily'] for r in summary['details'])}条")
    logger.info(f"总资金流: {sum(r['moneyflow'] for r in summary['details'])}条")
    logger.info(f"总基本面: {sum(r['basic'] for r in summary['details'])}条")
    return summary


if __name__ == "__main__":
    result = import_all()
    print(json.dumps(result, ensure_ascii=False, indent=2))
