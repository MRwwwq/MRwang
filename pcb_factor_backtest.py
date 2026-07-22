#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcb_factor_backtest.py — 300476 胜宏科技 PCB 12因子3年回测引擎

用途:
  对300476胜宏科技执行12个PCB赛道因子的信息系数(IC)回测,
  生成双周期自适应权重快照, 检测因子方向冲突并自动修正估值折价幅度。

输出:
  /opt/stock_agent/reports/pcb_factor_backtest_YYYY-MM-DD.json
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pcb_factor_backtest")

# ── DB连接配置 ──
DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}

STOCK_CODE = "300476"
REPORT_DIR = Path("/opt/stock_agent/reports")


# ========================================================================
#  第1步: 从PG加载数据
# ========================================================================
def load_stock_data(cur, code: str) -> pd.DataFrame:
    """加载股票日线数据"""
    sql = """
        SELECT trade_date, close, pct_chg, vol, amount, open, high, low,
               ma5, ma10, ma20
        FROM stock_daily
        WHERE stock_code = %s
        ORDER BY trade_date
    """
    cur.execute(sql, (code,))
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    df = pd.DataFrame(rows, columns=cols)
    numeric_cols = [
        c for c in cols
        if c not in ("trade_date", "ts_code", "stock_code", "sector")
    ]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    logger.info(f"stock_daily: {len(df)} rows, {df.index.min()} ~ {df.index.max()}")
    return df


def _to_float_df(df: pd.DataFrame, date_col="trade_date") -> pd.DataFrame:
    """Convert all Decimal/numeric columns to float"""
    for c in df.columns:
        if c != date_col:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col])
        df.set_index(date_col, inplace=True)
    return df


def load_money_flow(cur, code: str) -> pd.DataFrame:
    """加载资金流数据"""
    sql = """
        SELECT trade_date,
               buy_sm_amount, sell_sm_amount,
               buy_md_amount, sell_md_amount,
               buy_lg_amount, sell_lg_amount,
               buy_elg_amount, sell_elg_amount,
               net_mf_amount
        FROM stock_money_flow
        WHERE stock_code = %s
        ORDER BY trade_date
    """
    cur.execute(sql, (code,))
    rows = cur.fetchall()
    df = _to_float_df(pd.DataFrame(rows, columns=[desc[0] for desc in cur.description]))
    logger.info(f"stock_money_flow: {len(df)} rows, {df.index.min()} ~ {df.index.max()}")
    return df


def load_daily_basic(cur, code: str) -> pd.DataFrame:
    """加载每日基本面数据 (PE, PB, turnover)"""
    sql = """
        SELECT trade_date, turnover_rate, pe_ttm, pb, ps_ttm,
               total_mv, circ_mv, volume_ratio
        FROM stock_daily_basic
        WHERE stock_code = %s
        ORDER BY trade_date
    """
    cur.execute(sql, (code,))
    rows = cur.fetchall()
    df = _to_float_df(pd.DataFrame(rows, columns=[desc[0] for desc in cur.description]))
    logger.info(f"stock_daily_basic: {len(df)} rows, {df.index.min()} ~ {df.index.max()}")
    return df


def query_all_data() -> pd.DataFrame:
    """查询所有数据并合并为一个宽表"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()

        daily = load_stock_data(cur, STOCK_CODE)
        mf = load_money_flow(cur, STOCK_CODE)
        basic = load_daily_basic(cur, STOCK_CODE)

        # 合并
        df = daily.join(mf, how="left", rsuffix="_mf")
        df = df.join(basic, how="left", rsuffix="_basic")

        # 填充资金流缺失值
        mf_cols = [c for c in mf.columns if c != "trade_date"]
        for c in mf_cols:
            if c in df.columns:
                df[c] = df[c].fillna(0)

        # 填充基本面缺失值
        basic_cols = [c for c in basic.columns if c != "trade_date"]
        for c in basic_cols:
            if c in df.columns:
                df[c] = df[c].ffill().bfill()

        cur.close()
        return df

    finally:
        conn.close()


# ========================================================================
#  第2步: 加载PCB因子定义
# ========================================================================
def load_factor_init() -> dict:
    """从 pcb_factor_init.py 加载PCB_FACTOR_INIT"""
    sys.path.insert(0, "/opt/stock_agent")
    from pcb_factor_init import PCB_FACTOR_INIT

    return PCB_FACTOR_INIT


# ========================================================================
#  第3步: 计算因子得分
# ========================================================================
def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """基于可用数据计算12个PCB因子每日得分

    由于income / fina_indicator表不存在, 使用可得数据计算代理因子。
    每个因子输出 z-score 标准化后的值。
    """
    factor_df = pd.DataFrame(index=df.index)

    # ── 辅助列 ──
    # 滚动20日收益率 (用作市场周期判断, 以及因子对比)
    df["ret_20d"] = df["close"].pct_change(20)

    # ── 1. pe_forward_growth [正向] ──
    # 代理: 低PE_TTM + 强价格趋势 → 增长匹配度高
    # 用 -pe_ttm * ret_20d 组合: 低PE且上涨趋势 = 好的增长匹配
    if "pe_ttm" in df.columns and df["pe_ttm"].notna().sum() > 0:
        pe_inv = 1.0 / (df["pe_ttm"].clip(lower=1).abs() + 1)
        factor_df["pe_forward_growth"] = (
            pe_inv * (1 + df["pct_chg"].rolling(10).mean().fillna(0) / 100)
        )
    else:
        # fallback: 价格动量
        factor_df["pe_forward_growth"] = df["close"] / df["close"].rolling(20).mean()

    # ── 2. customer_concentration [负向] ──
    # 代理: 大单资金占比高 = 机构集中度高 = 负面
    large_buy = (
        df.get("buy_lg_amount", pd.Series(0, index=df.index))
        + df.get("buy_elg_amount", pd.Series(0, index=df.index))
    )
    large_sell = (
        df.get("sell_lg_amount", pd.Series(0, index=df.index))
        + df.get("sell_elg_amount", pd.Series(0, index=df.index))
    )
    total_amount = df.get("amount", pd.Series(1, index=df.index))
    # 大单净比率 (负值=流出=负面, 正值=流入=正面)
    large_net_ratio = (large_buy - large_sell) / (total_amount + 1)
    factor_df["customer_concentration"] = large_net_ratio

    # ── 3. gross_margin_trend [正向] ──
    # 代理: 价格相对于MA20的强度 (趋势跟随)
    ma20 = df.get("ma20", df["close"].rolling(20).mean())
    factor_df["gross_margin_trend"] = df["close"] / ma20 - 1.0

    # ── 4. revenue_yoy [正向] ──
    # 代理: 20日价格动量 (反映增长预期)
    factor_df["revenue_yoy"] = df["ret_20d"].fillna(0)

    # ── 5. capex_intensity [负向] ──
    # 代理: 成交额波动率 / 换手率 (高成交波动 = 高资本开支强度 = 负面)
    if "volume_ratio" in df.columns:
        factor_df["capex_intensity"] = -df["volume_ratio"].fillna(1).clip(0, 10)
    else:
        vol_ma = df["vol"].rolling(20).mean().replace(0, 1)
        factor_df["capex_intensity"] = -(df["vol"] / vol_ma).fillna(1).clip(0, 10)

    # ── 6. ar_turnover_days [负向] ──
    # 代理: 换手率倒数 (高换手=周转快=好, 低换手=应收慢=负面)
    if "turnover_rate" in df.columns:
        tr = df["turnover_rate"].fillna(0).clip(lower=0.01)
        factor_df["ar_turnover_days"] = -(1.0 / tr)
    else:
        # fallback: 使用vol/close ratio
        vol_ratio = df["vol"] / (df["close"] * 100 + 1)
        factor_df["ar_turnover_days"] = -vol_ratio.rolling(20).mean().fillna(0)

    # ── 7. inventory_turnover [正向] ──
    # 代理: 成交量比率 (高成交量 = 高周转)
    if "volume_ratio" in df.columns:
        factor_df["inventory_turnover"] = df["volume_ratio"].fillna(1).clip(0, 10)
    else:
        vol_ma = df["vol"].rolling(20).mean().replace(0, 1)
        factor_df["inventory_turnover"] = (df["vol"] / vol_ma).fillna(1).clip(0, 10)

    # ── 8. raw_material_index [负向] ──
    # 代理: 价格波动率 (高波动 = 原材料价格不稳定 = 负面)
    factor_df["raw_material_index"] = -(
        df["close"].pct_change().rolling(10).std().fillna(0)
    )

    # ── 9. overseas_capacity [正向] ──
    # 代理: 外资/大单净流入 (反映海外资金认可)
    net_mf = df.get("net_mf_amount", pd.Series(0, index=df.index))
    factor_df["overseas_capacity"] = net_mf / (df["amount"] + 1)

    # ── 10. r_d_intensity [正向] ──
    # 代理: PE/PB比率的变化 (高研发投入通常有较高的PB/PE)
    if "pe_ttm" in df.columns and "pb" in df.columns:
        pe = df["pe_ttm"].fillna(20).clip(1, 200)
        pb = df["pb"].fillna(2).clip(0.1, 50)
        factor_df["r_d_intensity"] = (pe / pb).pct_change(20).fillna(0)
    else:
        factor_df["r_d_intensity"] = df["close"].pct_change(10).fillna(0)

    # ── 11. industry_supply_gap [正向] ──
    # 代理: 资金净流入强度 (供需缺口 = 资金流入强度)
    net_amount = df.get("net_mf_amount", pd.Series(0, index=df.index))
    factor_df["industry_supply_gap"] = (
        net_amount.rolling(5).mean() / (df["amount"].rolling(5).mean() + 1)
    )

    # ── 12. asic_diversification [正向] ──
    # 代理: 大小单资金流方向分歧 (大单净买 vs 小单净买)
    small_net = (
        df.get("buy_sm_amount", pd.Series(0, index=df.index))
        - df.get("sell_sm_amount", pd.Series(0, index=df.index))
    )
    large_net = large_buy - large_sell
    # 大单流入且小单流出 = 机构看好 = ASIC拓客进展好
    factor_df["asic_diversification"] = (large_net - small_net) / (df["amount"] + 1)

    # ── 处理无穷值和缺失 ──
    factor_df.replace([np.inf, -np.inf], np.nan, inplace=True)

    # ── Z-score 标准化每个因子 ──
    for col in factor_df.columns:
        s = factor_df[col]
        mean_v = s.mean()
        std_v = s.std()
        if std_v > 0 and not np.isnan(std_v):
            factor_df[col] = (s - mean_v) / std_v
        else:
            factor_df[col] = 0.0

    factor_df.fillna(0, inplace=True)
    return factor_df


# ========================================================================
#  第4步: 计算IC和IR
# ========================================================================
def compute_next_20d_return(df: pd.DataFrame) -> pd.Series:
    """计算每个交易日的未来20日收益率"""
    close = df["close"]
    # 向前偏移20个交易日
    future_close = close.shift(-20)
    ret_20d_fwd = (future_close - close) / close
    ret_20d_fwd.name = "next_20d_return"
    return ret_20d_fwd


def compute_ic_spearman(factor_scores: pd.Series, forward_returns: pd.Series) -> float:
    """计算单日 Spearman 秩相关系数 (IC)"""
    valid = factor_scores.notna() & forward_returns.notna()
    if valid.sum() < 5:
        return np.nan
    from scipy.stats import spearmanr

    r, _ = spearmanr(factor_scores[valid], forward_returns[valid])
    return r


def compute_ic_series(
    factor_df: pd.DataFrame, forward_ret: pd.Series, min_periods: int = 20
) -> pd.DataFrame:
    """计算每个因子每日IC序列 (滚动窗口秩相关)"""
    from scipy.stats import spearmanr

    ic_dict = {}
    for factor_name in factor_df.columns:
        ic_values = []
        valid_dates = []
        for i in range(min_periods, len(factor_df)):
            slice_end = i
            factor_slice = factor_df[factor_name].iloc[:slice_end]
            ret_slice = forward_ret.iloc[:slice_end]
            valid = factor_slice.notna() & ret_slice.notna()
            if valid.sum() >= min_periods:
                r, _ = spearmanr(factor_slice[valid], ret_slice[valid])
                ic_values.append(r)
                valid_dates.append(factor_df.index[i])

        ic_dict[factor_name] = pd.Series(
            ic_values, index=pd.DatetimeIndex(valid_dates)
        )
        logger.info(
            f"  {factor_name}: {len(ic_values)} daily IC values computed"
        )

    return pd.DataFrame(ic_dict)


def compute_ir(ic_series: pd.Series) -> float:
    """信息比率 = mean(IC) / std(IC)"""
    valid = ic_series.dropna()
    if len(valid) < 5 or valid.std() == 0:
        return 0.0
    return float(valid.mean() / valid.std())


# ========================================================================
#  第5步: 周期划分
# ========================================================================
def classify_cycles(df: pd.DataFrame) -> pd.Series:
    """根据20日收益率划分牛熊周期

    - up-cycle: ret_20d > 0.05 (涨幅>5%)
    - down-cycle: ret_20d < -0.05 (跌幅>5%)
    - neutral: 其余
    """
    ret_key = "ret_20d"
    if ret_key not in df.columns:
        df[ret_key] = df["close"].pct_change(20)

    cycles = pd.Series("neutral", index=df.index)
    cycles[df[ret_key] > 0.05] = "up"
    cycles[df[ret_key] < -0.05] = "down"
    return cycles


# ========================================================================
#  第6步: 因子方向冲突检测与修正
# ========================================================================
def detect_and_correct_conflicts(
    factor_init: dict, ic_summary: pd.DataFrame
) -> dict:
    """检测因子实际IC方向与理论方向的冲突

    如果实际平均IC的符号与理论方向不一致, 说明该因子的估值折价方向
    与市场实际表现相反, 需要自动修正:
    - 降低该因子的权重
    - 记录修正幅度

    Returns:
        dict: {factor_name: {theoretical_direction, actual_ic_sign,
                              conflict, correction_magnitude, corrected_weight}}
    """
    conflicts = {}
    for factor_name, (init_weight, direction, desc) in factor_init.items():
        if factor_name not in ic_summary.index:
            continue

        actual_mean_ic = ic_summary.loc[factor_name, "mean_ic"]
        actual_sign = 1 if actual_mean_ic >= 0 else -1
        theoretical_sign = direction

        is_conflict = actual_sign != theoretical_sign

        correction = 0.0
        corrected_weight = float(init_weight)
        if is_conflict and abs(actual_mean_ic) > 0.01:
            # 冲突修正: 权重减半, 实际IC越强修正幅度越大
            conflict_strength = min(abs(actual_mean_ic) * 5, 0.5)
            correction = conflict_strength
            corrected_weight = float(init_weight) * (1 - correction)

        conflicts[factor_name] = {
            "theoretical_direction": theoretical_sign,
            "actual_mean_ic": round(float(actual_mean_ic), 4),
            "actual_ic_sign": actual_sign,
            "is_conflict": is_conflict,
            "correction_magnitude": round(correction, 4),
            "initial_weight": float(init_weight),
            "corrected_weight": round(corrected_weight, 4),
            "description": desc,
        }

    return conflicts


# ========================================================================
#  第7步: 生成双周期自适应权重
# ========================================================================
def generate_adaptive_weights(
    ic_per_cycle: dict,
    factor_init: dict,
    conflicts: dict,
) -> dict:
    """生成牛熊周期自适应权重快照

    Args:
        ic_per_cycle: {"up": DataFrame, "down": DataFrame, "all": DataFrame}
        factor_init: PCB_FACTOR_INIT dict
        conflicts: directional conflict results

    Returns:
        {"up_weights": dict, "down_weights": dict}
    """
    weights = {}

    for cycle in ["up", "down"]:
        ic_df = ic_per_cycle.get(cycle)
        if ic_df is None or ic_df.empty:
            # 无数据时使用修正权重
            w = {
                f: conflicts.get(f, {}).get("corrected_weight", init[0])
                for f, init in factor_init.items()
            }
        else:
            ir_values = {}
            for factor_name in ic_df.columns:
                valid_ic = ic_df[factor_name].dropna()
                if len(valid_ic) >= 5:
                    ir = valid_ic.mean() / (valid_ic.std() + 1e-8)
                    ir_values[factor_name] = abs(ir)
                else:
                    ir_values[factor_name] = 0.0

            # 使用绝对IR作为权重, 但使用修正后的初始权重作为基底
            w = {}
            for factor_name, init_weight in factor_init.items():
                base = conflicts.get(factor_name, {}).get(
                    "corrected_weight", init_weight[0]
                )
                ir = ir_values.get(factor_name, 0.0)
                # 合并: base * (1 + IR) 作为自适应权重
                w[factor_name] = base * (1 + min(ir * 2, 0.5))

            # 归一化
            total = sum(w.values())
            if total > 0:
                w = {k: round(v / total, 4) for k, v in w.items()}

        weights[f"{cycle}_weights"] = w

    return weights


# ========================================================================
#  第8步: 生成报告
# ========================================================================
def generate_report(
    factor_init: dict,
    factor_df: pd.DataFrame,
    ic_df: pd.DataFrame,
    ic_summary: pd.DataFrame,
    cycle_labels: pd.Series,
    ic_per_cycle: dict,
    conflicts: dict,
    adaptive_weights: dict,
    df_raw: pd.DataFrame,
) -> dict:
    """生成完整回测报告"""
    today_str = date.today().isoformat()

    # 按周期统计IC
    cycle_stats = {}
    for cycle_name in ["up", "down", "neutral"]:
        cycle_dates = cycle_labels[cycle_labels == cycle_name].index
        if len(cycle_dates) == 0:
            cycle_stats[cycle_name] = {
                "days": 0,
                "date_range": "",
            }
            continue

        cycle_ic = ic_df.loc[ic_df.index.isin(cycle_dates)] if len(cycle_dates) > 0 else pd.DataFrame()
        cycle_factor_stats = {}
        for factor_name in factor_init:
            if factor_name in cycle_ic.columns:
                s = cycle_ic[factor_name].dropna()
                cycle_factor_stats[factor_name] = {
                    "mean_ic": round(float(s.mean()), 4) if len(s) > 0 else 0.0,
                    "std_ic": round(float(s.std()), 4) if len(s) > 1 else 0.0,
                    "count": len(s),
                    "ir": round(float(s.mean() / (s.std() + 1e-8)), 4) if len(s) > 1 else 0.0,
                }

        cycle_stats[cycle_name] = {
            "days": len(cycle_dates),
            "date_range": f"{cycle_dates.min().date()} ~ {cycle_dates.max().date()}"
            if len(cycle_dates) > 0
            else "",
            "factors": cycle_factor_stats,
        }

    # 汇总IC统计
    factor_stats = {}
    for factor_name in factor_init:
        if factor_name in ic_df.columns:
            s = ic_df[factor_name].dropna()
            factor_stats[factor_name] = {
                "mean_ic": round(float(s.mean()), 4) if len(s) > 0 else 0.0,
                "std_ic": round(float(s.std()), 4) if len(s) > 1 else 0.0,
                "min_ic": round(float(s.min()), 4) if len(s) > 0 else 0.0,
                "max_ic": round(float(s.max()), 4) if len(s) > 0 else 0.0,
                "positive_pct": round(float((s > 0).mean()), 4) if len(s) > 0 else 0.0,
                "ir": round(float(s.mean() / (s.std() + 1e-8)), 4) if len(s) > 1 else 0.0,
                "count": len(s),
            }

    report = {
        "report_type": "pcb_factor_backtest",
        "stock_code": STOCK_CODE,
        "stock_name": "胜宏科技",
        "generated_at": datetime.now().isoformat(),
        "backtest_date": today_str,
        "data_period": {
            "start": str(df_raw.index.min().date()) if len(df_raw) > 0 else "",
            "end": str(df_raw.index.max().date()) if len(df_raw) > 0 else "",
            "trading_days": len(df_raw),
        },
        "factor_init_snapshot": {
            "source": "/opt/stock_agent/weight_snapshots/pcb_factor_init_2026-07-19.json",
            "factors": {
                k: {"weight": v[0], "direction": v[1], "description": v[2]}
                for k, v in factor_init.items()
            },
        },
        "ic_analysis": {
            "method": "Spearman rank correlation with forward 20-day return",
            "overall": factor_stats,
            "by_cycle": cycle_stats,
        },
        "directional_conflicts": conflicts,
        "adaptive_weights": adaptive_weights,
        "summary": {
            "total_factors": len(factor_init),
            "factors_with_conflict": sum(
                1 for c in conflicts.values() if c["is_conflict"]
            ),
            "up_cycle_days": cycle_stats.get("up", {}).get("days", 0),
            "down_cycle_days": cycle_stats.get("down", {}).get("days", 0),
            "overall_mean_ir": round(
                np.mean(
                    [
                        s.get("ir", 0)
                        for s in factor_stats.values()
                        if s.get("count", 0) > 0
                    ]
                ),
                4,
            )
            if factor_stats
            else 0.0,
        },
    }

    return report


# ========================================================================
#  第9步: 保存快照
# ========================================================================
def save_adaptive_weight_snapshots(
    adaptive_weights: dict, conflicts: dict, factor_init: dict
):
    """保存 up_weights.json 和 down_weights.json"""
    snap_dir = Path("/opt/stock_agent/weight_snapshots")

    for cycle in ["up", "down"]:
        key = f"{cycle}_weights"
        if key not in adaptive_weights:
            continue

        data = {
            "sector": "PCB制造",
            "stock_code": STOCK_CODE,
            "stock_name": "胜宏科技",
            "cycle_type": cycle,
            "generated_at": datetime.now().isoformat(),
            "generated_by": "pcb_factor_backtest.py",
            "weight_source": f"Adaptive IC-weighted, {cycle}-cycle only",
            "weights": adaptive_weights[key],
            "conflict_corrections": {
                k: {
                    "is_conflict": v["is_conflict"],
                    "initial_weight": v["initial_weight"],
                    "corrected_weight": v["corrected_weight"],
                    "correction_magnitude": v["correction_magnitude"],
                }
                for k, v in conflicts.items()
            },
        }

        fp = snap_dir / f"{key}_{date.today().isoformat()}.json"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Save snapshot: {fp}")


# ========================================================================
#  第10步: 打印摘要
# ========================================================================
def print_summary(report: dict):
    """打印回测结果摘要到stdout"""
    s = report["summary"]
    data_period = report["data_period"]
    ic = report["ic_analysis"]["overall"]
    cycles = report["ic_analysis"]["by_cycle"]

    print("\n" + "=" * 70)
    print(f"  PCB因子回测报告 — {report['stock_name']}({STOCK_CODE})")
    print(f"  生成日期: {report['generated_at'][:19]}")
    print("=" * 70)
    print(f"  数据周期: {data_period['start']} ~ {data_period['end']}")
    print(f"  交易日数: {data_period['trading_days']}")
    print(f"  回测因子数: {s['total_factors']}")
    print(f"  方向冲突因子: {s['factors_with_conflict']}")
    print(f"  整体平均IR: {s['overall_mean_ir']:.4f}")
    print(f"  牛市日数: {s['up_cycle_days']}  |  熊市日数: {s['down_cycle_days']}")
    print("-" * 70)
    print(f"{'因子名称':<24} {'均值IC':>8} {'IR':>8} {'正比例':>8} {'冲突':>6}")
    print("-" * 70)

    conflicts_map = {
        k: v["is_conflict"] for k, v in report["directional_conflicts"].items()
    }

    for fname in sorted(ic.keys()):
        info = ic[fname]
        conflict_flag = "⚠️" if conflicts_map.get(fname, False) else "✅"
        print(
            f"{fname:<24} {info['mean_ic']:>8.4f} {info['ir']:>8.4f} "
            f"{info['positive_pct']:>8.2%} {conflict_flag:>6}"
        )

    print("-" * 70)
    print(f"\n  周期IC统计:")
    for cycle_name in ["up", "down", "neutral"]:
        c = cycles.get(cycle_name, {})
        print(f"    {cycle_name:<8}: {c.get('days', 0)}天")

    print(f"\n  自适应权重 (前5):")
    for cycle in ["up", "down"]:
        key = f"{cycle}_weights"
        if key in report.get("adaptive_weights", {}):
            w = report["adaptive_weights"][key]
            top5 = sorted(w.items(), key=lambda x: -x[1])[:5]
            print(f"    {cycle}-cycle: ", end="")
            print(", ".join([f"{k}={v:.3f}" for k, v in top5]))

    print(f"\n  完整报告: /opt/stock_agent/reports/pcb_factor_backtest_{date.today().isoformat()}.json")
    print("=" * 70 + "\n")


# ========================================================================
#  主流程
# ========================================================================
def main():
    logger.info("=" * 60)
    logger.info("PCB因子回测引擎启动")

    # 1. 加载因子定义
    logger.info("[1/8] 加载PCB因子定义...")
    factor_init = load_factor_init()
    logger.info(f"  已加载 {len(factor_init)} 个因子")

    # 2. 查询数据
    logger.info("[2/8] 查询PG数据库...")
    df_raw = query_all_data()
    if len(df_raw) == 0:
        logger.error("无数据, 退出")
        return

    # 3. 计算因子得分
    logger.info("[3/8] 计算因子得分...")
    factor_df = compute_factors(df_raw)
    logger.info(f"  已计算 {len(factor_df.columns)} 个因子, {len(factor_df)} 天")

    # 4. 计算未来20日收益率
    logger.info("[4/8] 计算未来20日收益率...")
    forward_ret = compute_next_20d_return(df_raw)

    # 5. 计算IC序列
    logger.info("[5/8] 计算因子IC序列...")
    ic_df = compute_ic_series(factor_df, forward_ret, min_periods=20)
    logger.info(f"  IC矩阵: {ic_df.shape}")

    # 6. 周期划分
    logger.info("[6/8] 划分牛熊周期...")
    cycle_labels = classify_cycles(df_raw)
    cycle_counts = cycle_labels.value_counts()
    for k, v in cycle_counts.items():
        logger.info(f"  {k}: {v}天")

    # 7. 按周期计算IC
    ic_per_cycle = {"all": ic_df}
    for cycle_name in ["up", "down"]:
        cycle_dates = cycle_labels[cycle_labels == cycle_name].index
        mask = ic_df.index.isin(cycle_dates)
        ic_per_cycle[cycle_name] = ic_df[mask]

    # 8. 计算各因子整体IC/IR统计
    logger.info("[7/8] 计算IC/IR统计...")
    ic_summary_data = []
    for factor_name in ic_df.columns:
        s = ic_df[factor_name].dropna()
        ic_summary_data.append(
            {
                "factor": factor_name,
                "mean_ic": s.mean() if len(s) > 0 else 0.0,
                "std_ic": s.std() if len(s) > 1 else 0.0,
                "ir": compute_ir(s),
                "count": len(s),
            }
        )
    ic_summary = pd.DataFrame(ic_summary_data).set_index("factor")

    # 9. 检测方向冲突
    logger.info("[8/8] 检测方向冲突并修正...")
    conflicts = detect_and_correct_conflicts(factor_init, ic_summary)
    n_conflicts = sum(1 for c in conflicts.values() if c["is_conflict"])
    logger.info(f"  检测到 {n_conflicts} 个方向冲突因子")

    # 10. 生成自适应权重
    adaptive_weights = generate_adaptive_weights(
        ic_per_cycle, factor_init, conflicts
    )

    # 11. 生成报告
    report = generate_report(
        factor_init, factor_df, ic_df, ic_summary,
        cycle_labels, ic_per_cycle, conflicts,
        adaptive_weights, df_raw,
    )

    # 12. 保存报告
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"pcb_factor_backtest_{date.today().isoformat()}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"报告已保存: {report_path}")

    # 13. 保存权重快照
    save_adaptive_weight_snapshots(adaptive_weights, conflicts, factor_init)

    # 14. 打印摘要
    print_summary(report)

    logger.info("PCB因子回测完成 ✅")


if __name__ == "__main__":
    main()
