#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_review.py — 当日复盘模块
消费 trade_calibration 表格人工校准数据 → 输出复盘分析

三组统计:
  1. 预判误差统计(按error_tag分组)
  2. 风控有效性统计(风控判断有效 vs 入场条件失效)
  3. 入场条件成功率统计

依赖: trade_calibration 表必须有当日数据
"""
import sys
import os
import json
import logging
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DailyReview")

try:
    from config import TARGET_CODES, SECTOR_GROUPS, pg_engine
except ImportError:
    TARGET_CODES = []
    SECTOR_GROUPS = {}
    pg_engine = None

# ── 赛道因子权重默认值 ──
DEFAULT_WEIGHTS = {
    "valuation": 0.25, "momentum": 0.20, "flow": 0.25,
    "fundamental": 0.15, "sentiment": 0.15,
}

SECTOR_DEFAULT_WEIGHTS = {sector: dict(DEFAULT_WEIGHTS) for sector in SECTOR_GROUPS}


def load_calibration(trade_date):
    """加载当日全部人工校准数据"""
    if not pg_engine:
        logger.error("PG未连接")
        return []
    try:
        import pandas as pd
        from sqlalchemy import text
        sql = text(f"""
            SELECT ticker, ticker_name, real_close, real_change_pct,
                   support_resistance_result, real_trade_action, error_tag,
                   yesterday_ai_prediction
            FROM trade_calibration
            WHERE trade_date = :d
            ORDER BY ticker
        """)
        df = pd.read_sql(sql, pg_engine, params={"d": trade_date})
        if df.empty:
            logger.warning(f"{trade_date} 无人工校准记录")
            return []
        records = []
        for _, r in df.iterrows():
            ai = r.get("yesterday_ai_prediction")
            if ai and isinstance(ai, str):
                try:
                    ai = json.loads(ai)
                except json.JSONDecodeError:
                    ai = None
            records.append({
                "ticker": r["ticker"], "name": r["ticker_name"],
                "close": float(r["real_close"]) if r["real_close"] else None,
                "change": float(r["real_change_pct"]) if r["real_change_pct"] else None,
                "sr": r["support_resistance_result"],
                "action": r["real_trade_action"],
                "error_tag": r["error_tag"],
                "ai_pred": ai or {},
            })
        return records
    except Exception as e:
        logger.error(f"加载校准数据异常: {e}")
        return []


def run_review(trade_date):
    """执行当日复盘→返回完整复盘报告"""
    records = load_calibration(trade_date)

    print(f"\n{'='*70}")
    print(f"📋 当日复盘报告: {trade_date}")
    print(f"{'='*70}")

    if not records:
        print("\n⚠️ 无人工校准数据,无法生成复盘分析")
        print("   复盘无法区分预判对错,无误差分类,无风控效果统计")
        print("   仅输出纯行情摘要:\n")
        # 无校准数据时-仅输出行情摘要
        try:
            import pandas as pd
            from sqlalchemy import text
            sql = text("""
                SELECT ts_code, close, pct_chg FROM stock_daily
                WHERE trade_date = :d ORDER BY ts_code
            """)
            df = pd.read_sql(sql, pg_engine, params={"d": trade_date})
            if not df.empty:
                for _, r in df.iterrows():
                    print(f"  {r['ts_code']}: 收盘{r['close']} 涨跌{r['pct_chg']:+.2f}%")
        except Exception:
            pass
        print("\n❌ 缺失实战归因分析 — 无法识别AI预判失误点")
        return {"status": "NO_CALIBRATION_DATA", "count": 0, "error_stats": {}, "sector_stats": {}}

    n = len(records)

    # ── 统计1: 预判误差统计(按error_tag分组) ──
    print(f"\n{'─'*40}")
    print("📊 统计1: 预判误差统计")
    print(f"{'─'*40}")
    error_stats = {}
    for r in records:
        tag = r["error_tag"]
        error_stats[tag] = error_stats.get(tag, 0) + 1
    for tag, cnt in sorted(error_stats.items(), key=lambda x: -x[1]):
        pct = cnt / n * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {tag:<20} {cnt:>3d}次 {bar} {pct:5.1f}%")
    print(f"  {'合计':<20} {n:>3d}次")

    # ── 统计2: 风控有效性统计 ──
    print(f"\n{'─'*40}")
    print("🛡️ 统计2: 风控有效性统计")
    print(f"{'─'*40}")
    risk_valid = error_stats.get("【风控判断有效】", 0)
    entry_fail = error_stats.get("【入场条件失效】", 0)
    over_est = error_stats.get("【预判高估，负误差】", 0)
    under_est = error_stats.get("【预判低估，负误差】", 0)
    risk_total = risk_valid + entry_fail + over_est + under_est
    match = error_stats.get("【预判匹配，无误差】", 0)

    print(f"  风控判断有效(减仓正确): {risk_valid:>3d}次  ✅")
    print(f"  入场条件失效(浮亏被套): {entry_fail:>3d}次  ❌")
    print(f"  预判高估(看多实际跌):   {over_est:>3d}次  🔴")
    print(f"  预判低估(看空实际涨):   {under_est:>3d}次  🟢")
    print(f"  ─────────────────────────────")
    print(f"  需修正样本(负误差合计):  {risk_total:>3d}次")
    print(f"  预判匹配(无误差):       {match:>3d}次")
    if risk_total > 0:
        print(f"  修正率: {risk_total/n*100:.1f}% → 需要因子权重修正")

    # ── 统计3: 赛道分布统计 ──
    print(f"\n{'─'*40}")
    print("🏷️ 统计3: 赛道误差分布")
    print(f"{'─'*40}")
    sector_stats = {}
    for code, sector in SECTOR_GROUPS.items():
        for ticker_code in sector:
            for r in records:
                if r["ticker"] == ticker_code:
                    tag = r["error_tag"]
                    if sector not in sector_stats:
                        sector_stats[sector] = {"total": 0, "match": 0, "error": 0}
                    sector_stats[sector]["total"] += 1
                    if tag == "【预判匹配，无误差】":
                        sector_stats[sector]["match"] += 1
                    else:
                        sector_stats[sector]["error"] += 1
                    break
    for sector, stats in sorted(sector_stats.items()):
        err_pct = stats["error"] / stats["total"] * 100 if stats["total"] > 0 else 0
        bar = "█" * int(err_pct / 10) if err_pct > 0 else "░"
        print(f"  {sector:<8} {stats['total']}只 | 正确{stats['match']} | 偏差{stats['error']} | 偏差率{err_pct:5.1f}% {bar}")

    # ── 归因分析 ──
    print(f"\n{'─'*40}")
    print("🔍 归因分析")
    print(f"{'─'*40}")
    if over_est > 0:
        print(f"  ⚠️ {over_est}只标的预判高估 → 需下调动量因子权重,上调资金因子权重")
    if under_est > 0:
        print(f"  ⚠️ {under_est}只标的预判低估 → 需上调动量因子权重,下调估值因子权重")
    if entry_fail > 0:
        print(f"  ⚠️ {entry_fail}只标的人场条件失效 → 入场阈值需收紧(连续流入天数+1)")
    if risk_valid > 0:
        print(f"  ✅ {risk_valid}只标的风控有效 → 风控因子权重保持,支撑压力位算法验证通过")
    print(f"  ✅ {match}只标的预判匹配 → 维持当前因子权重不变")
    print(" (以上归因分析全部来自人工校准误差标签)")

    print(f"\n{'='*70}")
    return {
        "status": "OK",
        "count": n,
        "error_stats": error_stats,
        "sector_stats": sector_stats,
    }


def run_review_no_calibration(trade_date):
    """模拟无校准数据时的复盘输出(实验B用)"""
    print(f"\n{'='*70}")
    print(f"📋 当日复盘报告(无校准数据): {trade_date}")
    print(f"{'='*70}")
    print("\n⚠️ 未加载到人工校准数据")
    print("   复盘无法区分预判对错,无误差分类,无风控效果统计")
    print("   仅输出纯行情数据:\n")
    try:
        import pandas as pd
        from sqlalchemy import text
        sql = text("""
            SELECT ts_code, close, pct_chg FROM stock_daily
            WHERE trade_date = :d ORDER BY ts_code
        """)
        df = pd.read_sql(sql, pg_engine, params={"d": trade_date})
        if not df.empty:
            for _, r in df.iterrows():
                print(f"  {r['ts_code']}: {r['close']} ({r['pct_chg']:+.2f}%)")
    except Exception as e:
        print(f"  行情加载异常: {e}")
    print("\n❌ 缺失实战归因分析 — 无法识别AI预判失误点")
    print("❌ 无法区分预判对错 → 模型不知哪些标的看对哪些看错")
    print("❌ 无风控效果统计 → 模型不知减仓建议是否正确")
    print("❌ 无人场成功率 → 模型不知入场条件是否有效")
    return {"status": "NO_DATA", "count": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    args = parser.parse_args()
    run_review(args.date)
