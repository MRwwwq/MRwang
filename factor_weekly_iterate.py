#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_weekly_iterate.py — 加权因子迭代

生产环境强制前置校验:
  pre_calibration_check() 首行调用, 校验全标的人工校准完整性
  任意标的任意交易日缺失 → 阻断调参, exit(1)
  通过后加载 trade_calibration 误差标签执行自适应调参

依赖: trade_calibration 表 (人工校准标注数据)
       psycopg2 直连 (不使用SQLAlchemy)
"""
import sys
import os
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import psycopg2

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("FactorIterate")

# ── 日志持久化: 每周迭代日志文件 ──
LOG_DIR = Path(SCRIPT_DIR) / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"factor_iterate_{date.today().strftime('%Y%m%d')}.log"
file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

# ── 数据库连接(直连,替换get_pg_conn中的参数即可) ──
DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}

try:
    from config import TARGET_CODES, SECTOR_GROUPS
except ImportError:
    TARGET_CODES, SECTOR_GROUPS = [], {}

DEFAULT_WEIGHTS = {"valuation": 0.25, "momentum": 0.20, "flow": 0.25, "fundamental": 0.15, "sentiment": 0.15}
DEFAULT_ENTRY = {"consecutive_flow_days": 3, "volume_ratio": 1.0}
ADJUST_STEP = 0.03


# ═══════════════════════════════════════════════
#  数据库连接
# ═══════════════════════════════════════════════

def get_pg_conn():
    """获取psycopg2直连"""
    return psycopg2.connect(**DB_CONFIG)


# ═══════════════════════════════════════════════
#  生产环境: 前置校验 (首行调用, 每日自动执行)
# ═══════════════════════════════════════════════

def pre_calibration_check(target_code_list: list, week_trade_date_list: list):
    """
    周迭代前置强制校验：缺失人工校准直接阻断调参流程

    :param target_code_list: 全部跟踪标的代码列表，格式 ["600547","002044","601138"]
    :param week_trade_date_list: 本周交易日列表，格式 ["20260713","20260714","20260715"]
    """
    if not target_code_list or not week_trade_date_list:
        msg = "🚨 TARGET_CODES 或 WEEK_TRADE_DATES 为空,阻断调参"
        print("\033[91m" + msg + "\033[0m")
        logger.error(msg)
        sys.exit(1)

    conn = get_pg_conn()
    cur = conn.cursor()

    # 查询已存在的校准记录
    sql = """
        SELECT DISTINCT ticker, trade_date
        FROM trade_calibration
        WHERE ticker IN %s AND trade_date IN %s
    """
    cur.execute(sql, (tuple(target_code_list), tuple(week_trade_date_list)))
    exist_records = cur.fetchall()
    cur.close()
    conn.close()

    # 组装已存在记录集合(trade_date返回datetime.date,转YYYYMMDD对比)
    exist_set = set()
    for ticker, dt in exist_records:
        if hasattr(dt, 'strftime'):
            dt_str = dt.strftime('%Y%m%d')
        else:
            dt_str = str(dt).replace('-', '')
        exist_set.add((str(ticker), dt_str))
    missing_items = []

    # 遍历所有需要校验的标的+日期组合
    for code in target_code_list:
        for dt in week_trade_date_list:
            if (code, dt) not in exist_set:
                missing_items.append(f"标的:{code} 交易日:{dt}")

    # 存在缺失记录，阻断程序
    if missing_items:
        msg_block = (
            "🚨 人工校准记录存在缺失，禁止执行本次自主调参\n"
            "====================待补录校准清单====================\n"
            + "\n".join(missing_items) + "\n"
            "======================================================\n"
            "规则说明：无完整人工真实标注数据，智能体无法识别预判误差，不执行复盘与因子调参\n"
            "操作指引：请根据清单补全所有标的每日校准记录后，重新运行脚本"
        )
        print("\033[91m" + msg_block + "\033[0m")
        logger.error(f"校准校验失败: {len(missing_items)}条缺失\n{msg_block}")
        sys.exit(1)
    else:
        msg_pass = "✅ 本周所有标的人工校准记录完整，放行周度复盘与自动调参流程"
        print("\033[92m" + msg_pass + "\033[0m")
        logger.info(msg_pass)
        return True


# ═══════════════════════════════════════════════
#  自适应调参逻辑 (基于trade_calibration误差标签)
# ═══════════════════════════════════════════════

def load_calibration_stats(trade_date):
    """从trade_calibration加载误差标签统计数据"""
    conn = get_pg_conn()
    cur = conn.cursor()

    # 误差标签统计
    cur.execute("""
        SELECT error_tag, COUNT(*) as cnt FROM trade_calibration
        WHERE trade_date = %s GROUP BY error_tag
    """, (trade_date,))
    error_stats = {row[0]: row[1] for row in cur.fetchall()}

    # 分赛道统计
    sector_stats = {}
    for sector, codes in SECTOR_GROUPS.items():
        sector_stats[sector] = {"total": 0, "errors": []}
        for code in codes:
            cur.execute("""
                SELECT error_tag FROM trade_calibration
                WHERE trade_date = %s AND ticker = %s
            """, (trade_date, code))
            row = cur.fetchone()
            if row:
                tag = row[0]
                sector_stats[sector]["total"] += 1
                if tag != "【预判匹配，无误差】":
                    sector_stats[sector]["errors"].append({"code": code, "tag": tag})

    cur.close()
    conn.close()
    return {"error_stats": error_stats, "sector_stats": sector_stats}


def adjust_weights(stats):
    """
    根据误差标签分布 → 定向修正因子权重 + 入场条件
    """
    weights = {s: dict(DEFAULT_WEIGHTS) for s in SECTOR_GROUPS}
    entry = dict(DEFAULT_ENTRY)
    adjustment_log = []

    if not stats or not stats.get("error_stats"):
        adjustment_log.append("❌ 无校准数据或统计异常,返回默认权重")
        return {"weights": weights, "entry_conditions": entry,
                "adjustment_log": adjustment_log, "has_calibration": False}

    error_stats = stats["error_stats"]
    sector_stats = stats.get("sector_stats", {})
    total = sum(error_stats.values())

    if total == 0:
        adjustment_log.append("❌ 无校准数据 → 无权重修正")
        return {"weights": weights, "entry_conditions": entry,
                "adjustment_log": adjustment_log, "has_calibration": False}

    over_est = error_stats.get("【预判高估，负误差】", 0)
    under_est = error_stats.get("【预判低估，负误差】", 0)
    risk_valid = error_stats.get("【风控判断有效】", 0)
    entry_fail = error_stats.get("【入场条件失效】", 0)

    if over_est > 0:
        for sector in weights:
            w = weights[sector]
            w["momentum"] = max(0.05, w["momentum"] - ADJUST_STEP)
            w["flow"] = min(0.40, w["flow"] + ADJUST_STEP)
        adjustment_log.append(f"🔻 预判高估{over_est}次 → 动量-{ADJUST_STEP:.0%} 资金+{ADJUST_STEP:.0%}")

    if under_est > 0:
        for sector in weights:
            w = weights[sector]
            w["momentum"] = min(0.35, w["momentum"] + ADJUST_STEP)
            w["valuation"] = max(0.10, w["valuation"] - ADJUST_STEP)
        adjustment_log.append(f"🟢 预判低估{under_est}次 → 动量+{ADJUST_STEP:.0%} 估值-{ADJUST_STEP:.0%}")

    if risk_valid > 0:
        for sector in weights:
            w = weights[sector]
            w["flow"] = min(0.40, w["flow"] + ADJUST_STEP * 0.5)
            w["sentiment"] = min(0.30, w["sentiment"] + ADJUST_STEP * 0.5)
        adjustment_log.append(f"✅ 风控有效{risk_valid}次 → 资金+{ADJUST_STEP*0.5:.0%} 情绪+{ADJUST_STEP*0.5:.0%}")

    if entry_fail > 0:
        entry["consecutive_flow_days"] = min(5, entry["consecutive_flow_days"] + 1)
        entry["volume_ratio"] = min(1.5, entry["volume_ratio"] + 0.1)
        adjustment_log.append(f"🔴 入场失效{entry_fail}次 → 连续流入天数+1 量比阈值+0.1")

    # 分赛道精调
    for sector, st in sector_stats.items():
        for err in st.get("errors", []):
            tag = err["tag"]
            if tag == "【预判高估，负误差】":
                weights[sector]["momentum"] = max(0.05, weights[sector]["momentum"] - ADJUST_STEP * 0.5)
                weights[sector]["flow"] = min(0.40, weights[sector]["flow"] + ADJUST_STEP * 0.5)
                adjustment_log.append(f"  ↳ {sector}[{err['code']}] 高估 → 额外-0.5%动量 +0.5%资金")
            elif tag == "【入场条件失效】":
                weights[sector]["flow"] = max(0.10, weights[sector]["flow"] - ADJUST_STEP * 0.3)
                adjustment_log.append(f"  ↳ {sector}[{err['code']}] 入场失效 → 资金-0.3%")

    # 归一化
    for sector in weights:
        w = weights[sector]
        tw = sum(w.values())
        if abs(tw - 1.0) > 0.001:
            for k in w:
                w[k] = w[k] / tw

    adjustment_log.append(f"✅ 因子权重定向修正完成(基于{total}条人工校准标签)")
    return {"weights": weights, "entry_conditions": entry,
            "adjustment_log": adjustment_log, "has_calibration": True}


def save_weight_snapshot(result, tag="weekly"):
    """保存权重快照到JSON"""
    snap_dir = Path(SCRIPT_DIR) / "weight_snapshots"
    snap_dir.mkdir(exist_ok=True)
    fp = snap_dir / f"weight_snap_{tag}_{date.today().isoformat()}.json"
    data = {
        "snapshot_tag": tag,
        "timestamp": date.today().isoformat(),
        "weights": result["weights"],
        "entry_conditions": result["entry_conditions"],
        "adjustment_log": result["adjustment_log"],
        "has_calibration": result["has_calibration"],
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"权重快照已保存: {fp}")
    return str(fp)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", nargs="+", help="标的代码列表(默认TARGET_CODES)")
    parser.add_argument("--dates", nargs="+", default=["20260715"], help="交易日列表")
    args = parser.parse_args()

    target_codes = args.codes or TARGET_CODES
    week_dates = args.dates

    print(f"\n{'='*60}")
    print(f"📊 加权因子迭代 | 交易日: {week_dates}")
    print(f"{'='*60}")

    # ═══ 首行: 前置校验 ═══
    pre_calibration_check(target_codes, week_dates)

    # ═══ 校验通过: 自适应调参 ═══
    latest_date = week_dates[-1]
    print(f"\n加载 {latest_date} trade_calibration 误差标签...")
    stats = load_calibration_stats(latest_date)

    if stats and stats.get("error_stats"):
        print("\n误差标签分布:")
        for tag, cnt in sorted(stats["error_stats"].items(), key=lambda x: -x[1]):
            print(f"  {tag}: {cnt}次")

    result = adjust_weights(stats)

    print(f"\n{'─'*40}")
    print("调整日志:")
    for log in result["adjustment_log"]:
        print(f"  {log}")

    print(f"\n各赛道最终权重:")
    print(f"{'赛道':<10} {'估值':>6} {'动量':>6} {'资金':>6} {'基本面':>6} {'情绪':>6}")
    for sector, w in sorted(result["weights"].items()):
        print(f"{sector:<10} {w['valuation']:>6.2f} {w['momentum']:>6.2f} {w['flow']:>6.2f} {w['fundamental']:>6.2f} {w['sentiment']:>6.2f}")

    print(f"\n入场条件: 连续流入{result['entry_conditions']['consecutive_flow_days']}天 量比≥{result['entry_conditions']['volume_ratio']}")

    snap_path = save_weight_snapshot(result, "weekly")
    print(f"\n✅ 权重快照: {snap_path}")
    print(f"{'='*60}\n")
