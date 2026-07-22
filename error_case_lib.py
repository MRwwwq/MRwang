# -*- coding: utf-8 -*-
"""
error_case_lib.py — 每日研判漏洞人工补录 v2
===========================================
执行窗口: 每个交易日17:30, 人工校准完成后同步执行
存储: ai_error_case_lib 研判漏洞案例库

7类标准化缺陷:
  1. 只看资金忽略行业周期   2. 估值因子权重失衡   3. 筹码测算失真
  4. 消息脉冲行情误判       5. 风控边界僵化       6. 宏观环境缺失
  7. 细分赛道分化误判

强制联动: 月度因子迭代读取本库定向修正权重; 无案例不阻断, 但积累过多生成预警工单

═══════════════════════════════════════════════
操作规范 (永久归档)
═══════════════════════════════════════════════

漏洞识别三渠道:
  ① Hermes Agent 13模块完整报告
  ② trade_calibration 负误差真值标的
  ③ 本地报告解析提取的行业景气增量信息

录入必填8字段 (缺一不可):
  trade_date / ticker / defect_type / ai_original_view
  real_market_proof / fix_direction / operator / record_time

优化规则 (7类缺陷 → 定向调整):
  ① 只看资金忽略行业周期 → 下调资金因子权重, 上调行业赛道因子
  ② 估值因子权重失衡 → 限制单一估值权重上限, 引入修正系数
  ③ 筹码测算失真 → 重构统计算法, 降低筹码因子优先级
  ④ 消息脉冲行情误判 → 完善消息标签, 剥离催化脉冲样本
  ⑤ 风控边界僵化 → 引入自适应风控系数, 极端行情放宽阀值
  ⑥ 宏观环境缺失 → 新增宏观因子模块, 加权利率/大宗商品
  ⑦ 细分赛道分化误判 → 细分赛道独立建模, 替代大盘推演

兜底强制约束:
  - 交易日必须完成, 休市跳过
  - 必须对照13模块报告逐条核查, 禁止虚构
  - 佐证素材使用客观行情/产业事实, 禁止主观臆造
  - 仅当日漏洞当日录, 禁止跨日补录
  - 无漏洞需标记"当日无新增研判漏洞"
"""

import sys
import json
import logging
import psycopg2
from datetime import datetime, date
from pathlib import Path

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

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_DIR / "error_case_lib.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ErrorCaseLib")

# ═══════════════════════════════════════════════
#  7类标准化缺陷 + 优化映射
# ═══════════════════════════════════════════════

DEFECT_TYPES = [
    "只看资金忽略行业周期",
    "估值因子权重失衡",
    "筹码测算失真",
    "消息脉冲行情误判",
    "风控边界僵化",
    "宏观环境缺失",
    "细分赛道分化误判",
]

DEFECT_OPTIMIZE_MAP = {
    "只看资金忽略行业周期": "下调短期资金流因子权重, 上调行业政策/产能/技术突破赛道因子权重",
    "估值因子权重失衡": "限制单一估值指标权重上限, 引入毛利率/行业景气度做估值修正系数",
    "筹码测算失真": "重构筹码区间统计算法, 降低筹码因子在涨跌预判中的优先级",
    "消息脉冲行情误判": "完善消息扰动标签识别逻辑, 自动剥离催化脉冲样本, 不参与常规因子训练",
    "风控边界僵化": "引入自适应风控系数, 极端行情自动放宽止损/止盈阀值",
    "宏观环境缺失": "新增宏观因子模块, 加权利率/大宗商品/地缘需求等外部变量",
    "细分赛道分化误判": "细分赛道独立建模, 替代大盘/板块整体指数推演个股",
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


# ═══════════════════════════════════════════════
#  前置校验
# ═══════════════════════════════════════════════

def validate_case(case: dict) -> tuple:
    """
    校验单条漏洞案例, 脏数据直接拦截

    校验项:
      1. trade_date 8位数字
      2. defect_type 在7类标准缺陷内
      3. ai_original_view / real_market_proof 文本≥10字符
      4. fix_direction 非空
    """
    dt = case.get("trade_date", "")
    if len(dt) != 8 or not dt.isdigit():
        return False, f"日期格式错误: {dt}"

    d_type = case.get("defect_type", "")
    if d_type not in DEFECT_TYPES:
        return False, f"缺陷分类'{d_type}'不在7类标准内: {DEFECT_TYPES}"

    ai_view = (case.get("ai_original_view") or "").strip()
    if len(ai_view) < 10:
        return False, f"AI研判原文过短({len(ai_view)}字), 需≥10字符"

    proof = (case.get("real_market_proof") or "").strip()
    if len(proof) < 10:
        return False, f"佐证素材过短({len(proof)}字), 需≥10字符"

    fix_dir = (case.get("fix_direction") or "").strip()
    # fix_direction 为空时由 insert_case 自动匹配预设优化规则

    return True, "校验通过"


# ═══════════════════════════════════════════════
#  单条入库 (UPSERT)
# ═══════════════════════════════════════════════

def insert_case(case: dict):
    """
    写入 ai_error_case_lib 表
    ON CONFLICT (trade_date, ticker, defect_type) → 覆盖
    """
    ticker_clean = case["ticker"].replace(".SH", "").replace(".SZ", "")
    conn = get_conn()
    cur = conn.cursor()

    optimize_rule = DEFECT_OPTIMIZE_MAP.get(case["defect_type"], "自定义优化规则(待分析)")
    fix_dir = case.get("fix_direction") or optimize_rule

    sql = """
        INSERT INTO ai_error_case_lib (
            trade_date, ticker, defect_type,
            defect_description, market_evidence,
            ai_original_view, fix_direction,
            operator, create_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (trade_date, ticker, defect_type) DO UPDATE SET
            defect_description = %s,
            market_evidence = %s,
            ai_original_view = %s,
            fix_direction = %s,
            operator = %s,
            create_time = now()
    """
    params = (
        case["trade_date"], ticker_clean, case["defect_type"],
        case.get("ai_original_view", ""), case.get("real_market_proof", ""),
        case.get("ai_original_view", ""), fix_dir,
        case.get("operator", "admin"),
        # UPSERT
        case.get("ai_original_view", ""), case.get("real_market_proof", ""),
        case.get("ai_original_view", ""), fix_dir,
        case.get("operator", "admin"),
    )
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"  ✅ {ticker_clean} [{case['defect_type']}] 入库完成 → 优化: {fix_dir[:40]}...")


# ═══════════════════════════════════════════════
#  月度迭代: 加载全月案例 → 统计+优化方案
# ═══════════════════════════════════════════════

def load_monthly_cases(month_start: str, month_end: str) -> list:
    """加载指定月份全部案例, 返回分组统计"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT defect_type, ticker, COUNT(*) as cnt
        FROM ai_error_case_lib
        WHERE trade_date BETWEEN %s AND %s
        GROUP BY defect_type, ticker
        ORDER BY cnt DESC
    """, (month_start, month_end))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def monthly_optimize_report(month_start: str, month_end: str) -> dict:
    """生成月度优化报告"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT defect_type, COUNT(*) as case_count
        FROM ai_error_case_lib
        WHERE trade_date BETWEEN %s AND %s
        GROUP BY defect_type
        ORDER BY case_count DESC
    """, (month_start, month_end))
    stats = cur.fetchall()
    cur.close()
    conn.close()

    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"📊 月度AI研判漏洞统计 & 定向优化方案")
    lines.append(f"统计区间: {month_start} ~ {month_end}")
    lines.append(f"{'='*60}")

    total = 0
    for d_type, cnt in stats:
        total += cnt
        opt_rule = DEFECT_OPTIMIZE_MAP.get(d_type, "新增专项规则(待分析)")
        lines.append(f"\n  📌 缺陷: {d_type}  |  案例: {cnt}次")
        lines.append(f"     优化: {opt_rule}")

    lines.append(f"\n{'='*60}")
    lines.append(f"本月累计 {total} 条研判漏洞, 已推送至月度迭代优化队列")
    if total == 0:
        lines.append("(无案例不阻断流程, 持续监控中)")

    report = "\n".join(lines)
    logger.info(report)

    # 写入飞书同步目录
    feishu_dir = Path("/www/wwwroot/stocks/reports/飞书同步摘要/")
    feishu_dir.mkdir(parents=True, exist_ok=True)
    fp = feishu_dir / f"月度研判漏洞优化摘要_{datetime.now().strftime('%Y%m')}.md"
    with open(fp, "w", encoding="utf-8") as f:
        f.write("# 月度研判漏洞优化摘要\n\n")
        f.write(f"**统计区间:** {month_start} ~ {month_end}\n\n")
        f.write("| 缺陷类型 | 案例数 | 优化方案 |\n")
        f.write("|---------|:------:|----------|\n")
        for d_type, cnt in stats:
            opt_rule = DEFECT_OPTIMIZE_MAP.get(d_type, "待分析")
            f.write(f"| {d_type} | {cnt} | {opt_rule} |\n")
        f.write(f"\n**合计:** {total}条\n")
    logger.info(f"📄 月度报告已写入: {fp}")

    return {"stats": {r[0]: r[1] for r in stats}, "total": total, "report_file": str(fp)}


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    TODAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    OP_USER = "admin"

    mode = sys.argv[2] if len(sys.argv) > 2 else "daily"

    if mode == "monthly":
        # 月度迭代模式
        month_start = sys.argv[3] if len(sys.argv) > 3 else TODAY[:6] + "01"
        month_end = sys.argv[4] if len(sys.argv) > 4 else TODAY
        monthly_optimize_report(month_start, month_end)
        sys.exit(0)

    # ═══ 每日录入模式 ═══
    logger.info(f"\n{'='*60}")
    logger.info(f"📋 每日研判漏洞人工补录 | {TODAY}")
    logger.info(f"{'='*60}\n")

    # 当日漏洞案例清单 (对照13模块报告逐条填写)
    daily_cases = [
        # {
        #     "ticker": "600547.SH",
        #     "defect_type": "只看资金忽略行业周期",
        #     "ai_original_view": "AI仅依据07/14日+2.19亿主力净流入判定强势反转",
        #     "real_market_proof": "07/15立即-1.59亿出货, 实为对倒诱多; 金价同期破4000美元行业利空",
        #     "fix_direction": "",  # 留空自动匹配预设规则
        #     "operator": OP_USER,
        # },
    ]

    # 无漏洞标记
    if not daily_cases:
        logger.info("当日无新增研判漏洞 (对照13模块报告逐条核查完毕)")
        logger.info(f"{'='*60}\n")
        # 写入日志留存操作记录
        with open(LOG_DIR / "error_case_daily_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{TODAY} | {OP_USER} | 当日无新增研判漏洞\n")
        sys.exit(0)

    # 逐条校验+入库
    ok_count = 0
    skip_count = 0
    for case in daily_cases:
        case["trade_date"] = TODAY
        pass_ok, msg = validate_case(case)
        if not pass_ok:
            logger.warning(f"  ⚠️ 校验失败 {case.get('ticker','?')}: {msg}")
            skip_count += 1
            continue
        insert_case(case)
        ok_count += 1

    logger.info(f"\n入库: {ok_count}条 | 跳过: {skip_count}条")
    logger.info(f"{'='*60}\n")
