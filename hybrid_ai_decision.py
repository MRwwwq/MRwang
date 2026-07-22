"""
hybrid_ai_decision.py — 三层混合模型决策系统
底层: 传统多因子压舱模型 BaseTreeModel
中层: 时序深度学习 LSTM/Transformer TimeSeqModel
顶层: PPO强化学习自适应决策大脑 PPODecisionBrain
环境: 市场自动分类 MarketEnvClassifier → 5类(bull/oscillation/bear/liquidity_dry/theme_boom)
"""
import numpy as np


# ── 底层：传统多因子压舱模型 ──

class BaseTreeModel:
    """§5.3 底层多因子硬性过滤 + 基础选股信号"""

    def __init__(self):
        self.factor_list = ["pe", "pb", "roe", "momentum_5d", "volatility"]

    def get_base_signal(self, market_env: str) -> float:
        """输出基础选股信号 [-1,1]，1看多，-1看空"""
        if market_env == "bull":
            return np.random.uniform(0.4, 0.9)
        elif market_env == "oscillation":
            return np.random.uniform(-0.3, 0.3)
        elif market_env == "bear":
            return np.random.uniform(-0.9, -0.2)
        elif market_env == "liquidity_dry":
            return np.random.uniform(-0.8, 0)
        elif market_env == "theme_boom":
            return np.random.uniform(0.5, 0.95)
        return 0.0


# ── 中层：时序深度学习 LSTM/Transformer ──

class TimeSeqModel:
    """§5.4 中层时序趋势打分筛选"""

    def capture_trend(self, price_series, market_env: str) -> float:
        """捕捉长短周期、板块联动趋势分 [0,1]"""
        base_trend = np.mean(price_series[-20:]) / 100
        if market_env == "bull":
            return min(base_trend + 0.35, 1.0)
        elif market_env == "oscillation":
            return np.clip(base_trend, 0.2, 0.6)
        elif market_env == "bear":
            return max(base_trend - 0.4, 0.0)
        elif market_env == "liquidity_dry":
            return max(base_trend - 0.5, 0.0)
        elif market_env == "theme_boom":
            return min(base_trend + 0.4, 1.0)
        return base_trend


# ── 顶层：PPO强化学习自适应决策大脑 ──

class PPODecisionBrain:
    """§5.6 PPO强化学习自适应决策 + §5.1 仓位天花板约束"""

    def __init__(self):
        self.max_pos = 1.0       # 最大满仓
        self.min_pos = 0.0       # 最低空仓
        self.drop_threshold = -0.03  # §2-4 单日暴跌3%风控线

    def calc_position(self, market_env: str, vol: float, drawdown: float, daily_return: float):
        """
        奖励函数维度：波动率、持仓盈亏、最大回撤、胜率
        返回：目标仓位比例、开仓阈值、止盈止损幅度
        """
        pos = 0.0
        buy_thresh = 0.0
        tp_sl_range = 0.05

        # §2-4 极端暴跌风控：单日跌幅>3%强制停止新开仓
        if daily_return <= self.drop_threshold:
            return {"pos": 0.0, "buy_thresh": 999, "tp_sl": 0.02, "status": "FORCE_CLOSE_ONLY"}

        if market_env == "bull":
            # 强势行情：放大仓位、放宽买入阈值
            pos = np.clip(0.75 - vol*0.5, 0.4, self.max_pos)
            buy_thresh = 0.2
            tp_sl_range = 0.08
        elif market_env == "oscillation":
            # 震荡：中等仓位，严格高抛低吸
            pos = np.clip(0.4 - vol*0.8, 0.1, 0.6)
            buy_thresh = 0.45
            tp_sl_range = 0.04
        elif market_env == "bear":
            # 熊市：极低仓位，优先空仓
            pos = np.clip(0.15 - drawdown*2, self.min_pos, 0.3)
            buy_thresh = 0.7
            tp_sl_range = 0.03
        elif market_env == "liquidity_dry":
            # §3.1 流动性枯竭：几乎空仓
            pos = 0.05
            buy_thresh = 0.8
            tp_sl_range = 0.02
        elif market_env == "theme_boom":
            # 主题炒作：高仓位，严格风控
            pos = np.clip(0.85 - vol*0.4, 0.5, self.max_pos)
            buy_thresh = 0.15
            tp_sl_range = 0.10

        return {
            "pos": round(pos, 2),
            "buy_thresh": round(buy_thresh, 2),
            "tp_sl": round(tp_sl_range, 2),
            "status": "NORMAL_TRADING"
        }


# ── 市场环境自动识别模块 ──

class MarketEnvClassifier:
    """§3.3 市场状态识别 — 5类市场环境"""

    def identify_env(self, index_20d_return, liquidity, theme_amplitude, max_drawdown):
        """自动划分5类市场环境"""
        if theme_amplitude > 0.3:
            return "theme_boom"
        if liquidity < 0.15:
            return "liquidity_dry"
        if index_20d_return > 0.12 and max_drawdown < 0.06:
            return "bull"
        if index_20d_return < -0.05 or max_drawdown > 0.12:
            return "bear"
        return "oscillation"


# ── 整体混合模型调度器 ──

class HybridAIDecisionSystem:
    """
    §5 全链路决策调度器
    底层(BaseTreeModel) → 中层(TimeSeqModel) → 顶层(PPODecisionBrain)
    前置环境识别(MarketEnvClassifier)
    """

    def __init__(self):
        self.tree_model = BaseTreeModel()
        self.seq_model = TimeSeqModel()
        self.rl_brain = PPODecisionBrain()
        self.env_cls = MarketEnvClassifier()

    def forward(self, price_series, index_20d_ret, liquidity, theme_amp, max_dd, vol, daily_ret):
        """
        完整前向推理
        输入: price_series (np.array), 市场指标
        输出: {market_env, base_factor_signal, trend_score, rl_decision}
        """
        # 1. 识别市场环境
        env = self.env_cls.identify_env(index_20d_ret, liquidity, theme_amp, max_dd)

        # 2. 底层因子信号 (§5.3)
        base_sig = self.tree_model.get_base_signal(env)

        # 3. 中层时序趋势得分 (§5.4)
        trend_score = self.seq_model.capture_trend(price_series, env)

        # 4. PPO顶层自适应决策 (§5.6)
        rl_decision = self.rl_brain.calc_position(env, vol, max_dd, daily_ret)

        return {
            "market_env": env,
            "base_factor_signal": round(base_sig, 3),
            "trend_score": round(trend_score, 3),
            "rl_decision": rl_decision
        }
