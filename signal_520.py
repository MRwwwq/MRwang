#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_520.py — 520均线交易信号系统 v2.0 (pandas向量化)

核心信号规则（pandas向量化，无循环）：

金叉判定:
    df['gold_cross'] = (df['ma5'] > df['ma20']) & (df['ma5'].shift(1) <= df['ma20'].shift(1))

死叉判定:
    df['dead_cross'] = (df['ma5'] < df['ma20']) & (df['ma5'].shift(1) >= df['ma20'].shift(1))

前置趋势过滤:
    trend_ok = df['ma20'] > df['ma20'].shift(3)   # 20日线向上

成交量确认:
    vol_ok = df['volume'] > df['volume'].rolling(5).mean() * 1.3

买入信号 = gold_cross & trend_ok & vol_ok
卖出信号 = dead_cross

硬性前置过滤（独立于向量化信号）:
  1. MA20斜率 >= 0 (ma20 > ma20.shift(3))
  2. 日均成交额 >= 5000万
  3. 排除ST/*ST/退市/换手率>25%妖股
"""

import logging
import pandas as pd
import numpy as np
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [SIG520] %(message)s",
                    datefmt="%H:%M:%S")


# ===================== 向量化信号计算 =====================

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """对完整DataFrame向量化计算全部520信号。

    输入df必须含列: close, volume
    自动计算: ma5, ma20, gold_cross, dead_cross, trend_ok, vol_ok, buy, sell

    返回: 原df + 新增列
    """
    df = df.copy()
    df['ma5'] = df['close'].rolling(window=5).mean()
    df['ma20'] = df['close'].rolling(window=20).mean()

    # 金叉：当日MA5>MA20 且 昨日MA5≤MA20
    df['gold_cross'] = (df['ma5'] > df['ma20']) & (df['ma5'].shift(1) <= df['ma20'].shift(1))

    # 死叉：当日MA5<MA20 且 昨日MA5≥MA20
    df['dead_cross'] = (df['ma5'] < df['ma20']) & (df['ma5'].shift(1) >= df['ma20'].shift(1))

    # 前置趋势过滤：MA20 比3日前高（走平/向上）
    df['trend_ok'] = df['ma20'] > df['ma20'].shift(3)

    # 成交量确认：当日量 > 近5日均量 × 1.3
    df['vol_ma5'] = df['volume'].rolling(5).mean()
    df['vol_ok'] = df['volume'] > df['vol_ma5'] * 1.3

    # 综合信号
    df['buy'] = df['gold_cross'] & df['trend_ok'] & df['vol_ok']
    df['sell'] = df['dead_cross']

    return df


def get_latest_signals(df: pd.DataFrame) -> dict:
    """从向量化计算结果提取最新交易日的信号。

    返回:
        {
            "has_buy_signal": bool,
            "has_sell_signal": bool,
            "gold_cross": bool,
            "dead_cross": bool,
            "trend_ok": bool,
            "vol_ok": bool,
            "ma5": float,
            "ma20": float,
            "close": float,
            "vol_ratio": float,   # 当日量/5日均量
            "detail": str,
        }
    """
    df_sig = compute_signals(df)
    last = df_sig.iloc[-1]

    vol_ratio = last['volume'] / last['vol_ma5'] if pd.notna(last['vol_ma5']) and last['vol_ma5'] > 0 else 0

    parts = []
    if last['gold_cross']:
        parts.append(f"MA5({last['ma5']:.2f})上穿MA20({last['ma20']:.2f})✅")
    elif last['ma5'] > last['ma20']:
        parts.append(f"MA5({last['ma5']:.2f})>MA20({last['ma20']:.2f})多头✅")
    else:
        parts.append(f"MA5({last['ma5']:.2f})<MA20({last['ma20']:.2f})空头❌")
    parts.append(f"MA20趋势{'向上' if last['trend_ok'] else '向下/走平'} {'✅' if last['trend_ok'] else '❌'}")
    parts.append(f"量比{vol_ratio:.2f}x{'✅' if last['vol_ok'] else '❌'}")

    return {
        "has_buy_signal": bool(last['buy']),
        "has_sell_signal": bool(last['sell']),
        "gold_cross": bool(last['gold_cross']),
        "dead_cross": bool(last['dead_cross']),
        "trend_ok": bool(last['trend_ok']),
        "vol_ok": bool(last['vol_ok']),
        "ma5": round(last['ma5'], 2) if pd.notna(last['ma5']) else None,
        "ma20": round(last['ma20'], 2) if pd.notna(last['ma20']) else None,
        "close": float(last['close']),
        "vol_ratio": round(vol_ratio, 2),
        "detail": " | ".join(parts),
    }


# ===================== 硬性前置过滤 =====================

def filter_ma20_trend(df: pd.DataFrame) -> dict:
    """MA20斜率 >= 0（走平/向上），条件是 ma20 > ma20.shift(3)。

    返回: {"passed": bool, "detail": str}
    """
    df_sig = compute_signals(df)
    last = df_sig.iloc[-1]
    passed = bool(last['trend_ok']) if pd.notna(last['trend_ok']) else False
    return {
        "passed": passed,
        "detail": f"MA20{'向上' if passed else '向下'}, 斜率条件{'✅' if passed else '❌'}",
    }


def filter_liquidity(avg_daily_amount: float,
                     min_amount: float = 50_000_000) -> dict:
    """日均成交额 >= 5000万"""
    amount_wan = avg_daily_amount / 10_000
    passed = avg_daily_amount >= min_amount
    return {
        "passed": passed,
        "amount_wan": round(amount_wan, 0),
        "detail": f"日均{amount_wan:.0f}万{'✅' if passed else '❌'}(需≥{min_amount/10000:.0f}万)",
    }


def filter_exclude_st(stock_name: str = "",
                      turnover_rate: float = 0,
                      max_turnover: float = 25.0) -> dict:
    """排除ST/*ST/退市/换手率>25%妖股"""
    reasons = []
    if "ST" in stock_name.upper() or "退" in stock_name:
        reasons.append(f"ST/退市: {stock_name}")
    if turnover_rate > max_turnover:
        reasons.append(f"换手{turnover_rate:.1f}%>{max_turnover}%")
    passed = len(reasons) == 0
    return {
        "passed": passed,
        "detail": " | ".join(reasons) if reasons else "非ST/非妖股 ✅",
    }


def run_hard_filters(df: pd.DataFrame,
                     avg_daily_amount: float,
                     stock_name: str = "",
                     turnover_rate: float = 0) -> dict:
    """全部前置过滤（必须全部通过）。"""
    f1 = filter_ma20_trend(df)
    f2 = filter_liquidity(avg_daily_amount)
    f3 = filter_exclude_st(stock_name, turnover_rate)
    failed = []
    if not f1["passed"]:
        failed.append(f1["detail"])
    if not f2["passed"]:
        failed.append(f2["detail"])
    if not f3["passed"]:
        failed.append(f3["detail"])
    return {
        "all_passed": len(failed) == 0,
        "filters": {"ma20_trend": f1, "liquidity": f2, "exclude_st": f3},
        "failed_reason": " | ".join(failed) if failed else "",
    }


# ===================== 龙回头模式 =====================

def check_dragon_return(df: pd.DataFrame) -> dict:
    """龙回头模式。

    条件:
      1. 多头区间：近10日收盘大部分(>=70%)在MA20上方
      2. 回踩MA20：近3日最低曾接近MA20(1.5%以内)但未跌破
      3. 重新站上MA5：当日收盘 > MA5
    """
    df_sig = compute_signals(df)
    lookback = min(10, len(df_sig))
    above_ma20 = sum(df_sig['close'].iloc[-lookback:] > df_sig['ma20'].iloc[-lookback:])
    cond1 = above_ma20 >= lookback * 0.7

    recent3_close = df_sig['close'].iloc[-3:]
    recent3_ma20 = df_sig['ma20'].iloc[-3:]
    if len(recent3_close) >= 2:
        ratios = abs(recent3_close.values - recent3_ma20.values) / recent3_ma20.values
        dipped = any(r < 0.015 for r in ratios)
        never_broke = all(close >= ma20 * 0.985 for close, ma20 in zip(recent3_close, recent3_ma20))
        cond2 = dipped and never_broke
    else:
        cond2 = False

    last = df_sig.iloc[-1]
    cond3 = bool(last['close'] > last['ma5']) if pd.notna(last['ma5']) else False

    all_pass = cond1 and cond2 and cond3
    return {
        "signal": all_pass,
        "type": "龙回头" if all_pass else "未触发",
        "conditions": {"bull_market": bool(cond1),
                       "pullback_ma20": bool(cond2),
                       "reclaim_ma5": bool(cond3)},
        "detail": f"多头{above_ma20}/{lookback}日{'✅' if cond1 else '❌'} | "
                  f"回踩MA20{'✅' if cond2 else '❌'} | "
                  f"站回MA5{'✅' if cond3 else '❌'}",
    }


# ===================== 综合信号判定 =====================

def full_signal_check(
    df: pd.DataFrame,
    avg_daily_amount: float = 100_000_000,
    stock_name: str = "",
    turnover_rate: float = 0,
) -> dict:
    """综合520信号全量检查。

    参数:
        df: 含 close, volume 列的日线DataFrame
        avg_daily_amount: 日均成交额(元)，用于流动性过滤
        stock_name: 股票名称，用于ST排除
        turnover_rate: 当日换手率%

    返回: 完整信号字典
    """
    # 1. 前置过滤
    prefilters = run_hard_filters(df, avg_daily_amount, stock_name, turnover_rate)

    # 2. 向量化信号
    signals = get_latest_signals(df)

    # 3. 龙回头
    dragon = check_dragon_return(df)

    # 4. 联动警告
    lolla_warning = ""
    if signals["has_buy_signal"] and not prefilters["all_passed"]:
        lolla_warning = "⚠️ 金叉信号但前置过滤未通过→可能下跌中继假金叉"
    if signals["gold_cross"] and signals["dead_cross"]:
        lolla_warning = "⚠️ 金叉+死叉同时信号→震荡市假信号"

    # 5. 综合
    if signals["has_sell_signal"]:
        composite = {"action": "卖出(520死叉)", "priority": "high"}
    elif prefilters["all_passed"] and signals["has_buy_signal"]:
        composite = {"action": "买入(520金叉)", "priority": "high"}
    elif prefilters["all_passed"] and dragon["signal"]:
        composite = {"action": "低吸(龙回头)", "priority": "medium"}
    elif signals["gold_cross"] and not prefilters["all_passed"]:
        composite = {"action": "观望(前置过滤未通过)", "priority": "low"}
    else:
        composite = {"action": "观望", "priority": "none"}

    return {
        "prefilters": prefilters,
        "signals": signals,
        "dragon_return": dragon,
        "composite_signal": composite,
        "lolla_warning": lolla_warning,
    }


# ===================== 快捷入口（向后兼容列表接口） =====================

def full_signal_check_from_lists(
    close: list, volume: list,
    avg_daily_amount: float = 100_000_000,
    stock_name: str = "",
    turnover_rate: float = 0,
) -> dict:
    """列表输入兼容版（用于测试/非pandas环境）。"""
    df = pd.DataFrame({"close": close, "volume": volume})
    return full_signal_check(df, avg_daily_amount, stock_name, turnover_rate)


# ===================== 自测 =====================

if __name__ == "__main__":
    print("=" * 60)
    print("  520均线交易信号系统 v2.0 — pandas向量化自测")
    print("=" * 60)

    # 构建测试数据
    days = 30
    np.random.seed(42)

    # 构建精确测试数据
    print("\n--- 测试1: 520金叉(精确构造) ---")
    # 40日数据: 前25日下跌, 后15日稳步回升
    p1 = list(np.linspace(10, 7.5, 22))   # 下跌
    p2 = list(np.linspace(7.5, 8.0, 6))   # 筑底
    p3 = [8.2, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5]  # 拉升
    prices_gold = p1 + p2 + p3
    vols_gold = [60] * 20 + [70] * 8 + [80, 90, 150, 200, 220, 250, 280, 300, 320, 350, 380, 400]

    df1 = pd.DataFrame({"close": prices_gold, "volume": vols_gold})
    df1_sig = compute_signals(df1)

    # 查询全部金叉信号行
    gold_rows = df1_sig[df1_sig['gold_cross'] == True]
    buy_rows = df1_sig[df1_sig['buy'] == True]
    print(f"  总行数: {len(df1_sig)}, 金叉行: {len(gold_rows)}, 买入行: {len(buy_rows)}")
    if len(gold_rows) > 0:
        last_gold = gold_rows.iloc[-1]
        print(f"  最新金叉行: close={last_gold['close']:.2f} ma5={last_gold['ma5']:.2f} ma20={last_gold['ma20']:.2f}")
    if len(buy_rows) > 0:
        last_buy = buy_rows.iloc[-1]
        print(f"  最新买入行: close={last_buy['close']:.2f} trend={last_buy['trend_ok']} vol={last_buy['vol_ok']}")

    r1 = full_signal_check(df1, avg_daily_amount=100_000_000)
    last = df1_sig.iloc[-1]
    print(f"  最新行情: MA5={last['ma5']:.2f} MA20={last['ma20']:.2f} 趋势={last['trend_ok']} 量ok={last['vol_ok']}")
    print(f"  最新金叉: {r1['signals']['gold_cross']} | 买入: {r1['signals']['has_buy_signal']}")
    # 关键: 验证向量化计算本身正确——金叉信号确实出现了(不一定在最后一天)
    assert len(gold_rows) > 0, "应至少出现一次金叉信号"
    print("  ✅ 金叉信号产生正确")

    # 测试2: 无金叉
    print("\n--- 测试2: 持续下跌→无金叉 ---")
    prices2 = list(10 - np.cumsum(np.abs(np.random.randn(days)) * 0.2))
    prices2 = [max(p, 5) for p in prices2]
    vols2 = [80] * days
    df2 = pd.DataFrame({"close": prices2, "volume": vols2})
    r2 = full_signal_check(df2, avg_daily_amount=100_000_000)
    print(f"  买入信号: {r2['signals']['has_buy_signal']} | 金叉: {r2['signals']['gold_cross']}")
    assert not r2['signals']['has_buy_signal']
    print("  ✅")

    # 测试3: 死叉场景 — 先涨后跌
    print("\n--- 测试3: 520死叉 ---")
    p1_death = list(np.linspace(8, 14, 20))  # 上涨
    p2_death = list(np.linspace(14, 10, 20))  # 下跌
    prices_death = p1_death + p2_death
    vols_death = [100] * 40
    df3 = pd.DataFrame({"close": prices_death, "volume": vols_death})
    df3_sig = compute_signals(df3)
    death_rows = df3_sig[df3_sig['dead_cross'] == True]
    print(f"  总行数: {len(df3_sig)}, 死叉行: {len(death_rows)}")
    r3 = full_signal_check(df3, avg_daily_amount=100_000_000)
    print(f"  卖出信号: {r3['signals']['has_sell_signal']} | 综合: {r3['composite_signal']['action']}")
    assert len(death_rows) > 0, "应至少出现一次死叉"
    print("  ✅ 死叉信号产生正确")

    # 测试4: 前置过滤 — 流动性不足
    print("\n--- 测试4: 流动性不足(<5000万) ---")
    r4 = full_signal_check(df1, avg_daily_amount=10_000_000)
    assert not r4['prefilters']['all_passed']
    print(f"  过滤: {r4['prefilters']['failed_reason']} ✅")

    # 测试5: ST排除
    print("\n--- 测试5: ST排除 ---")
    r5 = full_signal_check(df1, avg_daily_amount=100_000_000, stock_name="ST华泰")
    assert not r5['prefilters']['all_passed']
    print(f"  过滤: {r5['prefilters']['failed_reason']} ✅")

    # 测试6: 综合信号 + 龙回头
    print("\n--- 测试6: 综合信号 ---")
    r6 = full_signal_check(df1, avg_daily_amount=100_000_000)
    print(f"  前置: {'通过' if r6['prefilters']['all_passed'] else '不通过'}")
    print(f"  金叉: {r6['signals']['gold_cross']} | 趋势ok: {r6['signals']['trend_ok']} | 量ok: {r6['signals']['vol_ok']}")
    print(f"  综合: {r6['composite_signal']['action']}")
    if r6.get("lolla_warning"):
        print(f"  🚨 {r6['lolla_warning']}")
    print("  ✅")

    print(f"\n{'='*60}")
    print("  全部测试通过")
    print(f"{'='*60}")
