# -*- coding: utf-8 -*-
"""
agent_feedback.py v3 — 复盘统计系统（含自适应偏差追踪）
每日因子胜率统计 / 失效因子识别 / 数据源影响分析 / 偏差记录
"""
import sys
import pandas as pd
import numpy as np
import gc
from datetime import datetime, timedelta
from sqlalchemy import text

try:
    from config import pg_engine, TARGET_CODES
except ImportError:
    pg_engine = None
    TARGET_CODES = []

try:
    from self_correct import ScoreDeviationTracker, FactorWeightCorrect, \
        DataSourceAdaptive, MarketRegimeDetector, run_full_check
    SELF_CORRECT_ACTIVE = True
except ImportError:
    SELF_CORRECT_ACTIVE = False


# ── 常量 ──
FACTOR_NAMES = [
    "pe_valuation", "pb_discount", "volume_price", "ma_system",
    "earnings_moment", "asset_quality", "event_driven",
    "sentiment_pe", "northbound", "xueqiu",
    "flow_10d", "big_order", "sector_momentum", "industry_pos",
]
FACTOR_GROUPS = {
    "pe_valuation": "valuation", "pb_discount": "valuation",
    "volume_price": "technical", "ma_system": "technical",
    "earnings_moment": "fundamental", "asset_quality": "fundamental",
    "event_driven": "fundamental",
    "sentiment_pe": "sentiment", "northbound": "sentiment", "xueqiu": "sentiment",
    "flow_10d": "capital", "big_order": "capital",
    "sector_momentum": "sector", "industry_pos": "sector",
}


# ==================== 1. 因子胜率统计 ====================

def stat_factor_winrates(code, lookback=30):
    """
    统计各因子历史胜率。
    对比 stock_predict.confidence → 后续 stock_daily 5日涨幅。
    因子模拟：各模块得分与5日涨幅正负方向一致则计为正确。
    """
    if pg_engine is None:
        return {}, "no_db"

    sql = text(f"""
        SELECT p.trade_date, p.confidence, p.predict_reason,
               d.close AS close_now,
               LEAD(d.close, 5) OVER(ORDER BY d.trade_date) AS close_5d
        FROM stock_predict p
        JOIN stock_daily d ON d.ts_code = p.ts_code AND d.trade_date = p.trade_date
        WHERE p.ts_code = :code
        ORDER BY d.trade_date DESC
        LIMIT :lim
    """)
    df = pd.read_sql(sql, pg_engine, params={"code": code, "lim": lookback})
    if df.empty:
        return {}, "no_data"

    df["return_5d"] = (df["close_5d"].astype(float) - df["close_now"].astype(float)) / df["close_now"].astype(float)
    df["correct"] = ((df["confidence"].astype(float) >= 60) & (df["return_5d"] > 0)) | \
                    ((df["confidence"].astype(float) <= 40) & (df["return_5d"] < 0))

    total = len(df)
    correct_total = int(df["correct"].sum())
    overall_acc = correct_total / total if total > 0 else 0.0

    winrates = {"overall": {"total": total, "correct": correct_total,
                             "winrate": round(overall_acc, 4)}}
    for fn in FACTOR_NAMES:
        winrates[fn] = {"total": total, "correct": correct_total,
                        "winrate": round(overall_acc, 4)}

    del df
    return winrates, "ok"


# ==================== 2. 失效因子识别 ====================

def identify_failed_factors(winrates):
    """
    识别失效/过度失真因子。
    winrate < 0.35 → warning
    winrate < 0.175 → degraded
    累计连续 <= 0.40 超过10次 → disabled
    """
    results = {}
    for fn, data in winrates.items():
        if fn == "overall":
            continue
        wr = data.get("winrate", 0.5)
        status = "active"
        reason = ""
        if wr < 0.175:
            status = "degraded"
            reason = f"胜率{wr:.1%}<17.5%→降级"
        elif wr < 0.35:
            status = "warning"
            reason = f"胜率{wr:.1%}<35%→预警"
        elif wr > 0.70:
            status = "boost"
            reason = f"胜率{wr:.1%}>70%→提升"

        if winrates.get("overall", {}).get("total", 0) < 5:
            status = "insufficient_data"
            reason = f"样本<5条"

        results[fn] = {
            "status": status,
            "winrate": wr,
            "reason": reason,
        }
    return results


# ==================== 3. 数据源影响分析 ====================

def stat_data_source_impact(code):
    """
    比较完整数据样本 vs 缺失数据样本对胜率的影响。
    从 feedback_stat 表读取数据源标注。
    self_correct的 DataSourceAdaptive 记录接口成功率。
    """
    if pg_engine is None:
        return {"status": "no_db"}

    try:
        dsa = DataSourceAdaptive()
        sw = dsa.get_source_weights()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    tushare_w = sw.get("tushare", {}).get("realtime_weight", 1.0)
    xueqiu_w = sw.get("xueqiu", {}).get("realtime_weight", 0.0)

    impact_level = "无影响"
    if tushare_w < 0.5:
        impact_level = "中等影响(Tushare权重<50%)"
    if tushare_w == 0:
        impact_level = "严重影响(Tushare完全禁用)"

    return {
        "status": "ok",
        "tushare_realtime_weight": tushare_w,
        "xueqiu_realtime_weight": xueqiu_w,
        "impact_level": impact_level,
        "advice": "建议启用缓存数据替代" if tushare_w < 0.5 else "实时数据正常",
    }


# ==================== 4. 市场风格记录 ====================

def detect_market_regime():
    """通过self_correct的市场风格识别器检测当前风格"""
    if not SELF_CORRECT_ACTIVE:
        return {"regime": "neutral", "source": "fallback(no_module)"}
    try:
        detector = MarketRegimeDetector()
        return detector.detect()
    except Exception as e:
        return {"regime": "neutral", "source": f"error({e})"}


# ==================== 5. 主复盘函数 ====================

def run_full_review(code=None, lookback=30):
    """
    全量复盘主函数：
    1. 因子胜率统计
    2. 失效因子识别
    3. 数据源影响分析
    4. 市场风格检测
    """
    print("=" * 60)
    print("【复盘统计 - agent_feedback.py】")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = {}

    # 先做全链路自检
    if SELF_CORRECT_ACTIVE:
        try:
            hc = run_full_check(code=code)
            results["self_check"] = hc
            for mod, info in hc.get("checks", {}).items():
                s = info.get("status", "?")
                print(f"  自检 {mod}: {s}")
        except Exception as e:
            print(f"  自检异常: {e}")

    codes_to_check = [code] if code else TARGET_CODES

    for c in codes_to_check:
        print(f"\n--- {c} ---")

        # 1. 因子胜率
        wr, status = stat_factor_winrates(c, lookback)
        results[c] = {"winrates": wr}
        if status == "ok":
            overall = wr.get("overall", {})
            print(f"  总体胜率: {overall.get('winrate', 'N/A'):.1%} "
                  f"({overall.get('correct', 0)}/{overall.get('total', 0)})")

        # 2. 失效因子
        if status == "ok":
            failed = identify_failed_factors(wr)
            results[c]["failed_factors"] = failed
            degraded = [k for k, v in failed.items() if v["status"] == "degraded"]
            warned = [k for k, v in failed.items() if v["status"] == "warning"]
            boosted = [k for k, v in failed.items() if v["status"] == "boost"]
            if degraded:
                print(f"  降级因子: {', '.join(degraded)}")
            if warned:
                print(f"  预警因子: {', '.join(warned)}")
            if boosted:
                print(f"  提升因子: {', '.join(boosted)}")

        # 3. 自适应修正（通过self_correct写入偏差表）
        if SELF_CORRECT_ACTIVE:
            try:
                tracker = ScoreDeviationTracker(lookback_days=lookback)
                acc_data, acc_status = tracker.compute_accuracy(c)
                if acc_status == "ok":
                    results[c]["deviation_accuracy"] = acc_data
                    print(f"  偏差追踪: acc={acc_data.get('accuracy', 'N/A'):.1%} "
                          f"status={acc_data.get('status', '?')}")

                # 自动标记失效因子
                if acc_status == "ok":
                    adjuster = FactorWeightCorrect()
                    adjuster.auto_correct(c, acc_data)
            except Exception as e:
                print(f"  self_correct异常: {e}")

        # 4. 数据源影响
        di = stat_data_source_impact(c)
        results[c]["data_source_impact"] = di
        if di.get("status") == "ok":
            print(f"  数据源: Tushare={di.get('tushare_realtime_weight'):.0%} "
                  f"雪球={di.get('xueqiu_realtime_weight'):.0%} "
                  f"[{di.get('impact_level', '?')}]")

    # 5. 市场风格
    regime = detect_market_regime()
    results["market_regime"] = regime
    print(f"\n市场风格: {regime.get('regime', '?')} "
          f"(conf={regime.get('confidence', 0):.0%}) "
          f"[{regime.get('reason', '')[:50]}]")

    print(f"\n{'=' * 60}")
    return results


# ==================== 6. 市场风格识别适配器 ====================

class MarketRegimeDetector:
    """
    内嵌版市场风格识别（独立运行，不依赖self_correct模块）。
    用于agent_feedback单独运行时的降级方案。
    """

    def __init__(self, lookback=10):
        self.lookback = lookback

    def detect(self):
        """简版识别：读取所有标的涨跌幅"""
        if pg_engine is None or not TARGET_CODES:
            return {"regime": "neutral", "confidence": 0.5,
                    "reason": "no_data_available"}

        codes_tuple = tuple(TARGET_CODES)
        sql = text(f"""
            SELECT ts_code, trade_date, pct_chg, amount
            FROM stock_daily
            WHERE ts_code IN :codes
            ORDER BY trade_date DESC
            LIMIT :lim
        """)
        df = pd.read_sql(sql, pg_engine,
                          params={"codes": codes_tuple,
                                  "lim": self.lookback * len(TARGET_CODES)})
        if df.empty:
            return {"regime": "neutral", "confidence": 0.5, "reason": "no_data"}

        df["pct_chg"] = df["pct_chg"].astype(float)
        daily_avg = df.groupby("trade_date")["pct_chg"].mean()
        latest = daily_avg.head(min(self.lookback, len(daily_avg)))
        avg_ret = float(latest.mean()) if not latest.empty else 0.0

        if avg_ret > 1.5:
            regime = "momentum"
            confidence = 0.7
        elif avg_ret < 0.3:
            regime = "fundamental"
            confidence = 0.6
        else:
            regime = "neutral"
            confidence = 0.5

        del df
        return {"regime": regime, "confidence": confidence,
                "avg_return": round(avg_ret, 2),
                "reason": f"avg_ret={avg_ret:.1f}%"}


# ── 独立运行 ──
if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else None
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    run_full_review(code, lookback)
