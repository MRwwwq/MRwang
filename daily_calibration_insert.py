# -*- coding: utf-8 -*-
"""
daily_calibration_insert.py — 盘后校准标准化录入 (三层保障体系)
===============================================================
执行时机: 每个交易日17:30 (run_daily.sh step 3.5)
录入前强校验 → 自动误差标签 → UPSERT入库 → 事后逻辑复核

三层可靠性校验保障体系:
  第一层(代码校验层-事前): data_validate() 拦截格式/范围脏数据
  第二层(代码校验层-事后): logic_review()  标签冲突+漏录检测
  第三层(周迭代前置校验层): factor_weekly_iterate.py 首行调用 pre_calibration_check()

使用方式:
  手动: python3 daily_calibration_insert.py 20260716
  cron: 已集成至 run_daily.sh step 3.5
"""

import sys
import psycopg2
from datetime import date


# ═══════════════════════════════════════════════
#  数据库连接
# ═══════════════════════════════════════════════

DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}


def get_db_conn():
    """获取数据库连接"""
    return psycopg2.connect(**DB_CONFIG)


# ═══════════════════════════════════════════════
#  第一层: 录入前置强校验 (事前拦截脏数据)
# ═══════════════════════════════════════════════

def data_validate(row: dict) -> tuple:
    """
    录入前强校验所有字段，不满足直接拒绝入库

    校验项:
      1. 交易日期: 8位数字 YYYYMMDD
      2. 涨跌幅: 数字, 范围-20~20 (A股限制)
      3. 收盘价: 数字, 大于0
      4. 操作: 仅允许 持仓/止盈/止损/空仓
      5. 支撑压力描述: 至少4个字符
      6. AI预判: 仅允许 预判上涨/预判下跌/预判中性
      7. 风控提示: 仅允许 提示入场/提示减仓/持有不动
    """
    dt = row.get("trade_date", "")
    if len(dt) != 8 or not dt.isdigit():
        return False, f"日期格式错误: {dt}, 必须8位数字YYYYMMDD"

    pct = row.get("real_change_pct")
    if not isinstance(pct, (int, float)) or not -20 <= pct <= 20:
        return False, f"涨跌幅{pct}超出合法区间-20~20"

    price = row.get("close_price")
    if not isinstance(price, (int, float)) or price <= 0:
        return False, f"收盘价{price}非法, 必须大于0"

    allow_action = {"持仓", "止盈", "止损", "空仓"}
    if row.get("real_trade_action") not in allow_action:
        return False, f"操作仅允许{allow_action}"

    sp_desc = row.get("support_resistance_status", "").strip()
    if len(sp_desc) < 4:
        return False, "支撑压力描述需写明突破/受阻/跌破/企稳, 内容不可过短"

    if row.get("ai_pred") not in ["预判上涨", "预判下跌", "预判中性"]:
        return False, "AI预判仅允许: 预判上涨/预判下跌/预判中性"

    if row.get("ai_risk_tip") not in ["提示入场", "提示减仓", "持有不动"]:
        return False, "风控提示仅允许: 提示入场/提示减仓/持有不动"

    return True, "数据校验通过"


# ═══════════════════════════════════════════════
#  自动匹配误差标签规则 (四类+默认)
# ═══════════════════════════════════════════════

def get_error_label(ai_pred: str, real_change_pct: float, ai_risk_tip: str, is_trapped: bool) -> str:
    """
    根据AI预判 vs 实际走势 → 自动绑定误差标签

    Rule1: AI预判上涨但实际大跌(≤-2%)    → 【预判高估，负误差】
    Rule2: AI预判下跌但实际大涨(≥+2%)    → 【预判低估，负误差】
    Rule3: AI提示减仓且后续大跌(≤-3%)    → 【风控判断有效】
    Rule4: AI提示入场且进场后被套        → 【入场条件失效】
    Default: 无偏差                      → 【预判匹配，无误差】
    """
    if ai_pred == "预判上涨" and real_change_pct <= -2:
        return "【预判高估，负误差】"
    if ai_pred == "预判下跌" and real_change_pct >= 2:
        return "【预判低估，负误差】"
    if ai_risk_tip == "提示减仓" and real_change_pct <= -3:
        return "【风控判断有效】"
    if ai_risk_tip == "提示入场" and is_trapped:
        return "【入场条件失效】"
    return "【预判匹配，无误差】"


# ═══════════════════════════════════════════════
#  单条数据入库 (UPSERT)
# ═══════════════════════════════════════════════

def insert_calibration(row: dict, error_tag: str):
    """
    写入 trade_calibration 表
    ON CONFLICT (trade_date, ticker) → 覆盖更新
    全部字段: trade_date, ticker, real_change_pct, close_price,
              support_resistance_status, real_trade_action,
              ai_pred, ai_risk_tip, error_label, is_trapped,
              operator, update_time
    """
    conn = get_db_conn()
    cur = conn.cursor()

    # 去除股票后缀 (统一存纯数字)
    ticker_clean = row["ticker"].replace(".SH", "").replace(".SZ", "")

    sql = """
        INSERT INTO trade_calibration (
            trade_date, ticker,
            real_change_pct, close_price,
            support_resistance_status, real_trade_action,
            ai_pred, ai_risk_tip, error_label,
            is_trapped, operator, update_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (trade_date, ticker) DO UPDATE SET
            real_change_pct = %s,
            close_price = %s,
            support_resistance_status = %s,
            real_trade_action = %s,
            ai_pred = %s,
            ai_risk_tip = %s,
            error_label = %s,
            is_trapped = %s,
            operator = %s,
            update_time = now()
    """
    params = (
        row["trade_date"], ticker_clean,
        row["real_change_pct"], row["close_price"],
        row["support_resistance_status"], row["real_trade_action"],
        row["ai_pred"], row["ai_risk_tip"], error_tag,
        row["is_trapped"], row["operator"],
        # UPSERT 更新值
        row["real_change_pct"], row["close_price"],
        row["support_resistance_status"], row["real_trade_action"],
        row["ai_pred"], row["ai_risk_tip"], error_tag,
        row["is_trapped"], row["operator"],
    )
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ {ticker_clean} 校准数据入库完成 [{error_tag}]")


# ═══════════════════════════════════════════════
#  第二层: 入库后逻辑复核 (事后校验标签冲突+漏录检测)
# ═══════════════════════════════════════════════

def logic_review(trade_date: str, code_list: list):
    """
    录入完成后自动执行:
      1. 漏录检测: 检查code_list中每只标的是否已入库
      2. 逻辑一致性: 高估负误差但跌幅不足1% → 矛盾
                     低估负误差但涨幅不足1% → 矛盾
    输出报告 → 红色高亮问题, 绿色表示全部通过
    """
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT ticker, real_change_pct, error_label
        FROM trade_calibration
        WHERE trade_date = %s AND ticker IN %s
    """, (trade_date, tuple(code_list)))
    records = cur.fetchall()
    cur.close()
    conn.close()

    record_map = {r[0]: (r[1], r[2]) for r in records}

    # 漏录检测
    missing = [c for c in code_list if c not in record_map]

    # 逻辑一致性检测
    logic_err = []
    for ticker, (pct, tag) in record_map.items():
        if pct is None:
            continue
        if tag == "【预判高估，负误差】" and pct > -1:
            logic_err.append(f"{ticker}: 标签为大跌负误差, 实际跌幅不足1%({pct:+.2f}%), 逻辑矛盾")
        if tag == "【预判低估，负误差】" and pct < 1:
            logic_err.append(f"{ticker}: 标签为大涨负误差, 实际涨幅不足1%({pct:+.2f}%), 逻辑矛盾")

    # 输出报告
    print("\n========== 盘后校准复核报告 ==========")
    print(f"交易日: {trade_date}  |  标的: {len(code_list)}只  |  已录入: {len(records)}条")

    if missing:
        print(f"\033[91m【漏录标的】{missing}\033[0m")
    if logic_err:
        print(f"\033[91m【逻辑冲突记录】{logic_err}\033[0m")
    if not missing and not logic_err:
        print("\033[92m✅ 全部数据完整、逻辑无异常, 当日校准完成\033[0m")
    print("=" * 40)


# ═══════════════════════════════════════════════
#  主流程入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    # 命令行传参: python3 daily_calibration_insert.py [YYYYMMDD]
    TODAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    OPERATOR = "admin"

    # ═══ 当日所有标的校准数据模板 ═══
    # 每日实盘后按此模板填充(涨跌幅/收盘价直接复制行情终端,禁止人工估算)
    daily_cal_data = [
        {
            "ticker": "600547.SH",
            "real_change_pct": -3.25,
            "close_price": 24.51,
            "support_resistance_status": "跌破MA20支撑位",
            "real_trade_action": "止损",
            "ai_pred": "预判上涨",
            "ai_risk_tip": "持有不动",
            "is_trapped": True,
            "operator": OPERATOR,
        },
        # 后续标的按此模板追加
    ]

    ALL_CODES = [i["ticker"].replace(".SH", "").replace(".SZ", "") for i in daily_cal_data]

    print(f"\n===== 启动 {TODAY} 盘后校准标准化录入流程 =====")
    print(f"标的数量: {len(daily_cal_data)}只")
    print(f"操作人: {OPERATOR}")
    print("-" * 50)

    count_ok = 0
    count_skip = 0

    for row in daily_cal_data:
        row["trade_date"] = TODAY

        # 第一层: 前置校验
        pass_ok, msg = data_validate(row)
        if not pass_ok:
            print(f"\033[91m校验失败 {row['ticker']}: {msg}\033[0m")
            count_skip += 1
            continue

        # 自动匹配误差标签
        tag = get_error_label(
            row["ai_pred"],
            row["real_change_pct"],
            row["ai_risk_tip"],
            row["is_trapped"],
        )

        # 入库
        insert_calibration(row, tag)
        count_ok += 1

    # 汇总
    print("-" * 50)
    print(f"入库: {count_ok}只  |  跳过(校验未通过): {count_skip}只")

    # 第二层: 事后逻辑复核
    if count_ok > 0:
        logic_review(TODAY, ALL_CODES)

    print(f"===== {TODAY} 录入流程执行完毕 =====\n")
