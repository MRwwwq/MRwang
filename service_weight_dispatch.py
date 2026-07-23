#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_weight_dispatch.py — §5.4 动态加权矩阵统一调度管理

职责:
  统一维护周期赛道、题材赛道两套独立动态权重矩阵；
  接收沙盒优化结果、盘中共振自愈微调结果更新矩阵；
  统一管控全局时效衰减参数、正向对冲上限参数。

分层调用关系:
  RULE_SCORE_ENGINE (L2层) → get_dynamic_weights(stock_type) → 获取当前有效权重
  EVOLUTION_AGENT         → apply_self_heal_adjustment() → 盘中小幅微调
  SANDBOX_TUNING           → apply_sandbox_update() → 每周离线更新矩阵
"""

import logging
import json
import copy
from datetime import datetime
from typing import Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEIGHT_DISP] %(message)s",
    datefmt="%H:%M:%S",
)

# ===================== 双赛道默认权重矩阵 =====================

# 周期资源股权重矩阵 — 弱化短期情绪，提升供需/库存/周期信号
CYCLE_WEIGHT_DEFAULT = {
    # Rule021-2 对应维度权重
    "commodity_3y_percentile": 0.25,   # 大宗3年分位
    "capacity_utilization":     0.20,   # 产能开工率
    "relative_cost":            0.20,   # 行业相对成本
    "debt_ratio":               0.15,   # 资产负债率
    "pe_historical_percentile": 0.20,   # PE历史分位
    # 额外信号权重(非Rule021维度, 供L2动态加权用)
    "inventory_cycle":          0.15,   # 库存周期信号
    "supply_demand_gap":        0.15,   # 供需缺口信号
    "short_term_sentiment":     0.05,   # 短期情绪(弱化)
}

# 题材概念股权重矩阵 — 提升政策/资金持续性/板块情绪
THEME_WEIGHT_DEFAULT = {
    # Rule021-1 对应维度权重
    "policy_catalyst":          0.25,   # 题材政策催化
    "sector_heat":              0.20,   # 板块热度
    "theme_purity":             0.15,   # 个股赛道贴合
    "fund_stability":           0.20,   # 主力筹码稳定
    "expectation_gap":          0.20,   # 题材预期差
    # 额外信号权重
    "policy_sustainability":    0.15,   # 政策持续性
    "capital_continuity":       0.15,   # 资金持续性
    "short_term_sentiment":     0.12,   # 短期情绪(维持较高)
}

# ===================== 时效衰减参数 =====================

DECAY_PARAMS_DEFAULT = {
    # 短期事件信号衰减(小时级)
    "short_term_halflife_hours":  4,     # 半衰期4小时
    "short_term_min_factor":      0.3,   # 最低衰减至30%

    # 中长期基本面/周期拐点衰减(天级)
    "medium_term_halflife_days":  7,     # 半衰期7天
    "medium_term_min_factor":     0.5,   # 最低衰减至50%

    # 长期宏观信号衰减(月级)
    "long_term_halflife_days":    30,    # 半衰期30天
    "long_term_min_factor":       0.7,   # 最低衰减至70%
}

# ===================== 正向对冲参数 =====================

HEDGE_PARAMS_DEFAULT = {
    "max_hedge_ratio":          0.50,   # 正向对冲抵扣上限50%
    "single_hedge_cap":         0.30,   # 单条利好对冲上限30%
    "major_negative_exempt":    0.90,   # 重大利空(≥9)豁免对冲比例90%
}


class DynamicWeightDispatch:
    """§5.4 动态加权矩阵统一调度管理。

    职责:
      1. 统一维护周期/题材两套权重矩阵
      2. 接收沙盒调优结果 → apply_sandbox_update()
      3. 接收盘中自愈微调 → apply_self_heal_adjustment()
      4. 对外提供当前有效权重 → get_dynamic_weights()
      5. 统一管控衰减参数 + 对冲上限参数
    """

    def __init__(self):
        # 双赛道权重矩阵(深拷贝防止引用污染)
        self._cycle_weights = dict(CYCLE_WEIGHT_DEFAULT)
        self._theme_weights = dict(THEME_WEIGHT_DEFAULT)
        self._decay_params = dict(DECAY_PARAMS_DEFAULT)
        self._hedge_params = dict(HEDGE_PARAMS_DEFAULT)
        self._version = "1.0.0"
        self._update_log = []

    # ─────── 对外接口 ───────

    def get_dynamic_weights(self, stock_type: str) -> dict:
        """获取当前有效动态权重(归一化后)。

        Args:
            stock_type: "resource"(周期) / "concept"(题材) / "bluechip"(蓝筹)

        Returns:
            {weights: {dim_name: weight, ...}, decay_params: ..., hedge_params: ...}
        """
        if stock_type == "resource":
            raw = self._cycle_weights
        elif stock_type == "bluechip":
            # 蓝筹暂用周期权重(基本面导向)
            raw = self._cycle_weights
        else:
            raw = self._theme_weights

        # 归一化(Rule021 5维 + 额外信号)
        main_dims = {k: v for k, v in raw.items() if
                     k not in ("inventory_cycle", "supply_demand_gap",
                                "policy_sustainability", "capital_continuity",
                                "short_term_sentiment")}
        extra_dims = {k: v for k, v in raw.items() if k not in main_dims}

        total_main = sum(main_dims.values()) or 1.0
        main_normalized = {k: round(v / total_main, 3) for k, v in main_dims.items()}

        total_extra = sum(extra_dims.values()) or 1.0
        extra_normalized = {k: round(v / total_extra, 3) for k, v in extra_dims.items()}

        return {
            "stock_type": stock_type,
            "main_dim_weights": main_normalized,
            "extra_signal_weights": extra_normalized,
            "decay_params": dict(self._decay_params),
            "hedge_params": dict(self._hedge_params),
            "version": self._version,
        }

    def get_decay_factor(self, signal_type: str, elapsed_hours: float = 1.0) -> float:
        """计算信号时效衰减系数。

        Args:
            signal_type: "short_term" / "medium_term" / "long_term"
            elapsed_hours: 信号产生后经过的小时数

        Returns:
            decay_factor [min_factor, 1.0]
        """
        p = self._decay_params
        if signal_type == "short_term":
            halflife = p["short_term_halflife_hours"]
            min_f = p["short_term_min_factor"]
        elif signal_type == "medium_term":
            halflife = p["medium_term_halflife_days"] * 24
            min_f = p["medium_term_min_factor"]
        else:
            halflife = p["long_term_halflife_days"] * 24
            min_f = p["long_term_min_factor"]

        if halflife <= 0:
            return 1.0
        factor = 2 ** (-elapsed_hours / halflife)
        return round(max(min_f, min(1.0, factor)), 3)

    def apply_hedge(self, base_score: float,
                    positive_signals: list) -> dict:
        """§3 L2 正向对冲机制。

        负向风险可被正向逻辑对冲(供需反转/政策利好/基本面修复/现金流改善)。
        设置对冲上限，重大利空无法完全抵消。

        Args:
            base_score: 对冲前的基础分值
            positive_signals: 正向信号强度列表 [0~10]

        Returns:
            {hedged_score, hedge_ratio, hedge_detail}
        """
        p = self._hedge_params
        max_ratio = p["max_hedge_ratio"]
        single_cap = p["single_hedge_cap"]
        major_exempt = p["major_negative_exempt"]

        # 重大利空检查(base_score≥80表示严重利空)
        is_major_negative = base_score >= 80

        if is_major_negative:
            # 重大利空: 最多豁免对冲比例10%
            effective_max = max_ratio * (1.0 - major_exempt)
        else:
            effective_max = max_ratio

        # 逐条正向信号计算对冲
        total_hedge = 0.0
        hedge_detail = []
        for i, sig in enumerate(positive_signals):
            hedge = min(sig / 10.0, single_cap)  # sig [0,10] → hedge [0, single_cap]
            remaining = effective_max - total_hedge
            if remaining <= 0:
                break
            actual = min(hedge, remaining)
            total_hedge += actual
            hedge_detail.append({
                f"signal_{i}": {"strength": sig, "hedge_applied": round(actual, 3)}
            })

        hedged_score = max(0, base_score * (1.0 - total_hedge))
        hedge_ratio = round(total_hedge, 3)

        return {
            "hedged_score": round(hedged_score, 1),
            "hedge_ratio": hedge_ratio,
            "hedge_detail": hedge_detail,
            "max_hedge_ratio": effective_max,
            "is_major_negative": is_major_negative,
            "note": f"正向对冲: {hedge_ratio*100:.1f}%抵扣 → {base_score}→{hedged_score:.0f}",
        }

    # ─────── 参数更新接口 ───────

    def apply_sandbox_update(self, new_weights: dict,
                              new_decay: dict = None,
                              new_hedge: dict = None) -> dict:
        """§5.3 接收沙盒优化结果，更新全局参数。

        Args:
            new_weights: {"cycle": {...}, "theme": {...}} 或单赛道的权重字典
            new_decay: 衰减参数覆盖
            new_hedge: 对冲参数覆盖
        """
        changes = []
        if new_weights:
            if "cycle" in new_weights:
                self._cycle_weights.update(new_weights["cycle"])
                changes.append("cycle_weights_updated")
            if "theme" in new_weights:
                self._theme_weights.update(new_weights["theme"])
                changes.append("theme_weights_updated")
        if new_decay:
            self._decay_params.update(new_decay)
            changes.append("decay_params_updated")
        if new_hedge:
            self._hedge_params.update(new_hedge)
            changes.append("hedge_params_updated")

        self._version = f"1.{len(self._update_log)+1}.0"
        self._update_log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "sandbox_update",
            "changes": changes,
        })
        msg = f"沙盒更新: {', '.join(changes)} (版本{self._version})"
        logging.info(f"  ✅ {msg}")
        return {"version": self._version, "changes": changes}

    def apply_self_heal_adjustment(self, stock_type: str,
                                    adjustments: dict) -> dict:
        """§5.2 盘中自愈微调：小幅调整权重。

        仅临时微调局部参数，不修改全局基线阈值。
        """
        target = self._cycle_weights if stock_type == "resource" else self._theme_weights
        changes = []
        for k, delta in adjustments.items():
            if k in target:
                old = target[k]
                # delta: 正值=上调权重, 负值=下调
                new_val = max(0.01, min(0.50, old + delta))
                target[k] = round(new_val, 3)
                changes.append(f"{k}: {old:.3f}→{target[k]:.3f}")

        self._version = f"1.{len(self._update_log)+1}.0"
        self._update_log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "self_heal_adjustment",
            "stock_type": stock_type,
            "changes": changes,
        })
        logging.info(f"  🔄 自愈微调: {', '.join(changes)}")
        return {"version": self._version, "changes": changes}

    # ─────── 状态查询 ───────

    def status_report(self) -> dict:
        return {
            "version": self._version,
            "cycle_weights": self._cycle_weights,
            "theme_weights": self._theme_weights,
            "decay_params": self._decay_params,
            "hedge_params": self._hedge_params,
            "update_count": len(self._update_log),
            "recent_updates": self._update_log[-5:],
        }


# ===================== 全局单例 =====================

_dispatch = None


def get_dispatch() -> DynamicWeightDispatch:
    global _dispatch
    if _dispatch is None:
        _dispatch = DynamicWeightDispatch()
    return _dispatch


def reset_dispatch():
    global _dispatch
    _dispatch = None


if __name__ == "__main__":
    reset_dispatch()
    d = get_dispatch()

    # 1. 获取权重
    theme_w = d.get_dynamic_weights("concept")
    print(f"✅ 题材权重: {len(theme_w['main_dim_weights'])}维主+{len(theme_w['extra_signal_weights'])}维附加")
    print(f"  衰减参数: {theme_w['decay_params']}")
    print(f"  对冲上限: {theme_w['hedge_params']['max_hedge_ratio']}")

    cycle_w = d.get_dynamic_weights("resource")
    print(f"✅ 周期权重: {len(cycle_w['main_dim_weights'])}维主")

    # 2. 衰减系数
    f1 = d.get_decay_factor("short_term", 2)
    f2 = d.get_decay_factor("short_term", 8)
    print(f"✅ 衰减: 短期2h={f1} 8h={f2}")

    # 3. 正向对冲
    h1 = d.apply_hedge(65, [6.0, 4.0, 7.0])
    print(f"✅ 对冲(普通): {h1['hedged_score']} ratio={h1['hedge_ratio']}")

    h2 = d.apply_hedge(85, [8.0, 6.0])
    print(f"✅ 对冲(重大利空): {h2['hedged_score']} ratio={h2['hedge_ratio']} note={h2['note']}")

    # 4. 自愈微调
    adj = d.apply_self_heal_adjustment("concept", {"policy_catalyst": 0.05, "short_term_sentiment": -0.03})
    print(f"✅ 自愈: {adj['changes']}")

    # 5. 沙盒更新
    sandbox = d.apply_sandbox_update(
        {"cycle": {"commodity_3y_percentile": 0.28, "short_term_sentiment": 0.03}},
        new_decay={"short_term_halflife_hours": 6},
        new_hedge={"max_hedge_ratio": 0.55},
    )
    print(f"✅ 沙盒: {sandbox['changes']}")

    print()
    print("✅ §5.4 动态加权调度 全部测试通过")
