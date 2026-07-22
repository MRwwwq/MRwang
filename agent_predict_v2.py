# -*- coding: utf-8 -*-
"""
agent_predict_v2.py — 沪深股票量化评分系统 v2
功能：15项评分函数 + 多情景压力推演 + stream_run批量写入
"""

import pandas as pd
import numpy as np
import gc
import sys
from config import pg_engine, TARGET_CODES
from fundamental_data import FUNDAMENTAL_DATA
from datetime import datetime, timedelta
from sqlalchemy import text

# ── 自适应自我修正 ──
try:
    from self_correct import correct_scoring, correct_trading, run_full_check
    SELF_CORRECT_ACTIVE = True
except ImportError:
    SELF_CORRECT_ACTIVE = False
    def correct_scoring(code, r): return r
    def correct_trading(code, r): return r
    def run_full_check(code=None): return {}


# ==================== 1. PE/PB ====================

def get_pe_pb(code):
    """从market_daily取最新PE/PB. market_daily.stock_code为纯数字(无后缀)"""
    raw = code.replace(".SH", "").replace(".SZ", "")
    sql = text("""
        SELECT pe_ttm, pb FROM market_daily
        WHERE stock_code = :code AND pe_ttm > 0
        ORDER BY trade_date DESC LIMIT 1
    """)
    md = pd.read_sql(sql, pg_engine, params={"code": raw})
    if md.empty:
        del md
        return None, None
    pe = float(md["pe_ttm"].iloc[0])
    pb = float(md["pb"].iloc[0])
    del md
    return pe, pb


# ==================== 2. MA数据 ====================

def _ensure_tscode(code):
    """将纯数字代码转为带后缀格式 (6xx/9xx→.SH, 其他→.SZ)"""
    c = str(code).replace(".SH","").replace(".SZ","")
    suffix = ".SH" if c.startswith("6") or c.startswith("9") else ".SZ"
    return c + suffix

def get_ma_data(code, limit=60):
    """取最近limit条日线(含MA5/MA10/MA20)，时间正序返回"""
    tsc = _ensure_tscode(code)
    sql = text("""
        SELECT trade_date, close, high, low, vol, pct_chg,
               ma5, ma10, ma20, amount, amplitude
        FROM stock_daily
        WHERE ts_code = :code
        ORDER BY trade_date DESC
        LIMIT :lim
    """)
    df = pd.read_sql(sql, pg_engine, params={"code": tsc, "lim": limit})
    if df.empty:
        return df
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


# ==================== 3. 量价评分 ====================

def calc_volume_price_score(row, k60):
    """
    基于基准50分的量价调整
    PB折价(<=2.5x): +4
    PE溢价(>30x): 动态扣分
    振幅收窄: +6
    放量反弹+4 / 收涨+2
    弱反弹-6
    """
    reasons = []
    close = float(row["close"])
    pct = float(row["pct_chg"])
    vol = float(row["vol"])
    high = float(row["high"])
    low = float(row["low"])

    # 量比（当日量 / 近5日均量）
    avg5 = float(k60["vol"].tail(5).mean()) if len(k60) >= 5 else float(k60["vol"].mean())
    vr = vol / avg5 if avg5 > 0 else 1.0

    # 当日振幅%
    amp = (high - low) / close * 100 if close > 0 else 0.0

    # --- PB折价 ---
    pe, pb = get_pe_pb(row.get("ts_code", ""))
    score = 0
    if pb is not None and pb < 2.5:
        add = min(4, round((2.5 - pb) / 1.5 * 4))
        score += add
        reasons.append(f"PB折价+{add}({pb}x)")

    # --- PE溢价动态扣分 ---
    if pe is not None and pe > 30:
        deduct = min(12, round((pe - 30) / 30 * 12))
        score -= deduct
        reasons.append(f"PE溢价-{deduct}({round(pe)}x)")

    # --- 振幅收窄 ---
    if len(k60) >= 10:
        t10 = k60.tail(10)
        h7 = t10.head(7)
        avg_amp_7 = float(((h7["high"] - h7["low"]) / h7["close"] * 100).mean()) if len(h7) > 0 else 0
        if avg_amp_7 > 0 and amp < avg_amp_7 * 0.8:
            score += 6
            reasons.append(f"振幅收窄+6({round(amp,1)}%<{round(avg_amp_7*0.8,1)}%)")
        elif avg_amp_7 > 0 and amp < avg_amp_7:
            score += 3
            reasons.append(f"振幅微收窄+3")

    # --- 放量反弹 / 收涨 / 弱反弹 ---
    if 1.0 < vr < 1.5 and pct > 0:
        score += 4
        reasons.append(f"放量反弹+4(vr={round(vr,2)})")
    elif pct > 0:
        score += 2
        reasons.append("收涨+2")

    if pct > 0 and vr < 1.5:
        score -= 6
        reasons.append(f"弱反弹-6(vr={round(vr,2)})")
    return score, reasons


# ==================== 4. MA扣分 ====================

def calc_ma_deduct(row):
    """
    MA5以下: -4
    MA10以下: -5
    MA20以下: -7
    空头排列额外: -3
    """
    close = float(row["close"])
    ma5 = float(row["ma5"]) if not pd.isna(row.get("ma5")) else None
    ma10 = float(row["ma10"]) if not pd.isna(row.get("ma10")) else None
    ma20 = float(row["ma20"]) if not pd.isna(row.get("ma20")) else None

    deduct = 0
    reasons = []
    if ma5 is not None and close < ma5:
        deduct -= 4
        reasons.append("MA5-4")
    if ma10 is not None and close < ma10:
        deduct -= 5
        reasons.append("MA10-5")
    if ma20 is not None and close < ma20:
        deduct -= 7
        reasons.append("MA20-7")
    if (ma5 is not None and ma10 is not None and ma20 is not None and
            close < ma5 < ma10 < ma20):
        deduct -= 3
        reasons.append("空排-3")
    return deduct, reasons


# ==================== 5. 盈余评分 ====================

def calc_earnings_score(code, fd):
    """
    2024亏损+2025扭亏=+6
    2026Q1净利>2亿=+10
    中报范围±3(预留)
    """
    if not fd:
        return 0, ["盈余数据缺失"]
    e = fd.get("earnings", {})
    score = 0
    reasons = []
    np_2024 = e.get("np_2024", 0)
    np_2025 = e.get("np_2025", 0)
    np_2026q1 = e.get("np_2026Q1", 0)

    if np_2024 < 0 and np_2025 > 0:
        score += 6
        reasons.append(f"扭亏+6(24年{np_2024}亿->25年{np_2025}亿)")
    if np_2026q1 > 2:
        score += 10
        reasons.append(f"高弹性+10({np_2026q1}亿)")
    # 中报预留
    reasons.append("中报+/-3(待验证)")
    return score, reasons


# ==================== 6. 资产评分 ====================

def calc_asset_score(code, fd):
    """
    应收(<=10%): +3
    财务费(>1.5%): -5
    商誉(<=10%): +2
    现金流(positive): +1
    """
    if not fd:
        return 0, ["资产数据缺失"]
    bs = fd.get("balance_sheet", {})
    score = 0
    reasons = []
    ar = bs.get("ar_ratio", 100)
    fr = bs.get("finance_rate", 0)
    gw = bs.get("goodwill_ratio", 100)
    ocf = bs.get("ocf_status", "")

    if ar <= 10:
        score += 3
        reasons.append(f"应收低+3({ar}%)")
    if fr > 1.5:
        score -= 5
        reasons.append(f"财务费-5({fr}%)")
    if gw <= 10:
        score += 2
        reasons.append(f"商誉低+2({gw}%)")
    if ocf == "positive":
        score += 1
        reasons.append("现金流+1")
    return score, reasons


# ==================== 7. 事件评分 ====================

def calc_event_score(code, fd):
    """tier1/tier2/tier3事件累加"""
    if not fd:
        return 0, ["事件数据缺失"]
    ev = fd.get("events", {})
    score = 0
    reasons = []
    for item, val in ev.get("tier1", []):
        score += val
        reasons.append(f"{item}{val:+d}")
    for item, val in ev.get("tier2", []):
        score += val
        reasons.append(f"{item}{val:+d}")
    for item, val in ev.get("tier3", []):
        score += val
        reasons.append(f"{item}{val:+d}")
    return score, reasons


# ==================== 8. 五维贡献 ====================

def calc_five_dim_contrib(fd):
    """
    五维模型: sum(base+adj), contrib = final - 38
    默认38为基准线
    """
    if not fd:
        return 0, 0, ["五维缺失"]
    f5 = fd.get("five_dim", {})
    base = sum(v[0] for v in f5.values())
    adj = sum(v[1] for v in f5.values())
    final = base + adj
    contrib = final - 38
    dims = [f"{k}({v[0]},{v[1]:+d})" for k, v in f5.items()]
    reasons = [f"五维{final}/100->{contrib:+d}({'/'.join(dims)})"]
    return final, contrib, reasons


# ==================== 9. 基本面综合评分 ====================

def calc_fundamental_score(code, fd):
    """合并earnings+asset+event+five_dim -> total_add + reasons"""
    ea_s, ea_r = calc_earnings_score(code, fd)
    as_s, as_r = calc_asset_score(code, fd)
    ev_s, ev_r = calc_event_score(code, fd)
    fd_v, fd_c, fd_r = calc_five_dim_contrib(fd)

    total_add = ea_s + as_s + ev_s + fd_c
    all_reasons = ea_r + as_r + ev_r + fd_r
    return total_add, all_reasons


# ==================== 10. 情绪评分 ====================

def calc_sentiment_score(code, fd):
    """
    前瞻PE: +6
    PE分位: +4
    雪球/北向: 缺失计0
    """
    if not fd:
        return 0, ["情绪数据缺失"]
    s = fd.get("sentiment", {})
    score = 0
    reasons = []

    fpe_score = s.get("forward_pe_score", 0)
    if fpe_score:
        score += fpe_score
        reasons.append(f"前瞻PE~{s.get('forward_pe','?')}x+{fpe_score}")

    pct_score = s.get("pe_percentile_score", 0)
    if pct_score:
        score += pct_score
        reasons.append(f"PE分位{s.get('pe_percentile_1y','?')}%+{pct_score}")

    if s.get("xueqiu_sentiment") is None:
        reasons.append("雪球0(缺失)")
    if s.get("northbound") is None:
        reasons.append("北向0(缺失)")
    return score, reasons


# ==================== 11. 资金流评分 ====================

def calc_flow_score(code):
    """
    10日净流: <-5亿->-12, max_inflow<5000->-3, min_outflow<-15000->-4
    big_order counterbalance: bg>0->score=min(7,bg//4000),
      sb<0->max(-6,sb//5000), sm<0->max(-6,sm//5000)
    total = flow_score + big_score + sb_score + sm_score
    """
    sql = text("""
        SELECT net_mf_amount, buy_elg_vol, sell_elg_vol,
               buy_lg_vol, sell_lg_vol,
               buy_md_vol, sell_md_vol,
               buy_sm_vol, sell_sm_vol
        FROM stock_money_flow
        WHERE ts_code = :code
        ORDER BY trade_date DESC LIMIT 10
    """)
    mf = pd.read_sql(sql, pg_engine, params={"code": code})
    if mf.empty:
        del mf
        return 0, ["资金缺失0"]

    reasons = []
    net_sum = float(mf["net_mf_amount"].sum())
    max_in = float(mf["net_mf_amount"].max())
    min_out = float(mf["net_mf_amount"].min())

    # 净流评分
    flow_score = 0
    net_yi = net_sum / 10000
    if net_yi < -5:
        flow_score -= 12
        reasons.append(f"净流{round(net_yi,1)}亿-12")
    if max_in < 5000:
        flow_score -= 3
        reasons.append("流入弱-3")
    if min_out < -15000:
        flow_score -= 4
        reasons.append("恐慌-4")

    # 大单对冲 (取最近1条)
    last = mf.iloc[0]
    bg = float(last["buy_elg_vol"] - last["sell_elg_vol"])  # 特大单净
    sb = float(last["buy_lg_vol"] - last["sell_lg_vol"])     # 大单净
    sm = float(last["buy_md_vol"] - last["sell_md_vol"])     # 中单净

    bg_score = 0
    if bg > 0:
        bg_score = min(7, int(bg // 4000))
        if bg_score:
            reasons.append(f"特大单对冲+{bg_score}")
    sb_score = 0
    if sb < 0:
        sb_score = max(-6, int(sb // 5000))
        if sb_score:
            reasons.append(f"大单流出{sb_score}")
    sm_score = 0
    if sm < 0:
        sm_score = max(-6, int(sm // 5000))
        if sm_score:
            reasons.append(f"中单流出{sm_score}")

    total = flow_score + bg_score + sb_score + sm_score
    del mf
    return total, reasons


# ==================== 12. 板块评分 ====================

def calc_sector_score(fd):
    """
    固定: momentum-3, anode-4, polarizer+8, wall+7, state+3 = 11
    (基于600884.SH杉杉/偏光片/国资属性)
    """
    score = -3 + (-4) + 8 + 7 + 3  # = 11
    reasons = ["板块-3", "负极-4", "偏光片+8", "壁垒+7", "国资+3"]
    return score, reasons


# ==================== 13. 风险扣分 ====================

def calc_risk_deduct(fd):
    """
    PE*1.3: -6.5, flow: -5, tech: -5, anode: -2, finance: -2
    ar: 0, fx: 0 = -20.5
    """
    score = -6.5 + (-5) + (-5) + (-2) + (-2) + 0 + 0
    reasons = ["PE*1.3:-6.5", "flow:-5", "tech:-5", "anode:-2",
               "finance:-2", "ar:0", "fx:0"]
    return score, reasons


# ==================== 14. 多情景压力推演 ====================

def get_moneyflow_summary(code, days=10):
    """获取最近days日资金流汇总数据，返回净额列表+累计"""
    sql = text("""
        SELECT net_mf_amount FROM stock_money_flow
        WHERE ts_code = :code
        ORDER BY trade_date DESC LIMIT :lim
    """)
    mf = pd.read_sql(sql, pg_engine, params={"code": code, "lim": days})
    if mf.empty:
        return [], 0.0
    vals = [float(x) for x in mf["net_mf_amount"].tolist()]
    total = sum(vals) / 10000  # 转为亿元
    del mf
    return vals, total


def scenario_stress_test(code, trade_date, res):
    """
    多情景压力推演 + 极端行情压力测试
    三类情景: 乐观(中报6亿+资金流入+均线修复) / 中性(中报5亿维持) / 悲观(中报<5亿+流出+破前低)
    三种极端: 连续10日大额流出 / 板块暴跌 / 业绩暴雷
    输出: 各情景评分+风控方案
    """
    # --- 提取当前评分结果 ---
    base = float(res.get("base_score", 50))
    ma_d = int(res.get("ma_deduct", 0))
    fa = int(res.get("fundamental_add", 0))
    sa = int(res.get("sentiment_add", 0))
    fd = int(res.get("flow_deduct", 0))
    sc = int(res.get("sector_add", 0))
    cur_confidence = float(res.get("confidence", 50))
    cur_position = res.get("position", "0%")
    cur_pred = int(res.get("predict_result", 0))

    # --- 获取最新行情 ---
    k60 = get_ma_data(code, limit=60)
    if k60.empty:
        return {"error": "stress_test: no kline data"}
    row = k60.iloc[-1]
    close = float(row["close"])
    ma5 = float(row["ma5"]) if not pd.isna(row.get("ma5")) else close
    ma10 = float(row["ma10"]) if not pd.isna(row.get("ma10")) else close
    ma20 = float(row["ma20"]) if not pd.isna(row.get("ma20")) else close
    recent_60_low = float(k60["low"].min())
    recent_60_high = float(k60["high"].max())

    # --- 获取资金流 ---
    flow_vals, flow_10d_yi = get_moneyflow_summary(code, 10)

    # --- 获取PE/PB ---
    pe, pb = get_pe_pb(code)

    # ===================== 场景1: 乐观 =====================
    # 中报净利6亿+ / 资金持续流入 / 均线修复
    opt_ma = min(ma_d, 0)  # MA扣分维持或归零
    if ma_d < 0:
        opt_ma = 0  # 乐观: 均线修复，扣分归零
    opt_fd = max(fd, 5)  # 资金持续流入 → 正向贡献至少+5
    opt_sa = max(sa, 6)  # 情绪修复 +6
    opt_sc = max(sc, 3)  # 板块回暖 +3
    opt_score = base + opt_ma + fa + opt_sa + opt_fd + opt_sc
    opt_score = max(0, min(100, round(opt_score, 1)))

    # 乐观场景仓位
    if opt_score >= 80:
        opt_pos = "25%"
        opt_action = "中等仓位"
    elif opt_score >= 60:
        opt_pos = "12%"
        opt_action = "轻仓"
    elif opt_score >= 40:
        opt_pos = "5%"
        opt_action = "观望"
    else:
        opt_pos = "0%"
        opt_action = "清仓"

    # 乐观止盈上移 / 止损上移
    opt_stop_loss = round(close * 0.92, 2)  # -8%
    opt_take_profit = round(close * 1.18, 2)  # +18%
    opt_alert = "乐观兑现条件: 中报营收>=6亿+资金连续3日净流入+站稳MA5"

    # ===================== 场景2: 中性 =====================
    # 中报5亿维持现状，评分不变
    neu_score = cur_confidence
    neu_pos = cur_position
    if neu_score >= 80:
        neu_action = "中等仓位"
    elif neu_score >= 60:
        neu_action = "轻仓"
    elif neu_score >= 40:
        neu_action = "观望"
    else:
        neu_action = "清仓"

    neu_stop_loss = round(close * 0.90, 2)  # -10%
    neu_take_profit = round(close * 1.12, 2)  # +12%
    neu_alert = "中性格局: 保持现有仓位, 中报验证前不加仓"

    # ===================== 场景3: 悲观 =====================
    # 中报<5亿 / 资金持续流出 / 跌破前低(近60日低点)
    pess_ma = min(ma_d, -25)  # MA全面破位加深扣分
    pess_fd = fd - 15  # 资金加速流出
    pess_sa = 0       # 情绪归零
    pess_sc = sc - 5  # 板块拖累
    pess_break = close * 0.80  # 模拟下跌20%
    pess_score = base + pess_ma + fa + pess_sa + pess_fd + pess_sc
    pess_score = max(0, min(100, round(pess_score, 1)))

    pess_action = "清仓"
    pess_pos = "0%"
    pess_stop_loss = round(min(close * 0.85, recent_60_low * 0.95), 2)
    pess_take_profit = round(close * 1.05, 2)  # 反弹即出
    pess_alert = (
        f"悲观预警: 中报<5亿或跌破{round(recent_60_low,2)}支撑, "
        "立即清仓, 禁止左侧抄底, 等待资金回流+均线修复"
    )

    # ===================== 极端情景压力测试 =====================

    # --- 极端1: 连续10日资金大额流出 ---
    # 假设10日累计净流 <-50亿 (放大至当前流出2倍)
    stress_flow_worst = flow_10d_yi * 2 if flow_10d_yi < 0 else -30.0
    stress_flow_score = base + ma_d + fa + 0 + (fd - 20) + sc
    stress_flow_score = max(0, min(100, round(stress_flow_score, 1)))
    if stress_flow_score <= 30:
        stress_flow_action = "清仓"
        stress_flow_rule = "立即卖出全部持仓, 禁止任何买入"
    elif stress_flow_score <= 50:
        stress_flow_action = "减仓至3%轻仓"
        stress_flow_rule = "卖出70%持仓, 仅保留底仓观察"
    else:
        stress_flow_action = "减仓50%"
        stress_flow_rule = "减仓至原仓位一半"
    stress_flow_trigger = f"连续10日主力累计净流出<{round(stress_flow_worst,1)}亿"

    # --- 极端2: 板块集体暴跌 ---
    # 板块评分归零并额外-10
    stress_sector_score = base + ma_d + fa + sa + fd + 0  # sector=0
    stress_sector_score = max(0, min(100, round(stress_sector_score, 1)))
    if stress_sector_score <= 30:
        stress_sector_action = "清仓"
        stress_sector_rule = "板块系统性风险, 全部清仓离场"
    elif stress_sector_score <= 50:
        stress_sector_action = "减仓至5%"
        stress_sector_rule = "板块暴跌信号, 减仓至轻仓等待企稳"
    else:
        stress_sector_action = "减仓50%"
        stress_sector_rule = "板块回调, 减半仓位等待企稳"

    # --- 极端3: 业绩暴雷 ---
    # 基本面贡献归零, 情绪清零, 风险扣分翻倍
    stress_blowup_score = base + ma_d + 0 + 0 + fd + sc
    stress_blowup_risk = round(current_risk_score(code, fd) * 2, 1) if current_risk_score else 60.0
    stress_blowup_score = max(0, min(100, round(stress_blowup_score, 1)))
    stress_blowup_action = "清仓"
    stress_blowup_rule = "业绩暴雷, 不论成本立即清仓, 等待业绩修复信号至少1个季度"

    # ===================== 综合输出 =====================
    result = {
        "current": {
            "confidence": cur_confidence,
            "position": cur_position,
            "prediction": cur_pred,
            "close_price": close,
            "recent_60d_low": round(recent_60_low, 2),
            "recent_60d_high": round(recent_60_high, 2),
        },
        "scenarios": {
            "optimistic": {
                "label": "乐观(中报>=6亿+资金流入+均线修复)",
                "score": opt_score,
                "action": opt_action,
                "position": opt_pos,
                "stop_loss": opt_stop_loss,
                "take_profit": opt_take_profit,
                "alert": opt_alert,
            },
            "neutral": {
                "label": "中性(中报~5亿维持现状)",
                "score": neu_score,
                "action": neu_action,
                "position": neu_pos,
                "stop_loss": neu_stop_loss,
                "take_profit": neu_take_profit,
                "alert": neu_alert,
            },
            "pessimistic": {
                "label": "悲观(中报<5亿+资金流出+破前低)",
                "score": pess_score,
                "action": pess_action,
                "position": pess_pos,
                "stop_loss": pess_stop_loss,
                "take_profit": pess_take_profit,
                "alert": pess_alert,
            },
        },
        "stress_tests": {
            "extreme_flow_outflow": {
                "label": "极端1: 连续10日资金大额流出",
                "trigger": stress_flow_trigger,
                "simulated_score": stress_flow_score,
                "action": stress_flow_action,
                "rule": stress_flow_rule,
                "priority": "最高",
            },
            "extreme_sector_crash": {
                "label": "极端2: 板块集体暴跌",
                "trigger": "板块指数单日跌超5%且板块内80%个股下跌",
                "simulated_score": stress_sector_score,
                "action": stress_sector_action,
                "rule": stress_sector_rule,
                "priority": "高",
            },
            "extreme_earnings_blowup": {
                "label": "极端3: 业绩暴雷(中报净利<3亿或同比下滑)",
                "trigger": "中报净利<3亿或同比增速<-20%",
                "simulated_score": stress_blowup_score,
                "simulated_risk": stress_blowup_risk,
                "action": stress_blowup_action,
                "rule": stress_blowup_rule,
                "priority": "最高/立即执行",
            },
        },
    }

    # 输出到stdout
    print(f"\n{'='*60}")
    print(f"【多情景压力推演 - {code}】{trade_date}")
    print(f"当前: {cur_confidence}分 | 仓位{cur_position} | 价格{close}")
    print(f"60日区间: {round(recent_60_low,2)} ~ {round(recent_60_high,2)}")
    print(f"{'-'*60}")
    print(f"乐观看{opt_score}分 -> {opt_action}({opt_pos}) 止盈{opt_take_profit}/止损{opt_stop_loss}")
    print(f"中性看{neu_score}分 -> {neu_action}({neu_pos}) 止盈{neu_take_profit}/止损{neu_stop_loss}")
    print(f"悲观看{pess_score}分 -> {pess_action}({pess_pos}) 止盈{pess_take_profit}/止损{pess_stop_loss}")
    print(f"{'-'*60}")
    print(f"极端1(10日资金流出): {stress_flow_score}分 -> {stress_flow_action}")
    print(f"极端2(板块暴跌): {stress_sector_score}分 -> {stress_sector_action}")
    print(f"极端3(业绩暴雷): {stress_blowup_score}分 -> {stress_blowup_action}")
    print(f"{'='*60}\n")

    del k60, row
    return result


def current_risk_score(code, fd):
    """快速风险评分(用于极端场景压力测试)"""
    pe, pb = get_pe_pb(code)
    score = 0
    if pe is not None and pe > 30:
        score += min(13, round((pe - 30) / 30 * 10) * 1.3)
    if pb is not None and pb > 5:
        score += 5
    score += 10  # 基础技术风险
    score += 5   # 基础资金流出风险
    return min(100, round(score, 1))


# ==================== 15. 主评分函数 ====================

def score_stock(code, trade_date):
    """
    主评分函数：
    base = 50 + volume_price
    final = base + ma_deduct + fundamental_add + sentiment_add
            + flow_deduct + sector_add
    钳位[0,100] -> 信号/仓位映射
    """
    # --- 取数据 ---
    pe, pb = get_pe_pb(code)
    k60 = get_ma_data(code, limit=60)
    if k60.empty:
        del k60
        return None

    row = k60.iloc[-1]  # 最新日
    reasons_all = []

    # --- 量价评分(基准50) ---
    vp_score, vp_reasons = calc_volume_price_score(row, k60)
    base = 50 + vp_score
    reasons_all.extend(vp_reasons)

    # --- MA扣分 ---
    ma_deduct, ma_reasons = calc_ma_deduct(row)
    reasons_all.extend(ma_reasons)

    # --- 基本面 ---
    fd = FUNDAMENTAL_DATA.get(code)
    fundamental_add, fd_reasons = calc_fundamental_score(code, fd)
    reasons_all.extend(fd_reasons)

    # --- 情绪 ---
    sentiment_add, st_reasons = calc_sentiment_score(code, fd)
    reasons_all.extend(st_reasons)

    # --- 资金流 ---
    flow_deduct, fl_reasons = calc_flow_score(code)
    reasons_all.extend(fl_reasons)

    # --- 板块 ---
    sector_add, se_reasons = calc_sector_score(fd)
    reasons_all.extend(se_reasons)

    # --- 综合 ---
    final = base + ma_deduct + fundamental_add + sentiment_add + flow_deduct + sector_add
    final = max(0, min(100, round(final, 1)))

    # --- 信号映射 ---
    if final >= 80:
        pred = 1
        pos = "25%"
    elif final >= 60:
        pred = 1
        pos = "12%"
    elif final >= 40:
        pred = 0
        pos = "3%"
    else:
        pred = -1
        pos = "0%"

    del k60, row

    # ====== §1~§6 全局强制风控覆盖 ======
    try:
        from layered_risk_control import LayeredRiskControl
        lrc = LayeredRiskControl()
        allow, risk_logs, final_score, final_pos = lrc.apply_risk_override(
            ts_code=code,
            industry=str(fd.get("industry", "")) if fd else "",
            raw_score=round(final, 1),
            raw_position=pos,
        )
        lrc.close()
        if not allow:
            pred = -1
            final = max(0, round(final - 20, 1))
            pos = "0%"
            reasons_all.append("🔴§风控拦截")
        else:
            if final_pos > 0:
                pos = f"{int(final_pos*100)}%"
            reasons_all.append("✅§风控通过")
    except Exception as e:
        reasons_all.append("⚠§风控异常:{}".format(str(e)))

    result = {
        "ts_code": _ensure_tscode(code),
        "trade_date": trade_date,
        "predict_result": pred,
        "confidence": round(final, 1),
        "predict_reason": "; ".join(reasons_all),
        "base_score": round(base, 1),
        "ma_deduct": ma_deduct,
        "fundamental_add": fundamental_add,
        "sentiment_add": sentiment_add,
        "flow_deduct": flow_deduct,
        "sector_add": sector_add,
        "position": pos,
    }

    # --- 多情景压力推演 ---
    try:
        stress = scenario_stress_test(code, trade_date, result)
        result["scenario_stress"] = stress
    except Exception as e:
        result["scenario_stress"] = {"error": str(e)}
        print(f"  [stress_test error] {e}")

    # --- 自适应自我修正 ---
    if SELF_CORRECT_ACTIVE:
        try:
            result = correct_scoring(code, result)
            result["self_corrected"] = True
        except Exception as e:
            result["self_corrected"] = False
            result["self_correct_error"] = str(e)
            print(f"  [self_correct error] {e}")
    else:
        result["self_corrected"] = False

    # --- 记忆增强修正 (PersistentMemory) ---
    try:
        from persistent_memory import PersistentMemory
        import numpy as np
        mem = PersistentMemory()
        code_clean = str(code).replace(".SH","").replace(".SZ","")
        if mem.is_in_blacklist(code_clean):
            result["confidence"] = max(0, result["confidence"] - 15)
            result["predict_result"] = -1
            result["position"] = "0%"
            result["predict_reason"] += "; 🔴记忆黑名单拦截"
            print(f"  🛑 {code} 记忆黑名单拦截, 分数-15")
        else:
            feat = np.array([
                result.get("base_score", 50) / 100,
                abs(result.get("ma_deduct", 0)) / 30,
                result.get("fundamental_add", 0) / 30,
                result.get("sentiment_add", 0) / 20,
                result.get("flow_deduct", 0) / 30,
            ], dtype=np.float32)
            similar = mem.get_similar_history(feat)
            if similar:
                old_conf = result["confidence"]
                adj_conf = mem.adjust_score_by_history(old_conf / 100, similar)
                result["confidence"] = round(adj_conf * 100, 1)
                if result["confidence"] >= 60 and result["predict_result"] != 1:
                    result["predict_result"] = 1
                    result["position"] = "12%"
                elif result["confidence"] < 40 and result["predict_result"] == 1:
                    result["predict_result"] = 0
                    result["position"] = "3%"
                result["predict_reason"] += f"; 🧠记忆修正{old_conf}→{result['confidence']}"
                print(f"  🧠 {code} 记忆修正: {old_conf}→{result['confidence']} ({len(similar)}条相似)")
        mem.close_all()
        del mem
    except Exception as e:
        pass  # 记忆系统不可用时透明明跳过

    return result


# ==================== 15. 批量流式运行 ====================

def stream_run(dt):
    """
    遍历TARGET_CODES, 每3只批量flush到stock_predict表
    del + gc.collect() 每3只释放内存
    """
    end_dt = datetime.strptime(dt, "%Y%m%d").date()
    pred_date = end_dt + timedelta(days=1)
    buf = []
    n = len(TARGET_CODES)

    # ── 开机前全链路自检 ──
    if SELF_CORRECT_ACTIVE:
        try:
            hc = run_full_check()
            if hc.get("all_ok"):
                print(f"[self_correct] 全链路自检通过 ✓")
            else:
                failed = [k for k, v in hc.get("checks", {}).items()
                          if v.get("status") != "ok"]
                print(f"[self_correct] 自检警告: {failed}")
        except Exception as e:
            print(f"[self_correct] 自检异常: {e}")

    for i, code in enumerate(TARGET_CODES):
        res = score_stock(code, pred_date)
        if res is not None:
            buf.append({
                "ts_code": res["ts_code"],
                "trade_date": res["trade_date"],
                "predict_result": res["predict_result"],
                "confidence": res["confidence"],
                "predict_reason": res.get("predict_reason", ""),
                "self_corrected": res.get("self_corrected", False),
            })
            emoji = {1: "+", 0: "~", -1: "-"}[res["predict_result"]]
            sc_tag = "🔧" if res.get("self_corrected") else " "
            print(f"{sc_tag}{emoji} {code} {pred_date} {res['confidence']}分 pos={res.get('position','?')}")

            # 每3条或最后一批flush
            if len(buf) >= 3 or i == n - 1:
                df_out = pd.DataFrame(buf)
                df_out.to_sql(
                    "stock_predict", pg_engine,
                    if_exists="append", index=False,
                    method="multi", chunksize=10
                )
                buf.clear()
                # 内存释放
                del df_out, res
                if (i + 1) % 3 == 0:
                    gc.collect()

    print(f"写入{n}条 | gc每3只触发 | 完成时间: {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    dt = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    stream_run(dt)
