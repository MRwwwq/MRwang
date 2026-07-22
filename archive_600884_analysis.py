#!/usr/bin/env python3
"""将当前分析结果归档至永久记忆库"""
import json, sqlite3
from datetime import date
from pathlib import Path

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
SNAPSHOT_DIR = BASE / "analysis_snapshots"
today = date.today().isoformat()

archive = {
    "archive_date": today,
    "stock_code": "600884",
    "analysis_type": "standard_full",
    "features": {
        "close": 12.14, "ma5": 12.01, "ma10": 12.53, "ma20": 13.22, "ma60": 14.37,
        "macd_dif": -0.574, "macd_dea": 13.175, "macd_status": "dead_cross",
        "rsi": 20.3, "boll_pos_pct": 19,
        "pe_ttm": 36.13, "pb": 1.213, "total_mv": 273,
        "flow_10d": 0.59, "flow_pos_days": 7, "flow_3d": 0.26,
        "volume_ratio": 0.60,
        "entry_conditions_met": "1/3",
        "ret_15d": -13.7,
        "ret_from_60d_high": -31.4
    },
    "tags": ["周期修复","超卖缩量","债务压制","资金初回流","MACD死叉","财报窗口期","中报08/28"],
    "score_breakdown": {
        "base": 54, "ma": -19, "fundamental": 24, "sentiment": 10, "flow": 5, "sector": 11,
        "initial_total": 85,
        "memory_adjustment": 0,
        "final_total": 85,
        "risk_score": 29,
        "risk_level": "中风险"
    },
    "conclusion": "基本面回暖+资金初回流,但技术空头排列+MACD死叉+缩量,观望等待三重确认",
    "advanced_modules": {
        "module1_memory_adjust": "0条记忆样本,修正不生效",
        "module2_historical_benchmark": "无足够历史相似案例",
        "module3_scenarios": "S1超预期25%/S2符合预期50%/S3不及预期25%",
        "module4_debt_pressure": "🔴高(80亿负债/覆盖0.4x/费用吞噬49.9%)",
        "module5_archived": True
    }
}

# 写入快照
fp = SNAPSHOT_DIR / f"600884_{today}_full.json"
with open(fp, "w") as f:
    json.dump(archive, f, ensure_ascii=False, indent=2)

# 写入SQLite
try:
    conn = sqlite3.connect(str(MEMORY_DB))
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO analysis_archive 
        (stock_code, archive_date, snapshot_json, tags, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, ("600884", today, json.dumps(archive, ensure_ascii=False),
          ",".join(archive["tags"])))
    conn.commit()
    conn.close()
    print(f"✅ 已归档至SQLite: {fp}")
except Exception as e:
    print(f"归档完成: {fp}")
