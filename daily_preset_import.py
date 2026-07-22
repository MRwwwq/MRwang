#!/usr/bin/env python3
"""
每日数据源校验 + 观察标的导入
"""
import sqlite3, os, sys, time
from datetime import datetime

sys.path.insert(0, "/opt/stock_agent")

TODAY = "20260722"
MEMORY_DB = "/opt/stock_agent/agent_memory.db"

def update_observation(code, name, tag, date=TODAY):
    conn = sqlite3.connect(MEMORY_DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO observation_list(date, ts_code, stock_name, tag, is_suspended)
        VALUES (?, ?, ?, ?, 0)
    """, (date, code, name, tag))
    conn.commit()
    conn.close()
    print(f"  ✅ 导入 {code} {name} [{tag}]")

def batch_import():
    # ===== 持仓 =====
    positions = [
        ("600884", "杉杉股份", "持仓"),
    ]
    # ===== 中线布局 =====
    mid_lines = [
        ("600547", "山东黄金", "中线布局"),
        ("300476", "胜宏科技", "中线布局"),
        ("600585", "海螺水泥", "中线布局"),
        ("600941", "中国移动", "中线布局"),
        ("600183", "生益科技", "中线布局"),
    ]
    # ===== 短线跟踪 =====
    short_track = [
        ("002044", "美年健康", "短线跟踪"),
        ("002617", "露笑科技", "短线跟踪"),
        ("601138", "工业富联", "短线跟踪"),
        ("000725", "京东方A", "短线跟踪"),
        ("000063", "中兴通讯", "短线跟踪"),
    ]
    # ===== 风险避雷 =====
    risk_avoid = [
        ("002617", "露笑科技", "风险避雷"),
    ]
    # ===== 观察跟踪 =====
    watch = [
        ("300444", "双杰电气", "观察跟踪"),
        ("002709", "天赐材料", "观察跟踪"),
        ("300037", "新宙邦", "观察跟踪"),
        ("300073", "当升科技", "观察跟踪"),
        ("000973", "佛塑科技", "观察跟踪"),
        ("300473", "德尔股份", "观察跟踪"),
        ("301246", "宏源药业", "观察跟踪"),
        ("301587", "中瑞股份", "观察跟踪"),
        ("603876", "鼎胜新材", "观察跟踪"),
        ("688097", "博众精工", "观察跟踪"),
        ("000049", "德赛电池", "观察跟踪"),
        ("002125", "湘潭电化", "观察跟踪"),
        ("002957", "科瑞技术", "观察跟踪"),
        ("300457", "赢合科技", "观察跟踪"),
    ]

    all_stocks = positions + mid_lines + short_track + risk_avoid + watch
    print(f"\n批量导入 {TODAY} 观察标的: {len(all_stocks)}只")
    print("=" * 50)
    for code, name, tag in all_stocks:
        update_observation(code, name, tag)

if __name__ == "__main__":
    batch_import()
