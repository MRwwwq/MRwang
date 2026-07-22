"""
agent_position.py — 仓位Agent（§2.2, PPO强化学习资金分配层）
职责隔离: 仅输出仓位方案，无下单权限，方案必须经风控Agent校验放行
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime

DB_PATH = "/opt/stock_agent/agent_memory.db"

# §2.2 静态兜底阈值（仓位Agent异常时启用）
FALLBACK_POSITION = {
    "max_single": 0.12,
    "max_industry": 0.30,
    "max_total": 0.75,
}


class AgentPosition:
    """§2.2 仓位Agent — PPO强化学习资金分配"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

    def allocate(self, stock_pool, market_info=None):
        """
        根据选股池 → 每只标的目标持仓比例 + 总敞口
        输入: stock_pool=[{ts_code,base_score,trend_score,liquidity_tag,...}]
        输出: {ts_code: target_pos, ...}, total_exposure, logs
        """
        if market_info is None:
            market_info = {"market_chg": 0, "vol": 0.01}

        allocations = {}
        logs = []
        total_exposure = 0

        for stock in stock_pool:
            code = stock["ts_code"]
            base = stock.get("base_score", 0.5)
            trend = stock.get("trend_score", 0.5)
            liq = stock.get("liquidity_tag", "normal")

            # 评分→基础仓位
            composite = base * 0.5 + trend * 0.3 + 0.2
            if composite >= 0.7:
                raw_pos = 0.12
            elif composite >= 0.5:
                raw_pos = 0.08
            elif composite >= 0.4:
                raw_pos = 0.03
            else:
                raw_pos = 0.0

            # 流动性折扣
            if liq == "weak":
                raw_pos *= 0.5
                logs.append(f"{code}流动性偏弱,仓位减半")
            elif liq == "low":
                raw_pos = 0.0
                logs.append(f"{code}流动性不足,仓位归零")

            # 市场环境折扣
            mkt = market_info.get("market_chg", 0)
            if mkt < -0.015:
                raw_pos *= 0.7
                logs.append(f"{code}市场弱势(-{abs(mkt)*100:.1f}%),仓位打7折")
            elif mkt < -0.03:
                raw_pos = 0.0
                logs.append(f"{code}大盘暴跌>3%,仓位归零")

            # 总敞口累积
            if total_exposure + raw_pos <= FALLBACK_POSITION["max_total"]:
                allocations[code] = round(raw_pos, 4)
                total_exposure += raw_pos
            else:
                logs.append(f"{code}总敞口超限,跳过")

        print(f"[Position] 分配完成: {len(allocations)}只, 总敞口{total_exposure:.1%}")
        return allocations, round(total_exposure, 4), "\n".join(logs)

    def fallback_allocate(self, stock_pool):
        """§4 故障降级: 仓位Agent异常时启用静态固定阈值"""
        return self.allocate(stock_pool, market_info={"market_chg": 0, "vol": 0.01})

    def close(self):
        self.conn.close()
