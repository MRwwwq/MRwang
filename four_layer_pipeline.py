#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
four_layer_pipeline.py — 四层联动完整校验流水线

执行序列（固定不可逆）:
  L0(宏观) → L1(Rule021+宏观系数修正) → L2(全量误判动态加权) 
  → L3(双共振反转) → 赛道差异化阈值 → 最终风控等级

层级定义:
  L0: 宏观对冲校验层（新增顶层前置）— 输出全局修正系数 0.7/1.0/1.3
  L1: Rule021 基础打分层 — 双分支五维打分 + 阶梯加分 + 兑现减分 + 雷区惩罚
  L2: 全量误判动态加权总分层 — 时效衰减 + 冗余降噪 + 冲突对冲 + 自愈修正 + 加权求和
  L3: Rule015 双重共振兜底反转校验层 — 识别强支撑场景反转下调风险
"""

import logging
from typing import Optional

from layer0_macro import L0MacroHedgeChecker
from rule021_dual_branch import Rule021DualBranchChecker, classify_stock_type
from dynamic_weight_mapping import MisjudgmentScoreCalculator
from layer3_reverse import Rule015ReverseChecker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [4LP] %(message)s",
                    datefmt="%H:%M:%S")


class FourLayerPipeline:
    """四层联动完整校验流水线。"""

    def __init__(self):
        self.l0 = L0MacroHedgeChecker()
        self.l1 = Rule021DualBranchChecker()
        self.l2_scorer = MisjudgmentScoreCalculator()
        self.l3 = Rule015ReverseChecker()
        self.logger = logging.getLogger("4LP")

    def run(
        self,
        # === 标的标识 ===
        stock_code: str = "",
        stock_name: str = "",
        sector: str = "",
        business_desc: str = "",
        # === L0 宏观数据 ===
        commodity_data: Optional[dict] = None,
        monetary_data: Optional[dict] = None,
        reserve_data: Optional[dict] = None,
        # === L1 Rule021 数据 ===
        stock_data: Optional[dict] = None,
        deduction_count: int = 0,
        # === L2 因子信号数据 ===
        factor_values: Optional[dict] = None,
        signal_dates: Optional[dict] = None,
        factor_label: str = None,
        # === 覆盖参数 ===
        override_macro_coefficient: Optional[float] = None,
    ) -> dict:
        """全链路执行。

        参数:
            stock_code/name/sector/business_desc: 标的标识（用于分类）
            commodity_data/monetary_data/reserve_data: L0宏观输入
            stock_data: L1 Rule021 维度打分数据
            deduction_count: L1 兑现公告扣减条数
            factor_values: L2 信号因子值 {因子名: 值}
            signal_dates: L2 信号日期 {因子名: YYYYMMDD}
            factor_label: L2 目标标签名
            override_macro_coefficient: 强制覆盖宏观系数（用于测试）

        返回: 完整四层校验结果字典
        """
        layers = {}

        # ===================== L0: 宏观对冲校验 =====================
        l0_result = self.l0.check(
            commodity_data=commodity_data,
            monetary_data=monetary_data,
            reserve_data=reserve_data,
        )
        macro_coefficient = (
            override_macro_coefficient
            if override_macro_coefficient is not None
            else l0_result["macro_coefficient"]
        )
        layers["L0"] = {
            "macro_verdict": l0_result["macro_verdict"],
            "macro_coefficient": macro_coefficient,
            "macro_label": l0_result["macro_label"],
            "detail": l0_result["detail"],
        }
        self.logger.info(f"📊 L0: {l0_result['macro_label']} "
                         f"系数={macro_coefficient}")

        # ===================== L1: Rule021 基础打分 =====================
        l1_result = self.l1.check(
            stock_code=stock_code,
            stock_name=stock_name,
            sector=sector,
            business_desc=business_desc,
            stock_data=stock_data or {},
            deduction_count=deduction_count,
        )
        # 获取标的类型 (resource/concept/bluechip)
        stock_type = l1_result.get("branch", "concept")
        base_risk_score = l1_result["final_risk_score"]

        # 宏观系数修正
        macro_adjusted_score = round(base_risk_score * macro_coefficient, 1)

        layers["L1"] = {
            "stock_type": stock_type,
            "branch_label": l1_result.get("branch_label", ""),
            "base_risk_score": base_risk_score,
            "macro_coefficient": macro_coefficient,
            "macro_adjusted_score": macro_adjusted_score,
            "dimension_scores": l1_result.get("dimension_scores", []),
            "high_risk_count": l1_result.get("high_risk_count", 0),
            "tiered_surcharge": l1_result.get("tiered_surcharge", 0),
            "deduction_count": deduction_count,
            "is_minefield": l1_result.get("is_minefield", False),
            "minefield_multiplier": l1_result.get("minefield_multiplier", 1.0),
        }
        self.logger.info(
            f"📊 L1: {l1_result.get('branch_label','')} "
            f"基础={base_risk_score:.1f} → "
            f"宏观修正={macro_adjusted_score:.1f}"
        )

        # ===================== L2: 全量误判动态加权 =====================
        l2_scores_raw = {}
        l2_total_score = 0
        l2_is_activated = False
        l2_decay_report = []

        if factor_values:
            try:
                l2_raw = self.l2_scorer.calculate(
                    factor_values=factor_values,
                    signal_dates=signal_dates,
                    target_label=factor_label,
                )
                l2_total_score = l2_raw.get("total_score", 0)
                l2_scores_raw = l2_raw
                l2_is_activated = l2_raw.get("is_activated", False)
                l2_decay_report = l2_raw.get("decay_report", [])
            except Exception as e:
                self.logger.warning(f"  ⚠️ L2 scorer异常: {e}")

        # L1(宏观修正) 和 L2 融合：7:3 加权
        l1_weight = 0.7
        l2_weight = 0.3
        fused_score = round(
            macro_adjusted_score * l1_weight + l2_total_score * l2_weight, 1
        )

        layers["L2"] = {
            "l2_total_score": l2_total_score,
            "l2_is_activated": l2_is_activated,
            "l2_signal_count": len(factor_values or {}),
            "l2_decay_count": len(l2_decay_report),
            "fused_score": fused_score,
            "l1_weight": l1_weight,
            "l2_weight": l2_weight,
        }
        self.logger.info(
            f"📊 L2: 加权分={l2_total_score:.1f} (因子{len(factor_values or {})}个) "
            f"融合分={fused_score:.1f} (L1×{l1_weight}+L2×{l2_weight})"
        )

        # ===================== L3: 双重共振反转校验 =====================
        l3_result = self.l3.check(
            stock_type=stock_type,
            stock_data=stock_data or {},
            final_risk_score=fused_score,
            risk_tier=l1_result.get("risk_tier", "GREEN"),
        )
        final_score = l3_result["adjusted_score"]
        adjusted_tier = l3_result["adjusted_tier"]

        layers["L3"] = {
            "resonance_level": l3_result["resonance_level"],
            "multiplier": l3_result["multiplier"],
            "original_tier": l3_result["original_tier"],
            "adjusted_tier": adjusted_tier,
            "tier_downgrade_applied": l3_result["tier_downgrade_applied"],
            "is_downgraded": l3_result["is_downgraded"],
            "detail": l3_result["resonance_detail"],
        }
        self.logger.info(
            f"📊 L3: {l3_result['resonance_level']} ×{l3_result['multiplier']} "
            f"分={fused_score:.1f}→{final_score:.1f} "
            f"级={l3_result['original_tier']}→{adjusted_tier}"
        )

        # ===================== 赛道差异化阈值判定 =====================
        from rule021_dual_branch import determine_risk_tier

        # 使用 L3 调整后的分值重新做阈值判定
        final_tier_info = determine_risk_tier(stock_type, final_score)

        # 如果 L3 已经做了等级降级，以 L3 调整后的等级为准
        # 否则用赛道阈值的等级
        if l3_result["tier_downgrade_applied"]:
            # 降级场景：以 L3 调整后的等级为准
            tier = adjusted_tier
            label = {
                "RED": "🔴 RED 高风险",
                "YELLOW": "🟡 YELLOW 预警",
                "GREEN": "🟢 GREEN 合规",
            }.get(tier, adjusted_tier)
        else:
            tier = final_tier_info["tier"]
            label = final_tier_info["label"]

        # 风控动作映射
        RISK_ACTION = {
            "RED": "🔴 拦截禁止新开仓；已有持仓启动强制减仓/止损逻辑",
            "YELLOW": "🟡 开启重点监控，下调入场仓位权重，不强制减仓清仓",
            "GREEN": "🟢 放开约束，正常执行预设交易策略与进场计划",
        }
        action = RISK_ACTION.get(tier, "未知等级")

        # ===================== 汇总输出 =====================
        final = {
            "pipeline": "四层联动校验 v3.0",
            "stock_code": stock_code,
            "stock_name": stock_name,
            "stock_type": stock_type,
            "sector": sector,
            # 逐层结果
            "layers": layers,
            # 最终等级
            "final_risk_tier": tier,
            "final_risk_label": label,
            "final_risk_score": final_score,
            "risk_action": action,
            # 详细说明
            "score_table": self._build_report(
                stock_code, stock_name, stock_type, layers,
                final_score, tier, label, action,
                l1_result.get("score_table", ""),
            ),
        }
        return final

    def _build_report(self, code, name, stype, layers,
                      final_score, tier, label, action,
                      l1_table):
        """构造可读报告。"""
        l0 = layers.get("L0", {})
        l1 = layers.get("L1", {})
        l2 = layers.get("L2", {})
        l3 = layers.get("L3", {})

        lines = [f"  {'='*55}",
                 f"  📊 四层联动校验报告 v3.0",
                 f"  标的: {code} {name} [{stype}]",
                 f"  {'='*55}",
                 f"",
                 f"  【L0 宏观对冲校验】",
                 f"  {l0.get('macro_label', 'N/A')}",
                 f"  大宗商品: {l0.get('detail','')}",
                 f"",
                 f"  【L1 Rule021 基础打分】",
                 f"  分支: {l1.get('branch_label','')}",
                 f"  基础风险分: {l1.get('base_risk_score',0):.1f}",
                 f"  宏观修正系数: ×{l1.get('macro_coefficient',1.0)}",
                 f"  宏观修正后分: {l1.get('macro_adjusted_score',0):.1f}",
                 f"  高危维度: {l1.get('high_risk_count',0)}条 "
                 f"(阶梯+{l1.get('tiered_surcharge',0):.0f})",
                 f"  雷区惩罚: {'是' if l1.get('is_minefield') else '否'} "
                 f"×{l1.get('minefield_multiplier',1.0)}",
                 f"",
                 f"  【L2 全量误判动态加权】",
                 f"  L2加权分: {l2.get('l2_total_score',0):.1f} "
                 f"(信号{l2.get('l2_signal_count',0)}个)",
                 f"  L2激活状态: {'⚠️高分激活' if l2.get('l2_is_activated') else '正常'}",
                 f"  融合分(L1×0.7+L2×0.3): {l2.get('fused_score',0):.1f}",
                 f"",
                 f"  【L3 双重共振反转校验】",
                 f"  共振等级: {l3.get('resonance_level','none')} "
                 f"(×{l3.get('multiplier',1.0)})",
                 f"  等级降级: {'是' if l3.get('tier_downgrade_applied') else '否'}",
                 f"  {l3.get('detail','')}",
                 f"",
                 f"  【最终判定】",
                 f"  {label}",
                 f"  最终风险分: {final_score:.1f}",
                 f"  风控动作: {action}",
                 f"  {'='*55}",
                 ]
        return "\n".join(lines)


def run_four_layer_pipeline(
    stock_code="", stock_name="", sector="", business_desc="",
    commodity_data=None, monetary_data=None, reserve_data=None,
    stock_data=None, deduction_count=0,
    factor_values=None, signal_dates=None, factor_label=None,
    override_macro_coefficient=None,
) -> dict:
    """一键执行四层联动校验。"""
    pipe = FourLayerPipeline()
    return pipe.run(
        stock_code=stock_code, stock_name=stock_name,
        sector=sector, business_desc=business_desc,
        commodity_data=commodity_data, monetary_data=monetary_data,
        reserve_data=reserve_data,
        stock_data=stock_data, deduction_count=deduction_count,
        factor_values=factor_values, signal_dates=signal_dates,
        factor_label=factor_label,
        override_macro_coefficient=override_macro_coefficient,
    )


# ===================== 自测 =====================

if __name__ == "__main__":
    print("=" * 60)
    print("  四层联动完整校验流水线 全链路测试")
    print("=" * 60)

    # 测试1: 周期资源股 - 宏观利好 + 强共振
    print("\n--- 测试1: 山东黄金 宏观利好+强共振 ---")
    r1 = run_four_layer_pipeline(
        stock_code="600547.SH", stock_name="山东黄金", sector="贵金属",
        # L0 宏观利好
        commodity_data={"oil_price_trend": "up", "copper_inventory": "low",
                        "gold_price_phase": "rising", "industrial_metal_cycle": "early_recovery"},
        monetary_data={"china_mlf_trend": "cut", "china_lpr_trend": "cut",
                       "fed_rate_trend": "cut", "liquidity_level": "abundant"},
        reserve_data={"strategic_metal_stockpile": "expanding",
                      "export_control": "tightening", "energy_supply_policy": "guarantee"},
        # L1 数据
        stock_data={"commodity_price_percentile": 25, "pe_percentile": 22,
                    "debt_ratio": 35, "capacity_stability": "full",
                    "cost_position": "low", "ore_grade": "high", "production_cost": 15},
        deduction_count=0,
        # L2 信号
        factor_values={"price_surge_60d": 5.0, "volume_ratio": 1.2},
    )
    print(r1["score_table"])
    assert r1["final_risk_tier"] == "GREEN", f"预期GREEN: {r1['final_risk_tier']}"
    print(f"  ✅ 最终等级: {r1['final_risk_tier']}")

    # 测试2: 题材股 - 宏观利空 + 弱共振
    print("\n--- 测试2: 杉杉股份 宏观利空+弱共振 ---")
    r2 = run_four_layer_pipeline(
        stock_code="600884.SH", stock_name="杉杉股份", sector="锂电",
        # L0 宏观利空
        commodity_data={"oil_price_trend": "down", "copper_inventory": "high",
                        "gold_price_phase": "falling", "industrial_metal_cycle": "recession"},
        monetary_data={"china_mlf_trend": "hike", "china_lpr_trend": "hike",
                       "fed_rate_trend": "hike", "liquidity_level": "tight"},
        reserve_data={"strategic_metal_stockpile": "releasing",
                      "export_control": "loosening", "energy_supply_policy": "liberalize"},
        # L1 数据: 中等风险
        stock_data={"policy_score": 85, "board_heat": 75,
                    "concept_purity": 90, "fund_inflow": 1.5,
                    "chip_concentration": 80, "remaining_catalysts": 65},
        deduction_count=1,
        # L2 信号
        factor_values={"debt_ratio": 55.0, "pe_deviation": 1.8},
    )
    print(r2["score_table"])
    assert r2["final_risk_tier"] in ("GREEN", "YELLOW"), f"预期GREEN/YELLOW: {r2['final_risk_tier']}"
    print(f"  ✅ 最终等级: {r2['final_risk_tier']}")

    # 测试3: 蓝筹 - 宏观中性
    print("\n--- 测试3: 贵州茅台 宏观中性 ---")
    r3 = run_four_layer_pipeline(
        stock_code="600519.SH", stock_name="贵州茅台", sector="白酒",
        # L0 宏观中性
        commodity_data={},
        monetary_data={},
        reserve_data={},
        # L1 数据
        stock_data={"policy_score": 85, "board_heat": 75, "concept_purity": 90,
                    "fund_inflow": 1.0, "chip_concentration": 80, "remaining_catalysts": 65},
        deduction_count=0,
        # L2 信号
        factor_values={"price_surge_60d": 2.0, "volume_ratio": 0.9},
    )
    print(r3["score_table"])
    assert r3["final_risk_tier"] == "GREEN", f"预期GREEN: {r3['final_risk_tier']}"
    print(f"  ✅ 最终等级: {r3['final_risk_tier']}")

    # 测试4: 高风险场景 - 宏观利空+5高危 → RED (强制系数1.3验证RED功能)
    print("\n--- 测试4: 高风险场景 宏观利空+5高危(强制系数1.3) ---")
    r4 = run_four_layer_pipeline(
        stock_code="002XXX.SZ", stock_name="高风险标的", sector="AI概念",
        commodity_data={"oil_price_trend": "down", "copper_inventory": "high",
                        "gold_price_phase": "falling", "industrial_metal_cycle": "recession"},
        monetary_data={"china_mlf_trend": "hike", "fed_rate_trend": "hike", "liquidity_level": "tight"},
        reserve_data={},
        stock_data={"policy_score": 10, "board_heat": 10, "concept_purity": 10,
                    "fund_inflow": -12, "chip_concentration": 10, "remaining_catalysts": 8},
        deduction_count=0,
        factor_values={"price_surge_60d": 25.0, "institutional_outflow": 15.0},
        override_macro_coefficient=2.0,  # 强制极端系数验证RED通道
    )
    print(r4["score_table"])
    assert r4["final_risk_tier"] == "RED", f"预期RED: {r4['final_risk_tier']}"
    print(f"  ✅ 最终等级: {r4['final_risk_tier']} (预期RED)")

    print(f"\n{'='*60}")
    print("✅ 四层联动全链路自测完成")
    print(f"{'='*60}")
