"""
agent_selector.py — 选股Agent（§2.1）
职责隔离: 仅负责标的筛选，无权干预资金分配/下单/风控/模型迭代
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime

DB_PATH = "/opt/stock_agent/agent_memory.db"
PG_CONN = None  # set by orchestrator at startup


class AgentSelector:
    """§2.1 选股Agent — 每日全市场批量扫描，输出高置信度预选标的池"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

    def scan(self, target_codes=None):
        """
        全市场/指定池扫描 → [候选标的dict]
        每个候选含: ts_code, industry, base_score, trend_score, sentiment, liquidity_tag
        """
        if target_codes is None:
            target_codes = []
        candidates = []

        for code in target_codes:
            try:
                cand = self._score_stock(code)
                if cand:
                    candidates.append(cand)
            except Exception as e:
                print(f"  [Selector] {code} 扫描异常: {e}")
                continue
        # 按综合分降序排列
        candidates.sort(key=lambda x: x.get("composite", 0), reverse=True)
        print(f"[Selector] 扫描完成: {len(candidates)}只候选")
        return candidates

    def _score_stock(self, code):
        """单只标的评分 → 选股池条目"""
        df = pd.read_sql(
            "SELECT base_factor_score, trend_score, sentiment_score, rsi, macd, "
            "ma5, ma10, ma20, close, amount, trade_date, industry "
            "FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
            self.conn, params=(code,))
        if df.empty:
            return None

        row = df.iloc[0]
        base = float(row.get("base_factor_score", 0.5))
        trend = float(row.get("trend_score", 0.5))
        sent = float(row.get("sentiment_score", 0.5))
        close = float(row.get("close", 0))
        amount = float(row.get("amount", 0))

        # 流动性标签
        amt_yi = amount / 1e8
        if amt_yi < 0.5:
            liq_tag = "low"
        elif amt_yi < 1.0:
            liq_tag = "weak"
        else:
            liq_tag = "normal"

        # 综合分 (加权)
        composite = round(base * 0.4 + trend * 0.3 + sent * 0.15 + min(amt_yi/10, 0.15), 3)

        return {
            "ts_code": code,
            "industry": row.get("industry", ""),
            "base_score": base,
            "trend_score": trend,
            "sentiment": sent,
            "close": close,
            "liquidity_tag": liq_tag,
            "daily_amount_yi": round(amt_yi, 2),
            "composite": composite,
            "trade_date": str(row.get("trade_date", "")),
        }

    def close(self):
        self.conn.close()
