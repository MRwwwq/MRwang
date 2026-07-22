#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio_trap_scan.py — 全17只标的诱多/资金周期扫描
===================================================
基于山东黄金600547诱多识别框架，扫描全组合标的：
  ① 7项诱多信号检测（命中≥3=高概率诱多）
  ② 资金周期阶段判定（建仓/派发/脉冲/诱多/破位）
  ③ 五维交叉验证评分
  ④ 输出分级预警清单

用法:
  python3 portfolio_trap_scan.py                          # 扫描全部标的
  python3 portfolio_trap_scan.py 600547 600884           # 指定标的
  python3 portfolio_trap_scan.py --period 20             # 设置检测周期天数

数据源: Tushare Pro (moneyflow + daily + daily_basic)
输出: 控制台 + /www/wwwroot/stocks/reports/portfolio_trap_scan_{date}.json
"""

import sys
import os
import json
import logging
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

import psycopg2

# ═══════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════

DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}

# 全17只标的代码(纯数字)
ALL_STOCKS = [
    "000063", "000725", "002044", "002169", "002617",
    "300098", "300433", "300476", "300693",
    "600183", "600487", "600547", "600585", "600884", "600941",
    "601138", "601868",
]

# 判定阈值（万元）
TRAP_THRESHOLDS = {
    "big_inflow": 5000,        # 单日净流入 > 5000万(适配小盘)
    "big_outflow": 5000,       # 单日净流出 > 5000万
    "surge_pct": 4.0,          # 单日涨幅 > 4%
    "drop_pct": -4.0,          # 单日跌幅 < -4%
    "next_day_reversal_pct": 50,  # 次日反手流出 > 前日流入50%
    "super_large_ratio": 0.3,  # 特大单占比阈值
    "retail_buy_signal": True, # 小单净买入=接盘
    "outflow_days": 3,         # 连续流出天数阈值
    "key_support_breach_pct": -3.0,  # 破位跌幅
}

# 日志
LOG_PATH = "/www/wwwroot/stocks/reports/report_read_log.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8", mode="a"),
    ],
)
logger = logging.getLogger("PortfolioTrapScan")


def get_pg_conn():
    return psycopg2.connect(**DB_CONFIG)


def load_stock_names():
    """加载标的名称"""
    conn = get_pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT stock_code, stock_name FROM dim_stock")
    names = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return names


def load_money_flow(code, days=20):
    """
    加载标的近N日资金流数据
    返回: list of dicts
    """
    conn = get_pg_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, pct_chg, close, amount,
               net_mf_amount,
               buy_elg_amount, sell_elg_amount,
               buy_lg_amount, sell_lg_amount,
               buy_md_amount, sell_md_amount,
               buy_sm_amount, sell_sm_amount
        FROM stock_daily sd
        JOIN stock_money_flow mf USING (stock_code, trade_date)
        WHERE sd.stock_code = %s
        ORDER BY sd.trade_date DESC
        LIMIT %s
    """, (code, days))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    for r in rows:
        result.append({
            "trade_date": str(r[0]),
            "pct_chg": float(r[1] or 0),
            "close": float(r[2] or 0),
            "amount": float(r[3] or 0),
            "net_mf": float(r[4] or 0),
            "buy_elg": float(r[5] or 0),
            "sell_elg": float(r[6] or 0),
            "buy_lg": float(r[7] or 0),
            "sell_lg": float(r[8] or 0),
            "buy_md": float(r[9] or 0),
            "sell_md": float(r[10] or 0),
            "buy_sm": float(r[11] or 0),
            "sell_sm": float(r[12] or 0),
            "super_large_net": float(r[5] or 0) - float(r[6] or 0),
            "large_net": float(r[7] or 0) - float(r[8] or 0),
            "mid_net": float(r[9] or 0) - float(r[10] or 0),
            "small_net": float(r[11] or 0) - float(r[12] or 0),
        })
    return result  # 最新在前


# ═══════════════════════════════════════════════
#  7项诱多检测
# ═══════════════════════════════════════════════

def check_trap_signals(data_15d):
    """
    对最近15日数据运行7项诱多检测
    返回: (hits, details_list)
    """
    if len(data_15d) < 5:
        return 0, ["数据不足5日"]

    hits = 0
    details = []
    T = TRAP_THRESHOLDS

    # 最新在前, 反转让最早在前方便计算
    d = list(reversed(data_15d))
    if len(d) > 15:
        d = d[-15:]

    # 信号1: 15日累计净流出 < 0 但某日净流入 > 1亿
    cum_net = sum(row["net_mf"] for row in d)
    max_inflow = max(row["net_mf"] for row in d)
    if cum_net < 0 and max_inflow > T["big_inflow"]:
        hits += 1
        details.append(f"信号1: 15日累计{int(cum_net/10000)}万净流出但峰值流入{int(max_inflow/10000)}万")

    # 信号2: 高位阴跌2日后出现单日>4%大阳线
    for i in range(2, len(d)):
        if d[i-2]["pct_chg"] < -1 and d[i-1]["pct_chg"] < 0 and d[i]["pct_chg"] > T["surge_pct"]:
            hits += 1
            details.append(f"信号2: {d[i]['trade_date']}阴跌后大阳{d[i]['pct_chg']:+.2f}%")
            break

    # 信号3: 大阳日主力净额 > 1亿 但 特大单净额为负(对倒)
    for row in reversed(d):
        if row["pct_chg"] > T["surge_pct"] and row["net_mf"] > T["big_inflow"]:
            if row["super_large_net"] < 0:
                hits += 1
                details.append(f"信号3: {row['trade_date']}特大单净额{int(row['super_large_net']/10000)}万为负(对倒)")
            break

    # 信号4: 大阳次日转阴且主力净额 < -1亿
    for i in range(len(d) - 1):
        if d[i]["pct_chg"] > T["surge_pct"] and d[i]["net_mf"] > T["big_inflow"]:
            if d[i+1]["pct_chg"] < 0 and d[i+1]["net_mf"] < -T["big_outflow"]:
                hits += 1
                details.append(f"信号4: {d[i]['trade_date']}大阳→{d[i+1]['trade_date']}反手流出{int(d[i+1]['net_mf']/10000)}万")
            break

    # 信号5: 大阳日小单净买入(散户接盘)
    for row in reversed(d):
        if row["pct_chg"] > T["surge_pct"] and row["net_mf"] > T["big_inflow"]:
            if row["small_net"] > 0:
                hits += 1
                details.append(f"信号5: {row['trade_date']}散户净买入{int(row['small_net']/10000)}万(接盘)")
            break

    # 信号6: 近3日累计流出 > 5000万（加速出货）
    last3_net = sum(row["net_mf"] for row in d[-3:]) if len(d) >= 3 else 0
    if last3_net < -5000:
        hits += 1
        details.append(f"信号6: 近3日累计流出{int(last3_net/10000)}万(加速出货)")
    
    # 信号7: 最近一日放量下跌 > 4%
    last = d[-1] if d else None
    if last and last["pct_chg"] < T["drop_pct"]:
        if len(d) >= 5:
            avg_amount = sum(row["amount"] for row in d[-6:-1]) / 5
            if avg_amount > 0 and last["amount"] > avg_amount * 1.5:
                hits += 1
                details.append(f"信号7: {last['trade_date']}放量暴跌{last['pct_chg']:.2f}%(量比{last['amount']/avg_amount:.1f}x)")
    
    # 信号8(新增): 弱势独立诱多(板块弱势+个股涨+特大单卖+散户买)
    # 适配杉杉07.13类型:涨幅不大但资金分层明显
    for row in reversed(d[-8:]):  # 只看近8日
        if (row["pct_chg"] >= 1.5 and row["pct_chg"] < 6 and 
            row["net_mf"] < 0 and row["super_large_net"] < 0 and
            row["small_net"] > 0):
            hits += 1
            details.append(f"信号8(弱势独立诱多): {row['trade_date']}涨{row['pct_chg']:+.2f}%但特大单净{int(row['super_large_net'])}万+散户净{int(row['small_net'])}万")

    return hits, details


# ═══════════════════════════════════════════════
#  资金周期阶段判定
# ═══════════════════════════════════════════════

def classify_phase(data_15d):
    """
    根据15日资金流数据判定当前周期阶段
    返回: (阶段名称, 阶段编号, 置信度)
    """
    if len(data_15d) < 10:
        return "数据不足", -1, 0

    d = list(reversed(data_15d))  # 最早在前
    cum_net = sum(row["net_mf"] for row in d)
    last5_net = sum(row["net_mf"] for row in d[-5:])
    pos_days = sum(1 for row in d if row["net_mf"] > 0)
    neg_days = len(d) - pos_days

    # 检测是否有诱多信号
    trap_hits, _ = check_trap_signals(data_15d)
    
    # 检测最近是否破位
    last = d[-1]
    last3 = d[-3:]

    # 阶段6: 破位下跌
    if (last["pct_chg"] < -4 and last["net_mf"] < -5000):
        return f"末期破位({last['trade_date']}放量跌{last['pct_chg']:.2f}%)", 6, 0.9

    # 阶段5: 诱多
    if trap_hits >= 4:
        return "诱多出货(≥4项信号命中)", 5, 0.8
    
    if trap_hits >= 3:
        return "疑似诱多(3项信号命中)", 5, 0.6

    # 阶段4: 预期证伪/杀跌
    if last5_net < -20000 and sum(r["pct_chg"] for r in last3) < -5:
        return "杀跌/预期证伪", 4, 0.7
    
    # 阶段1: 建仓
    if cum_net > 5000 and pos_days > neg_days:
        return "低位建仓", 1, 0.7
    
    # 阶段2: 分批派发
    max_inflow_val = max(r["net_mf"] for r in d) if d else 0
    if cum_net < -5000 and neg_days > pos_days and max_inflow_val < 10000:
        return "分批派发(阴跌出货)", 2, 0.6

    # 阶段3: 消息脉冲
    for i in range(1, len(d)-1):
        if d[i]["net_mf"] > 8000 and d[i-1]["net_mf"] < 0 and d[i+1]["net_mf"] < 0:
            return f"消息脉冲({d[i]['trade_date']}单日+{int(d[i]['net_mf']/10000)}万无持续性)", 3, 0.7

    if trap_hits >= 1:
        return f"关注(命中{trap_hits}项信号)", 0, 0.3
    return "正常/震荡", 0, 0.5


# ═══════════════════════════════════════════════
#  主力行为识别
# ═══════════════════════════════════════════════

def detect_behavior(data_15d):
    """识别8种主力行为"""
    if len(data_15d) < 5:
        return []
    
    d = list(reversed(data_15d))
    behaviors = []
    
    # 行为6: 对倒诱多
    for i in range(min(3, len(d)-1)):
        if (d[i]["pct_chg"] > 4 and d[i]["net_mf"] > 10000 and 
            d[i]["super_large_net"] < 0 and d[i+1]["net_mf"] < -5000):
            behaviors.append(f"🚨对倒诱多({d[i]['trade_date']}→{d[i+1]['trade_date']})")
            break
    
    # 行为7: 末期出货
    last = d[-1]
    if last["pct_chg"] < -4 and last["net_mf"] < -8000:
        behaviors.append(f"🔴末期出货({last['trade_date']}放量跌{last['pct_chg']:.2f}%)")
    
    # 行为5: 消息套利
    for i in range(1, len(d)-1):
        if d[i]["net_mf"] > 8000 and d[i-1]["net_mf"] < 0 and d[i+1]["net_mf"] < 0:
            behaviors.append(f"🟠消息套利({d[i]['trade_date']}单日脉冲)")
            break
    
    # 行为4: 分批减仓
    neg_streak = 0
    for row in d[-8:]:
        if row["net_mf"] < 0:
            neg_streak += 1
        else:
            neg_streak = 0
    if neg_streak >= 4:
        behaviors.append(f"⚠️分批减仓(连续{neg_streak}日净流出)")
    
    if not behaviors:
        behaviors.append("✅正常")
    
    return behaviors


# ═══════════════════════════════════════════════
#  扫描函数
# ═══════════════════════════════════════════════

def scan_stock(code, name, data):
    """单只标的扫描"""
    if len(data) < 5:
        return {"code": code, "name": name, "error": "数据不足5日"}
    
    trap_hits, trap_details = check_trap_signals(data)
    phase, phase_id, confidence = classify_phase(data)
    behaviors = detect_behavior(data)
    
    # 计算关键统计
    d = list(reversed(data)) if data else []
    cum_15d = sum(row["net_mf"] for row in d[-15:]) if len(d) >= 15 else sum(row["net_mf"] for row in d)
    last_pct = d[-1]["pct_chg"] if d else 0
    last_close = d[-1]["close"] if d else 0
    
    # 风控等级
    if trap_hits >= 5:
        risk_level = "🔴 L3 红色预警"
        risk_action = "禁止开仓, 反弹减仓50%"
    elif trap_hits >= 3:
        risk_level = "🟠 L2 橙色预警"
        risk_action = "暂停新开仓, 现有仓位持有观察"
    elif trap_hits >= 1:
        risk_level = "🟡 L1 黄色预警"
        risk_action = "加强监控, 不追高"
    else:
        risk_level = "🟢 L0 正常"
        risk_action = "正常操作"
    
    # 三重信号状态
    triple_signal = {
        "主力3日净流入": sum(row["net_mf"] for row in d[-3:]) > 0 if len(d) >= 3 else False,
        "股价站上MA5": False,  # 需额外查询
        "无连续流出": d[-1]["net_mf"] > -3000 if d else False,
    }
    
    return {
        "code": code,
        "name": name,
        "close": last_close,
        "last_chg": f"{last_pct:+.2f}%",
        "cum_15d_net": f"{int(cum_15d/10000)}万",
        "phase": phase,
        "phase_id": phase_id,
        "trap_hits": f"{trap_hits}/7",
        "trap_hits_count": trap_hits,
        "behaviors": behaviors,
        "trap_details": trap_details,
        "risk_level": risk_level,
        "risk_action": risk_action,
        "triple_signals": triple_signal,
    }


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main(target_codes=None):
    target_codes = target_codes or ALL_STOCKS
    today = date.today().strftime("%Y%m%d")
    
    logger.info(f"{'='*60}")
    logger.info(f"全17只标的诱多/资金周期扫描 [{today}]")
    logger.info(f"{'='*60}")
    
    # 加载名称
    names = load_stock_names()
    
    results = []
    for code in target_codes:
        name = names.get(code, "未知")
        logger.info(f"扫描 {code} {name}...")
        
        data = load_money_flow(code, days=25)
        result = scan_stock(code, name, data)
        results.append(result)
        
        # 打印简要
        r = result
        if "error" in r:
            logger.warning(f"  ⚠️ {code} {name}: {r['error']}")
        else:
            logger.info(f"  {r['risk_level']} | 阶段:{r['phase']} | 诱多:{r['trap_hits']} | 15日:{r['cum_15d_net']}")
    
    # 排序：先按风险等级（预警>正常），再按诱多命中数
    def sort_key(r):
        level_order = {"🔴": 0, "🟠": 1, "🟡": 2, "🟢": 3}
        level = r.get("risk_level", "🟢")[0]
        hits = r.get("trap_hits_count", 0)
        return (level_order.get(level, 9), -hits)
    
    results.sort(key=sort_key)
    
    # ─── 输出报告 ───
    print(f"\n{'='*80}")
    print(f"  全17只标的资金周期 & 诱多扫描报告 [{today}]")
    print(f"{'='*80}")
    
    print(f"\n{'代码':<8} {'名称':<10} {'收盘价':>8} {'涨跌幅':>8} {'15日净额':<12} {'诱多':<6} {'风险等级':<20} {'阶段判定'}")
    print(f"{'-'*8} {'-'*10} {'-'*8} {'-'*8} {'-'*12} {'-'*6} {'-'*20} {'-'*20}")
    
    high_risk = []
    watch_list = []
    
    for r in results:
        if "error" in r:
            print(f"{r['code']:<8} {r['name']:<10} {'N/A':>8} {'N/A':>8} {'无数据':<12} {'':<6} {'⚠️':<20} {r['error']}")
            continue
        
        risk = r["risk_level"]
        print(f"{r['code']:<8} {r['name']:<10} {r['close']:>8.2f} {r['last_chg']:>8} {r['cum_15d_net']:<12} {r['trap_hits']:<6} {risk:<20} {r['phase']}")
        
        if "L3" in risk or "L2" in risk:
            high_risk.append(r)
        elif "L1" in risk:
            watch_list.append(r)
    
    # ─── 风险汇总 ───
    print(f"\n{'='*80}")
    print(f"  🚨 风险汇总")
    print(f"{'='*80}")
    
    if high_risk:
        print(f"\n  🔴 高风险标的 ({len(high_risk)}只):")
        for r in high_risk:
            print(f"    · {r['code']} {r['name']} | {r['risk_level']} | {r['risk_action']}")
            for b in r.get("behaviors", []):
                print(f"      主力行为: {b}")
            for d in r.get("trap_details", []):
                print(f"      → {d}")
    
    if watch_list:
        print(f"\n  🟡 关注标的 ({len(watch_list)}只):")
        for r in watch_list:
            print(f"    · {r['code']} {r['name']} | {r['risk_level']} | {', '.join(r.get('trap_details', ['待观察']))}")
    
    normal_count = sum(1 for r in results if "L0" in r.get("risk_level", ""))
    print(f"\n  🟢 正常标的: {normal_count}只")
    
    # ─── 五维交叉验证得分最高的风险标的 ───
    print(f"\n{'='*80}")
    print(f"  📊 重点标的行为详情")
    print(f"{'='*80}")
    for r in results[:5]:  # 前5只风险最高
        if "error" in r:
            continue
        print(f"\n  [{r['code']} {r['name']}] {r['risk_level']}")
        print(f"    15日累计: {r['cum_15d_net']}  |  最新: {r['last_chg']} @ {r['close']}")
        print(f"    周期阶段: {r['phase']}")
        print(f"    诱多信号: {r['trap_hits']}")
        for d in r.get("trap_details", []):
            print(f"      · {d}")
        for b in r.get("behaviors", []):
            print(f"    行为: {b}")
        sig = r.get("triple_signals", {})
        print(f"    三重信号: 主力流入={'✅' if sig.get('主力3日净流入') else '❌'} | MA5={'❌' if sig.get('股价站上MA5')==False else '?'} | 无流出={'✅' if sig.get('无连续流出') else '❌'}")
    
    # ─── 保存JSON ───
    output_dir = "/www/wwwroot/stocks/reports"
    os.makedirs(output_dir, exist_ok=True)
    output_path = f"{output_dir}/portfolio_trap_scan_{today}.json"
    
    json_output = {
        "scan_date": today,
        "total_stocks": len(results),
        "high_risk_count": len(high_risk),
        "watch_count": len(watch_list),
        "normal_count": normal_count,
        "results": results,
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)
    
    logger.info(f"扫描报告已保存: {output_path}")
    
    print(f"\n{'='*80}")
    print(f"  扫描完成 | 共{len(results)}只 | 高风险{len(high_risk)}只 | 关注{len(watch_list)}只 | 正常{normal_count}只")
    print(f"  报告: {output_path}")
    print(f"{'='*80}\n")
    
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全标的诱多/资金周期扫描")
    parser.add_argument("codes", nargs="*", help="指定标的代码(多空格分隔)，缺省扫描全部17只")
    parser.add_argument("--period", type=int, default=20, help="检测周期天数(默认20)")
    args = parser.parse_args()
    
    target = args.codes or ALL_STOCKS
    TRAP_THRESHOLDS["period"] = args.period
    
    main(target)
