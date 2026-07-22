#!/usr/bin/env python3
"""
cross_compare.py — Cross-sector horizontal comparison
Compares 300476 (胜宏科技) vs 600884 (杉杉股份), 600547 (山东黄金),
002044 (美年健康), 002617 (露笑科技) across 5 dimensions.

Output: /opt/stock_agent/reports/cross_compare_{YYYY-MM-DD}.md
"""

import psycopg2
import json
from datetime import datetime, date
from collections import defaultdict
import statistics
import math
import sys
import os

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
TARGET_STOCK = "300476"
PEER_STOCKS = ["600884", "600547", "002044", "002617"]
ALL_STOCKS = [TARGET_STOCK] + PEER_STOCKS

STOCK_NAMES = {
    "300476": "胜宏科技",
    "600884": "杉杉股份",
    "600547": "山东黄金",
    "002044": "美年健康",
    "002617": "露笑科技",
}

STOCK_SECTORS = {
    "300476": "PCB制造",
    "600884": "负极材料+偏光片双龙头",
    "600547": "贵金属避险",
    "002044": "医疗政策反转",
    "002617": "碳化硅概念+光伏",
}

DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": 5432,
}

REPORT_DIR = "/opt/stock_agent/reports"

# ──────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def fetch_latest_daily(conn):
    """Fetch latest daily data for all stocks."""
    sql = """
    SELECT a.stock_code, a.ts_code, a.trade_date, a.close, a.amount,
           b.pe_ttm, b.pb, b.total_mv, b.volume_ratio
    FROM stock_daily a
    JOIN stock_daily_basic b
      ON a.stock_code = b.stock_code AND a.trade_date = b.trade_date
    WHERE a.stock_code = ANY(%s)
      AND a.trade_date = (SELECT max(trade_date) FROM stock_daily
                           WHERE stock_code = a.stock_code)
    ORDER BY a.stock_code
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ALL_STOCKS,))
        rows = cur.fetchall()
    result = {}
    for r in rows:
        result[r[0]] = {
            "ts_code": r[1],
            "trade_date": r[2],
            "close": float(r[3]) if r[3] else 0,
            "amount": float(r[4]) if r[4] else 0,
            "pe_ttm": float(r[5]) if r[5] else None,
            "pb": float(r[6]) if r[6] else None,
            "total_mv": float(r[7]) if r[7] else 0,
            "volume_ratio": float(r[8]) if r[8] else None,
        }
    return result


def fetch_10day_money_flow(conn):
    """Fetch 10-day cumulative net money flow for each stock."""
    sql = """
    SELECT stock_code, SUM(net_mf_amount) as cum_10d
    FROM (
        SELECT stock_code, net_mf_amount,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) rn
        FROM stock_money_flow
        WHERE stock_code = ANY(%s)
    ) sub
    WHERE rn <= 10
    GROUP BY stock_code
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ALL_STOCKS,))
        rows = cur.fetchall()
    return {r[0]: float(r[1]) if r[1] else 0 for r in rows}


def fetch_pe_history(conn):
    """Fetch PE_TTM history for percentile calculation."""
    sql = """
    SELECT stock_code, trade_date, pe_ttm
    FROM stock_daily_basic
    WHERE stock_code = ANY(%s) AND pe_ttm IS NOT NULL
    ORDER BY stock_code, trade_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ALL_STOCKS,))
        rows = cur.fetchall()
    hist = defaultdict(list)
    for r in rows:
        hist[r[0]].append((r[1], float(r[2])))
    return dict(hist)


def fetch_close_history(conn):
    """Fetch all close prices for BOLL calculation."""
    sql = """
    SELECT stock_code, trade_date, close
    FROM stock_daily
    WHERE stock_code = ANY(%s) AND close IS NOT NULL
    ORDER BY stock_code, trade_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ALL_STOCKS,))
        rows = cur.fetchall()
    hist = defaultdict(list)
    for r in rows:
        hist[r[0]].append((r[1], float(r[2])))
    return dict(hist)


def fetch_ma_reference(conn):
    """Fetch MA data from stock_daily (ma5, ma10, ma20)."""
    # ma columns exist but are 0 — compute from close data instead
    pass


# ──────────────────────────────────────────────────────────────
# Computation helpers
# ──────────────────────────────────────────────────────────────
def compute_boll_position(code, close_hist):
    """Compute BOLL position: where current close sits in Bollinger Bands.
    Returns (position_pct, middle, upper, lower) or (None, None, None, None).
    position_pct: 0=at lower, 50=middle, 100=upper, >100=above upper, <0=below lower
    """
    if len(close_hist) < 21:
        return None, None, None, None
    # Use 20-day window ending at latest
    prices = [c for _, c in close_hist[-20:]]
    current = close_hist[-1][1]
    ma20 = statistics.mean(prices)
    std = statistics.stdev(prices) if len(prices) > 1 else 0
    upper = ma20 + 2 * std
    lower = ma20 - 2 * std
    if upper == lower:
        pct = 50.0
    else:
        pct = (current - lower) / (upper - lower) * 100
    return round(pct, 1), round(ma20, 2), round(upper, 2), round(lower, 2)


def compute_pe_percentile(code, pe_hist, current_pe):
    """Compute where current PE sits in its historical range."""
    if not pe_hist or current_pe is None:
        return None
    values = [v for _, v in pe_hist]
    if not values:
        return None
    below = sum(1 for v in values if v <= current_pe)
    return round(below / len(values) * 100, 1)


def compute_peg(code, pe_ttm, close_hist, daily):
    """Simple PEG = PE_TTM / earnings_growth_rate_estimate.
    Growth rate approximated from 20-day avg close change annualized
    as a proxy. If unavailable, use None.
    """
    if pe_ttm is None or len(close_hist) < 20:
        return None
    # 20-day price change % as growth proxy
    recent_close = close_hist[-1][1]
    old_close = close_hist[-20][1]
    if old_close == 0:
        return None
    growth_20d = (recent_close - old_close) / old_close
    # Annualize roughly: 20 trading days ~ 1 month, so *12 for annual
    growth_annual = growth_20d * 12
    # If growth is negative or zero, PEG is undefined/infinite
    if growth_annual <= 0:
        return None
    return round(pe_ttm / (growth_annual * 100), 2)


def classify_risk_level(pe_ttm, pb, boll_pct, cum_flow_10d, pe_pctl):
    """Assign a risk label for each stock."""
    risks = []
    if pe_ttm and pe_ttm > 60:
        risks.append("高PE")
    if pb and pb > 5:
        risks.append("高PB")
    if boll_pct and boll_pct > 90:
        risks.append("超买(BOLL上轨)")
    if boll_pct and boll_pct < 10:
        risks.append("超卖(BOLL下轨)")
    if cum_flow_10d and cum_flow_10d < -50000:
        risks.append("资金大幅流出")
    if pe_pctl and pe_pctl > 80:
        risks.append("PE历史高分位")
    if pe_pctl and pe_pctl < 20:
        risks.append("PE历史低分位")
    return risks if risks else ["正常"]


def get_primary_factors(code):
    """Return primary factors for each stock based on training case refs.
    Only 300476 has a formal training case; others get sector-based factors."""
    factors = {
        "300476": [
            ("算力PCB龙头", 0.15),
            ("客户集中度", -0.12),
            ("毛利率趋势", 0.12),
            ("营收增速", 0.10),
            ("资本开支强度", -0.08),
            ("应收周转", -0.08),
            ("存货周转", 0.07),
            ("原材料价格", -0.07),
            ("海外产能", 0.06),
            ("研发投入", 0.06),
        ],
        "600884": [
            ("负极材料龙头", 0.14),
            ("偏光片双主业", 0.12),
            ("新能源车需求", 0.12),
            ("原材料成本(锂/石墨)", -0.10),
            ("产能扩张", 0.09),
            ("客户结构", 0.08),
            ("毛利率趋势", 0.08),
            ("应收账款质量", -0.07),
            ("研发强度", 0.06),
        ],
        "600547": [
            ("金价跟随因子", 0.18),
            ("避险情绪/地缘", 0.15),
            ("矿产金成本", -0.12),
            ("储量/产量增长", 0.11),
            ("美元指数负相关", -0.10),
            ("毛利率(金价-成本)", 0.09),
            ("矿山运营效率", 0.08),
            ("资本开支", -0.07),
        ],
        "002044": [
            ("体检行业龙头", 0.14),
            ("政策反转预期", 0.13),
            ("体检量/客单价", 0.12),
            ("门店扩张", 0.11),
            ("毛利率改善", 0.10),
            ("商誉减值风险", -0.10),
            ("医保政策", 0.08),
            ("线上渗透率", 0.07),
        ],
        "002617": [
            ("碳化硅衬底", 0.15),
            ("光伏业务", 0.13),
            ("新能源车配套", 0.11),
            ("技术突破(8英寸SiC)", 0.10),
            ("产能爬坡", 0.09),
            ("下游需求景气", 0.09),
            ("竞争格局", -0.08),
            ("资金消耗/负债", -0.07),
        ],
    }
    return factors.get(code, [])


def compute_entry_score(code, daily, boll_pct, cum_flow_10d, pe_pctl, peg):
    """Score-based entry condition assessment. 0-100 scale.
    Higher = more attractive entry."""
    score = 50  # neutral baseline

    # PE percentile: low percentile = cheaper = score up
    if pe_pctl is not None:
        if pe_pctl < 20:
            score += 20
        elif pe_pctl < 40:
            score += 10
        elif pe_pctl > 80:
            score -= 20
        elif pe_pctl > 60:
            score -= 10

    # BOLL position: near lower band = oversold = score up
    if boll_pct is not None:
        if boll_pct < 15:
            score += 15
        elif boll_pct < 30:
            score += 8
        elif boll_pct > 85:
            score -= 15
        elif boll_pct > 70:
            score -= 8

    # Money flow: positive cumulative = strength
    if cum_flow_10d is not None:
        if cum_flow_10d > 50000:
            score += 12
        elif cum_flow_10d > 0:
            score += 5
        elif cum_flow_10d < -100000:
            score -= 15
        elif cum_flow_10d < -30000:
            score -= 8

    # Volume ratio: >1 = active = slight boost
    vr = daily.get("volume_ratio")
    if vr is not None:
        if vr > 1.5:
            score += 5
        elif vr > 1.0:
            score += 2
        elif vr < 0.5:
            score -= 3

    return max(0, min(100, score))


def entry_label(score):
    if score >= 70:
        return "★★★ 强烈推荐"
    elif score >= 55:
        return "★★ 推荐关注"
    elif score >= 40:
        return "★ 中性观察"
    elif score >= 25:
        return "⚠ 谨慎观望"
    else:
        return "✗ 回避"


def format_num(v, decimals=2, suffix=""):
    if v is None:
        return "N/A"
    if suffix == "亿" and abs(v) >= 1e8:
        return f"{v/1e8:.{decimals}f}亿"
    if suffix == "万" and abs(v) >= 1e4:
        return f"{v/1e4:.{decimals}f}万"
    return f"{v:.{decimals}f}"


def format_large_num(v):
    """Format large numbers. DB stores total_mv in 万元.
    Convert: 万元->亿元 (/10000), or 万元 display."""
    if v is None:
        return "N/A"
    if abs(v) >= 10000:
        return f"{v/10000:.2f}亿"
    elif abs(v) >= 1:
        return f"{v:.2f}万"
    return f"{v:.2f}"


# ──────────────────────────────────────────────────────────────
# Report generation
# ──────────────────────────────────────────────────────────────
def generate_report(all_data, report_date_str):
    """Generate the full comparison markdown report."""
    lines = []
    lines.append(f"# 跨行业横向对比报告 — {report_date_str}")
    lines.append("")
    lines.append(f"> 核心标的: **{STOCK_NAMES[TARGET_STOCK]} ({TARGET_STOCK})**")
    lines.append(f"> 对比组: {', '.join(f'{STOCK_NAMES[s]}({s})' for s in PEER_STOCKS)}")
    lines.append(f"> 数据日期: {all_data[TARGET_STOCK]['trade_date']}")
    lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ── Dimension 1: Factor Dimension ──
    lines.append("---")
    lines.append("## 一、因子维度 — 各标的核心驱动因子")
    lines.append("")
    lines.append("| 标的 | 行业 | 核心因子(IC权重) |")
    lines.append("|:----|:----|:----------------|")
    for code in ALL_STOCKS:
        factors = get_primary_factors(code)
        factors_str = "; ".join([f"{name}({w})" for name, w in factors[:5]])
        sector = STOCK_SECTORS.get(code, "N/A")
        lines.append(f"| **{STOCK_NAMES[code]}**({code}) | {sector} | {factors_str} |")
    lines.append("")

    # ── Dimension 2: Risk Dimension ──
    lines.append("---")
    lines.append("## 二、风险维度 — 估值/资金/技术对比")
    lines.append("")

    # Compute PE percentile from history
    conn = get_conn()
    pe_hist = fetch_pe_history(conn)
    close_hist = fetch_close_history(conn)
    conn.close()

    lines.append("| 指标 | " + " | ".join([f"**{STOCK_NAMES[c]}**({c})" for c in ALL_STOCKS]) + " |")
    lines.append("|:----|" + "|".join([":---:" for _ in ALL_STOCKS]) + "|")

    # PE_TTM row
    pe_row = "| PE_TTM |"
    for c in ALL_STOCKS:
        d = all_data[c]
        pe_row += f" {d['pe_ttm'] if d['pe_ttm'] else 'N/A'} |"
    lines.append(pe_row)

    # PB row
    pb_row = "| PB |"
    for c in ALL_STOCKS:
        d = all_data[c]
        pb_row += f" {d['pb'] if d['pb'] else 'N/A'} |"
    lines.append(pb_row)

    # Total MV row
    mv_row = "| 总市值 |"
    for c in ALL_STOCKS:
        d = all_data[c]
        mv_row += f" {format_large_num(d['total_mv'])} |"
    lines.append(mv_row)

    # Volume ratio
    vr_row = "| 量比 |"
    for c in ALL_STOCKS:
        d = all_data[c]
        vr_val = d.get("volume_ratio")
        vr_row += f" {vr_val if vr_val else 'N/A'} |"
    lines.append(vr_row)

    # Debt ratio — not available in DB
    debt_row = "| 资产负债率 |"
    for c in ALL_STOCKS:
        debt_row += " N/A(暂无数据) |"
    lines.append(debt_row)

    # 10-day cumulative money flow
    flow_10d = fetch_10day_money_flow(conn2 := get_conn())
    conn2.close()
    flow_row = "| 10日累计资金净流(亿元) |"
    for c in ALL_STOCKS:
        v = flow_10d.get(c)
        flow_row += f" {format_large_num(v) if v else 'N/A'} |"
    lines.append(flow_row)

    # BOLL position
    boll_data = {}
    boll_row = "| BOLL位置(当前/中轨/上/下) |"
    for c in ALL_STOCKS:
        ch = close_hist.get(c, [])
        boll_pct, mid, up, low = compute_boll_position(c, ch)
        boll_data[c] = {"pct": boll_pct, "mid": mid, "up": up, "low": low}
        if boll_pct is not None:
            boll_row += f" {boll_pct}%(中{mid}/上{up}/下{low}) |"
        else:
            boll_row += " N/A(数据不足) |"
    lines.append(boll_row)

    lines.append("")

    # Risk summary
    lines.append("### 风险标签汇总")
    lines.append("")
    lines.append("| 标的 | 风险标签 |")
    lines.append("|:----|:--------|")
    for c in ALL_STOCKS:
        d = all_data[c]
        pe_pctl = compute_pe_percentile(c, pe_hist.get(c, []), d["pe_ttm"])
        risks = classify_risk_level(
            d["pe_ttm"], d["pb"],
            boll_data[c].get("pct"),
            flow_10d.get(c),
            pe_pctl,
        )
        lines.append(f"| **{STOCK_NAMES[c]}**({c}) | {', '.join(risks)} |")
    lines.append("")

    # ── Dimension 3: Valuation Dimension ──
    lines.append("---")
    lines.append("## 三、估值维度 — PE历史百分位 & PEG")
    lines.append("")
    lines.append("| 标的 | 当前PE_TTM | PE历史百分位 | 估值水位 | PEG(估) |")
    lines.append("|:----|:---------:|:-----------:|:--------:|:------:|")
    for c in ALL_STOCKS:
        d = all_data[c]
        pctl = compute_pe_percentile(c, pe_hist.get(c, []), d["pe_ttm"])
        ch = close_hist.get(c, [])
        peg = compute_peg(c, d["pe_ttm"], ch, d)

        if pctl is not None:
            if pctl >= 80:
                level = "🔴 高估"
            elif pctl >= 60:
                level = "🟡 偏高"
            elif pctl >= 40:
                level = "🟢 适中"
            elif pctl >= 20:
                level = "🟢 偏低"
            else:
                level = "🔵 低估"
        else:
            level = "N/A"

        lines.append(
            f"| **{STOCK_NAMES[c]}**({c}) | "
            f"{d['pe_ttm'] if d['pe_ttm'] else 'N/A'} | "
            f"{pctl if pctl is not None else 'N/A'}% | "
            f"{level} | "
            f"{peg if peg else 'N/A'} |"
        )
    lines.append("")

    # ── Dimension 4: Position Recommendations ──
    lines.append("---")
    lines.append("## 四、仓位建议 — 入场条件 & 评分")
    lines.append("")
    lines.append("| 标的 | 综合评分 | 建议 | 入场条件 | 仓位上限 |")
    lines.append("|:----|:-------:|:----|:---------|:-------:|")
    for c in ALL_STOCKS:
        d = all_data[c]
        pctl = compute_pe_percentile(c, pe_hist.get(c, []), d["pe_ttm"])
        ch = close_hist.get(c, [])
        boll_pct, _, _, _ = compute_boll_position(c, ch)
        peg = compute_peg(c, d["pe_ttm"], ch, d)
        score = compute_entry_score(c, d, boll_pct, flow_10d.get(c), pctl, peg)
        label = entry_label(score)

        # Entry conditions
        conditions = []
        if boll_pct is not None and boll_pct < 25:
            conditions.append("BOLL下轨超卖区域")
        if pctl is not None and pctl < 30:
            conditions.append("PE历史低位")
        flow_v = flow_10d.get(c)
        if flow_v is not None and flow_v > 0:
            conditions.append("10日资金净流入")
        if flow_v is not None and flow_v > 100000:
            conditions.append("大额资金流入")
        vr = d.get("volume_ratio")
        if vr is not None and vr > 1.2:
            conditions.append("放量信号")

        entry_str = "; ".join(conditions) if conditions else "暂无明确信号,观望"

        # Position cap
        if c == "300476":
            pos_cap = "12%"
        else:
            if score >= 70:
                pos_cap = "10%"
            elif score >= 55:
                pos_cap = "7%"
            elif score >= 40:
                pos_cap = "5%"
            else:
                pos_cap = "0%(观望)"

        lines.append(
            f"| **{STOCK_NAMES[c]}**({c}) | "
            f"{score}/100 | {label} | "
            f"{entry_str} | {pos_cap} |"
        )
    lines.append("")

    # ── Dimension 5: Cross-Correlation ──
    lines.append("---")
    lines.append("## 五、交叉相关性 — 跨行业联动风险检查")
    lines.append("")
    lines.append("### 5.1 行业归属")
    lines.append("")
    lines.append("| 标的 | 行业 | 相关性说明 |")
    lines.append("|:----|:----|:----------|")
    for c in ALL_STOCKS:
        sector = STOCK_SECTORS.get(c, "N/A")
        if c == "300476":
            corr_note = "**(核心标的)** PCB制造，属算力硬件中游"
        elif c == "600884":
            corr_note = "新能源材料+偏光片，与PCB无直接关联"
        elif c == "600547":
            corr_note = "贵金属避险，与科技股负相关，分散组合风险"
        elif c == "002044":
            corr_note = "医疗体检，消费属性，与科技/周期低相关"
        elif c == "002617":
            corr_note = "碳化硅+光伏，新能源赛道，与PCB间接关联(电子材料)"
        else:
            corr_note = ""
        lines.append(f"| **{STOCK_NAMES[c]}**({c}) | {sector} | {corr_note} |")
    lines.append("")

    # Price correlation matrix (close price over common period)
    lines.append("### 5.2 价格相关性矩阵(近60日)")
    lines.append("")
    # Compute correlation
    common_dates = None
    price_series = {}
    for c in ALL_STOCKS:
        ch = close_hist.get(c, [])
        # Use last 60 data points
        recent = ch[-60:] if len(ch) > 60 else ch
        series = {}
        for dt, pr in recent:
            series[dt] = pr
        price_series[c] = series
        if common_dates is None:
            common_dates = set(series.keys())
        else:
            common_dates &= set(series.keys())

    common_dates = sorted(common_dates)

    def correlation(x, y):
        n = len(x)
        if n < 3:
            return None
        mean_x, mean_y = statistics.mean(x), statistics.mean(y)
        num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        den = math.sqrt(sum((xi - mean_x) ** 2 for xi in x)) * \
              math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
        return num / den if den != 0 else 0

    # Headers
    lines.append("| 标的 | " + " | ".join([STOCK_NAMES[c] for c in ALL_STOCKS]) + " |")
    lines.append("|:----|" + "|".join([":---:" for _ in ALL_STOCKS]) + "|")
    for c1 in ALL_STOCKS:
        row = f"| **{STOCK_NAMES[c1]}**({c1}) |"
        for c2 in ALL_STOCKS:
            if c1 == c2:
                row += " 1.00 |"
            else:
                p1 = [price_series[c1].get(d) for d in common_dates]
                p2 = [price_series[c2].get(d) for d in common_dates]
                valid = [(a, b) for a, b in zip(p1, p2) if a is not None and b is not None]
                if len(valid) < 3:
                    row += " N/A |"
                else:
                    corr = correlation([v[0] for v in valid], [v[1] for v in valid])
                    corr_label = f"{corr:.2f}"
                    row += f" {corr_label} |"
        lines.append(row)
    lines.append("")

    # Interpretation
    lines.append("### 5.3 分散化评估")
    lines.append("")
    high_corr_pairs = []
    for i, c1 in enumerate(ALL_STOCKS):
        for c2 in ALL_STOCKS[i+1:]:
            p1 = [price_series[c1].get(d) for d in common_dates]
            p2 = [price_series[c2].get(d) for d in common_dates]
            valid = [(a, b) for a, b in zip(p1, p2) if a is not None and b is not None]
            if len(valid) >= 3:
                corr = correlation([v[0] for v in valid], [v[1] for v in valid])
                if corr > 0.6:
                    high_corr_pairs.append((c1, c2, corr))

    lines.append(f"- **分析区间**: {common_dates[0]} ~ {common_dates[-1]} (共{len(common_dates)}个交易日)" if common_dates else "- N/A")
    if high_corr_pairs:
        lines.append("- ⚠️ **高相关性组合** (r>0.6):")
        for c1, c2, corr in high_corr_pairs:
            lines.append(f"  - {STOCK_NAMES[c1]}({c1}) ↔ {STOCK_NAMES[c2]}({c2}): r={corr:.2f} — 分散效果较差")
    else:
        lines.append("- ✅ 各标的间相关性普遍较低，分散化效果较好")
    lines.append("")

    # ── Summary ──
    lines.append("---")
    lines.append("## 六、综合结论")
    lines.append("")

    # Scoring summary
    scores = {}
    for c in ALL_STOCKS:
        d = all_data[c]
        pctl = compute_pe_percentile(c, pe_hist.get(c, []), d["pe_ttm"])
        ch = close_hist.get(c, [])
        boll_pct, _, _, _ = compute_boll_position(c, ch)
        peg_tmp = compute_peg(c, d["pe_ttm"], ch, d)
        scores[c] = compute_entry_score(c, d, boll_pct, flow_10d.get(c), pctl, peg_tmp)

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    lines.append("| 排名 | 标的 | 综合评分 | 建议 |")
    lines.append("|:---:|:----|:-------:|:----|")
    for rank, (code, sc) in enumerate(sorted_scores, 1):
        label = entry_label(sc)
        prefix = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"  #{rank}"))
        lines.append(f"| {prefix} | **{STOCK_NAMES[code]}**({code}) | {sc}/100 | {label} |")
    lines.append("")

    # Target stock specific
    target_score = scores[TARGET_STOCK]
    lines.append(f"### 核心标的({STOCK_NAMES[TARGET_STOCK]})定位")
    lines.append("")
    if target_score >= 55:
        lines.append(f"- ✅ **{STOCK_NAMES[TARGET_STOCK]}** 在当前对比组中评分排名靠前，具备一定的相对配置价值。")
    elif target_score >= 40:
        lines.append(f"- 🟡 **{STOCK_NAMES[TARGET_STOCK]}** 评分中等，建议等待更好的入场时机。")
    else:
        lines.append(f"- 🔴 **{STOCK_NAMES[TARGET_STOCK]}** 当前评分偏低，应予以回避或等待回调后重新评估。")
    lines.append("")
    lines.append("> ⚠️ **免责声明**: 本报告仅基于公开数据进行量化分析，不构成任何投资建议。")
    lines.append("> 数据来源: Tushare Pro / 腾讯行情API")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Stdout summary
# ──────────────────────────────────────────────────────────────
def print_summary(all_data, report_date_str):
    """Print a concise summary to stdout."""
    conn = get_conn()
    pe_hist = fetch_pe_history(conn)
    close_hist = fetch_close_history(conn)
    flow_10d = fetch_10day_money_flow(conn)
    conn.close()

    print("\n" + "=" * 72)
    print(f"  CROSS-SECTOR COMPARISON SUMMARY — {report_date_str}")
    print(f"  Core: {STOCK_NAMES[TARGET_STOCK]}({TARGET_STOCK}) vs {', '.join(PEER_STOCKS)}")
    print("=" * 72)

    # Basic data table
    print(f"\n{'Stock':<12} {'Close':>8} {'PE_TTM':>8} {'PB':>8} {'10dFlow(亿)':>12} {'Score':>6} {'Suggestion':<16}")
    print("-" * 72)
    scores = {}
    for c in ALL_STOCKS:
        d = all_data[c]
        pctl = compute_pe_percentile(c, pe_hist.get(c, []), d["pe_ttm"])
        ch = close_hist.get(c, [])
        boll_pct, _, _, _ = compute_boll_position(c, ch)
        peg_tmp = compute_peg(c, d["pe_ttm"], ch, d)
        sc = compute_entry_score(c, d, boll_pct, flow_10d.get(c), pctl, peg_tmp)
        scores[c] = sc
        label = entry_label(sc)
        name = f"{STOCK_NAMES[c]}({c})"
        flow = flow_10d.get(c, 0)
        close_v = d["close"]
        pe_v = d["pe_ttm"] if d["pe_ttm"] else 0
        pb_v = d["pb"] if d["pb"] else 0
        print(f"{name:<12} {close_v:>8.2f} {pe_v:>8.2f} {pb_v:>8.2f} {flow/1e4:>10.2f}亿  {sc:>3}/100 {label:<16}")

    print("-" * 72)

    # Ranking
    sorted_sc = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    print(f"\n  Ranking by composite score:")
    for i, (c, sc) in enumerate(sorted_sc, 1):
        print(f"    #{i} {STOCK_NAMES[c]}({c}): {sc}/100 — {entry_label(sc)}")

    d_target = all_data[TARGET_STOCK]
    print(f"\n  {STOCK_NAMES[TARGET_STOCK]} key metrics:")
    print(f"    Close: {d_target['close']}")
    print(f"    PE_TTM: {d_target['pe_ttm']}")
    print(f"    PB: {d_target['pb']}")
    print(f"    Total MV: {format_large_num(d_target['total_mv'])}")
    flow_v = flow_10d.get(TARGET_STOCK, 0)
    flow_str = f"{flow_v/10000:.2f}亿" if abs(flow_v) >= 10000 else f"{flow_v:.2f}万"
    print(f"    10d Flow: {flow_str}")
    pe_pctl = compute_pe_percentile(TARGET_STOCK, pe_hist.get(TARGET_STOCK, []), d_target["pe_ttm"])
    print(f"    PE Percentile: {pe_pctl}%" if pe_pctl else "    PE Percentile: N/A")

    print(f"\n  Full report saved to: {REPORT_DIR}/cross_compare_{report_date_str}.md")
    print("=" * 72 + "\n")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_date_str = date.today().strftime("%Y-%m-%d")

    conn = get_conn()
    all_data = fetch_latest_daily(conn)
    conn.close()

    if not all_data:
        print("ERROR: No data fetched from database.", file=sys.stderr)
        sys.exit(1)

    report = generate_report(all_data, report_date_str)

    # Write report
    report_path = os.path.join(REPORT_DIR, f"cross_compare_{report_date_str}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report written to {report_path}")

    # Summary to stdout
    print_summary(all_data, report_date_str)


if __name__ == "__main__":
    main()
