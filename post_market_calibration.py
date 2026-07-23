#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
post_market_calibration.py — 盘后样本人工校准与智能体自修正流水线

执行时机: 收盘后10分钟内 (17:00~17:10)
前置条件:
  1. 当日全部标的收盘行情数据就绪
  2. 智能体盘中原始信号存档完整
  3. 交易者真实操作记录完整

流程:
  归集→匹配标签→校验→推送迭代
"""

import logging
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [CALIB] %(message)s",
                    datefmt="%H:%M:%S")

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
CALIB_TABLE = "daily_calibration"

# ====================== 10类误差标签定义 ======================

LABEL_RULES = {
    "rule_01": {
        "condition": "AI预判上涨，标的实际大跌",
        "tag": "预判高估，负误差",
    },
    "rule_02": {
        "condition": "AI预判下跌，标的实际大涨",
        "tag": "预判低估，负误差",
    },
    "rule_03": {
        "condition": "AI提示减仓，后续股价持续大跌",
        "tag": "风控判断有效",
    },
    "rule_04": {
        "condition": "AI提示入场，开仓后被套",
        "tag": "入场条件失效",
    },
    "rule_05": {
        "condition": "AI预判震荡，行情单边突破压力/跌破支撑",
        "tag": "区间判断失效",
    },
    "rule_06": {
        "condition": "AI提示持有，持仓持续创出新高顺利止盈",
        "tag": "持仓信号有效",
    },
    "rule_07": {
        "condition": "AI提示观望空仓，后续走出下跌行情",
        "tag": "规避信号有效",
    },
    "rule_08": {
        "condition": "AI提示观望空仓，后续走出上涨行情",
        "tag": "机会漏判，负误差",
    },
    "rule_09": {
        "condition": "触发止损规则后股价立刻反弹",
        "tag": "止损阈值误判",
    },
    "rule_10": {
        "condition": "止盈离场后行情继续上行",
        "tag": "止盈阈值偏保守",
    },
}

# 误差等级分类
ERROR_TAGS = {"预判高估，负误差", "预判低估，负误差", "入场条件失效",
              "区间判断失效", "机会漏判，负误差", "止损阈值误判", "止盈阈值偏保守"}
EFFECTIVE_TAGS = {"风控判断有效", "持仓信号有效", "规避信号有效"}


# ====================== 数据库 ======================

def ensure_table():
    """确保 daily_calibration 表存在"""
    conn = sqlite3.connect(str(MEMORY_DB))
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {CALIB_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            ai_pred TEXT,                -- AI预判方向
            ai_risk_tip TEXT,            -- 风控提示
            real_change_pct REAL,        -- 真实涨跌幅
            close_price REAL,            -- 收盘价
            support_resistance TEXT,     -- 支撑压力突破验证
            real_trade_action TEXT,       -- 人工操作
            error_label TEXT,            -- 误差标签
            short_attribution TEXT,      -- 简短归因(选填)
            is_trapped INTEGER DEFAULT 0,-- 是否被套
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(trade_date, ts_code)
        )
    """)
    conn.commit()
    conn.close()


def insert_record(row: dict) -> bool:
    """录入一条校准记录"""
    ensure_table()
    try:
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute(f"""
            INSERT OR REPLACE INTO {CALIB_TABLE}
            (trade_date, ts_code, ai_pred, ai_risk_tip, real_change_pct,
             close_price, support_resistance, real_trade_action,
             error_label, short_attribution, is_trapped)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row["trade_date"],
            row["ts_code"],
            row.get("ai_pred", ""),
            row.get("ai_risk_tip", ""),
            row.get("real_change_pct", 0.0),
            row.get("close_price", 0.0),
            row.get("support_resistance", ""),
            row.get("real_trade_action", ""),
            row.get("error_label", ""),
            row.get("short_attribution", ""),
            1 if "被套" in row.get("short_attribution", "") else 0,
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"  入库失败: {e}")
        return False


# ====================== 动态加权映射 (Module0: 前置量化层) ======================

def analyze_dynamic_mapping(record: dict) -> dict:
    """
    对单条校准记录执行动态加权多对多映射分析。

    这是原有10类标签体系的**前置量化判定层**。
    返回结果作为post_market_calibration的输入补充。
    """
    from dynamic_weight_mapping import DynamicMappingEngine

    # 从record提取因子值
    factors = {
        "price_surge_60d":  abs(record.get("real_change_pct", 0)) * 3,
        "price_surge_20d":  abs(record.get("real_change_pct", 0)) * 2,
        "price_surge_3d":   abs(record.get("real_change_pct", 0)),
        "debt_ratio":       record.get("debt_ratio", 50),
        "pe_deviation":     abs(record.get("pe_ttm", 20)),
        "volume_ratio":     1.0,
        "consecutive_up_days": 1 if record.get("real_change_pct", 0) > 0 else 0,
        "board_divergence": abs(record.get("real_change_pct", 0)),
        "retail_inflow_ratio": 0,
        "institutional_outflow": 0,
        "turnover_spike":   0,
        "goodwill_ratio":   record.get("goodwill_ratio", 0),
        "profit_decline":   max(0, -record.get("real_change_pct", 0)),
        "short_term_volatility": abs(record.get("real_change_pct", 0)),
        "concept_purity":   100,
    }

    engine = DynamicMappingEngine()
    return engine.analyze_stock(
        factors,
        stock_code=record.get("ts_code", ""),
        trade_date=record.get("trade_date", ""),
    )

def auto_match_label(ai_pred: str, ai_risk_tip: str,
                     real_change: float, real_action: str,
                     support_resistance: str) -> tuple[str, str]:
    """
    根据AI信号与真实行情自动匹配最合适的误差标签。

    参数:
        ai_pred: 预判上涨/预判下跌/预判中性
        ai_risk_tip: 提示入场/提示减仓/持有不动
        real_change: 真实涨跌幅(%)
        real_action: 持仓/止盈/止损/空仓
        support_resistance: 支撑压力突破验证

    返回:
        (rule_id, tag)
    """
    rules_triggered = []

    # rule_01: AI看涨 + 实际大跌(≤-2%)
    if "上涨" in ai_pred and real_change <= -2:
        rules_triggered.append(("rule_01", "预判高估，负误差"))

    # rule_02: AI看跌 + 实际大涨(≥2%)
    if "下跌" in ai_pred and real_change >= 2:
        rules_triggered.append(("rule_02", "预判低估，负误差"))

    # rule_03: 提示减仓 + 后续持续大跌
    if "减仓" in ai_risk_tip and real_change <= -3:
        rules_triggered.append(("rule_03", "风控判断有效"))

    # rule_04: 提示入场 + 开仓被套(止损离场)
    if "入场" in ai_risk_tip and real_action == "止损":
        rules_triggered.append(("rule_04", "入场条件失效"))

    # rule_05: 预判震荡 + 单边突破/跌破
    if "中性" in ai_pred or "震荡" in ai_pred:
        if any(kw in support_resistance for kw in ["突破", "跌破"]):
            rules_triggered.append(("rule_05", "区间判断失效"))

    # rule_06: 提示持有 + 止盈离场
    if "持有" in ai_risk_tip and real_action == "止盈":
        rules_triggered.append(("rule_06", "持仓信号有效"))

    # rule_07: 提示观望/空仓 + 实际下跌
    if "持有" in ai_risk_tip or "空仓" in real_action:
        if real_change <= -2:
            rules_triggered.append(("rule_07", "规避信号有效"))

    # rule_08: 提示观望/空仓 + 实际大涨
    if "空仓" in real_action and real_change >= 3:
        rules_triggered.append(("rule_08", "机会漏判，负误差"))

    # rule_09: 止损后反弹(需要人工判断)
    if real_action == "止损" and real_change > 0:
        rules_triggered.append(("rule_09", "止损阈值误判"))

    # rule_10: 止盈后继续上行(需要人工判断)
    if real_action == "止盈":
        rules_triggered.append(("rule_10", "止盈阈值偏保守"))

    # 选取最匹配的规则: 优先负误差(风险敏感), 其次有效信号
    error_rules = [r for r in rules_triggered if r[1] in ERROR_TAGS]
    effective_rules = [r for r in rules_triggered if r[1] in EFFECTIVE_TAGS]

    if error_rules:
        return error_rules[0]
    if effective_rules:
        return effective_rules[0]
    if rules_triggered:
        return rules_triggered[0]

    return ("", "")


# ====================== 批量录入 ======================

def batch_import_calibration(records: list[dict]) -> dict:
    """
    批量导入当日全部标的校准记录。

    参数:
        records: [
            {
                "ts_code": "600884",
                "ai_pred": "预判上涨",
                "ai_risk_tip": "持有不动",
                "real_change_pct": -3.5,
                "close_price": 18.20,
                "support_resistance": "跌破20日线",
                "real_trade_action": "止损",
                "short_attribution": "业绩利好出尽",
            },
            ...
        ]

    返回:
        {
            "total": N,
            "inserted": N,
            "tagged": N,
            "untagged": N,
            "by_label": {...},
            "errors": [...],
        }
    """
    ensure_table()
    trade_date = datetime.now().strftime("%Y%m%d")
    result = {
        "trade_date": trade_date,
        "total": len(records),
        "inserted": 0,
        "tagged": 0,
        "untagged": 0,
        "by_label": {},
        "errors": [],
    }

    for rec in records:
        rec["trade_date"] = trade_date

        # 自动匹配标签
        if not rec.get("error_label"):
            rule_id, tag = auto_match_label(
                rec.get("ai_pred", ""),
                rec.get("ai_risk_tip", ""),
                rec.get("real_change_pct", 0),
                rec.get("real_trade_action", ""),
                rec.get("support_resistance", ""),
            )
            rec["error_label"] = tag

        if rec.get("error_label"):
            result["tagged"] += 1
            result["by_label"][rec["error_label"]] = result["by_label"].get(rec["error_label"], 0) + 1
        else:
            result["untagged"] += 1

        ok = insert_record(rec)
        if ok:
            result["inserted"] += 1
        else:
            result["errors"].append(f"{rec.get('ts_code')} 入库失败")

    msg = (f"📊 盘后校准导入: {result['inserted']}/{result['total']}条 | "
           f"已标记{result['tagged']} 未标记{result['untagged']}")
    logging.info(msg)
    return result


# ====================== 校验约束 ======================

def validate_completeness(trade_date: str = None) -> dict:
    """
    校验当日校准完整性。

    约束: 当日未完成全部标的标注，则当日样本锁定，
          禁止参与智能体离线迭代训练。
          连续3日标注缺失，触发系统告警。

    返回:
        {
            "complete": bool,
            "total": N,
            "labeled": N,
            "unlabeled": N,
            "missing_days": N,
            "status": "OK" | "LOCKED" | "ALERT",
        }
    """
    trade_date = trade_date or datetime.now().strftime("%Y%m%d")
    ensure_table()
    conn = sqlite3.connect(str(MEMORY_DB))

    # 查当日完成情况
    cur = conn.cursor()
    cur.execute(f"""
        SELECT error_label, COUNT(*) FROM {CALIB_TABLE}
        WHERE trade_date=? GROUP BY error_label
    """, (trade_date,))
    rows = cur.fetchall()

    total = sum(r[1] for r in rows)
    labeled = sum(r[1] for r in rows if r[0] and r[0].strip())
    unlabeled = total - labeled

    # 检查连续缺失天数
    cur.execute(f"""
        SELECT trade_date, COUNT(*) as cnt FROM {CALIB_TABLE}
        WHERE trade_date < ? AND trade_date >= date(?, '-7 days')
        GROUP BY trade_date ORDER BY trade_date DESC
    """, (trade_date, trade_date))
    daily_counts = cur.fetchall()
    conn.close()

    # 首次运行: 无历史数据则连续缺失为0
    if daily_counts:
        completed_days = [d for d, c in daily_counts if c >= labeled]
        expected_days = min(len(daily_counts) + 1, 7)
    else:
        completed_days = [trade_date]
        expected_days = 1
    actual_completed = len(completed_days)
    missing_days = max(0, min(3, expected_days - actual_completed))

    complete = (labeled > 0 and unlabeled == 0)
    status = "OK"
    if not complete and labeled == 0:
        status = "LOCKED"
    if missing_days >= 3:
        status = "ALERT"

    return {
        "complete": complete,
        "total": total,
        "labeled": labeled,
        "unlabeled": unlabeled,
        "missing_days": missing_days,
        "status": status,
    }


# ====================== 智能体迭代逻辑 ======================

def run_agent_iteration(trade_date: str = None) -> dict:
    """
    基于人工标签的智能体自修正。

    流程:
      1. 加载带标签的校准样本
      2. 回溯所有负误差/失效案例, 定位对应因子/参数
      3. 有效信号因子上调权重
      4. 失效信号因子下调权重/修正阈值
      5. 输出迭代报告

    返回:
        {
            "iterated": bool,
            "samples_loaded": N,
            "errors_analyzed": N,
            "effective_analyzed": N,
            "weight_adjustments": [...],
            "threshold_adjustments": [...],
        }
    """
    trade_date = trade_date or datetime.now().strftime("%Y%m%d")
    ensure_table()

    # 前置校验: 必须完善才可迭代
    completeness = validate_completeness(trade_date)
    if completeness["status"] == "LOCKED":
        logging.warning(f"⚠️  {trade_date} 样本未完全标注, 迭代锁定")
        return {"iterated": False, "reason": "样本未完全标注，锁定"}
    if completeness["status"] == "ALERT":
        logging.warning(f"🚨 连续{completeness['missing_days']}日标注缺失, 触发告警")
        # 仍然允许迭代, 但标记告警

    conn = sqlite3.connect(str(MEMORY_DB))
    df = conn.execute(f"""
        SELECT ts_code, ai_pred, ai_risk_tip, real_change_pct,
               real_trade_action, error_label, short_attribution
        FROM {CALIB_TABLE} WHERE trade_date=?
    """, (trade_date,)).fetchall()
    conn.close()

    result = {
        "iterated": True,
        "trade_date": trade_date,
        "samples_loaded": len(df),
        "errors_analyzed": 0,
        "effective_analyzed": 0,
        "weight_adjustments": [],
        "threshold_adjustments": [],
    }

    for row in df:
        ts_code, ai_pred, tip, pct, action, label, attr = row
        label = label or ""

        if label in ERROR_TAGS:
            result["errors_analyzed"] += 1
            adj = _analyze_error_case(ts_code, label, pct, attr)
            result["weight_adjustments"].extend(adj.get("weight_down", []))
            result["threshold_adjustments"].extend(adj.get("threshold_tighten", []))

        elif label in EFFECTIVE_TAGS:
            result["effective_analyzed"] += 1
            adj = _analyze_effective_case(ts_code, label, pct)
            result["weight_adjustments"].extend(adj.get("weight_up", []))

    # 去重
    result["weight_adjustments"] = list(set(result["weight_adjustments"]))
    result["threshold_adjustments"] = list(set(result["threshold_adjustments"]))

    # 输出摘要
    msg = (f"🔄 智能体迭代: {result['samples_loaded']}样本 | "
           f"负误差{result['errors_analyzed']}个 | "
           f"有效信号{result['effective_analyzed']}个 | "
           f"权重调整{len(result['weight_adjustments'])}项 | "
           f"阈值修正{len(result['threshold_adjustments'])}项")
    logging.info(msg)

    # 写入迭代记录
    _save_iteration_report(trade_date, result)
    return result


def _analyze_error_case(ts_code: str, label: str, pct: float, attr: str) -> dict:
    """分析负误差案例, 返回需下调的因子和收紧的阈值"""
    weight_down = []
    threshold_tighten = []

    if "高估" in label:
        weight_down.append("base_factor_weight")  # 基础因子过高
        if pct <= -5:
            threshold_tighten.append("买入门槛分数: 60→70")
    elif "低估" in label:
        weight_down.append("bearish_signal_weight")  # 看空信号权重过高
    elif "入场条件失效" in label:
        weight_down.append("entry_condition_weight")
        threshold_tighten.append("入场条件: 需增加技术面确认")
    elif "区间判断失效" in label:
        weight_down.append("oscillation_weight")
        threshold_tighten.append("震荡判定阈值: 振幅范围缩窄10%")
    elif "止损阈值误判" in label:
        threshold_tighten.append("止损缓冲: 增加0.5%缓冲区间")
    elif "止盈偏保守" in label:
        threshold_tighten.append("止盈阈值: 上调1~2%")

    return {
        "weight_down": [f"{ts_code}: {w}" for w in weight_down],
        "threshold_tighten": [f"{ts_code}: {t}" for t in threshold_tighten],
    }


def _analyze_effective_case(ts_code: str, label: str, pct: float) -> dict:
    """分析有效信号案例, 返回需上调的因子"""
    weight_up = []

    if "风控" in label:
        weight_up.append("risk_control_weight")
    elif "持仓信号有效" in label:
        weight_up.append("hold_signal_weight")
    elif "规避信号有效" in label:
        weight_up.append("avoid_signal_weight")

    return {
        "weight_up": [f"{ts_code}: {w}" for w in weight_up],
    }


def _save_iteration_report(trade_date: str, result: dict):
    """保存迭代报告到文件"""
    report_dir = BASE / "calibration_reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"iteration_{trade_date}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logging.info(f"  📝 迭代报告: {report_path}")


# ====================== 输出数据导出 ======================

def export_calibration(trade_date: str = None) -> list[dict]:
    """
    导出当日校准数据（符合output_data_schema规范）。

    返回:
        [
            {
                "标的代码": "600884",
                "AI原始信号": "预判上涨/持有不动",
                "盘面真实结果": "-3.50%",
                "实际操作": "止损",
                "误差标签": "入场条件失效",
                "简短归因": "业绩利好出尽",
            },
            ...
        ]
    """
    trade_date = trade_date or datetime.now().strftime("%Y%m%d")
    ensure_table()
    conn = sqlite3.connect(str(MEMORY_DB))
    rows = conn.execute(f"""
        SELECT ts_code, ai_pred, ai_risk_tip, real_change_pct,
               real_trade_action, error_label, short_attribution
        FROM {CALIB_TABLE} WHERE trade_date=?
    """, (trade_date,)).fetchall()
    conn.close()

    output = []
    for r in rows:
        output.append({
            "标的代码": r[0],
            "AI原始信号": f"{r[1]}/{r[2]}",
            "盘面真实结果": f"{r[3]:+.2f}%",
            "实际操作": r[4],
            "误差标签": r[5] or "未标记",
            "简短归因": r[6] or "",
        })
    return output


# ====================== 主流程 ======================

def run_calibration_pipeline(records: list[dict]) -> dict:
    """
    盘后校准全流程一键执行。

    参数:
        records: 当日全部标的 [{ts_code, ai_pred, ai_risk_tip, ...}]

    返回:
        {
            "trade_date": str,
            "import_result": {...},
            "completeness": {...},
            "iteration_result": {...},
            "export": [...],
        }
    """
    trade_date = datetime.now().strftime("%Y%m%d")
    logging.info(f"\n{'='*60}")
    logging.info(f"📋 盘后校准流水线启动 [{trade_date}]")
    logging.info(f"{'='*60}")

    # Step 1: 批量导入 + 自动匹配标签
    logging.info("\n--- Step 1: 批量导入 ---")
    import_result = batch_import_calibration(records)

    # Step 2: 完整性校验
    logging.info("\n--- Step 2: 完整性校验 ---")
    completeness = validate_completeness(trade_date)
    status_icon = {"OK": "✅", "LOCKED": "🔒", "ALERT": "🚨"}
    logging.info(f"  状态: {status_icon.get(completeness['status'], '❓')} {completeness['status']}")
    logging.info(f"  已标记: {completeness['labeled']}/{completeness['total']}")
    logging.info(f"  连续缺失: {completeness['missing_days']}日")

    # Step 3: 智能体迭代 (仅当标注完整时)
    logging.info("\n--- Step 3: 智能体迭代 ---")
    if completeness["complete"]:
        iteration_result = run_agent_iteration(trade_date)
    else:
        iteration_result = {
            "iterated": False,
            "reason": f"未完成全部标注 ({completeness['unlabeled']}条未标记)",
        }
        logging.warning(f"  ⏭️  {iteration_result['reason']}")

    # Step 4: 导出
    export = export_calibration(trade_date)

    logging.info(f"\n{'='*60}")
    logging.info(f"✅ 盘后校准流水线完成")
    logging.info(f"{'='*60}")

    return {
        "trade_date": trade_date,
        "import_result": import_result,
        "completeness": completeness,
        "iteration_result": iteration_result,
        "export": export,
    }


# ====================== 测试 / CLI ======================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        trade_date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")
        result = validate_completeness(trade_date)
        print(f"\n📊 完整性校验 ({trade_date})")
        print(f"  状态: {result['status']}")
        print(f"  已标记/总数: {result['labeled']}/{result['total']}")
        print(f"  连续缺失日: {result['missing_days']}")
        print(f"  {'✅ 可参与迭代' if result['complete'] else '🔒 锁定'}")

    elif len(sys.argv) > 1 and sys.argv[1] == "export":
        trade_date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")
        data = export_calibration(trade_date)
        print(f"\n📊 校准数据导出 ({trade_date})")
        print(f"{'标的代码':<10} {'AI原始信号':<25} {'盘面结果':<10} {'操作':<6} {'误差标签':<18} {'归因':<20}")
        print("-" * 90)
        for r in data:
            print(f"{r['标的代码']:<10} {r['AI原始信号']:<25} {r['盘面真实结果']:<10} "
                  f"{r['实际操作']:<6} {r['误差标签']:<18} {r['简短归因']:<20}")

    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # 模拟测试
        test_records = [
            {"ts_code": "600884", "ai_pred": "预判上涨", "ai_risk_tip": "持有不动",
             "real_change_pct": -3.5, "close_price": 18.20,
             "support_resistance": "跌破20日线", "real_trade_action": "止损",
             "short_attribution": "利好出尽杀"},
            {"ts_code": "002617", "ai_pred": "预判下跌", "ai_risk_tip": "持有不动",
             "real_change_pct": 4.2, "close_price": 12.50,
             "support_resistance": "突破压力位", "real_trade_action": "持12.50",
             "short_attribution": ""},
            {"ts_code": "600547", "ai_pred": "预判中性", "ai_risk_tip": "提示减仓",
             "real_change_pct": -4.8, "close_price": 45.30,
             "support_resistance": "跌破支撑", "real_trade_action": "止损",
             "short_attribution": "金价回调"},
            {"ts_code": "300476", "ai_pred": "预判上涨", "ai_risk_tip": "持有不动",
             "real_change_pct": 2.8, "close_price": 38.60,
             "support_resistance": "20日线企稳", "real_trade_action": "持仓",
             "short_attribution": ""},
        ]
        result = run_calibration_pipeline(test_records)
        print(f"\n{'='*40}")
        print(f"导入: {result['import_result']['inserted']}/{result['import_result']['total']}")
        print(f"标签分布: {result['import_result']['by_label']}")
        print(f"完整性: {result['completeness']['status']}")
        print(f"迭代: {'✅ 执行' if result['iteration_result'].get('iterated') else '⏭️ 跳过'}")
        print(f"\n导出:")
        for r in result['export']:
            print(f"  {r['标的代码']:8s} | {r['误差标签']:18s} | {r['盘面真实结果']:8s} | {r['AI原始信号'][:20]:20s}")
    else:
        print("用法:")
        print("  python3 post_market_calibration.py validate [YYYYMMDD]  # 校验完整性")
        print("  python3 post_market_calibration.py export [YYYYMMDD]    # 导出数据")
        print("  python3 post_market_calibration.py test                 # 模拟测试")
