#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_review_picker.py — 随机每日复盘选股
加权随机从5只关注股中选取1只，生成复盘报告。

用法:
    python3 daily_review_picker.py                      # 今日复盘
    python3 daily_review_picker.py --date 20260715      # 指定日期

输出:
    /opt/stock_agent/reviews/daily_review_{code}_{date}.md     # 复盘报告
    /opt/stock_agent/reviews/review_tracker.json               # 跟踪文件(自动创建)
"""

import argparse
import json
import os
import random
import sys
from datetime import date, datetime, timedelta

import psycopg2

# ── 配置 ──
STOCKS = [
    {"code": "600884", "ts_code": "600884.SH", "name": "杉杉股份"},
    {"code": "600547", "ts_code": "600547.SH", "name": "山东黄金"},
    {"code": "002044", "ts_code": "002044.SZ", "name": "美年健康"},
    {"code": "002617", "ts_code": "002617.SZ", "name": "露笑科技"},
    {"code": "300476", "ts_code": "300476.SZ", "name": "胜宏科技"},
]

REVIEWS_DIR = "/opt/stock_agent/reviews"
TRACKER_PATH = os.path.join(REVIEWS_DIR, "review_tracker.json")

DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}


def ensure_dirs():
    """确保 reviews 目录存在"""
    os.makedirs(REVIEWS_DIR, exist_ok=True)


def load_tracker():
    """加载复盘跟踪文件, 返回 {code: last_review_date_str}"""
    if os.path.exists(TRACKER_PATH):
        try:
            with open(TRACKER_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


def save_tracker(tracker, today_str):
    """更新跟踪文件并写入"""
    with open(TRACKER_PATH, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 跟踪文件已更新: {TRACKER_PATH}")


def pick_stock_weighted(tracker, today):
    """按距上次复盘天数加权随机选股 (天数越多, 概率越大)"""
    default_days = 7  # 默认距离天数(首次入选)
    weights = []
    codes_info = []

    for s in STOCKS:
        code = s["code"]
        last_str = tracker.get(code)
        if last_str:
            try:
                last_date = datetime.strptime(last_str, "%Y%m%d").date()
                days = (today - last_date).days
            except (ValueError, TypeError):
                days = default_days
        else:
            days = default_days
        # 最少权重1天, 最多30天封顶
        days = max(1, min(days, 30))
        weights.append(days)
        codes_info.append((code, days))

    print(f"\n📊 加权随机选股 (权重=距上次复盘天数):")
    for (code, name), days in zip(
        [(s["code"], s["name"]) for s in STOCKS], [w for _, w in zip(STOCKS, weights)]
    ):
        print(f"    {code} {name}: {days}天")

    # 加权随机选择
    chosen_idx = random.choices(range(len(STOCKS)), weights=weights, k=1)[0]
    chosen = STOCKS[chosen_idx]
    print(
        f"\n  🎯 选中: {chosen['code']} {chosen['name']} "
        f"(权重={weights[chosen_idx]}天)"
    )
    return chosen


def get_db_connection():
    """建立 PostgreSQL 连接"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.OperationalError as e:
        print(f"  ❌ 数据库连接失败: {e}")
        sys.exit(1)


def fetch_latest_price(conn, ts_code, trade_date):
    """获取指定股票在 trade_date 的最新行情数据"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, vol, amount, ma5, ma10, ma20
        FROM stock_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC
        LIMIT 20
        """,
        (ts_code, trade_date),
    )
    rows = cur.fetchall()
    cur.close()

    if not rows:
        print(f"  ❌ 未找到 {ts_code} 行情数据 (截至 {trade_date})")
        return None, None, None

    # 最新交易日的那条
    latest = rows[0]
    columns = [
        "ts_code", "trade_date", "open", "high", "low", "close",
        "pre_close", "pct_chg", "vol", "amount", "ma5", "ma10", "ma20",
    ]
    latest_data = dict(zip(columns, latest))

    # MAClose 用于计算 MA (如果数据库 MA 字段为 NULL, 手动计算)
    closes = [row[5] for row in rows if row[5] is not None]  # close = index 5

    # 手动计算 MA5/MA10/MA20
    def calc_ma(data_list, period):
        if len(data_list) >= period:
            vals = [float(v) for v in data_list[:period]]
            return round(sum(vals) / period, 2)
        return None

    # 数据库存储的 ma 可能为 0.00 (Decimal('0.00')), 此时须手动计算
    db_ma5 = float(latest_data["ma5"]) if latest_data["ma5"] is not None else 0.0
    db_ma10 = float(latest_data["ma10"]) if latest_data["ma10"] is not None else 0.0
    db_ma20 = float(latest_data["ma20"]) if latest_data["ma20"] is not None else 0.0
    ma5 = db_ma5 if db_ma5 > 0 else calc_ma(closes, 5)
    ma10 = db_ma10 if db_ma10 > 0 else calc_ma(closes, 10)
    ma20 = db_ma20 if db_ma20 > 0 else calc_ma(closes, 20)

    return latest_data, ma5, ma10, ma20, closes


def fetch_money_flow(conn, ts_code, trade_date, days=10):
    """获取指定股票近 days 天的资金流向"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts_code, trade_date, net_mf_amount,
               buy_sm_amount, sell_sm_amount,
               buy_md_amount, sell_md_amount,
               buy_lg_amount, sell_lg_amount,
               buy_elg_amount, sell_elg_amount
        FROM stock_money_flow
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC
        LIMIT %s
        """,
        (ts_code, trade_date, days),
    )
    rows = cur.fetchall()
    cur.close()

    flow_list = []
    for r in rows:
        flow_list.append(
            {
                "trade_date": str(r[1]),
                "net_mf_amount": float(r[2]) if r[2] else 0,
                "buy_sm_amount": float(r[3]) if r[3] else 0,
                "sell_sm_amount": float(r[4]) if r[4] else 0,
                "buy_md_amount": float(r[5]) if r[5] else 0,
                "sell_md_amount": float(r[6]) if r[6] else 0,
                "buy_lg_amount": float(r[7]) if r[7] else 0,
                "sell_lg_amount": float(r[8]) if r[8] else 0,
                "buy_elg_amount": float(r[9]) if r[9] else 0,
                "sell_elg_amount": float(r[10]) if r[10] else 0,
            }
        )
    return flow_list


def check_entry_conditions(latest_data, ma5, ma10, ma20, flow_list):
    """
    检查3个入场条件:
      1. Close > MA5 (价格站上5日均线)
      2. MA5 > MA10 (短期趋势向上)
      3. 近3日累计净流入 > 0 (资金面支持)
    """
    close = float(latest_data["close"]) if latest_data["close"] else None

    conditions = []

    # 条件1: Close > MA5
    cond1 = close is not None and ma5 is not None and close > ma5
    conditions.append(
        {
            "name": "价格站上MA5",
            "detail": f"收盘 {close} {'>' if cond1 else '<='} MA5 {ma5}",
            "passed": cond1,
        }
    )

    # 条件2: MA5 > MA10
    cond2 = ma5 is not None and ma10 is not None and ma5 > ma10
    conditions.append(
        {
            "name": "短期趋势向上(MA5>MA10)",
            "detail": f"MA5 {ma5} {'>' if cond2 else '<='} MA10 {ma10}",
            "passed": cond2,
        }
    )

    # 条件3: 近3日累计净流入 > 0
    recent_3d = flow_list[:3] if flow_list else []
    net_3d = sum(f["net_mf_amount"] for f in recent_3d)
    cond3 = net_3d > 0
    conditions.append(
        {
            "name": "近3日资金净流入",
            "detail": f"近3日净流入 {net_3d:+.2f} 万元",
            "passed": cond3,
        }
    )

    return conditions, net_3d


def build_md_report(stock, trade_date_str, latest_data, ma5, ma10, ma20,
                    flow_list, conditions, net_3d, closes):
    """生成 Markdown 复盘报告"""
    code = stock["code"]
    name = stock["name"]
    ts_code = stock["ts_code"]

    lines = []
    lines.append(f"# 📋 每日复盘报告: {name} ({code})")
    lines.append(f"")
    lines.append(f"**复盘日期**: {trade_date_str}")
    lines.append(f"**交易代码**: {ts_code}")
    lines.append(f"**股票名称**: {name}")
    lines.append(f"")

    # ── 行情概览 ──
    lines.append(f"## 一、行情概览")
    lines.append(f"")
    if latest_data:
        close = float(latest_data["close"]) if latest_data["close"] else "-"
        pct_chg = float(latest_data["pct_chg"]) if latest_data["pct_chg"] else "-"
        open_p = float(latest_data["open"]) if latest_data["open"] else "-"
        high = float(latest_data["high"]) if latest_data["high"] else "-"
        low = float(latest_data["low"]) if latest_data["low"] else "-"
        vol = float(latest_data["vol"]) if latest_data["vol"] else "-"
        amount = float(latest_data["amount"]) if latest_data["amount"] else "-"

        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 交易日 | {latest_data['trade_date']} |")
        lines.append(f"| 开盘价 | {open_p} |")
        lines.append(f"| 最高价 | {high} |")
        lines.append(f"| 最低价 | {low} |")
        lines.append(f"| 收盘价 | {close} |")
        lines.append(f"| 涨跌幅 | {pct_chg:+.2f}% |")
        lines.append(f"| 成交量 | {vol} |")
        lines.append(f"| 成交额 | {amount:.2f} 万 |")
        lines.append(f"")

    # ── 均线数据 ──
    lines.append(f"## 二、均线数据")
    lines.append(f"")
    lines.append(f"| 均线 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| MA5  | {ma5 if ma5 else '-'} |")
    lines.append(f"| MA10 | {ma10 if ma10 else '-'} |")
    lines.append(f"| MA20 | {ma20 if ma20 else '-'} |")
    lines.append(f"| 收盘价 | {float(latest_data['close']) if latest_data and latest_data['close'] else '-'} |")
    lines.append(f"")

    # 均线排列状态
    ma_status = []
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            ma_status.append("✅ 多头排列 (MA5>MA10>MA20)")
        elif ma5 < ma10 < ma20:
            ma_status.append("🔻 空头排列 (MA5<MA10<MA20)")
        else:
            ma_status.append("➖ 均线交叉/整理")
    lines.append(f"**均线排列**: {' '.join(ma_status) if ma_status else '数据不足'}")
    lines.append(f"")

    # ── 资金流向 ──
    lines.append(f"## 三、近10日资金流向")
    lines.append(f"")
    lines.append(f"| 日期 | 净流入(万) | 小单买 | 小单卖 | 中单买 | 中单卖 | 大单买 | 大单卖 | 特大单买 | 特大单卖 |")
    lines.append(f"|------|-----------|--------|--------|--------|--------|--------|--------|----------|----------|")

    for f in flow_list:
        lines.append(
            f"| {f['trade_date']} | {f['net_mf_amount']:+.2f} | "
            f"{f['buy_sm_amount']:.0f} | {f['sell_sm_amount']:.0f} | "
            f"{f['buy_md_amount']:.0f} | {f['sell_md_amount']:.0f} | "
            f"{f['buy_lg_amount']:.0f} | {f['sell_lg_amount']:.0f} | "
            f"{f['buy_elg_amount']:.0f} | {f['sell_elg_amount']:.0f} |"
        )
    lines.append(f"")

    # 累计统计
    total_net = sum(f["net_mf_amount"] for f in flow_list)
    positive_days = sum(1 for f in flow_list if f["net_mf_amount"] > 0)
    negative_days = sum(1 for f in flow_list if f["net_mf_amount"] < 0)
    lines.append(f"**10日累计净流入**: {total_net:+.2f} 万")
    lines.append(f"**净流入天数**: {positive_days}/{len(flow_list)} 天")
    lines.append(f"**净流出天数**: {negative_days}/{len(flow_list)} 天")
    lines.append(f"")

    # ── 入场条件检查 ──
    lines.append(f"## 四、入场条件检查")
    lines.append(f"")
    lines.append(f"| 条件 | 详情 | 结果 |")
    lines.append(f"|------|------|------|")

    passed_count = 0
    for c in conditions:
        icon = "✅" if c["passed"] else "❌"
        lines.append(f"| {c['name']} | {c['detail']} | {icon} |")
        if c["passed"]:
            passed_count += 1
    lines.append(f"")

    # 整体结论
    lines.append(f"**综合判定**: {passed_count}/3 条件满足")
    if passed_count == 3:
        lines.append(f"**建议**: ✅ 入场条件全部满足，可考虑建仓")
    elif passed_count >= 2:
        lines.append(f"**建议**: ⚠️ 部分条件满足，观望或轻仓试探")
    else:
        lines.append(f"**建议**: ❌ 多数条件不满足，建议等待")
    lines.append(f"")

    # ── 备注 ──
    lines.append(f"---")
    lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    lines.append(f"*本报告由 daily_review_picker.py 自动生成*")

    return "\n".join(lines)


def print_stdout_summary(stock, trade_date_str, latest_data, conditions,
                         flow_list, ma5, ma10, ma20):
    """打印 stdout 摘要 (cron 友好)"""
    code = stock["code"]
    name = stock["name"]
    close = float(latest_data["close"]) if latest_data and latest_data["close"] else "-"
    pct_chg = float(latest_data["pct_chg"]) if latest_data and latest_data["pct_chg"] else "-"

    print(f"\n{'='*60}")
    print(f"📋 每日复盘报告 — {name} ({code})")
    print(f"{'='*60}")
    print(f"  复盘日期:  {trade_date_str}")
    print(f"  收盘价:    {close}")
    print(f"  涨跌幅:    {pct_chg:+.2f}%")
    print(f"  MA5:       {ma5}")
    print(f"  MA10:      {ma10}")
    print(f"  MA20:      {ma20}")

    total_net = sum(f["net_mf_amount"] for f in flow_list)
    positive_days = sum(1 for f in flow_list if f["net_mf_amount"] > 0)
    print(f"  10日累计净流入: {total_net:+.2f} 万 ({positive_days}/{len(flow_list)}天净流入)")

    print(f"\n  ── 入场条件 ──")
    passed = 0
    for c in conditions:
        icon = "✅" if c["passed"] else "❌"
        print(f"    {icon} {c['name']}: {c['detail']}")
        if c["passed"]:
            passed += 1
    print(f"\n  📊 综合: {passed}/3 条件满足")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="随机每日复盘选股 — 加权随机从5只关注股中选取1只"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=date.today().strftime("%Y%m%d"),
        help="复盘日期 (YYYYMMDD, 默认今日)",
    )
    parser.add_argument(
        "--force-code",
        type=str,
        default=None,
        help="强制指定股票代码 (跳过随机选择, 用于测试)",
    )
    args = parser.parse_args()

    trade_date_str = args.date
    try:
        trade_date = datetime.strptime(trade_date_str, "%Y%m%d").date()
    except ValueError:
        print(f"❌ 日期格式错误: {args.date}, 应为 YYYYMMDD")
        sys.exit(1)

    ensure_dirs()
    tracker = load_tracker()

    print(f"🚀 启动每日复盘选股器")
    print(f"   日期:     {trade_date_str}")
    print(f"   跟踪文件: {TRACKER_PATH}")

    # 选股
    if args.force_code:
        chosen = None
        for s in STOCKS:
            if s["code"] == args.force_code or s["ts_code"] == args.force_code:
                chosen = s
                break
        if not chosen:
            print(f"❌ 未找到强制指定的股票: {args.force_code}")
            sys.exit(1)
        print(f"\n  🎯 强制指定: {chosen['code']} {chosen['name']}")
    else:
        chosen = pick_stock_weighted(tracker, trade_date)

    # 获取数据
    conn = get_db_connection()
    try:
        latest_data, ma5, ma10, ma20, closes = fetch_latest_price(
            conn, chosen["ts_code"], trade_date_str
        )
        if latest_data is None:
            print(f"❌ 无法获取 {chosen['ts_code']} 行情数据, 跳过")
            conn.close()
            sys.exit(1)

        flow_list = fetch_money_flow(conn, chosen["ts_code"], trade_date_str, days=10)

        # 检查入场条件
        conditions, net_3d = check_entry_conditions(
            latest_data, ma5, ma10, ma20, flow_list
        )

        # 生成报告
        report_md = build_md_report(
            chosen, trade_date_str, latest_data, ma5, ma10, ma20,
            flow_list, conditions, net_3d, closes
        )

        # 输出文件
        report_filename = f"daily_review_{chosen['code']}_{trade_date_str}.md"
        report_path = os.path.join(REVIEWS_DIR, report_filename)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"\n  ✅ 复盘报告已保存: {report_path}")

        # 更新跟踪文件
        tracker[chosen["code"]] = trade_date_str
        save_tracker(tracker, trade_date_str)

        # stdout 摘要
        print_stdout_summary(
            chosen, trade_date_str, latest_data, conditions,
            flow_list, ma5, ma10, ma20
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
