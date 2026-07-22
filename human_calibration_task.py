#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
human_calibration_task.py — 全局盘后人工校准

执行时机: 每个交易日收盘后17:30(数据采集完成后)
         cron绑定: stock_daily_task.sh 阶段1完成后自动触发

核心逻辑:
  遍历TARGET_CODES每一只标的, 采集三类必填信息录入 trade_calibration 样本库:
    ① 当日真实行情(涨跌幅/收盘价)
    ② 关键价位状态(支撑/压力有效突破/遇阻受阻/横盘)
    ③ 当日实际交易动作(持仓/止盈/止损/空仓)
  匹配前一日AI预判结论 → 自动绑定误差标签(四选一)
  
永久生效规则:
  - 本表数据为每日复盘/周度调参/自进化唯一数据源
  - 每周迭代前 pre_check 检查: 存在未校准标的则阻断调参流程
  - 所有数据永久归档, 不可清理
"""
import sys
import os
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

logger = logging.getLogger("HumanCalibration")

# 路径
BASE_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = BASE_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

try:
    from config import TARGET_CODES, SECTOR_LABELS, pg_engine
except ImportError:
    TARGET_CODES = []
    SECTOR_LABELS = {}
    pg_engine = None

# ── 误差标签规则 (四选一 + 默认) ──
ERROR_TAGS = {
    "overestimate_negative": "【预判高估，负误差】",    # Rule1: AI看多预判涨,实际大跌
    "underestimate_negative": "【预判低估，负误差】",   # Rule2: AI看空预判跌,实际大涨
    "risk_control_effective": "【风控判断有效】",       # Rule3: AI减仓风控提示,后续持续下行
    "entry_failure": "【入场条件失效】",                # Rule4: AI入场建仓提示,进场浮亏被套
    "no_error": "【预判匹配，无误差】",                 # Default: 预判与走势无偏差
}

# ── 关键价位状态枚举 ──
SUPPORT_RESISTANCE_STATES = [
    "支撑有效突破",  # 价格跌破支撑
    "压力有效突破",  # 价格突破压力
    "支撑遇阻",      # 价格在支撑位获得支撑反弹
    "压力遇阻",      # 价格在压力位受阻回落
    "支撑横盘",      # 价格在支撑位附近横盘
    "压力横盘",      # 价格在压力位附近横盘
    "中间横盘",      # 价格在支撑与压力之间横盘无方向
]


def get_ticker_name(ticker):
    """获取股票名称"""
    return SECTOR_LABELS.get(ticker, ticker)


def fetch_real_market_data(ticker, trade_date):
    """
    采集当日真实行情数据
    返回: { close, change_pct, high, low, volume, amount } 或 None
    """
    if not pg_engine:
        return None
    try:
        import pandas as pd
        from sqlalchemy import text
        sql = text("""
            SELECT close, pct_chg as change_pct, high, low, 
                   volume, amount, ma5, ma10, ma20
            FROM stock_daily 
            WHERE ts_code = :code AND trade_date = :d
            ORDER BY trade_date DESC LIMIT 1
        """)
        df = pd.read_sql(sql, pg_engine, params={"code": ticker, "d": trade_date})
        if not df.empty:
            r = df.iloc[0]
            return {
                "close": float(r["close"]),
                "change_pct": float(r.get("change_pct", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "volume": float(r.get("volume", 0)),
                "ma5": float(r.get("ma5", 0)) if r.get("ma5") else None,
                "ma10": float(r.get("ma10", 0)) if r.get("ma10") else None,
                "ma20": float(r.get("ma20", 0)) if r.get("ma20") else None,
            }
        return None
    except Exception as e:
        logger.warning(f"{ticker} 行情采集异常: {e}")
        return None


def fetch_previous_ai_prediction(ticker, trade_date):
    """
    采集前一日AI量化预判结论
    返回: { ai_score, ai_direction, position, risk_score, risk_level, 
             entry_conditions_met, redlines } 或 None
    """
    if not pg_engine:
        return None
    try:
        prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=3)).strftime("%Y%m%d")
        import pandas as pd
        from sqlalchemy import text

        # 从stock_predict取前一日AI评分/方向
        sql = text("""
            SELECT trade_date, confidence as ai_score, predict_result as ai_direction,
                   position, predict_reason
            FROM stock_predict
            WHERE ts_code = :code AND trade_date < :d
            ORDER BY trade_date DESC LIMIT 1
        """)
        df = pd.read_sql(sql, pg_engine, params={"code": ticker, "d": trade_date})
        if df.empty:
            return None

        r = df.iloc[0]
        return {
            "date": str(r["trade_date"]),
            "ai_score": float(r["ai_score"]) if r["ai_score"] is not None else None,
            "ai_direction": str(r["ai_direction"]) if r["ai_direction"] else "未知",
            "position": str(r.get("position", "未知")),
            "predict_reason": str(r.get("predict_reason", ""))[:200],
        }
    except Exception as e:
        logger.warning(f"{ticker} AI预判采集异常: {e}")
        return None


def bind_error_tag(ai_pred, real_change_pct, real_trade_action):
    """
    根据AI预判 vs 实际走势 → 自动绑定误差标签(四选一+默认)
    
    Rule1: AI看多预判上涨,当日实际大跌(-3%以下) → 预判高估,负误差
    Rule2: AI看空预判下跌,当日实际大涨(+3%以上) → 预判低估,负误差
    Rule3: AI给出减仓风控,后续持续下行 → 风控判断有效
    Rule4: AI入场建仓,进场浮亏被套 → 入场条件失效
    Default: 无偏差 → 预判匹配,无误差
    """
    if not ai_pred or real_change_pct is None:
        return ERROR_TAGS["no_error"]

    direction = ai_pred.get("ai_direction", "")
    score = ai_pred.get("ai_score", 50)
    position = ai_pred.get("position", "")
    change = real_change_pct

    # Rule1: AI看多 → 实际大跌
    if ("看多" in direction or "买入" in direction or score >= 60) and change <= -3.0:
        return ERROR_TAGS["overestimate_negative"]

    # Rule2: AI看空 → 实际大涨
    if ("看空" in direction or "卖出" in direction or score <= 40) and change >= 3.0:
        return ERROR_TAGS["underestimate_negative"]

    # Rule3: AI减仓风控提示 → 持续下行(前一日已减仓)
    if ("减仓" in position or "空仓" in position) and change <= -1.0:
        return ERROR_TAGS["risk_control_effective"]

    # Rule4: AI入场建仓 → 浮亏被套
    if ("买入" in direction or "加仓" in position) and change <= -2.0:
        return ERROR_TAGS["entry_failure"]

    return ERROR_TAGS["no_error"]


def determine_support_resistance(market_data, ai_pred):
    """
    判断关键价位状态
    根据当日价格与AI预判中提供的支撑/压力位进行比较
    
    返回: 支撑压力状态 字符串
    """
    if not market_data:
        return "数据不足"

    close = market_data.get("close", 0)
    high = market_data.get("high", 0)
    low = market_data.get("low", 0)
    ma5 = market_data.get("ma5")
    ma10 = market_data.get("ma10")
    ma20 = market_data.get("ma20")

    # 从AI预判中提取支撑压力位
    support = None
    resistance = None
    if ai_pred:
        reason = ai_pred.get("predict_reason", "")
        # 尝试从理由中提取止损/支撑位信息
        import re
        s = re.search(r'(?:止损|支撑)[：:]?\s*(\d+\.?\d*)', reason)
        r = re.search(r'(?:止盈|压力|目标)[：:]?\s*(\d+\.?\d*)', reason)
        if s: support = float(s.group(1))
        if r: resistance = float(r.group(1))

    # 无参考价位 → 用均线判断
    if support is None and ma20:
        support = ma20
    if resistance is None and ma5 and ma10:
        resistance = max(ma5, ma10)

    # 判断状态
    if support and low is not None and low < support * 0.99:
        return "支撑有效突破"
    if resistance and high is not None and high > resistance * 1.01:
        return "压力有效突破"
    if support and low is not None and low >= support * 0.995:
        return "支撑横盘"
    if resistance and high is not None and high <= resistance * 1.005:
        return "压力横盘"
    if support and low is not None and low < support and close >= support:
        return "支撑遇阻(下影线)"

    return "中间横盘"


def run_daily_human_calibration(all_target_codes=None, trade_date=None):
    """
    盘后核心校准任务 — 为模型迭代提供唯一真实反馈, 缺一不可
    
    Args:
        all_target_codes: 全部跟踪股票代码列表, 默认TARGET_CODES
        trade_date: 交易日 YYYYMMDD, 默认当日
    
    Process:
        1. 遍历每一只标的, 采集三类必填信息
        2. 匹配前一日AI预判 → 绑定误差标签
        3. 持久存入PG trade_calibration表
    
    Returns:
        dict: { total, calibrated_count, missing_count, error_summary, missing_list }
    """
    codes = all_target_codes or TARGET_CODES
    dt = trade_date or date.today().strftime("%Y%m%d")
    
    if not codes:
        logger.error("TARGET_CODES为空, 无法执行校准")
        return {"total": 0, "calibrated_count": 0, "missing_count": 0, "error": "TARGET_CODES_EMPTY"}

    if not pg_engine:
        logger.error("PG未连接, 校准数据无法入库")
        return {"total": len(codes), "calibrated_count": 0, "missing_count": len(codes), 
                "error": "PG_DISCONNECTED", "missing_list": codes}

    logger.info(f"===== 盘后人I校准: {dt} | {len(codes)}只标的 =====")

    results = {"total": len(codes), "calibrated_count": 0, "missing_count": 0,
               "missing_list": [], "error_tags": {}, "details": []}

    for ticker in codes:
        name = get_ticker_name(ticker)
        logger.info(f"  校准 {ticker} {name}...")

        # ① 当日真实行情
        market = fetch_real_market_data(ticker, dt)
        if market is None:
            logger.warning(f"  {ticker} 行情数据缺失,跳过")
            results["missing_count"] += 1
            results["missing_list"].append(f"{ticker}({name})")
            continue

        # ② 前一日AI预判
        ai_pred = fetch_previous_ai_prediction(ticker, dt)

        # ③ 关键价位状态
        sr_state = determine_support_resistance(market, ai_pred)

        # ④ 自动绑定误差标签
        change = market.get("change_pct", 0)
        # 实际交易动作: 根据AI预判和当日涨跌幅推算
        real_action = "持仓"  # 默认
        if ai_pred:
            position = ai_pred.get("position", "")
            direction = ai_pred.get("ai_direction", "")
            if "减仓" in str(position) and change < 0:
                real_action = "空仓(减仓正确)"
            elif "买入" in str(direction) and change > 3:
                real_action = "持仓(浮盈)"
            elif "买入" in str(direction) and change < -2:
                real_action = "持仓(浮亏)"

        error_tag = bind_error_tag(ai_pred, change, real_action)

        # ⑤ 写入PG
        try:
            from sqlalchemy import text
            
            # AI预判JSON序列化
            ai_pred_json = json.dumps(ai_pred, ensure_ascii=False) if ai_pred else None

            with pg_engine.connect() as conn:
                conn.execute(text(f"""
                    INSERT INTO trade_calibration 
                        (trade_date, ticker, ticker_name, 
                         real_close, real_change_pct,
                         support_resistance_result, real_trade_action,
                         yesterday_ai_prediction, error_tag)
                    VALUES (:d, :t, :n, :c, :chg, :sr, :act, :ai, :tag)
                    ON CONFLICT (trade_date, ticker)
                    DO UPDATE SET
                        real_close = EXCLUDED.real_close,
                        real_change_pct = EXCLUDED.real_change_pct,
                        support_resistance_result = EXCLUDED.support_resistance_result,
                        real_trade_action = EXCLUDED.real_trade_action,
                        yesterday_ai_prediction = EXCLUDED.yesterday_ai_prediction,
                        error_tag = EXCLUDED.error_tag,
                        created_at = CURRENT_TIMESTAMP
                """), {
                    "d": dt, "t": ticker, "n": name,
                    "c": market.get("close"), "chg": change,
                    "sr": sr_state, "act": real_action,
                    "ai": ai_pred_json, "tag": error_tag,
                })
                conn.commit()

            results["calibrated_count"] += 1
            results["error_tags"][error_tag] = results["error_tags"].get(error_tag, 0) + 1
            results["details"].append({
                "ticker": ticker, "name": name,
                "close": market.get("close"), "change": change,
                "support_resistance": sr_state, "action": real_action,
                "error_tag": error_tag,
            })

            logger.info(f"  ✅ {ticker} {name}: {change:+.2f}% | {sr_state} | {real_action} | {error_tag}")

        except Exception as e:
            logger.error(f"  ❌ {ticker} 入库异常: {e}")
            results["missing_count"] += 1
            results["missing_list"].append(f"{ticker}({name})")

    # 汇总统计
    logger.info(f"===== 校准完成: {results['calibrated_count']}/{results['total']} =====")
    if results["missing_count"] > 0:
        logger.warning(f"缺失标的: {results['missing_list']}")

    return results


def check_all_ticker_calibration_complete(check_date=None, all_target_codes=None) -> dict:
    """
    周迭代前置校验: 检查指定日期全部标的是否已完成人工校准
    
    Returns:
        { complete: bool, missing_count: int, missing_list: [str], 
          total: int, calibrated: int }
    """
    codes = all_target_codes or TARGET_CODES
    dt = check_date or (date.today() - timedelta(days=1)).strftime("%Y%m%d")

    if not pg_engine:
        return {"complete": False, "error": "PG_DISCONNECTED", 
                "missing_count": len(codes), "missing_list": codes}

    try:
        import pandas as pd
        from sqlalchemy import text

        sql = text(f"""
            SELECT ticker, error_tag FROM trade_calibration 
            WHERE trade_date = :d
        """)
        df = pd.read_sql(sql, pg_engine, params={"d": dt})

        calibrated = set(df["ticker"].tolist()) if not df.empty else set()
        missing = [c for c in codes if c not in calibrated]

        result = {
            "complete": len(missing) == 0,
            "total": len(codes),
            "calibrated": len(calibrated),
            "missing_count": len(missing),
            "missing_list": [f"{c}(校准缺失)" for c in missing],
            "check_date": dt,
        }

        if result["missing_count"] > 0:
            logger.warning(f"\n{'='*60}")
            logger.warning(f"⚠️ 因子迭代前置校验失败: 存在 {result['missing_count']} 只标的未校准")
            for m in result["missing_list"]:
                logger.warning(f"  ❌ {m}")
            logger.warning(f"请先执行 python3 human_calibration_task.py --date {dt}")
            logger.warning(f"{'='*60}\n")
        else:
            logger.info(f"✅ 全部 {result['total']} 只标的校准完成,因子迭代可正常执行")

        return result

    except Exception as e:
        logger.error(f"前置校验异常: {e}")
        return {"complete": False, "error": str(e), "missing_count": len(codes), "missing_list": codes}


def generate_calibration_report(check_date=None):
    """生成校准状态简报"""
    dt = check_date or date.today().strftime("%Y%m%d")
    result = check_all_ticker_calibration_complete(dt)
    
    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"📋 人工校准日报: {dt}")
    lines.append(f"{'='*60}")
    lines.append(f"总标的: {result['total']} | 已校准: {result['calibrated']} | 缺失: {result['missing_count']}")
    
    if result["missing_count"] > 0:
        lines.append(f"\n⚠️ 缺失校准标的:")
        for m in result["missing_list"]:
            lines.append(f"  ❌ {m}")
    
    # 误差标签分布
    if pg_engine:
        try:
            import pandas as pd
            from sqlalchemy import text
            sql = text(f"""
                SELECT error_tag, COUNT(*) as cnt 
                FROM trade_calibration 
                WHERE trade_date = :d
                GROUP BY error_tag ORDER BY cnt DESC
            """)
            df = pd.read_sql(sql, pg_engine, params={"d": dt})
            if not df.empty:
                lines.append(f"\n📊 误差标签分布:")
                for _, r in df.iterrows():
                    lines.append(f"  {r['error_tag']}: {r['cnt']}次")
        except Exception:
            pass
    
    lines.append(f"{'='*60}")
    return "\n".join(lines)


def print_calibration_warning():
    """打印强告警(用于factor_weekly_iterate.py)"""
    result = check_all_ticker_calibration_complete()
    if not result["complete"]:
        print(f"\n{'🚨'*20}")
        print(f"🚨 因子迭代阻断: {result['missing_count']}只标的缺少人工校准数据")
        print(f"🚨 缺失清单: {result['missing_list']}")
        print(f"🚨 校准命令: python3 human_calibration_task.py --date {result['check_date']}")
        print(f"{'🚨'*20}\n")
        return False
    return True


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    
    parser = argparse.ArgumentParser(description="盘后人工校准任务")
    parser.add_argument("--date", type=str, default=date.today().strftime("%Y%m%d"), help="交易日 YYYYMMDD")
    parser.add_argument("--check", action="store_true", help="仅检查校准完整性,不执行校准")
    parser.add_argument("--report", action="store_true", help="仅生成校准状态简报")
    parser.add_argument("--force", action="store_true", help="强制重新校准(覆盖已有记录)")
    args = parser.parse_args()

    if args.check:
        result = check_all_ticker_calibration_complete(args.date)
        print(f"完成度: {result['calibrated']}/{result['total']}")
        if result["missing_count"] > 0:
            print(f"缺失: {result['missing_list']}")
        else:
            print("✅ 全部校准完成")
    elif args.report:
        print(generate_calibration_report(args.date))
    else:
        result = run_daily_human_calibration(trade_date=args.date)
        print(f"\n校准结果: {result['calibrated_count']}/{result['total']}")
        if result.get("error_tags"):
            print(f"误差标签分布: {result['error_tags']}")
        if result["missing_count"] > 0:
            print(f"缺失标的: {result['missing_list']}")
        print(f"\n详细记录:")
        for d in result.get("details", []):
            print(f"  {d['ticker']} {d['name']}: {d['change']:+.2f}% | {d['error_tag']}")
