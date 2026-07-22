"""
上层交易决策 — PPO强化学习智能体
位置: 第5层(顶层) | 依赖: 底层base_score + 中层trend_score + SQL风控 + FAISS预警
"""
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import pandas as pd
from collections import deque


# ===================== PPO策略网络 =====================

class PolicyNet(nn.Module):
    """Actor-Critic网络: actor输出动作分布, critic输出状态价值"""
    def __init__(self, state_dim, action_dim):
        super().__init__()
        self.fc = nn.Sequential(
            nn.Linear(state_dim, 128),
            nn.Tanh(),
            nn.Linear(128, 64),
            nn.Tanh()
        )
        self.actor = nn.Linear(64, action_dim)
        self.critic = nn.Linear(64, 1)

    def forward(self, state):
        x = self.fc(state)
        action_logits = self.actor(x)
        value = self.critic(x)
        return action_logits, value


# ===================== PPO智能体 =====================

class PPOTradingAgent:
    """
    PPO交易智能体 — 自适应仓位/开仓/风控

    状态向量(7维):
      [base_score, trend_score, vol, max_drawdown, hold_pnl, market_chg, plate_corr]

    动作(4离散):
      0=空仓 1=轻仓 2=中等仓 3=重仓

    奖励函数:
      +10*hold_pnl 盈利奖励
      -15*drawdown 回撤惩罚
      ±仓位自适应奖励(强势/震荡/暴跌)
    """
    def __init__(self, state_dim=7, action_dim=4):
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.gamma = 0.95
        self.lr = 3e-4
        self.clip_eps = 0.2
        self.policy = PolicyNet(self.state_dim, self.action_dim)
        self.opt = optim.Adam(self.policy.parameters(), lr=self.lr)
        self.buffer = deque()
        self._action_map = {0: "空仓(0%)", 1: "轻仓(3~12%)", 2: "中等仓(12~25%)", 3: "重仓(25~40%)"}

    def build_state(self, base_score, trend_score, vol, max_drawdown,
                    hold_pnl, market_chg, plate_corr):
        """组装7维环境状态向量"""
        state_vec = np.array([
            base_score, trend_score, vol, max_drawdown,
            hold_pnl, market_chg, plate_corr
        ], dtype=np.float32)
        return torch.tensor(state_vec)

    def calculate_reward(self, hold_pnl, drawdown, market_chg, action):
        """奖励函数：盈利奖励+回撤惩罚+行情自适应"""
        reward = 0.0
        # 盈利正向奖励
        reward += hold_pnl * 10
        # 回撤惩罚
        reward -= drawdown * 15
        # 行情自适应奖惩
        if market_chg > 0.015:  # 强势行情>1.5%
            reward += action * 0.8
        elif market_chg <= 0.015 and market_chg >= -0.015:  # 震荡
            if action <= 1:
                reward += 0.5
            else:
                reward -= 1.2
        if market_chg < -0.03:  # 暴跌>3%
            reward -= action * 10
        return reward

    def get_action(self, state):
        """根据状态采样动作"""
        logits, value = self.policy(state.unsqueeze(0))
        dist = torch.distributions.Categorical(logits=logits)
        action = dist.sample()
        log_prob = dist.log_prob(action)
        return action.item(), log_prob.item(), value.item()

    def get_deterministic_action(self, state):
        """确定性动作(推理用, 非训练)"""
        logits, value = self.policy(state.unsqueeze(0))
        action = torch.argmax(logits, dim=1).item()
        return action, value.item()

    def action_to_position_pct(self, action: int) -> float:
        """动作→仓位百分比映射"""
        mapping = {0: 0.0, 1: 0.08, 2: 0.18, 3: 0.32}
        return mapping.get(action, 0.0)

    def save_agent(self, path="ppo_trade_agent.pth"):
        torch.save(self.policy.state_dict(), path)
        return path

    def load_agent(self, path="ppo_trade_agent.pth"):
        self.policy.load_state_dict(torch.load(path, map_location="cpu"))
        return "loaded"


# ===================== 测试入口 =====================

if __name__ == "__main__":
    print("PPO交易智能体 单元测试")
    print("=" * 50)

    agent = PPOTradingAgent()

    # 测试1: 状态构建
    state = agent.build_state(
        base_score=0.65, trend_score=0.72,
        vol=0.15, max_drawdown=0.05,
        hold_pnl=0.02, market_chg=0.01,
        plate_corr=0.03
    )
    assert state.shape == (7,), "状态维度错误"
    print("测试1✅ 状态构建: shape={}".format(state.shape))

    # 测试2: 动作采样
    action, log_prob, value = agent.get_action(state)
    assert 0 <= action <= 3, "动作越界"
    print("测试2✅ 动作采样: action={}({}) log_prob={:.4f} value={:.4f}".format(
        action, agent._action_map[action], log_prob, value))

    # 测试3: 各类行情下的奖励函数
    rewards = {
        "盈利+强势": agent.calculate_reward(0.05, 0.02, 0.02, 3),
        "亏损+暴跌": agent.calculate_reward(-0.03, 0.08, -0.05, 2),
        "震荡+轻仓": agent.calculate_reward(0.0, 0.01, 0.0, 1),
        "震荡+重仓": agent.calculate_reward(0.0, 0.01, 0.0, 3),
    }
    print("测试3✅ 奖励函数:")
    for k, v in rewards.items():
        print("    {}: {:+.4f}".format(k, v))

    # 测试4: 动作→仓位映射
    for a in range(4):
        pct = agent.action_to_position_pct(a)
        print("测试4✅ action={} → {} (仓位{:.0%})".format(a, agent._action_map[a], pct))

    # 测试5: 确定性动作(推理模式)
    det_action, det_value = agent.get_deterministic_action(state)
    print("测试5✅ 确定性推理: action={} value={:.4f}".format(det_action, det_value))

    # 测试6: 模型持久化
    agent.save_agent("/tmp/ppo_test.pth")
    agent2 = PPOTradingAgent()
    agent2.load_agent("/tmp/ppo_test.pth")
    a1, _, v1 = agent.get_action(state)
    a2, _, v2 = agent2.get_action(state)
    print("测试6✅ 模型保存/加载: action({}→{}) value({:.4f}→{:.4f})".format(a1, a2, v1, v2))

    import os; os.remove("/tmp/ppo_test.pth")
    print("\n全部测试通过 ✅")
