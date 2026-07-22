#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_calibration_pg_v3.py — PostgreSQL 盘后校准标准化录入流程
======================================================================
1. data_validate() — 前置强校验(基础字段+模型拓展字段:RAG/QClaw/固态/芒格)
2. get_error_label() — 自动匹配误差标签(兼容多误判共振/固态题材)
3. insert_calibration() — 单条数据入库(拓展表结构, ON CONFLICT UPSERT)
4. logic_review() — 入库后逻辑复核(漏录+标签冲突+新增模型字段逻辑)
5. main() — 主流程(通用模板,批量填充任意标的)

执行时机: 盘后 17:00~18:00
依赖: PostgreSQL trade_db
======================================================================
"""

import psycopg2
import sys
from datetime import datetime


def get_db_conn():
    """获取 PostgreSQL 数据库连接"""
    return psycopg2.connect(
        dbname="trade_db",
        user="admin",
        password="stock123",  # 已验证可连接
        host="127.0.0.1",
        port="5432"
    )


# ═══════════════════════════════════════════════
#  1. 前置强校验函数（代码校验层-事前拦截脏数据）
# ═══════════════════════════════════════════════

def data_validate(row: dict) -> tuple[bool, str]:
    """校验输入行数据完整性，适配RAG/QClaw/固态/芒格新增字段"""

    # 基础日期校验
    dt = row["trade_date"]
    if len(dt) != 8 or not dt.isdigit():
        return False, "日期格式错误，必须8位数字YYYYMMDD"

    # 行情数值校验
    pct = row["real_change_pct"]
    price = row["close_price"]
    if not isinstance(pct, (int, float)) or not -20 <= pct <= 20:
        return False, f"涨跌幅{pct}超出合法区间-20~20"
    if not isinstance(price, (int, float)) or price <= 0:
        return False, f"收盘价{price}非法，必须大于0"

    # 操作枚举校验
    allow_action = {"持仓", "止盈", "止损", "空仓"}
    if row["real_trade_action"] not in allow_action:
        return False, f"操作仅允许{allow_action}"

    # 支撑压力描述校验
    sp_desc = row["support_resistance_status"].strip()
    if len(sp_desc) < 4:
        return False, "支撑压力描述需写明突破/受阻/跌破/企稳，内容不可过短"

    # AI预判枚举校验
    if row["ai_pred"] not in ["预判上涨", "预判下跌", "预判中性"]:
        return False, "AI预判仅允许：预判上涨/预判下跌/预判中性"

    # 风控提示枚举校验
    if row["ai_risk_tip"] not in ["提示入场", "提示减仓", "持有不动"]:
        return False, "风控提示仅允许：提示入场/提示减仓/持有不动"

    # ====== 新增模型拓展字段校验（适配RAG+QClaw新增元素）======
    if "misjudge_hit_count" in row and (not isinstance(row["misjudge_hit_count"], int) or row["misjudge_hit_count"] < 0):
        return False, "芒格误判命中数量必须为非负整数"

    if "qclaw_rule_id" in row and len(row["qclaw_rule_id"].strip()) == 0:
        return False, "QClaw触发规则ID不可为空，无触发填写None"

    if "solid_tech_tag" in row and row["solid_tech_tag"] not in \
            ["无固态题材", "半固态电池", "全固态电池", "双固态题材共振"]:
        return False, "固态题材标签仅允许：无固态题材/半固态电池/全固态电池/双固态题材共振"

    if "rag_match_score" in row and (not 0 <= float(row["rag_match_score"]) <= 1):
        return False, "RAG向量匹配度必须在0~1区间"

    return True, "数据校验通过"


# ═══════════════════════════════════════════════
#  2. 自动匹配误差标签规则（兼容固态题材、多误判共振）
# ═══════════════════════════════════════════════

def get_error_label(ai_pred: str, real_change_pct: float, ai_risk_tip: str,
                    is_trapped: bool, misjudge_hit_count: int = 0,
                    solid_tech_tag: str = "无固态题材") -> str:
    """根据AI预判vs真实行情自动匹配误差标签"""

    # 基础误差判定逻辑
    if ai_pred == "预判上涨" and real_change_pct <= -2:
        return "【预判高估，负误差】"

    if ai_pred == "预判下跌" and real_change_pct >= 2:
        return "【预判低估，负误差】"

    if ai_risk_tip == "提示减仓" and real_change_pct <= -3:
        # 叠加多误判/固态题材强化标记
        if misjudge_hit_count >= 4 or solid_tech_tag != "无固态题材":
            return "【风控判断有效-多重风险共振强化】"
        return "【风控判断有效】"

    if ai_risk_tip == "提示入场" and is_trapped:
        return "【入场条件失效】"

    return "【预判匹配，无误差】"


# ═══════════════════════════════════════════════
#  3. 单条数据入库（拓展数据表，兼容新增模型字段）
# ═══════════════════════════════════════════════

def insert_calibration(row: dict, error_tag: str):
    """写入trade_calibration表，含RAG/QClaw/固态/芒格拓展字段"""
    conn = get_db_conn()
    cur = conn.cursor()

    sql = """
    INSERT INTO trade_calibration(
        trade_date, ticker, real_change_pct, close_price,
        support_resistance_status, real_trade_action,
        ai_pred, ai_risk_tip, error_label, is_trapped, operator, update_time,
        misjudge_hit_count, solid_tech_tag, qclaw_rule_id, rag_match_score
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s)
    ON CONFLICT (trade_date, ticker) DO UPDATE SET
        real_change_pct=%s, close_price=%s, support_resistance_status=%s,
        real_trade_action=%s, ai_pred=%s, ai_risk_tip=%s, error_label=%s,
        is_trapped=%s, operator=%s, misjudge_hit_count=%s, solid_tech_tag=%s,
        qclaw_rule_id=%s, rag_match_score=%s, update_time=now();
    """

    params = (
        row["trade_date"], row["ticker"], row["real_change_pct"], row["close_price"],
        row["support_resistance_status"], row["real_trade_action"],
        row["ai_pred"], row["ai_risk_tip"], error_tag, row["is_trapped"], row["operator"],
        row["misjudge_hit_count"], row["solid_tech_tag"], row["qclaw_rule_id"], row["rag_match_score"],
        # UPDATE 段重复参数
        row["real_change_pct"], row["close_price"], row["support_resistance_status"],
        row["real_trade_action"], row["ai_pred"], row["ai_risk_tip"], error_tag,
        row["is_trapped"], row["operator"], row["misjudge_hit_count"], row["solid_tech_tag"],
        row["qclaw_rule_id"], row["rag_match_score"]
    )

    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ {row['ticker']} 校准数据入库完成，拓展模型字段同步归档")


# ═══════════════════════════════════════════════
#  4. 入库后逻辑复核（事后校验标签冲突+漏录检测）
# ═══════════════════════════════════════════════

def logic_review(trade_date: str, code_list: list):
    """逐条校验：漏录、标签逻辑矛盾、新增模型字段逻辑合理性"""
    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT ticker, real_change_pct, error_label, misjudge_hit_count, solid_tech_tag
        FROM trade_calibration
        WHERE trade_date=%s AND ticker IN %s
    """, (trade_date, tuple(code_list)))

    records = cur.fetchall()
    cur.close()
    conn.close()

    record_map = {}
    for r in records:
        record_map[r[0]] = (r[1], r[2], r[3], r[4])
    missing = []
    logic_err = []

    # 漏录检测
    for code in code_list:
        if code not in record_map:
            missing.append(code)

    # 基础标签逻辑冲突校验 — 修复元组解包bug
    for ticker, (pct, tag, hit_num, solid_tag) in record_map.items():
        if tag == "【预判高估，负误差】" and pct > -1:
            logic_err.append(f"{ticker}:标签为大跌负误差，实际跌幅不足1%，逻辑矛盾")

        if tag == "【预判低估，负误差】" and pct < 1:
            logic_err.append(f"{ticker}:标签为大涨负误差，实际涨幅不足1%，逻辑矛盾")

        # 新增模型字段逻辑校验
        if tag.startswith("【风控判断有效") and hit_num == 0 and solid_tag == "无固态题材":
            logic_err.append(f"{ticker}:标记风控有效，但无芒格误判、无固态题材，逻辑存疑")

    print("\n========== 盘后校准复核报告 ==========")
    if missing:
        print(f"\033[91m【漏录标的】{missing}\033[0m")
    if logic_err:
        print(f"\033[91m【逻辑冲突记录】{logic_err}\033[0m")
    if not missing and not logic_err:
        print("\033[92m全部数据完整、逻辑无异常，当日校准完成，模型拓展字段校验通过\033[0m")


# ═══════════════════════════════════════════════
#  5. 主流程入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    TODAY = sys.argv[1] if len(sys.argv) > 1 else "20260721"
    OPERATOR = sys.argv[2] if len(sys.argv) > 2 else "admin"

    # 实盘数据：杉杉+山东黄金 2026-07-21
    daily_cal_data = [
        {
            "ticker": "600884",
            "real_change_pct": 9.98,
            "close_price": 12.45,
            "support_resistance_status": "涨停站上MA5/ MA10，受阻MA20(12.75)，Lollapalooza一票否决禁开仓，仅观望",
            "real_trade_action": "空仓",
            "ai_pred": "预判下跌",
            "ai_risk_tip": "提示减仓",
            "is_trapped": False,
            "operator": OPERATOR,
            # 新增模型拓展字段
            "misjudge_hit_count": 8,
            "solid_tech_tag": "全固态电池",
            "qclaw_rule_id": "QClaw_Rule_021",
            "rag_match_score": 0.95,
        },
        {
            "ticker": "600547",
            "real_change_pct": 5.94,
            "close_price": 25.87,
            "support_resistance_status": "放量站上MA5/MA10/MA20，贵金属板块+9.94%暴涨，但Lollapalooza(7项)一票否决禁开仓",
            "real_trade_action": "空仓",
            "ai_pred": "预判中性",
            "ai_risk_tip": "持有不动",
            "is_trapped": False,
            "operator": OPERATOR,
            # 新增模型拓展字段
            "misjudge_hit_count": 7,
            "solid_tech_tag": "无固态题材",
            "qclaw_rule_id": "None",
            "rag_match_score": 0.88,
        }
    ]

    ALL_CODES = [i["ticker"] for i in daily_cal_data]

    print(f"===== 启动{TODAY}盘后校准标准化录入流程（兼容RAG/QClaw新增模型元素） =====")
    print(f"操作人: {OPERATOR} | 标的: {ALL_CODES}")

    for row in daily_cal_data:
        row["trade_date"] = TODAY
        pass_ok, msg = data_validate(row)
        if not pass_ok:
            print(f"\033[91m校验失败 {row['ticker']}:{msg}\033[0m")
            continue

        tag = get_error_label(
            row["ai_pred"], row["real_change_pct"],
            row["ai_risk_tip"], row["is_trapped"],
            row["misjudge_hit_count"], row["solid_tech_tag"]
        )

        insert_calibration(row, tag)

    logic_review(TODAY, ALL_CODES)
    print("当日录入流程执行完毕，模型拓展字段同步归档完成")
