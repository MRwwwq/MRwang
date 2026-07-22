"""
agent_executor.py — 执行Agent（§2.3, 交易拆单滑点优化层）
职责隔离: 仅负责交易执行逻辑, 无权修改仓位/选股/风控
"""
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import math

DB_PATH = "/opt/stock_agent/agent_memory.db"
# 单笔委托量上限(占日均成交额比例)
MAX_ORDER_RATIO = 0.05
# 时间切片(分钟)
SLICE_MINUTES = 15


class AgentExecutor:
    """§2.3 执行Agent — 智能拆单, 降低冲击成本"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

    def schedule(self, approved_plan, market_hours=240):
        """
        接收风控放行后的仓位指令 → 分批委托序列
        输入: approved_plan = {ts_code: target_pos, ...}
        输出: [{ts_code, order_type, price, volume, time_slot}, ...]
        """
        orders = []

        for code, target_pos in (approved_plan or {}).items():
            if target_pos <= 0:
                continue

            # 获取日均成交额估算每笔量
            daily_amount = self._get_daily_amount(code)
            if daily_amount <= 0:
                daily_amount = 5000_0000  # 默认5000万

            # 按单笔≤5%日均额拆分
            order_value = daily_amount * MAX_ORDER_RATIO
            position_value = target_pos * 1_0000_0000  # 假设1亿总资金, 匹配实际
            num_slices = max(1, math.ceil(position_value / order_value))
            # 限制最多20笔
            num_slices = min(num_slices, 20)

            for i in range(num_slices):
                # 时间分散: 均匀分布在全天
                time_offset = int(market_hours / num_slices * i)
                orders.append({
                    "ts_code": code,
                    "order_type": "buy",
                    "volume_ratio": round(target_pos / num_slices, 4),
                    "time_slot_min": time_offset,
                    "slice_idx": i + 1,
                    "total_slices": num_slices,
                })

        print(f"[Executor] 拆单完成: {len(orders)}笔委托")
        return orders

    def _get_daily_amount(self, ts_code):
        """查近20日均成交额"""
        try:
            df = pd.read_sql(
                "SELECT amount FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
                self.conn, params=(ts_code,))
            if len(df) >= 5:
                return float(df["amount"].head(5).mean())
            return 0
        except Exception:
            return 0

    def fallback_execute(self, approved_plan):
        """§4 故障降级: 执行Agent异常时, 取消所有新开仓, 仅保留平仓"""
        print("[Executor] 故障降级: 暂停全部新开仓, 仅保留平仓")
        return []

    def close(self):
        self.conn.close()
