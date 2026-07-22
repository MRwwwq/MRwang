#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_data_feed.py — 赛道分类宏观数据投喂
每日补充: 行业PE分位/宏观政策/赛道景气度→入库
"""

import sys, json, logging
from datetime import datetime
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, "/opt/stock_agent")
from config import pg_engine, SECTOR_GROUPS

logger = logging.getLogger("DataFeed")

SECTOR_PE_RANGES = {
    "AI科技": {"pe_low": 25, "pe_high": 50, "pe_median": 35, "景气度": "高"},
    "新能源": {"pe_low": 20, "pe_high": 45, "pe_median": 30, "景气度": "中"},
    "消费电子": {"pe_low": 20, "pe_high": 40, "pe_median": 28, "景气度": "中"},
    "贵金属": {"pe_low": 22, "pe_high": 35, "pe_median": 28, "景气度": "高"},
    "医疗": {"pe_low": 30, "pe_high": 60, "pe_median": 45, "景气度": "中"},
    "周期防御": {"pe_low": 8, "pe_high": 18, "pe_median": 12, "景气度": "中"},
    "科技成长": {"pe_low": 30, "pe_high": 55, "pe_median": 40, "景气度": "中"},
}


def feed_sector_data():
    """投喂赛道行业数据"""
    logger.info("===== 赛道数据投喂 =====")
    records = []
    for sector, codes in SECTOR_GROUPS.items():
        pe_range = SECTOR_PE_RANGES.get(sector, {})
        record = {
            "sector": sector,
            "codes": ",".join(codes),
            "pe_median": pe_range.get("pe_median"),
            "pe_low": pe_range.get("pe_low"),
            "pe_high": pe_range.get("pe_high"),
            "景气度": pe_range.get("景气度", "中"),
            "updated_at": datetime.now(),
        }
        records.append(record)

    df = pd.DataFrame(records)
    df.to_sql("sector_pe_data", pg_engine, if_exists="replace", index=False)
    logger.info(f"  {len(records)}个赛道数据已投喂")
    return records


if __name__ == "__main__":
    feed_sector_data()
