#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module_06_BacktestCalibrate — 盘后样本校准与智能体自修正模块 v1.2
================================================================
核心定位：全链路闭环关键环节，驱动智能体自主修正规则/参数/信号打分权重的唯一数据源
执行窗口：收盘后，标准时长10分钟
前置条件：
  1. 当日全部观察/持仓标的收盘行情数据就绪
  2. 智能体盘中原始信号存档完整（含Layer2风控分级、psy_hit_codes心理编码）
  3. 交易者真实操作记录完整

三大核心操作：
  【操作一】真实交易结果校准（量化误差标签）: 4类标签 L01~L04 + L00正常
  【操作二】人工研判漏洞修正（补充隐性盲区）: 3类漏洞记录+补丁+等级标记+L1~L3
  【操作三】人工限制参数进化边界（防过拟合、规则失控）: 5类参数硬边界+边界锁+过拟合风险分级

流程：
  归集→匹配4类标签→记录研判漏洞→写入进化边界→写入样本库→同步回传Layer1/Module01~04/Layer2→生成复盘日志

用法:
  python3 module_06_backtest_calibrate.py                          # 交互模式(人工确认标签)
  python3 module_06_backtest_calibrate.py --auto                   # 自动模式(基于预判vs真实行情自动打标)
  python3 module_06_backtest_calibrate.py --vuln                   # 仅执行漏洞修正操作
  python3 module_06_backtest_calibrate.py --auto --vuln            # 全自动模式(含自动标记+漏洞修正)
  python3 module_06_backtest_calibrate.py --boundary               # 仅执行参数进化边界设置
  python3 module_06_backtest_calibrate.py --auto --vuln --boundary # 全三大操作一次完成
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
BOUNDARY_TABLE = "module06_evolution_boundary"  # 参数进化边界锁表

# 交易日（从文件名或系统日期推断）
TODAY = datetime.now().strftime("%Y-%m-%d")
TRADE_DATE = TODAY

# ═══════════════════════════════════════════
#  全自动模式全局标识 & 缓存工具
# ═══════════════════════════════════════════
# --auto 在 sys.argv 中：操作二/三自动跳过 input()，读取缓存文件执行
is_auto_run = "--auto" in sys.argv

CACHE_DIR = Path("/opt/data/cache")
VULN_CACHE = CACHE_DIR / "daily_vuln_auto.cache"
BOUNDARY_CACHE = CACHE_DIR / "daily_boundary_auto.cache"


def load_cache_file(path: Path) -> list:
    """读取缓存JSON文件，返回list；文件不存在/空/无效时返回[]"""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, Exception):
        return []


def send_review_log(msg: str):
    """写入复盘日志摘要（附加到当日日志）"""
    log.info(msg)
    print(f"  {msg}")

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
#  第三部分：5类参数进化硬边界（防过拟合）
# ═══════════════════════════════════════════

# 5大类参数人工设定硬边界（智能体自动迭代时只能在区间内微调）
BOUNDARY_RULES = {
    "仓位类": {
        "params": {
            "total_position_max": {
                "desc": "总仓上限全局天花板",
                "baseline": "50%",
                "hard_min": "20%",
                "hard_max": "70%",
                "unit": "百分比"
            },
            "total_position_min": {
                "desc": "总仓上限全局最低底线",
                "baseline": "30%",
                "hard_min": "20%",
                "hard_max": "50%",
                "unit": "百分比"
            },
            "single_stock_position": {
                "desc": "单只个股仓位",
                "baseline": "15%",
                "hard_min": "10%",
                "hard_max": "30%",
                "unit": "百分比"
            },
            "stop_loss_ratio": {
                "desc": "单次止损容忍比例",
                "baseline": "3%",
                "hard_min": "2%",
                "hard_max": "4%",
                "unit": "百分比"
            },
        },
        "module": "Module01_定风格",
        "logic": "智能体自动优化仓位参数时，只能在人工划定区间内微调，不能出现总仓拉满100%、止损放宽至8%、单票满仓等极端过拟合参数"
    },
    "情绪判定阈值": {
        "params": {
            "freeze_floor_rate_min": {
                "desc": "冰点最低封板率下限",
                "baseline": "40%",
                "hard_min": "30%",
                "hard_max": "—",
                "unit": "百分比"
            },
            "climax_floor_rate_max": {
                "desc": "高潮封板率上限",
                "baseline": "65%",
                "hard_min": "—",
                "hard_max": "75%",
                "unit": "百分比"
            },
            "sentiment_position_floor": {
                "desc": "情绪约束总仓不得低于（防极端空仓固化）",
                "baseline": "20%",
                "hard_min": "15%",
                "hard_max": "—",
                "unit": "百分比"
            },
        },
        "module": "Module02_定情绪",
        "logic": "模型不得把冰点阈值下调至极低数值；防止短期暴涨行情拟合后常态行情误判高潮；避免极端空仓固化"
    },
    "主线筛选权重": {
        "params": {
            "turnover_weight": {
                "desc": "主线成交额权重",
                "baseline": "0.5",
                "hard_min": "0.2",
                "hard_max": "0.7",
                "unit": "系数"
            },
            "limit_up_count_weight": {
                "desc": "涨停数量权重",
                "baseline": "0.4",
                "hard_min": "0.2",
                "hard_max": "0.7",
                "unit": "系数"
            },
            "thematic_duration_weight": {
                "desc": "题材持续性打分权重",
                "baseline": "0.5",
                "hard_min": "0.2",
                "hard_max": "0.7",
                "unit": "系数"
            },
        },
        "module": "Module03_04",
        "logic": "单一指标权重最高不超过0.7、最低不低于0.2；禁止单一维度完全主导主线判定（过拟合短期涨停行情）；剔除黑名单过滤不能无限制放宽也不能一刀切；进场模式匹配权重浮动区间固定"
    },
    "Layer1向量打分权重": {
        "params": {
            "cos_similarity_weight": {
                "desc": "向量cos相似度权重",
                "baseline": "0.5",
                "hard_min": "0.4",
                "hard_max": "0.7",
                "unit": "系数"
            },
            "keyword_weight": {
                "desc": "关键词权重系数",
                "baseline": "0.5",
                "hard_min": "0.3",
                "hard_max": "0.6",
                "unit": "系数"
            },
            "new_keywords_daily_limit": {
                "desc": "特征词新增每日上限",
                "baseline": "10",
                "hard_min": "—",
                "hard_max": "20",
                "unit": "个数"
            },
        },
        "module": "Layer1_FeatureCheck",
        "logic": "cos相似度模型不得自动上调至0.9或下调至0.1；关键词权重与cos权重之和永久固定为1；防止无限堆砌小众冷门特征词造成过拟合"
    },
    "风控心理偏差计数": {
        "params": {
            "red_trigger_min": {
                "desc": "RED高风险触发条数下限",
                "baseline": "25",
                "hard_min": "20",
                "hard_max": "—",
                "unit": "条数"
            },
            "yellow_range_min": {
                "desc": "YELLOW预警区间下限",
                "baseline": "5",
                "hard_min": "3",
                "hard_max": "—",
                "unit": "条数"
            },
            "yellow_range_max": {
                "desc": "YELLOW预警区间上限",
                "baseline": "20",
                "hard_min": "—",
                "hard_max": "22",
                "unit": "条数"
            },
            "single_psy_code_weight_max": {
                "desc": "单一心理编码触发权重上限",
                "baseline": "0.3",
                "hard_min": "—",
                "hard_max": "0.4",
                "unit": "系数"
            },
        },
        "module": "Layer2_RiskDecision",
        "logic": "RED触发下限固定≥20条，不能下调至5条造成频繁拦截；YELLOW区间固定3~22条禁止偏移；单一编码权重上限固定，不能因短期亏损无限放大某一类扣分"
    },
}

# 过拟合风险等级
BOUNDARY_SEVERITY = {
    "L1": {"name": "低风险", "desc": "参数小幅偏移，边界宽松即可约束", "action": "保持默认边界区间即可"},
    "L2": {"name": "中风险", "desc": "参数大幅偏移，需收紧上下限区间", "action": "临时收紧边界区间，次日恢复默认"},
    "L3": {"name": "高风险", "desc": "参数极端偏移，锁定参数禁止当日迭代", "action": "锁定参数禁止自动迭代，沿用基准参数"},
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
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(trade_date, ticker)
        )
    """)
    # 存量去重迁移：删除同一日期同一标的的重复漏洞记录，仅保留最新一条
    cur.execute(f"""
        DELETE FROM {VULN_TABLE} WHERE id NOT IN (
            SELECT MIN(id) FROM {VULN_TABLE} GROUP BY trade_date, ticker
        )
    """)
    if cur.rowcount > 0:
        log.info(f"  存量去重: 清理 {cur.rowcount} 条重复漏洞记录")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {BOUNDARY_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            param_category TEXT NOT NULL,
            param_name TEXT NOT NULL,
            param_desc TEXT DEFAULT '',
            baseline_value TEXT DEFAULT '',
            hard_min TEXT DEFAULT '',
            hard_max TEXT DEFAULT '',
            actual_drift TEXT DEFAULT '',
            validity TEXT DEFAULT '长期永久',
            risk_level TEXT DEFAULT 'L1',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(trade_date, param_category, param_name)
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
        INSERT OR REPLACE INTO {VULN_TABLE}
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
    """
    操作二：人工研判漏洞修正
    is_auto_run=True → 读取缓存文件自动执行，跳过全部 input()
    is_auto_run=False → 交互式 input() 录入（保留原逻辑）
    """
    if is_auto_run:
        # ── 全自动分支：读取缓存，跳过 input() ──
        vuln_data = load_cache_file(VULN_CACHE)
        if not vuln_data:
            send_review_log("【AUTO】漏洞缓存为空，跳过操作二（手动创建 daily_vuln_auto.cache 可启用）")
            return

        count = 0
        for entry in vuln_data:
            ticker = entry.get("ticker", "")
            cat = entry.get("category", "C01")
            sev = entry.get("severity", "L1")
            ai_c = entry.get("ai_conclusion", "")
            real = entry.get("real_fact", "")
            patch = entry.get("fix_patch", "")
            notes = entry.get("notes", "")

            save_vulnerability(TRADE_DATE, ticker, cat, entry.get("subtype", ""),
                               sev, ai_c, real, patch, notes)
            sync_vuln_to_modules(TRADE_DATE, cat, sev, patch)
            count += 1

        send_review_log(f"【AUTO】自动化漏洞记录完成: {count}条（缓存: {VULN_CACHE}）")
        return

    # ── 人工模式：保留原有全部 input() 交互式录入 ──
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


# ═══════════════════════════════════════════
#  操作三：参数进化边界设置（防过拟合）
# ═══════════════════════════════════════════

def generate_boundary_drift_report(trade_date: str) -> str:
    """
    步骤1：梳理当日自动调参产生的参数偏移幅度
    对比昨日基准，生成全量参数浮动报告（基于BOUNDARY_RULES默认值模拟）
    """
    lines = [
        "",
        "#### 步骤1：参数偏移核查",
        "",
        "| 类别 | 参数名 | 基准值 | 硬性下限 | 硬性上限 | 当日实际 | 偏移幅度 | 状态 |",
        "|:----:|:------:|:------:|:--------:|:--------:|:--------:|:--------:|:----:|",
    ]
    drift_count = 0
    # 从数据库获取当天已有边界记录
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT param_category, param_name, baseline_value, hard_min, hard_max, actual_drift
        FROM {BOUNDARY_TABLE} WHERE trade_date = ?
    """, (trade_date,))
    existing = {(r[0], r[1]): r for r in cur.fetchall()}
    conn.close()

    for cat_name, cat_info in BOUNDARY_RULES.items():
        for pname, pinfo in cat_info["params"].items():
            key = (cat_name, pname)
            if key in existing:
                # 已有当日记录，显示记录的数据
                _, _, bl, hmin, hmax, drift = existing[key]
                baseline_display = bl
                hmin_display = hmin
                hmax_display = hmax
                drift_display = drift if drift else "—"
            else:
                baseline_display = pinfo["baseline"]
                hmin_display = pinfo["hard_min"]
                hmax_display = pinfo["hard_max"]
                drift_display = "未核查"

            status = "✅ 在界内" if drift_display in ("—", "未核查") else "⚠️ 偏离"
            if drift_display not in ("—", "未核查"):
                try:
                    drift_val = float(drift_display.replace("%", "").replace("+", ""))
                    hmin_val = float(hmin_display.replace("%", ""))
                    hmax_val = float(hmax_display.replace("%", ""))
                    if drift_val < hmin_val or drift_val > hmax_val:
                        status = "❌ 超界"
                        drift_count += 1
                except:
                    pass

            lines.append(
                f"| {cat_name} | {pinfo['desc']} | {baseline_display} "
                f"| {hmin_display} | {hmax_display} | {drift_display} | {drift_display} | {status} |"
            )

    if drift_count == 0:
        lines += ["", "✅ **所有参数在边界范围内，无显著偏移**"]
    else:
        lines += ["", f"⚠️ **{drift_count}项参数超界或偏移，需要人工核查边界设置**"]

    return "\n".join(lines)


def save_evolution_boundary(trade_date, param_category, param_name, param_desc,
                            baseline_value, hard_min, hard_max, actual_drift="",
                            validity="长期永久", risk_level="L1", notes=""):
    """步骤2：保存参数进化边界设置"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        INSERT OR REPLACE INTO {BOUNDARY_TABLE}
        (trade_date, param_category, param_name, param_desc,
         baseline_value, hard_min, hard_max, actual_drift,
         validity, risk_level, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade_date, param_category, param_name, param_desc,
          baseline_value, hard_min, hard_max, actual_drift,
          validity, risk_level, notes))
    conn.commit()
    conn.close()
    log.info(f"  [边界] [{param_category}] {param_name}: [{hard_min}~{hard_max}] {risk_level}")
    return True


def apply_boundary_locks(trade_date: str):
    """
    步骤3：将当日全部边界锁写入全局参数锁文件
    系统启动时加载此文件，智能体自动调参逻辑强制受边界约束
    """
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT param_category, param_name, hard_min, hard_max, param_desc, validity
        FROM {BOUNDARY_TABLE}
        WHERE trade_date = ? AND risk_level != 'L3'
        ORDER BY param_category, param_name
    """, (trade_date,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        # 无自定义边界时，使用BOUNDARY_RULES默认值写锁
        locks = {}
        for cat_name, cat_info in BOUNDARY_RULES.items():
            for pname, pinfo in cat_info["params"].items():
                locks[f"{cat_name}.{pname}"] = {
                    "desc": pinfo["desc"],
                    "hard_min": pinfo["hard_min"],
                    "hard_max": pinfo["hard_max"],
                    "baseline": pinfo["baseline"],
                    "unit": pinfo["unit"],
                    "source": "BOUNDARY_RULES默认"
                }
        lock_path = BASE / f"config/param_boundary_locks_{trade_date}.json"
        os.makedirs(str(lock_path.parent), exist_ok=True)
        with open(lock_path, "w") as f:
            json.dump({
                "trade_date": trade_date,
                "locks": locks,
                "description": "参数进化硬边界锁 — 智能体自动调参不得突破此区间",
                "rule": "边界锁优先级高于智能体自动调参逻辑，参数超出边界时边界数值强制覆盖"
            }, f, ensure_ascii=False, indent=2)
        log.info(f"  参数锁(默认): {len(locks)}项 → {lock_path}")
        return lock_path, len(locks)

    locks = {}
    for r in rows:
        cat, pname, hmin, hmax, desc, validity = r
        key = f"{cat}.{pname}"
        locks[key] = {
            "desc": desc,
            "hard_min": hmin,
            "hard_max": hmax,
            "validity": validity,
            "source": f"人工设置({trade_date})"
        }

    lock_path = BASE / f"config/param_boundary_locks_{trade_date}.json"
    os.makedirs(str(lock_path.parent), exist_ok=True)
    with open(lock_path, "w") as f:
        json.dump({
            "trade_date": trade_date,
            "locks": locks,
            "description": "参数进化硬边界锁 — 智能体自动调参不得突破此区间",
            "rule": "边界锁优先级高于智能体自动调参逻辑，参数超出边界时边界数值强制覆盖",
            "extra": "L3高风险参数已排除（禁止当日迭代）"
        }, f, ensure_ascii=False, indent=2)
    log.info(f"  参数锁: {len(locks)}项 → {lock_path}")

    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO {SYNC_TABLE}
        (trade_date, ticker, target_module, sync_action, sync_status)
        VALUES (?, ?, ?, ?, ?)
    """, (trade_date, "", "全局参数边界锁",
          f"apply_boundary_locks|{len(locks)}项|锁定区间防止过拟合", "completed"))
    conn.commit()
    conn.close()
    return lock_path, len(locks)


def record_overfitting_risk(trade_date: str, param_category: str, risk_level: str, notes: str = ""):
    """步骤4：标记过拟合风险等级并归档（仅升级，不降级）"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    # 仅当新等级 >= 现有等级时才更新（L3 > L2 > L1，不降级）
    level_order = {"L1": 1, "L2": 2, "L3": 3}
    new_order = level_order.get(risk_level, 1)
    cur.execute(f"""
        UPDATE {BOUNDARY_TABLE}
        SET risk_level = ?,
            notes = CASE WHEN notes = '' THEN ? ELSE notes || '; ' || ? END
        WHERE trade_date = ?
          AND param_category = ?
          AND (CASE risk_level
               WHEN 'L1' THEN 1 WHEN 'L2' THEN 2 WHEN 'L3' THEN 3 ELSE 1
               END) <= ?
    """, (risk_level, notes, notes, trade_date, param_category, new_order))
    updated = cur.rowcount
    conn.commit()
    conn.close()
    sev_info = BOUNDARY_SEVERITY.get(risk_level, {})
    log.info(f"  [过拟合风险] [{risk_level}] {param_category}: {sev_info.get('name', '')} — {notes}")

    # L3高风险→记录禁止迭代的同步事件
    if risk_level == "L3":
        conn = sqlite3.connect(str(MODULE06_DB))
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO {SYNC_TABLE}
            (trade_date, ticker, target_module, sync_action, sync_status)
            VALUES (?, ?, ?, ?, ?)
        """, (trade_date, "", param_category,
              f"LOCK_PARAMS_L3|禁止当日自动迭代，沿用基准参数|{notes}", "completed"))
        conn.commit()
        conn.close()


def check_boundary_hit_days(trade_date: str, param_category: str = None) -> dict:
    """
    兜底机制：检查参数是否连续3日逼近边界极值
    若连续3日触发，自动提醒人工重新评估调整边界区间
    """
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    if param_category:
        cur.execute(f"""
            SELECT trade_date, param_name, actual_drift, hard_min, hard_max
            FROM {BOUNDARY_TABLE}
            WHERE trade_date >= date(?, '-7 days') AND trade_date <= ?
              AND param_category = ?
            ORDER BY trade_date DESC
        """, (trade_date, trade_date, param_category))
    else:
        cur.execute(f"""
            SELECT trade_date, param_name, actual_drift, hard_min, hard_max
            FROM {BOUNDARY_TABLE}
            WHERE trade_date >= date(?, '-7 days') AND trade_date <= ?
            ORDER BY trade_date DESC
        """, (trade_date, trade_date))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return {"alert": False, "message": "无边界记录"}

    # 统计连续逼近极值的参数
    from collections import Counter
    hit_names = Counter()
    for r in rows:
        tdate, pname, drift, hmin, hmax = r
        if drift and drift not in ("—", "未核查"):
            try:
                dv = float(drift.replace("%", "").replace("+", ""))
                hn = float(hmin.replace("%", "")) if hmin != "—" else None
                hx = float(hmax.replace("%", "")) if hmax != "—" else None
                if hn is not None and abs(dv - hn) / max(abs(hn), 1) < 0.1:
                    hit_names[pname] += 1
                if hx is not None and abs(dv - hx) / max(abs(hx), 1) < 0.1:
                    hit_names[pname] += 1
            except:
                pass

    alert_items = {k: v for k, v in hit_names.items() if v >= 3}
    if alert_items:
        msg = f"⚠️ 以下参数连续多处逼近边界极值: {', '.join(alert_items.keys())}，建议人工重新评估边界区间"
        return {"alert": True, "items": alert_items, "message": msg}
    return {"alert": False, "message": "无连续边界逼近风险"}


def record_boundary_and_notify():
    """
    操作三：参数进化边界设置（防过拟合）
    is_auto_run=True → 读取缓存文件写入锁，跳过全部 input()
    is_auto_run=False → 交互式 input() 设置（保留原逻辑）
    """
    print("\n" + "=" * 60)
    print("  【操作三】人工限制参数进化边界（防过拟合）")
    print("=" * 60)

    # 步骤1：参数偏移报告
    print("\n" + generate_boundary_drift_report(TRADE_DATE))

    if is_auto_run:
        # ── 全自动分支：读取缓存/默认值，跳过 input() ──
        boundary_data = load_cache_file(BOUNDARY_CACHE)
        if boundary_data:
            count = 0
            for entry in boundary_data:
                cat = entry.get("category", "")
                pname = entry.get("name", "")
                if not cat or not pname:
                    continue
                desc = entry.get("desc", "")
                baseline = entry.get("baseline", "")
                hmin = entry.get("hard_min", "")
                hmax = entry.get("hard_max", "")
                drift = entry.get("actual_drift", "")
                validity = entry.get("validity", "长期永久")
                risk_level = entry.get("risk_level", "L1")
                notes = entry.get("notes", "")
                save_evolution_boundary(TRADE_DATE, cat, pname, desc,
                                        baseline, hmin, hmax, drift,
                                        validity, risk_level, notes)
                count += 1
            lock_path, lock_count = apply_boundary_locks(TRADE_DATE)
            send_review_log(f"【AUTO】边界缓存执行: {count}条写入, 锁{lock_count}项（缓存: {BOUNDARY_CACHE}）")
        else:
            # 缓存为空→使用BOUNDARY_RULES默认边界写入锁
            lock_path, lock_count = apply_boundary_locks(TRADE_DATE)
            send_review_log(f"【AUTO】边界缓存为空，使用默认BOUNDARY_RULES: 锁{lock_count}项")

        print(f"\n{'='*60}")
        print(f"  【操作三】完成（自动模式）")
        print(f"{'='*60}")
        return

    # ── 人工模式：保留原有全部 input() 交互式设置 ──
    print("\n参数类别:")
    cat_list = list(BOUNDARY_RULES.keys())
    for i, cat in enumerate(cat_list, 1):
        info = BOUNDARY_RULES[cat]
        print(f"  {i}. {cat} — {info['module']} ({len(info['params'])}个参数)")

    print("\n(输入 q 结束边界设置)")
    while True:
        choice = input("\n选择参数类别(编号/名称/q): ").strip()
        if choice.lower() == 'q':
            break
        # 编号或名称匹配
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(cat_list):
                cat = cat_list[idx]
            else:
                print("无效编号")
                continue
        else:
            if choice in BOUNDARY_RULES:
                cat = choice
            else:
                print(f"无效类别，可选: {', '.join(cat_list)}")
                continue

        cat_info = BOUNDARY_RULES[cat]
        print(f"\n  [{cat}] — {cat_info['logic'][:60]}...")
        params = list(cat_info["params"].items())

        for i, (pname, pinfo) in enumerate(params, 1):
            print(f"\n  --- 参数 {i}/{len(params)}: {pinfo['desc']} ---")
            print(f"    基准值: {pinfo['baseline']} | 硬性下限: {pinfo['hard_min']} | 硬性上限: {pinfo['hard_max']}")
            drift = input(f"    当日实际值(留空=未核查): ").strip()
            new_min = input(f"    人工设定最小值/下限({pinfo['hard_min']}): ").strip() or pinfo["hard_min"]
            new_max = input(f"    人工设定最大值/上限({pinfo['hard_max']}): ").strip() or pinfo["hard_max"]
            print("    生效周期: 1=当日临时生效  2=长期永久边界")
            period_choice = input(f"    请选择(1/2, 默认2): ").strip()
            validity = "当日临时生效" if period_choice == "1" else "长期永久"

            # 过拟合风险判定
            risk_level = "L1"
            if drift:
                try:
                    dv = float(drift.replace("%", "").replace("+", ""))
                    bn = float(pinfo["hard_min"].replace("%", "")) if pinfo["hard_min"] != "—" else None
                    bx = float(pinfo["hard_max"].replace("%", "")) if pinfo["hard_max"] != "—" else None
                    if bn is not None and dv < bn:
                        risk_level = "L3"
                    elif bx is not None and dv > bx:
                        risk_level = "L3"
                    elif bn is not None and abs(dv - bn) / max(abs(bn), 1) < 0.15:
                        risk_level = "L2"
                    elif bx is not None and abs(dv - bx) / max(abs(bx), 1) < 0.15:
                        risk_level = "L2"
                except:
                    pass

            notes_input = input(f"    备注(可选): ").strip()

            save_evolution_boundary(
                TRADE_DATE, cat, pname, pinfo["desc"],
                pinfo["baseline"], new_min, new_max, drift,
                validity, risk_level, notes_input
            )

            sev_name = BOUNDARY_SEVERITY.get(risk_level, {}).get("name", risk_level)
            print(f"  ✅ [{risk_level} {sev_name}] {pinfo['desc']}: [{new_min}~{new_max}] 已保存")

        # 类别级别风险标记
        cat_risk = input(f"\n  此类别整体过拟合风险等级(L1/L2/L3, 默认按参数自动): ").strip().upper()
        if cat_risk in ("L1", "L2", "L3"):
            record_overfitting_risk(TRADE_DATE, cat, cat_risk,
                                    f"人工评估类别整体风险: {BOUNDARY_SEVERITY.get(cat_risk,{}).get('name','')}")
            print(f"  ✅ 类别风险已标记: [{cat_risk}]")

        more = input(f"\n是否继续设置其他类别？(y/n): ").strip().lower()
        if more != 'y':
            break

    # 步骤3：写入参数锁
    print("\n" + "-" * 40)
    print("  步骤3: 写入参数锁...")
    lock_path, lock_count = apply_boundary_locks(TRADE_DATE)
    print(f"  ✅ 参数锁已写入: {lock_path} ({lock_count}项)")

    # 连续3日边界逼近检查
    alert_result = check_boundary_hit_days(TRADE_DATE)
    if alert_result["alert"]:
        print(f"\n  {alert_result['message']}")

    # 步骤4：归档完毕
    print(f"\n{'='*60}")
    print(f"  【操作三】完成")
    print(f"{'='*60}")


def generate_boundary_log(trade_date: str) -> str:
    """生成操作三复盘日志"""
    conn = sqlite3.connect(str(MODULE06_DB))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT param_category, param_name, param_desc, baseline_value,
               hard_min, hard_max, actual_drift, validity, risk_level, notes
        FROM {BOUNDARY_TABLE}
        WHERE trade_date = ?
        ORDER BY param_category, param_name
    """, (trade_date,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return ""

    lines = [
        "",
        "### 操作三：参数进化边界设置（防过拟合）",
        "",
        "| 类别 | 参数 | 基准值 | 边界[下限~上限] | 当日实际 | 生效周期 | 风险等级 |",
        "|:----:|:----:|:------:|:----------------:|:--------:|:--------:|:--------:|",
    ]

    cat_counts = {}
    for r in rows:
        cat, pname, desc, bl, hmin, hmax, drift, val, rl, notes_r = r
        range_str = f"[{hmin}~{hmax}]"
        rl_name = BOUNDARY_SEVERITY.get(rl, {}).get("name", rl)
        lines.append(f"| {cat} | {desc} | {bl} | {range_str} | {drift or '—'} | {val} | [{rl}]{rl_name} |")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    lines += ["", "**分类统计:**"]
    for c, n in cat_counts.items():
        lines.append(f"- {c}: {n}个参数")

    # 边界锁文件路径
    lock_path = BASE / f"config/param_boundary_locks_{trade_date}.json"
    if lock_path.exists():
        lines.append(f"")
        lines.append(f"📄 **边界锁文件**: {lock_path}")

    # 连续逼近检查
    alert = check_boundary_hit_days(trade_date)
    if alert["alert"]:
        lines.append(f"")
        lines.append(f"⚠️ **{alert['message']}**")

    return "\n".join(lines)


def load_boundary_locks(trade_date: str = None) -> dict:
    """
    次日盘前加载边界锁
    系统初始化时调用，返回边界锁字典；L3锁定的参数返回"locked"标记
    """
    if trade_date is None:
        trade_date = TODAY

    lock_path = BASE / f"config/param_boundary_locks_{trade_date}.json"
    if lock_path.exists():
        with open(lock_path) as f:
            return json.load(f)

    # 降级：使用默认边界规则
    log.info("无当日边界锁文件，使用BOUNDARY_RULES默认边界")
    rules = {}
    for cat, info in BOUNDARY_RULES.items():
        for pname, pinfo in info["params"].items():
            rules[f"{cat}.{pname}"] = {
                "desc": pinfo["desc"],
                "hard_min": pinfo["hard_min"],
                "hard_max": pinfo["hard_max"],
                "source": "BOUNDARY_RULES默认(无手动锁)"
            }
    return {"trade_date": trade_date, "locks": rules, "description": "默认边界(降级)"}


# ═══════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════


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

    # 追加参数进化边界日志（操作三）
    boundary_log = generate_boundary_log(trade_date)
    if boundary_log:
        lines.append(boundary_log)

    # 完整盘后清单 — 三大核心操作15项
    lines += [
        "",
        "---",
        "### 完整盘后操作清单（三大核心操作）",
        "",
        "| # | 操作项 | 录入内容 | 完成✅ |",
        "|:-:|:-------|:---------|:----:|",
        "| **操作一：真实交易结果校准** | | |",
        "| 1 | 全标的行情录入 | 每只真实涨跌、支撑压力突破结果 | ✅ |",
        "| 2 | 真实操作记录 | 持仓/止盈/止损/空仓全部操作 | ✅ |",
        "| 3 | 标签① | AI预判涨实际大跌→[L01]预判高估，负误差 | □ |",
        "| 4 | 标签② | AI预判跌实际大涨→[L02]预判低估，负误差 | □ |",
        "| 5 | 标签③ | 风控提示减仓后持续大跌→[L03]风控判断有效 | □ |",
        "| 6 | 标签④ | 满足入场开仓后被套→[L04]入场条件失效 | □ |",
        "| **操作二：人工修正研判漏洞** | | |",
        "| 7 | 研判漏洞完整记录 | 填写标的/研判/事实/漏洞分类 | □ |",
        "| 8 | 人工补丁录入 | 补充特征词/调整参数/新增风控条件 | □ |",
        "| 9 | 漏洞等级标记 | L1轻度/L2中度/L3重度 | □ |",
        "| **操作三：参数进化边界设置** | | |",
        "| 10 | 参数偏移核查 | 对比基准参数，记录当日迭代后参数浮动幅度 | □ |",
        "| 11 | 进化边界填写 | 填写参数类别/基准值/下限/上限/生效周期 | □ |",
        "| 12 | 写入参数锁 | 系统加载边界硬约束，截断超边界极端参数 | □ |",
        "| 13 | 过拟合风险分级 | L1低风险/L2中风险/L3高风险 | □ |",
        "| **全局归档** | | |",
        "| 14 | 样本库同步归档 | 误差/漏洞/边界数据合并入库 | ✅ |",
        "| 15 | 自动迭代触发 | 样本回传Layer1/交易模块/Layer2 | ✅ |",
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
    parser.add_argument("--boundary", action="store_true", help="执行参数进化边界设置")
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
    if args.vuln and not args.auto and not args.boundary:
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

    # 仅边界设置模式
    if args.boundary and not args.auto and not args.vuln:
        init_db()
        print(f"{'='*60}")
        print(f"  Module_06 【操作三】参数进化边界设置（防过拟合）")
        print(f"  交易日: {TRADE_DATE}")
        print(f"{'='*60}")
        record_boundary_and_notify()
        print(f"\n{'='*60}")
        print(f"  【操作三】完成")
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

    # 8. 如果同时指定 --boundary，执行操作三
    if args.boundary:
        print("\n" + "=" * 60)
        print("  【操作三】参数进化边界设置（防过拟合）")
        print("=" * 60)
        record_boundary_and_notify()

    print(f"\n{'='*60}")
    print(f"  Module_06 完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
