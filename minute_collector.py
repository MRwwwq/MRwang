#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
minute_collector.py — 分钟K线采集调度器 v1.0 (P1补齐)
================================================================
功能：
  1. 从Tushare Pro stk_mins接口采集1min/5min K线
  2. 严格限频队列：1req/min (stk_mins API限制)
  3. 增量写入stock_minute表，去重(upsert)
  4. 采集后调用compute_minute衍生计算(分时均价/VWAP/MA/累计量)
  5. 兼容收盘后全量补采、盘中增量(预留)

执行约束：
  - 本任务优先级低于P0 Sina数据源修复
  - 请求队列严格控制1req/min，防止触发302封禁
  - 仅采集交易日数据(排除周末/节假日)

依赖：
  config.py (TUSHARE_TOKEN, pg_engine, TARGET_CODES)
  psycopg2 / sqlalchemy (已有)

cron建议：
  # 盘后17:00全量补采当日分钟数据(仅交易日)
  0 17 * * 1-5 cd /opt/stock_agent && python3 minute_collector.py --mode daily 2>> logs/minute_collector.log

  # 单标的临时补采
  python3 minute_collector.py --ts_code 600547.SH --date 20260715
"""

import os, sys, time, json, logging
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
import tushare as ts
from sqlalchemy import text

# ── 导入项目配置 ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import TUSHARE_TOKEN, pg_engine, TARGET_CODES, LEGACY_CODES, NEW_CODES

# ── 日志 ──
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "minute_collector.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("MinuteCollector")

# ── 限频配置 ──
STK_MINS_INTERVAL = 62      # 秒，stk_mins 1req/min → 62s间隔(含安全缓冲)
MAX_RETRIES = 3              # 单次采集失败重试
RETRY_DELAY = 10             # 重试等待秒数

# ── Tushare Pro 初始化 ──
pro = ts.pro_api(TUSHARE_TOKEN)


def get_stock_code(ts_code: str) -> str:
    """从 ts_code 提取纯数字代码"""
    return ts_code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")


def ts_code_pool() -> list:
    """返回全量标的带后缀ts_code列表"""
    result = []
    for code in TARGET_CODES:
        if code.startswith("6"):
            result.append(f"{code}.SH")
        elif code.startswith(("0", "3", "2")):
            result.append(f"{code}.SZ")
        else:
            result.append(f"{code}.SH")  # fallback
    return result


def check_stk_mins_ready() -> bool:
    """
    前置校验：stk_mins接口是否可访问（避免惩罚期空跑）
    
    :return: True=可采集, False=接口封禁/不可用
    """
    try:
        # 用1次快速试探(注意: 即使失败也会消耗1次额度)
        test_df = pro.stk_mins(ts_code='600547.SH', freq='5min',
                               start_date='20260714', end_date='20260714')
        if test_df is not None:
            logger.info("  🔍 stk_mins接口状态: ✅ 正常可访问")
            return True
        return False
    except Exception as e:
        err = str(e)
        if "频率超限" in err:
            logger.warning(f"  🔍 stk_mins接口状态: ⏳ 惩罚期({err[:60]})")
        elif "积分" in err:
            logger.warning(f"  🔍 stk_mins接口状态: ⚠️ 积分不足({err[:60]})")
        else:
            logger.warning(f"  🔍 stk_mins接口状态: ❌ {err[:60]}")
        return False


def fetch_minute_data(ts_code: str, trade_date: str, freq: str = "1min") -> Optional[pd.DataFrame]:
    """
    采集单只股票单日分钟K线
    
    :param ts_code:   如 600547.SH
    :param trade_date: 如 20260715
    :param freq:       1min / 5min / 15min / 30min / 60min
    :return:           DataFrame 或 None(失败)
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq=freq,
                             start_date=trade_date, end_date=trade_date)
            if df is not None and len(df) > 0:
                logger.info(f"  ✅ {ts_code} {trade_date} {freq}: {len(df)} bars")
                return df
            else:
                logger.warning(f"  ⚠️ {ts_code} {trade_date} {freq}: 无数据(停牌/未交易)")
                return pd.DataFrame()  # 空DataFrame表示已处理但无数据
        except Exception as e:
            err_msg = str(e)
            if "频率超限" in err_msg:
                logger.warning(f"  ⏳ {ts_code}: 频率超限, 等待{STK_MINS_INTERVAL}s...")
                time.sleep(STK_MINS_INTERVAL)
                continue
            elif attempt < MAX_RETRIES:
                logger.warning(f"  🔄 {ts_code} 重试{attempt}/{MAX_RETRIES}: {err_msg}")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"  ❌ {ts_code} {freq} 采集失败({MAX_RETRIES}次): {err_msg}")
                return None
    return None


def upsert_minute_batch(df: pd.DataFrame, ts_code: str, trade_date: str) -> int:
    """
    批量写入分钟数据到 stock_minute (去重)
    
    :return: 写入行数
    """
    if df is None or len(df) == 0:
        return 0
    
    stock_code = get_stock_code(ts_code)
    rows = []
    
    for _, row in df.iterrows():
        trade_time = pd.Timestamp(row.get("trade_time", row.get("trade_date", trade_date)))
        rows.append({
            "ts_code": ts_code,
            "stock_code": stock_code,
            "trade_date": trade_date,
            "trade_time": trade_time,
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "vol": float(row.get("vol", 0)),
            "amount": float(row.get("amount", 0)),
        })
    
    if not rows:
        return 0
    
    # 批量upsert: 使用INSERT ON CONFLICT DO NOTHING
    insert_sql = text("""
        INSERT INTO stock_minute 
            (ts_code, stock_code, trade_date, trade_time, open, high, low, close, vol, amount)
        VALUES 
            (:ts_code, :stock_code, :trade_date, :trade_time, :open, :high, :low, :close, :vol, :amount)
        ON CONFLICT ON CONSTRAINT idx_minute_ts_code_trade_time 
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            vol = EXCLUDED.vol,
            amount = EXCLUDED.amount,
            updated_at = NOW()
    """)
    
    with pg_engine.begin() as conn:
        for row in rows:
            conn.execute(insert_sql, row)
    
    logger.info(f"  💾 {ts_code} {trade_date}: {len(rows)}条写入完成")
    return len(rows)


def compute_minute_indicators(ts_code: str, trade_date: str):
    """
    日内衍生指标计算: VWAP / 累计量 / MA5 / MA20
    按分钟时间顺序计算
    """
    sql = text("""
        SELECT trade_time, close, vol, amount
        FROM stock_minute
        WHERE ts_code = :ts_code AND trade_date = :trade_date
        ORDER BY trade_time
    """)
    
    df = pd.read_sql(sql, pg_engine, params={"ts_code": ts_code, "trade_date": trade_date})
    if df.empty:
        return
    
    # 滚动累计
    df['cumulative_vol'] = df['vol'].cumsum()
    df['cumulative_amt'] = df['amount'].cumsum()
    
    # VWAP = 累计成交额 / 累计成交量(换算为均价 元/股)
    # amount单位千元, vol单位手=100股 → 累计金额(元)/累计股数
    df['vwap'] = (df['cumulative_amt'] * 1000) / (df['cumulative_vol'] * 100)
    
    # 分时均价 = 该分钟成交额/成交量
    df['avg_price'] = df.apply(
        lambda r: (r['amount'] * 1000) / (r['vol'] * 100) if r['vol'] > 0 else r['close'],
        axis=1
    )
    
    # 分钟MA(5, 20)
    df['ma5'] = df['close'].rolling(5, min_periods=1).mean()
    df['ma20'] = df['close'].rolling(20, min_periods=1).mean()
    
    # 回写DB
    update_sql = text("""
        UPDATE stock_minute SET
            avg_price = :avg_price,
            vwap = :vwap,
            ma5 = :ma5,
            ma20 = :ma20,
            cumulative_vol = :cumulative_vol,
            cumulative_amt = :cumulative_amt,
            updated_at = NOW()
        WHERE ts_code = :ts_code AND trade_time = :trade_time
    """)
    
    with pg_engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(update_sql, {
                "ts_code": ts_code,
                "trade_time": row['trade_time'],
                "avg_price": float(row['avg_price']) if pd.notna(row['avg_price']) else None,
                "vwap": float(row['vwap']) if pd.notna(row['vwap']) else None,
                "ma5": float(row['ma5']) if pd.notna(row['ma5']) else None,
                "ma20": float(row['ma20']) if pd.notna(row['ma20']) else None,
                "cumulative_vol": int(row['cumulative_vol']) if pd.notna(row['cumulative_vol']) else None,
                "cumulative_amt": float(row['cumulative_amt']) if pd.notna(row['cumulative_amt']) else None,
            })
    
    logger.info(f"  📊 {ts_code} {trade_date}: 衍生指标计算完成({len(df)}条)")


def collect_single(ts_code: str, trade_date: str, freq: str = "1min") -> bool:
    """
    采集单只股票单日完整链路：采集→写入→计算
    
    :return: True=全链路成功
    """
    logger.info(f"── {ts_code} {trade_date} {freq} ──")
    
    # Step 1: 采集
    df = fetch_minute_data(ts_code, trade_date, freq)
    if df is None:
        # 网络/限频等致命错误
        return False
    if len(df) == 0:
        # 正常无数据(停牌)
        return True
    
    # Step 2: 写入
    written = upsert_minute_batch(df, ts_code, trade_date)
    if written == 0:
        return False
    
    # Step 3: 衍生计算
    compute_minute_indicators(ts_code, trade_date)
    
    # Step 4: 限频等待
    logger.info(f"  ⏳ 等待{STK_MINS_INTERVAL}s (限频保护)...")
    time.sleep(STK_MINS_INTERVAL)
    
    return True


def collect_daily(trade_date: Optional[str] = None, freq: str = "1min"):
    """
    盘后全量采集：遍历所有标的
    
    :param trade_date: 如 20260715，默认今日
    :param freq: 分钟级别
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")
    
    # 前置校验: stk_mins接口是否可用
    if not check_stk_mins_ready():
        logger.error(f"🚨 stk_mins接口惩罚期/不可用, 跳过本次采集")
        return {"date": trade_date, "total": 0, "success": 0, "failed": 0, "skipped": True, 
                "reason": "stk_mins_penalty"}
    
    # 检查是否为交易日
    cal = pro.trade_cal(start_date=trade_date, end_date=trade_date)
    if len(cal) > 0 and cal.iloc[0]['is_open'] == 0:
        logger.info(f"📅 {trade_date} 非交易日, 跳过采集")
        return {"date": trade_date, "total": 0, "success": 0, "failed": 0, "skipped": True}
    
    pool = ts_code_pool()
    logger.info(f"📅 盘后分钟采集: {trade_date} | {len(pool)}只标的 | {freq}")
    
    results = {"date": trade_date, "total": len(pool), "success": 0, "failed": 0, "skipped": False}
    
    for i, ts_code in enumerate(pool, 1):
        logger.info(f"[{i}/{len(pool)}] {ts_code}")
        ok = collect_single(ts_code, trade_date, freq)
        if ok:
            results["success"] += 1
        else:
            results["failed"] += 1
    
    logger.info(f"📊 采集完成: 成功{results['success']}/{results['total']}, 失败{results['failed']}")
    return results


def collect_backfill(start_date: str, end_date: str, freq: str = "1min"):
    """
    历史回填：遍历日期范围+所有标的
    
    :param start_date: 如 20260701
    :param end_date: 如 20260717
    :param freq: 分钟级别
    """
    # 获取交易日历
    cal = pro.trade_cal(start_date=start_date, end_date=end_date)
    trading_days = cal[cal['is_open'] == 1]['cal_date'].tolist()
    
    logger.info(f"📅 历史回填: {start_date}~{end_date} | {len(trading_days)}个交易日")
    
    total_all = {"success": 0, "failed": 0, "total": 0}
    
    for trade_date in trading_days:
        logger.info(f"\n{'='*50}")
        logger.info(f"📅 回填日期: {trade_date}")
        result = collect_daily(trade_date, freq)
        total_all["success"] += result.get("success", 0)
        total_all["failed"] += result.get("failed", 0)
        total_all["total"] += result.get("total", 0)
    
    logger.info(f"\n{'='*50}")
    logger.info(f"📊 回填完成: {total_all['success']}/{total_all['total']} 成功, 失败{total_all['failed']}")
    return total_all


def get_intraday_amplitude(ts_code: str, trade_date: str) -> dict:
    """
    从分钟表计算日内实时振幅
    
    :return: {high, low, amplitude_pct, high_time, low_time, current_vs_vwap}
    """
    sql = text("""
        SELECT trade_time, high, low, close, vwap
        FROM stock_minute
        WHERE ts_code = :ts_code AND trade_date = :trade_date
        ORDER BY trade_time
    """)
    df = pd.read_sql(sql, pg_engine, params={"ts_code": ts_code, "trade_date": trade_date})
    
    if df.empty:
        return {}
    
    high = df['high'].max()
    low = df['low'].min()
    latest = df.iloc[-1]
    
    return {
        "high": float(high),
        "low": float(low),
        "amplitude_pct": round((high - low) / low * 100, 2) if low > 0 else 0,
        "high_time": str(df.loc[df['high'].idxmax(), 'trade_time']),
        "low_time": str(df.loc[df['low'].idxmin(), 'trade_time']),
        "close": float(latest['close']),
        "vwap": float(latest['vwap']) if pd.notna(latest.get('vwap')) else None,
        "close_vs_vwap_pct": round((latest['close'] - latest['vwap']) / latest['vwap'] * 100, 2)
        if pd.notna(latest.get('vwap')) and latest['vwap'] > 0 else None,
    }


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="分钟K线采集调度器 v1.0")
    parser.add_argument("--mode", choices=["daily", "backfill", "single"], 
                        default="daily", help="采集模式")
    parser.add_argument("--ts_code", type=str, default=None,
                        help="单只股票代码(single模式)")
    parser.add_argument("--date", type=str, default=None,
                        help="采集日期(如20260715, 默认今日)")
    parser.add_argument("--start", type=str, default=None,
                        help="回填开始日期(backfill模式)")
    parser.add_argument("--end", type=str, default=None,
                        help="回填结束日期(backfill模式)")
    parser.add_argument("--freq", type=str, default="1min",
                        choices=["1min", "5min", "15min", "30min", "60min"],
                        help="分钟级别(默认1min)")
    parser.add_argument("--amplitude", type=str, default=None,
                        help="查询日内振幅: ts_code:date (如 600547.SH:20260715)")
    
    args = parser.parse_args()
    
    # 振幅查询模式
    if args.amplitude:
        parts = args.amplitude.split(":")
        ts = parts[0]
        dt = parts[1] if len(parts) > 1 else datetime.now().strftime("%Y%m%d")
        result = get_intraday_amplitude(ts, dt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)
    
    if args.mode == "daily":
        result = collect_daily(args.date, args.freq)
    elif args.mode == "backfill":
        start = args.start or "20260701"
        end = args.end or datetime.now().strftime("%Y%m%d")
        result = collect_backfill(start, end, args.freq)
    elif args.mode == "single":
        if not args.ts_code:
            print("❌ single模式需要 --ts_code")
            sys.exit(1)
        date_str = args.date or datetime.now().strftime("%Y%m%d")
        ok = collect_single(args.ts_code, date_str, args.freq)
        result = {"ok": ok}
    
    print(json.dumps(result, ensure_ascii=False, default=str))
