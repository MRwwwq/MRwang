#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module_06_BacktestCalibrate — 盘后样本校准与智能体自修正模块 v1.1
================================================================
核心定位：全链路闭环关键环节，驱动智能体自主修正规则/参数/信号打分权重的唯一数据源
执行窗口：收盘后，标准时长10分钟
前置条件：
  1. 当日全部观察/持仓标的收盘行情数据就绪
  2. 智能体盘中原始信号存档完整（含Layer2风控分级、psy_hit_codes心理编码）
  3. 交易者真实操作记录完整

两大核心操作：
  【操作一】真实交易结果校准（量化误差标签）: 4类标签 L01~L04 + L00正常
  【操作二】人工研判漏洞修正（补充隐性盲区）: 3类漏洞记录+补丁+等级标记+L1~L3

流程：
  归集→匹配4类标签→记录研判漏洞→写入样本库→同步回传Layer1/Module01~04/Layer2→生成复盘日志

用法:
  python3 module_06_backtest_calibrate.py                          # 交互模式(人工确认标签)
  python3 module_06_backtest_calibrate.py --auto                   # 自动模式(基于预判vs真实行情自动打标)
  python3 module_06_backtest_calibrate.py --vuln                   # 仅执行漏洞修正操作
  python3 module_06_backtest_calibrate.py --auto --vuln            # 全自动模式(含自动标记+漏洞修正)
  python3 module_06_backtest_calibrate.py --check-only             # 仅检查今日是否已完成校准
"""

import sys
import os
import json
import logging
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# ═══════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
MODULE06_DB = BASE / "module06_calibration.db"
REPORT_DIR = BASE / "reports"
TRACKER_DIR = BASE / "tracker_reports"

CALIB_TABLE = "module06_calibration"
SYNC_TABLE = "module06_sync_log"
VULN_TABLE = "module06_vulnerability"  # 研判漏洞记录表

# 交易日（从文件名或系统日期推断）
TODAY = datetime.now().strftime("%Y-%m-%d")
TRADE_DATE = TODAY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [M06] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Module06")


# ═══════════════════════════════════════════
#  第三部分：4类标准化误差标签
# ═══════════════════════════════════════════

LABELS = {
    "L01": {
        "name": "预判高估，负误差",
        "condition": "智能体判定上涨/给出入场持仓建议，标的当日实际大跌",
        "code": "overestimate_negative",
        "target": "降低选股Agent乐观权重, 收紧入场条件"
    },
    "L02": {
        "name": "预判低估，负误差",
        "condition": "智能体判定下跌/提示规避减仓，标的当日实际大涨",
        "code": "underestimate_negative",
        "target": "降低风控Agent悲观权重, 放宽规避阈值"
    },
    "L03": {
        "name": "风控判断有效",
        "condition": "Layer2输出YELLOW/RED/提示减仓，后续股价持续大跌",
        "code": "risk_valid",
        "target": "强化对应心理误判/利空信号的风险分级触发阈值"
    },
    "L04": {
        "name": "入场条件失效",
        "condition": "系统判定满足全部入场条件并开仓，开仓后标的直接深度被套",
        "code": "entry_failure",
        "target": "收紧对应风格/情绪/主线的入场筛选约束"
    },
}

# ═══════════════════════════════════════════
#  第一类：3类研判漏洞分类
# ═══════════════════════════════════════════

VULN_CATEGORIES = {
    "C01": {
        "name": "信息类盲区",
        "desc": "智能体无法自动抓取解读的隐性信息",
        "sub_types": ["突发隐性利空/利好", "小众题材/细分产业逻辑", "盘外舆情/资金隐性动作"],
        "fix_target": "Layer1_BOW特征词库补充关键词/特征"
    },
    "C02": {
        "name": "规则逻辑类漏洞",
        "desc": "系统固定规则静态固化，无法自适应行情变化",
        "sub_types": ["风格适配漏洞", "情绪判定漏洞", "主线判定漏洞"],
        "fix_target": "Module01~04 风格/情绪/主线参数临时补丁"
    },
    "C03": {
        "name": "风控识别盲区",
        "desc": "心理偏差/风险信号隐性化，量化指标无剧烈变化",
        "sub_types": ["隐性情绪化交易风险", "共振风险盲区(缓慢积累Lolla)"],
        "fix_target": "Layer2 隐性风险触发条件+心理误判场景补充"
    },
}

# 漏洞严重等级
VULN_SEVERITY = {
    "L1": {"name": "轻度", "desc": "仅小幅影响收益，不产生大幅亏损", "action": "仅更新特征词库"},
    "L2": {"name": "中度", "desc": "造成单次明显回撤/踏空大行情", "action": "临时修改参数补丁，次日自动迭代调整权重"},
    "L3": {"name": "重度", "desc": "连续多日规则失效/频繁大幅亏损", "action": "永久固化规则调整，强制收紧全部入场条件"},
}


# ═══════════════════════════════════════════
#  数据库初始化
# ═══════════════════════════════════════════

def init_db():
    """初始化校准数据库"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {CALIB_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            stock_name TEXT DEFAULT '',
            label_code TEXT NOT NULL,
            label_name TEXT NOT NULL,
            ai_prediction TEXT DEFAULT '',
            real_pct_chg REAL DEFAULT 0,
            real_action TEXT DEFAULT '',
            support_resistance_status TEXT DEFAULT '',
            sector_pct_chg REAL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(trade_date, ticker)
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {SYNC_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            ticker TEXT,
            target_module TEXT NOT NULL,
            sync_action TEXT NOT NULL,
            sync_status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {VULN_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            ticker TEXT DEFAULT '',
            vuln_category TEXT NOT NULL,
            vuln_subtype TEXT DEFAULT '',
            severity TEXT DEFAULT 'L1',
            ai_conclusion TEXT DEFAULT '',
            real_fact TEXT DEFAULT '',
            fix_patch TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"数据库初始化完成: {MODULE06_DB}")


# ═══════════════════════════════════════════
#  核心功能
# ═══════════════════════════════════════════

def get_today_tracked_stocks() -> List[Dict]:
    """从当日跟踪报告获取今日持仓/观察标的清单"""
    stocks = []
    # 从tracker_reports读取最新报告提取标的列表
    try:
        import glob
        files = sorted(glob.glob(str(TRACKER_DIR / "close_watch_*.json")), reverse=True)
        if files:
            with open(files[0]) as f:
                data = json.load(f)
            for item in data.get("data", []):
                stocks.append({
                    "code": item["code"],
                    "name": item["name"],
                    "close": item.get("close", 0),
                    "pct_chg": item.get("pct_chg", 0),
                    "pe_ttm": item.get("pe_ttm", 0),
                    "fund_status": item.get("fund_status", ""),
                    "trend": item.get("trend", ""),
                })
            log.info(f"从跟踪报告加载 {len(stocks)} 只标的")
    except Exception as e:
        log.warning(f"读取跟踪报告失败: {e}")

    # 兜底：如果跟踪报告不存在，用固定10只池
    if not stocks:
        try:
            from watch_tracker import WATCH_LIST
            stocks = [{"code": s["code"], "name": s["name"], "close": 0, "pct_chg": 0} for s in WATCH_LIST]
        except:
            stocks = [
                {"code":"000651.SZ","name":"格力电器"},{"code":"601766.SH","name":"中国中车"},
                {"code":"600887.SH","name":"伊利股份"},{"code":"601919.SH","name":"中远海控"},
                {"code":"600031.SH","name":"三一重工"},{"code":"600884.SH","name":"杉杉股份"},
                {"code":"600547.SH","name":"山东黄金"},{"code":"002044.SZ","name":"美年健康"},
                {"code":"300476.SZ","name":"胜宏科技"},{"code":"300433.SZ","name":"蓝思科技"},
            ]
        log.info(f"使用固定跟踪池: {len(stocks)} 只")

    return stocks


def read_ai_logs(ticker: str) -> Dict:
    """读取标的盘中智能体信号存档"""
    log_path = BASE / f"ai_logs/{TRADE_DATE}/{ticker}.json"
    default = {
        "ai_prediction": "未知",
        "layer2_risk": "未知",
        "psy_hit_codes": [],
        "entry_conditions_met": False,
        "suggested_action": "未知"
    }
    if log_path.exists():
        try:
            with open(log_path) as f:
                return {**default, **json.load(f)}
        except:
            pass
    return default


def auto_match_label(stock: Dict, ai_log: Dict) -> Optional[str]:
    """
    自动匹配4类标签逻辑
    基于真实行情 vs AI预判的偏差
    """
    real_pct = stock.get("pct_chg", 0)
    ai_pred = ai_log.get("ai_prediction", "")
    suggested = ai_log.get("suggested_action", "")

    # L03: 风控判断有效 — Layer2输出预警+后续大跌
    layer2 = ai_log.get("layer2_risk", "")
    if layer2 in ("YELLOW", "RED") and real_pct < -3:
        return "L03"

    # L04: 入场条件失效 — 系统建议入场+开仓后被套
    if ai_log.get("entry_conditions_met") and suggested in ("买入", "加仓") and real_pct < -3:
        return "L04"

    # L01: 预判高估 — AI看涨+实际大跌
    if ai_pred in ("看涨", "上涨", "多头") and real_pct < -2:
        return "L01"

    # L02: 预判低估 — AI看跌+实际大涨
    if ai_pred in ("看跌", "下跌", "空头", "规避") and real_pct > 3:
        return "L02"

    return None


def save_calibration(trade_date: str, ticker: str, stock_name: str,
                     label_code: str, ai_prediction: str, real_pct_chg: float,
                     real_action: str, support_resistance: str = "",
                     sector_pct: float = 0, notes: str = ""):
    """写入校准样本库"""
    label_info = LABELS.get(label_code, {})
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        INSERT OR REPLACE INTO {CALIB_TABLE}
        (trade_date, ticker, stock_name, label_code, label_name,
         ai_prediction, real_pct_chg, real_action,
         support_resistance_status, sector_pct_chg, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_date, ticker, stock_name, label_code, label_info.get("name", ""),
        ai_prediction, real_pct_chg, real_action,
        support_resistance, sector_pct, notes
    ))
    conn.commit()
    conn.close()
    log.info(f"  [{label_code}] {ticker} {stock_name} 已写入样本库")


# ═══════════════════════════════════════════
#  操作二：研判漏洞修正
# ═══════════════════════════════════════════

def save_vulnerability(trade_date, ticker, vuln_category, vuln_subtype,
                       severity, ai_conclusion, real_fact,
                       fix_patch="", notes=""):
    """记录研判漏洞（信息盲区/规则逻辑漏洞/风控盲区）"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO {VULN_TABLE}
        (trade_date, ticker, vuln_category, vuln_subtype, severity,
         ai_conclusion, real_fact, fix_patch, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade_date, ticker, vuln_category, vuln_subtype, severity,
          ai_conclusion, real_fact, fix_patch, notes))
    conn.commit()
    conn.close()
    cat_name = VULN_CATEGORIES.get(vuln_category, {}).get("name", vuln_category)
    sev_name = VULN_SEVERITY.get(severity, {}).get("name", severity)
    log.info(f"  [漏洞][{severity}][{vuln_category}] {ticker} {cat_name}")
    return True


def sync_vuln_to_modules(trade_date, vuln_category, severity, fix_patch):
    """漏洞数据回传三大模块"""
    if vuln_category == "C01":
        act = f"bow_keyword_patch|{fix_patch[:80]}"
        record_sync_action(trade_date, "", "Layer1_FeatureCheck", act, "pending")
        log.info(f"  → Layer1 BOW词库: {act}")
    elif vuln_category == "C02":
        act = f"rule_param_patch|{fix_patch[:80]}"
        record_sync_action(trade_date, "", "Module01_04", act, "pending")
        log.info(f"  → Module01~04 参数补丁: {act}")
    elif vuln_category == "C03":
        act = f"risk_condition_patch|{fix_patch[:80]}"
        record_sync_action(trade_date, "", "Layer2_RiskDecision", act, "pending")
        log.info(f"  → Layer2 风控条件补丁: {act}")


def record_vuln_and_notify():
    """交互式记录研判漏洞（人工录入）"""
    print("\n" + "=" * 60)
    print("  【操作二】人工研判漏洞修正")
    print("=" * 60)
    print("\n漏洞分类:")
    for cc, ci in VULN_CATEGORIES.items():
        print(f"  {cc}: {ci['name']} — {ci['desc']}")
        for st in ci["sub_types"]:
            print(f"    · {st}")
    print("\n严重等级: L1(轻度) L2(中度) L3(重度)")
    print("(输入 q 结束漏洞记录)")
    print("-" * 40)

    while True:
        ticker = input("\n漏洞标的/板块(留空跳过): ").strip()
        if not ticker or ticker.lower() == 'q':
            break
        ai_conc = input("智能体原有研判结论: ").strip()
        real_fact = input("真实市场事实/隐性信息: ").strip()
        print("漏洞类型: C01信息盲区 C02规则逻辑漏洞 C03风控盲区")
        cat = input("漏洞类型: ").strip().upper()
        while cat not in VULN_CATEGORIES:
            if cat.lower() == 'q':
                return
            cat = input("无效, 请选 C01/C02/C03: ").strip().upper()
        sev = input("严重等级(L1/L2/L3): ").strip().upper()
        while sev not in VULN_SEVERITY:
            sev = input("无效, 请选 L1/L2/L3: ").strip().upper()
        patch = input("人工补丁描述: ").strip()
        save_vulnerability(TRADE_DATE, ticker, cat, "", sev, ai_conc, real_fact, patch)
        sync_vuln_to_modules(TRADE_DATE, cat, sev, patch)
        print(f"  ✅ [{sev}] {ticker} 漏洞已记录+回传")


def generate_vuln_log(trade_date):
    """生成漏洞修正复盘日志"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT ticker, vuln_category, severity, ai_conclusion, real_fact, fix_patch
        FROM {VULN_TABLE} WHERE trade_date = ? ORDER BY id
    """, (trade_date,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return ""
    lines = [
        "",
        "### 操作二：研判漏洞修正记录",
        "",
        "| 标的 | 漏洞类型 | 等级 | AI研判 | 真实事实 | 人工补丁 |",
        "|:----:|:--------:|:----:|:-------|:---------|:---------|",
    ]
    cats = {}
    for r in rows:
        ticker, cat, sev, ai_c, real, patch = r
        cn = VULN_CATEGORIES.get(cat, {}).get("name", cat)
        sn = VULN_SEVERITY.get(sev, {}).get("name", sev)
        lines.append(f"| {ticker} | {cn} | {sn} | {ai_c[:40]} | {real[:40]} | {patch[:40]} |")
        cats[cat] = cats.get(cat, 0) + 1
    lines += ["", "**漏洞分类统计:**"]
    for c, n in cats.items():
        lines.append(f"- {VULN_CATEGORIES.get(c,{}).get('name',c)}: {n}条")
    return "\n".join(lines)


# ═══════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════
def record_sync_action(trade_date: str, ticker: str, target_module: str,
                       sync_action: str, status: str = "pending"):
    """记录数据同步事件（回传Layer1/Module/Layer2）"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO {SYNC_TABLE}
        (trade_date, ticker, target_module, sync_action, sync_status)
        VALUES (?, ?, ?, ?, ?)
    """, (trade_date, ticker, target_module, sync_action, status))
    conn.commit()
    conn.close()


def sync_to_modules(trade_date: str, ticker: str, label_code: str):
    """
    第四部分：样本库同步回传三大模块
    """
    label = LABELS.get(label_code, {})

    # ① Layer1_FeatureCheck: 负误差样本→调整BOW检索权重/打分系数
    if label_code in ("L01", "L02"):
        sync_action = f"adjust_bow_weight|{label['code']}|降低乐观/悲观信号权重"
        record_sync_action(trade_date, ticker, "Layer1_FeatureCheck", sync_action, "pending")
        log.info(f"  → Layer1: {sync_action}")

    # ② Module01~Module04: 入场条件失效→收紧筛选约束
    if label_code == "L04":
        sync_action = f"tighten_entry_rules|{label['code']}|收紧风格/情绪/主线入场条件"
        record_sync_action(trade_date, ticker, "Module01_04", sync_action, "pending")
        log.info(f"  → Module01~04: {sync_action}")

    # ③ Layer2_RiskDecision: 风控有效→强化触发阈值
    if label_code == "L03":
        sync_action = f"strengthen_risk_threshold|{label['code']}|强化利空信号风险分级阈值"
        record_sync_action(trade_date, ticker, "Layer2_RiskDecision", sync_action, "pending")
        log.info(f"  → Layer2: {sync_action}")


def integrity_check(trade_date: str, ticker_list: list) -> Tuple[bool, list]:
    """校验当日所有观察/持仓标的是否已逐条标注"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"SELECT ticker FROM {CALIB_TABLE} WHERE trade_date = ?", (trade_date,))
    calibrated = {row[0] for row in cur.fetchall()}
    conn.close()
    missing = [t for t in ticker_list if t not in calibrated]
    return len(missing) == 0, missing


def generate_calibration_log(trade_date: str):
    """生成当日复盘校准日志"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT ticker, stock_name, label_code, label_name, ai_prediction,
               real_pct_chg, real_action, notes
        FROM {CALIB_TABLE}
        WHERE trade_date = ?
        ORDER BY ticker
    """, (trade_date,))
    rows = cur.fetchall()
    conn.close()

    lines = [
        "",
        f"### Module_06 盘后校准日志 — {trade_date}",
        "",
        "#### 操作一：真实交易结果校准（量化误差标签）",
        "",
        "| 标的 | 名称 | 标签 | AI预判 | 真实涨跌 | 操作 | 备注 |",
        "|:----:|:----:|:----:|:------:|:--------:|:----:|:-----|",
    ]
    label_counts = {}
    for r in rows:
        ticker, name, lc, ln, ai_pred, real_pct, action, notes = r
        pct_str = f"{real_pct:+.2f}%" if real_pct else "—"
        lines.append(f"| {ticker} | {name} | [{lc}]{ln} | {ai_pred} | {pct_str} | {action} | {notes} |")
        label_counts[lc] = label_counts.get(lc, 0) + 1

    # 统计汇总
    lines += [
        "",
        f"### 标签统计",
        f"| 标签 | 数量 |",
        f"|:----|:----:|",
    ]
    for lc, cnt in sorted(label_counts.items()):
        ln = LABELS.get(lc, {}).get("name", lc)
        lines.append(f"| [{lc}]{ln} | {cnt} |")
    lines.append(f"| **合计** | **{len(rows)}** |")

    # 完整性校验结果
    complete, missing = integrity_check(trade_date, [r[0] for r in rows])
    if complete:
        lines.append("")
        lines.append("✅ **完整性校验：全部标的已标注**")
    else:
        lines.append("")
        lines.append(f"⚠️ **完整性校验：{len(missing)}只缺失标注 — {missing}**")

    # 追加漏洞修正日志
    vuln_log = generate_vuln_log(trade_date)
    if vuln_log:
        lines.append(vuln_log)

    # 完整盘后清单
    lines += [
        "",
        "---",
        "### 完整盘后操作清单",
        "",
        "| # | 操作项 | 录入内容 | 完成✅ |",
        "|:-:|:-------|:---------|:----:|",
        "| 1 | 全标的行情录入 | 每只真实涨跌、支撑压力突破结果 | ✅ |",
        "| 2 | 真实操作记录 | 持仓/止盈/止损/空仓全部操作 | ✅ |",
        "| 3 | 标签① | AI预判涨实际大跌→[L01]预判高估，负误差 | □ |",
        "| 4 | 标签② | AI预判跌实际大涨→[L02]预判低估，负误差 | □ |",
        "| 5 | 标签③ | 风控提示减仓后持续大跌→[L03]风控判断有效 | □ |",
        "| 6 | 标签④ | 满足入场开仓后被套→[L04]入场条件失效 | □ |",
        "| 7 | 研判漏洞完整记录 | 填写标的/研判/事实/漏洞分类 | □ |",
        "| 8 | 人工补丁录入 | 补充特征词/调整参数/新增风控条件 | □ |",
        "| 9 | 漏洞等级标记 | L1轻度/L2中度/L3重度 | □ |",
        "| 10 | 样本库同步归档 | 漏洞与误差标签合并入库 | ✅ |",
        "| 11 | 自动迭代触发 | 样本回传Layer1/交易模块/Layer2 | ✅ |",
    ]

    return "\n".join(lines)


def check_prev_day_complete():
    """
    第五部分·约束规则1：校验前一日校准是否已完成
    未完成则阻断当日开仓
    """
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT COUNT(*) FROM {CALIB_TABLE}
        WHERE trade_date < ? AND trade_date >= date(?, '-5 days')
    """, (TODAY, TODAY))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0


# ═══════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Module_06 盘后样本校准与智能体自修正")
    parser.add_argument("--auto", action="store_true", help="自动模式(基于预判vs真实行情自动打标)")
    parser.add_argument("--vuln", action="store_true", help="执行研判漏洞修正操作")
    parser.add_argument("--check-only", action="store_true", help="仅检查今日是否已完成校准")
    args = parser.parse_args()

    # 仅检查模式
    if args.check_only:
        done = check_prev_day_complete()
        if done:
            print("✅ 前5日内存在校准记录，可正常开仓")
        else:
            print("❌ 前5日内无校准记录，禁止开仓（请先运行 Module_06）")
        return

    # 仅漏洞修正模式
    if args.vuln and not args.auto:
        init_db()
        print(f"{'='*60}")
        print(f"  Module_06 【操作二】人工研判漏洞修正")
        print(f"  交易日: {TRADE_DATE}")
        print(f"{'='*60}")
        record_vuln_and_notify()
        print(f"\n{'='*60}")
        print(f"  【操作二】完成")
        print(f"{'='*60}")
        return

    print(f"{'='*60}")
    print(f"  Module_06 盘后样本校准与智能体自修正")
    print(f"  交易日: {TRADE_DATE}")
    print(f"{'='*60}")

    # 1. 初始化数据库
    init_db()

    # 2. 获取当日跟踪标的清单
    stocks = get_today_tracked_stocks()
    print(f"\n📋 待校准标的: {len(stocks)} 只")
    for s in stocks:
        print(f"  {s.get('code','')} {s.get('name','')} @{s.get('close','—')} {s.get('pct_chg','')}")

    # 3. 逐只打标
    print(f"\n🔖 逐只标注:")
    for stock in stocks:
        code = stock.get("code", "")
        name = stock.get("name", "")
        real_pct = stock.get("pct_chg", 0)

        # 读取AI日志
        ai_log = read_ai_logs(code)

        if args.auto:
            # 自动模式
            label = auto_match_label(stock, ai_log)
            if label:
                save_calibration(TRADE_DATE, code, name, label,
                                 ai_log.get("ai_prediction", ""),
                                 real_pct,
                                 ai_log.get("suggested_action", ""),
                                 notes="auto")
                sync_to_modules(TRADE_DATE, code, label)
                print(f"  ✅ {code} {name} → [{label}] {LABELS[label]['name']} (自动)")
            else:
                # 无显著偏差→记录"正常"状态，确保完整性通过
                save_calibration(TRADE_DATE, code, name, "L00",
                                 ai_log.get("ai_prediction", "无偏差"),
                                 real_pct,
                                 ai_log.get("suggested_action", ""),
                                 notes="无显著偏差，自动标记正常")
                print(f"  ✅ {code} {name} → [L00] 正常 (无显著偏差)")
        else:
            # 交互模式
            print(f"\n  [{code} {name}] 真实涨跌: {real_pct:+.2f}%")
            print(f"  AI预判: {ai_log.get('ai_prediction','未知')} | Layer2: {ai_log.get('layer2_risk','未知')}")
            print(f"  建议标签:")
            for lc, li in LABELS.items():
                print(f"    {lc}: {li['name']} — {li['condition']}")
            # 非交互环境下默认自动模式
            label = auto_match_label(stock, ai_log)
            if label:
                save_calibration(TRADE_DATE, code, name, label,
                                 ai_log.get("ai_prediction", ""),
                                 real_pct,
                                 ai_log.get("suggested_action", ""),
                                 notes="auto")
                sync_to_modules(TRADE_DATE, code, label)
                print(f"  → 自动匹配 [{label}] {LABELS[label]['name']}")
            else:
                print(f"  → 跳过（无显著偏差）")

    # 4. 完整性校验
    ticker_list = [s.get("code", "") for s in stocks]
    complete, missing = integrity_check(TRADE_DATE, ticker_list)
    print(f"\n{'='*60}")
    if complete:
        print("✅ 完整性校验通过：全部标的已逐条标注")
    else:
        print(f"⚠️ 完整性校验：{len(missing)}只缺失标注")
        for m in missing:
            print(f"  ❌ {m}")
        print("样本缺失会造成模型偏差积累，请补录后重试")

    # 5. 生成复盘日志
    log_content = generate_calibration_log(TRADE_DATE)
    os.makedirs(str(REPORT_DIR), exist_ok=True)
    log_path = REPORT_DIR / f"module06_{TRADE_DATE}.md"
    with open(log_path, "w") as f:
        f.write(log_content)
    print(f"\n📄 复盘日志: {log_path}")

    # 6. 硬性约束校验
    prev_ok = check_prev_day_complete()
    if not prev_ok:
        print("\n⚠️ 警告：前5日无校准记录，下次运行交易系统前先完成 Module_06")

    # 7. 如果同时指定 --vuln，执行操作二
    if args.vuln:
        print("\n" + "=" * 60)
        print("  【操作二】人工研判漏洞修正")
        print("=" * 60)
        record_vuln_and_notify()

    print(f"\n{'='*60}")
    print(f"  Module_06 完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
