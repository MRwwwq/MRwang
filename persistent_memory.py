# persistent_memory.py
import numpy as np
from config_memory import (
    RECENT_WEIGHT, HISTORY_WEIGHT, PROFIT_THRESHOLD, LOSS_THRESHOLD
)
from memory_db import TradingMemoryDB
from memory_redis import ShortMemoryRedis
from memory_vector import VectorMemorySearch


class PersistentMemory:
    """
    持久记忆总控制器 — 统一管理SQLite/FAISS/Redis三层存储
    对外提供决策查询、记忆修正、写入归档接口
    """

    def __init__(self):
        self.db = TradingMemoryDB()
        self.vector = VectorMemorySearch()
        self.cache = ShortMemoryRedis()

    # ── 黑名单拦截 ──
    def is_in_blacklist(self, stock_code: str) -> bool:
        """检查是否在全局黑名单中"""
        bl = self.db.get_black_list("black_stock")
        return stock_code in bl

    # ── 相似历史检索 ──
    def get_similar_history(self, current_feat: np.ndarray):
        """
        检索相似历史交易，支持记忆蒸馏

        返回: [{"stock_code", "market_env", "feature", "profit_rate", "tag"}, ...]
        """
        hits = self.vector.search_similar(current_feat)
        return hits

    # ── 记忆修正模型得分 ──
    def adjust_score_by_history(self, raw_score: float,
                                 similar_cases: list) -> float:
        """
        利用历史相似交易的收益率修正原始模型得分

        修正逻辑:
          1. 统计相似交易中盈利/亏损比例
          2. 盈利比例>60% → 加分, <40% → 扣分
          3. 优质记忆(PROFIT_THRESHOLD以上)加权更多
          4. 负面记忆(LOSS_THRESHOLD以下)加大扣分力度

        :param raw_score: 模型原始得分 (0~1)
        :param similar_cases: 相似历史交易列表
        :return: 修正后得分 (0~1)
        """
        if not similar_cases:
            return raw_score

        profit_rates = [s["profit_rate"] for s in similar_cases]
        n = len(profit_rates)

        # 基础统计
        good_count = sum(1 for p in profit_rates if p > PROFIT_THRESHOLD)
        bad_count = sum(1 for p in profit_rates if p < LOSS_THRESHOLD)

        good_ratio = good_count / n if n > 0 else 0.5

        # 优质记忆加权收益率（盈利越多权重越大）
        weighted_profit = sum(
            p * (1 + p) for p in profit_rates if p > 0
        ) / max(1, sum(1 for p in profit_rates if p > 0))

        # 负面记忆加权
        weighted_loss = sum(
            abs(p) * (1 + abs(p)) for p in profit_rates if p < 0
        ) / max(1, sum(1 for p in profit_rates if p < 0))

        # 修正幅度计算
        # 盈利比例偏离0.5越远 → 修正越大
        base_adj = (good_ratio - 0.5) * 2  # -1 ~ 1

        # 加入幅度加权
        magnitude_adj = (weighted_profit - weighted_loss) * 0.3

        # 总调整量 (±0.15 限制)
        total_adj = np.clip(base_adj * 0.08 + magnitude_adj, -0.15, 0.15)

        adjusted = np.clip(raw_score + total_adj, 0.0, 1.0)

        # 调试信息
        adj_info = {
            "raw_score": round(raw_score, 3),
            "adjusted_score": round(adjusted, 3),
            "adjustment": round(total_adj, 4),
            "good_ratio": round(good_ratio, 3),
            "weighted_profit": round(weighted_profit, 4),
            "weighted_loss": round(weighted_loss, 4),
            "good_count": good_count,
            "bad_count": bad_count,
        }
        # 挂载到self供调试查看
        self._last_adj_info = adj_info

        return round(adjusted, 3)

    # ── 保存交易记忆 ──
    def save_trade_memory(self, trade_data: dict):
        """
        平仓后归档为永久记忆

        自动判断:
          - profit_rate > PROFIT_THRESHOLD → tag='good'
          - profit_rate < LOSS_THRESHOLD  → tag='bad', 加入黑名单
          - 其余 → tag='normal'
        """
        profit = trade_data.get("profit_rate", 0)

        if profit >= PROFIT_THRESHOLD:
            trade_data["memory_tag"] = "good"
        elif profit <= LOSS_THRESHOLD:
            trade_data["memory_tag"] = "bad"
            # 大额亏损自动拉黑
            stock_code = trade_data.get("stock_code", "")
            self.db.add_global_rule("black_stock", stock_code)
            print(f"  ⛔ {stock_code} 亏损{profit:.3f}, 已加入黑名单")
        else:
            trade_data["memory_tag"] = "normal"

        # 写入SQLite
        self.db.insert_trade(trade_data)

        # 更新FAISS索引
        feat = np.array(trade_data.get("feature", []), dtype=np.float32)
        self.vector.add_vector(feat)

        # 写入Redis缓存(最近交易摘要)
        self.cache.set_cache(f"last_trade_{trade_data['stock_code']}", trade_data)

    # ── 资源释放 ──
    def close_all(self):
        self.db.close()
        print("  📦 记忆系统已关闭，DB/索引已持久化")
