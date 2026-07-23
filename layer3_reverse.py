#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
layer3_reverse.py — L3 Rule015 双重共振兜底反转校验层

职能：识别"高风险打分但基本面强支撑共振"场景，反转下调风险等级，
      规避单一维度打分导致误判。

双重共振场景：
  1. 周期股：低价低位 + 极致成本优势（商品价格处于历史低位+成本行业前20%）
  2. 题材股：产业落地 + 长期政策扶持（技术已量产+国家级产业政策3年以上）

反转修正规则：
  - 强共振（两个条件都明确满足）→ 风险分 ×0.6，下调风控等级一级
  - 弱共振（一个条件明确+一个条件部分满足）→ 风险分 ×0.85，风控等级不降级但降低风险分
  - 无共振 → ×1.0，不修正

时序约束：L3 为四层最后一层，接收 L2 输出的总误判分做兜底反转校验。
          反转修正后的分值再送入赛道差异化阈值判定。
"""

import logging
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [L3-REV] %(message)s",
                    datefmt="%H:%M:%S")


# ===================== 共振条件定义 =====================

# 周期股双重共振判定阈值
RESOURCE_DUAL_RESONANCE = {
    "low_price": {  # 条件A: 商品价格低位
        "label": "商品价格处于历史低位",
        "commodity_price_percentile_max": 30,  # 价格分位≤30%
    },
    "extreme_cost": {  # 条件B: 极致成本优势
        "label": "企业成本行业前20%",
        "cost_key": "production_cost",
        "cost_percentile_max": 20,           # 成本分位≤20%
        "cost_position_strong": "low",        # 定性成本判定=low
    },
}

# 题材股双重共振判定阈值
CONCEPT_DUAL_RESONANCE = {
    "industry_landing": {  # 条件A: 产业已落地量产
        "label": "产业已进入量产/商业化阶段",
        "purity_min": 80,             # 正宗贴合度≥80%
        "policy_score_min": 70,       # 政策强度≥70(已出实质性政策)
        "remaining_catalysts_max": 60,  # 预期差≤60(已确定性兑现)
    },
    "long_term_policy": {  # 条件B: 长期政策扶持(≥3年)
        "label": "长期政策持续扶持",
        "policy_score_min": 70,
        "remaining_catalysts_min": 50,  # 仍有后续催化空间
    },
}

# 修正系数
RESONANCE_MULTIPLIERS = {
    "strong": 0.60,   # 双重共振 → ×0.6，下调等级
    "weak": 0.85,     # 弱共振 → ×0.85，降低风险分但不降级
    "none": 1.0,      # 无共振 → ×1.0
}

# 等级映射（三级反转降级）
TIER_DOWNGRADE = {
    "RED": "YELLOW",
    "YELLOW": "GREEN",
    "GREEN": "GREEN",  # 绿灯不降级
}


# ===================== 共振检测函数 =====================

def check_resource_dual_resonance(stock_data: dict) -> dict:
    """检测周期股双重共振条件。

    返回: {"condition_a": bool, "condition_b": bool, "detail": str}
    """
    conditions = RESOURCE_DUAL_RESONANCE
    detail_parts = []

    # 条件A: 商品价格低位
    price_pct = stock_data.get("commodity_price_percentile", 100)
    cond_a = price_pct <= conditions["low_price"]["commodity_price_percentile_max"]
    a_label = conditions["low_price"]["label"]
    detail_parts.append(f"A({a_label}): 分位={price_pct}%→{'✅满足' if cond_a else '❌不满足'}")

    # 条件B: 极致成本优势
    cost_pct = stock_data.get("production_cost",
                              stock_data.get("cost_percentile", 100))
    cost_pos = stock_data.get("cost_position", "mid")
    cond_b = (cost_pct <= conditions["extreme_cost"]["cost_percentile_max"]
              and cost_pos == conditions["extreme_cost"]["cost_position_strong"])
    b_label = conditions["extreme_cost"]["label"]
    detail_parts.append(f"B({b_label}): 成本分位={cost_pct}%/{cost_pos}→{'✅满足' if cond_b else '❌不满足'}")

    return {
        "condition_a": cond_a,
        "condition_b": cond_b,
        "detail": " | ".join(detail_parts),
    }


def check_concept_dual_resonance(stock_data: dict) -> dict:
    """检测题材股双重共振条件。

    返回: {"condition_a": bool, "condition_b": bool, "detail": str}
    """
    conditions = CONCEPT_DUAL_RESONANCE
    detail_parts = []

    # 条件A: 产业已落地量产
    purity = stock_data.get("concept_purity", 0)
    policy = stock_data.get("policy_score", 0)
    remaining = stock_data.get("remaining_catalysts", 50)
    cond_a = (purity >= conditions["industry_landing"]["purity_min"]
              and policy >= conditions["industry_landing"]["policy_score_min"]
              and remaining <= conditions["industry_landing"]["remaining_catalysts_max"])
    a_label = conditions["industry_landing"]["label"]
    detail_parts.append(
        f"A({a_label}): 纯度={purity}%政策={policy}预期差={remaining}"
        f"→{'✅满足' if cond_a else '❌不满足'}"
    )

    # 条件B: 长期政策扶持
    cond_b = (policy >= conditions["long_term_policy"]["policy_score_min"]
              and remaining >= conditions["long_term_policy"]["remaining_catalysts_min"])
    b_label = conditions["long_term_policy"]["label"]
    detail_parts.append(
        f"B({b_label}): 政策={policy}预期差={remaining}"
        f"→{'✅满足' if cond_b else '❌不满足'}"
    )

    return {
        "condition_a": cond_a,
        "condition_b": cond_b,
        "detail": " | ".join(detail_parts),
    }


def determine_resonance_level(cond_a: bool, cond_b: bool) -> str:
    """判定共振强度等级。"""
    if cond_a and cond_b:
        return "strong"   # 双条件都满足 → 强共振
    if cond_a or cond_b:
        return "weak"     # 至少一个满足 → 弱共振
    return "none"         # 都不满足 → 无共振


# ===================== 主入口 =====================

class Rule015ReverseChecker:
    """L3 Rule015 双重共振兜底反转校验层。"""

    def __init__(self):
        self.logger = logging.getLogger("L3-REV")

    def check(
        self,
        stock_type: str,           # "resource" / "concept" / "bluechip"
        stock_data: dict,
        final_risk_score: float,   # L2 输出的总误判得分
        risk_tier: str,            # "GREEN" / "YELLOW" / "RED"
    ) -> dict:
        """执行 L3 双重共振反转校验。

        参数:
            stock_type: 标的类型 (rule021_dual_branch的classify结果)
            stock_data: 包含所有维度的原始数据
            final_risk_score: L2输出的总误判得分
            risk_tier: 当前风控等级

        返回:
            {
                "resonance_level": str,       # "strong" / "weak" / "none"
                "resonance_detail": str,
                "original_score": float,
                "adjusted_score": float,
                "multiplier": float,
                "original_tier": str,
                "adjusted_tier": str,          # 反转后的等级
                "is_downgraded": bool,
                "tier_downgrade_applied": bool,  # 是否执行了等级降级
            }
        """
        self.logger.info(f"  L3校验: type={stock_type}, score={final_risk_score:.1f}, tier={risk_tier}")

        # 蓝筹不做反转（基本面已稳定）
        if stock_type == "bluechip":
            self.logger.info("  ⏭️  蓝筹标的跳过反转校验")
            return {
                "resonance_level": "none",
                "resonance_detail": "蓝筹标的跳过反转校验",
                "original_score": final_risk_score,
                "adjusted_score": final_risk_score,
                "multiplier": 1.0,
                "original_tier": risk_tier,
                "adjusted_tier": risk_tier,
                "is_downgraded": False,
                "tier_downgrade_applied": False,
            }

        # 检测共振
        if stock_type == "resource":
            resonance = check_resource_dual_resonance(stock_data)
        else:
            resonance = check_concept_dual_resonance(stock_data)

        resonance_level = determine_resonance_level(
            resonance["condition_a"], resonance["condition_b"]
        )
        multiplier = RESONANCE_MULTIPLIERS[resonance_level]

        # 计算修正后分值
        adjusted_score = round(final_risk_score * multiplier, 1)

        # 等级降级判定
        tier_downgrade_applied = False
        if resonance_level == "strong" and risk_tier != "GREEN":
            adjusted_tier = TIER_DOWNGRADE.get(risk_tier, risk_tier)
            tier_downgrade_applied = (adjusted_tier != risk_tier)
        else:
            adjusted_tier = risk_tier

        is_downgraded = (multiplier < 1.0) or tier_downgrade_applied

        level_label = {"strong": "强共振", "weak": "弱共振", "none": "无共振"}
        self.logger.info(
            f"  {level_label[resonance_level]}, multiplier={multiplier}, "
            f"score={final_risk_score:.1f}→{adjusted_score:.1f}, "
            f"tier={risk_tier}→{adjusted_tier}"
        )

        return {
            "resonance_level": resonance_level,
            "resonance_detail": resonance["detail"],
            "condition_a": resonance["condition_a"],
            "condition_b": resonance["condition_b"],
            "original_score": final_risk_score,
            "adjusted_score": adjusted_score,
            "multiplier": multiplier,
            "original_tier": risk_tier,
            "adjusted_tier": adjusted_tier,
            "is_downgraded": is_downgraded,
            "tier_downgrade_applied": tier_downgrade_applied,
        }


# ===================== 快捷入口 =====================

def run_l3_reverse(
    stock_type: str,
    stock_data: dict,
    final_risk_score: float,
    risk_tier: str,
) -> dict:
    """一键执行 L3 双重共振反转校验。"""
    checker = Rule015ReverseChecker()
    return checker.check(
        stock_type=stock_type,
        stock_data=stock_data,
        final_risk_score=final_risk_score,
        risk_tier=risk_tier,
    )


# ===================== 自测 =====================

if __name__ == "__main__":
    print("=" * 60)
    print("  L3 Rule015 双重共振兜底反转校验 自测")
    print("=" * 60)

    ck = Rule015ReverseChecker()

    # 测试1: 周期股强共振（低价+极致成本）
    print("\n--- 测试1: 周期资源股 强共振 ---")
    r1 = ck.check(
        stock_type="resource",
        stock_data={"commodity_price_percentile": 25,
                     "production_cost": 15, "cost_position": "low"},
        final_risk_score=65.0,
        risk_tier="YELLOW",
    )
    assert r1["resonance_level"] == "strong", f"预期strong: {r1['resonance_level']}"
    assert r1["multiplier"] == 0.6, f"预期0.6: {r1['multiplier']}"
    assert r1["adjusted_score"] == 39.0, f"预期39.0: {r1['adjusted_score']}"
    assert r1["adjusted_tier"] == "GREEN", f"预期GREEN(降级): {r1['adjusted_tier']}"
    assert r1["tier_downgrade_applied"], "预期等级降级"
    print(f"  {r1['resonance_detail']}")
    print(f"  等级={r1['resonance_level']} | ×{r1['multiplier']} | "
          f"{r1['original_score']}→{r1['adjusted_score']} | "
          f"{r1['original_tier']}→{r1['adjusted_tier']} ✅")

    # 测试2: 题材股强共振（落地+政策）
    print("\n--- 测试2: 题材股 强共振 ---")
    r2 = ck.check(
        stock_type="concept",
        stock_data={"concept_purity": 90, "policy_score": 85,
                     "remaining_catalysts": 55},
        final_risk_score=72.0,
        risk_tier="RED",
    )
    assert r2["resonance_level"] == "strong", f"预期strong: {r2['resonance_level']}"
    assert r2["adjusted_score"] == 43.2, f"预期43.2: {r2['adjusted_score']}"
    assert r2["adjusted_tier"] == "YELLOW", f"预期YELLOW(降级): {r2['adjusted_tier']}"
    print(f"  {r2['resonance_detail']}")
    print(f"  ×{r2['multiplier']} | {r2['original_score']}→{r2['adjusted_score']} | "
          f"{r2['original_tier']}→{r2['adjusted_tier']} ✅")

    # 测试3: 周期股弱共振（仅低价，无成本优势）
    print("\n--- 测试3: 周期股 弱共振(仅A) ---")
    r3 = ck.check(
        stock_type="resource",
        stock_data={"commodity_price_percentile": 25,
                     "production_cost": 50, "cost_position": "mid"},
        final_risk_score=75.0,
        risk_tier="RED",
    )
    assert r3["resonance_level"] == "weak", f"预期weak: {r3['resonance_level']}"
    assert r3["multiplier"] == 0.85, f"预期0.85: {r3['multiplier']}"
    assert abs(r3["adjusted_score"] - 63.75) < 0.1, f"预期≈63.75: {r3['adjusted_score']}"
    assert r3["adjusted_tier"] == "RED", f"弱共振不降级: {r3['adjusted_tier']}"
    assert not r3["tier_downgrade_applied"], "弱共振不应降级"
    print(f"  {r3['resonance_detail']}")
    print(f"  ×{r3['multiplier']} | {r3['original_score']}→{r3['adjusted_score']} | "
          f"等级不变={r3['adjusted_tier']} ✅")

    # 测试4: 无共振
    print("\n--- 测试4: 无共振 ---")
    r4 = ck.check(
        stock_type="resource",
        stock_data={"commodity_price_percentile": 75,
                     "production_cost": 50, "cost_position": "mid"},
        final_risk_score=82.0,
        risk_tier="RED",
    )
    assert r4["resonance_level"] == "none", f"预期none: {r4['resonance_level']}"
    assert r4["multiplier"] == 1.0, f"预期1.0: {r4['multiplier']}"
    assert r4["adjusted_score"] == 82.0, f"不变: {r4['adjusted_score']}"
    assert r4["adjusted_tier"] == "RED", f"不变: {r4['adjusted_tier']}"
    print(f"  {r4['resonance_detail']}")
    print(f"  ×{r4['multiplier']} | 不变 ✅")

    # 测试5: 蓝筹跳过
    print("\n--- 测试5: 蓝筹跳过 ---")
    r5 = ck.check(
        stock_type="bluechip",
        stock_data={},
        final_risk_score=85.0,
        risk_tier="YELLOW",
    )
    assert r5["resonance_level"] == "none"
    assert r5["multiplier"] == 1.0
    assert r5["adjusted_score"] == 85.0
    print(f"  蓝筹跳过→不变: {r5['adjusted_score']}, tier={r5['adjusted_tier']} ✅")

    # 测试6: 题材弱共振（仅B）
    print("\n--- 测试6: 题材弱共振(仅B政策) ---")
    r6 = ck.check(
        stock_type="concept",
        stock_data={"concept_purity": 50, "policy_score": 85,
                     "remaining_catalysts": 65},
        final_risk_score=68.0,
        risk_tier="YELLOW",
    )
    assert r6["resonance_level"] == "weak", f"预期weak: {r6['resonance_level']}"
    assert r6["multiplier"] == 0.85
    assert r6["adjusted_tier"] == "YELLOW", "弱共振不降级"
    print(f"  {r6['resonance_detail']}")
    print(f"  ×0.85→{r6['adjusted_score']}, 等级不变={r6['adjusted_tier']} ✅")

    print(f"\n{'='*60}")
    print("✅ L3全部测试通过")
    print(f"{'='*60}")
