#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
layer0_macro.py — L0 宏观对冲校验层（新增顶层前置校验）

职能：判断宏观环境方向，输出全局修正系数作用于 L1 基础分。
      正向宏观 → ×0.7（下调全体系风险）
      中性宏观 → ×1.0（不变）
      利空宏观 → ×1.3（放大系统性下行风险）

时序约束：L0 为全链路最顶层前置模块，先完成宏观系数修正，
          再流入 L1 层计算，宏观修正作用于整套打分体系。
"""

import logging
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [L0-MACRO] %(message)s",
                    datefmt="%H:%M:%S")

# ===================== 宏观因子档位定义 =====================

# 1. 大宗商品价格宏观
COMMODITY_TIERS = {
    "bullish": {  # 库存低位 + 价格中枢上行 → 利好
        "label": "大宗商品价格上行",
        "desc": "原油/铜/黄金等工业金属库存低位,价格中枢上行",
    },
    "neutral": {
        "label": "大宗商品价格温和",
        "desc": "商品价格区间震荡,供需基本平衡",
    },
    "bearish": {  # 价格持续下行 + 库存高企 → 利空
        "label": "大宗商品价格下行",
        "desc": "工业金属价格持续下行,库存高企,全球需求疲软",
    },
}

# 2. 货币政策宏观
MONETARY_TIERS = {
    "bullish": {
        "label": "货币政策宽松",
        "desc": "MLF/LPR降息,美联储降息周期,市场流动性充裕",
    },
    "neutral": {
        "label": "货币政策中性",
        "desc": "利率维持不变,流动性稳定均衡",
    },
    "bearish": {
        "label": "货币政策紧缩",
        "desc": "MLF/LPR加息,美联储加息,流动性收紧",
    },
}

# 3. 储备政策宏观
RESERVE_TIERS = {
    "bullish": {
        "label": "储备收储利好",
        "desc": "战略金属收储扩量,矿产出口管控,能源保供政策",
    },
    "neutral": {
        "label": "储备政策中性",
        "desc": "储备政策稳定,无重大变化",
    },
    "bearish": {
        "label": "储备投放利空",
        "desc": "国家储备投放市场,出口管制放松,供给释放",
    },
}

# 宏观环境 → 全局修正系数
MACRO_COEFFICIENT_MAP = {
    "bullish": 0.7,   # 正向利好 → 全体系风险×0.7
    "neutral": 1.0,   # 中性均衡 → 不变
    "bearish": 1.3,   # 利空压制 → 全体系风险×1.3
}


# ===================== 宏观因子判定函数 =====================

def assess_commodity(commodity_data: dict) -> str:
    """评估大宗商品宏观方向。

    参数:
        commodity_data: {
            "oil_price_trend": str,    # "up" / "stable" / "down"
            "copper_inventory": str,   # "low" / "normal" / "high"
            "gold_price_phase": str,   # "rising" / "range" / "falling"
            "industrial_metal_cycle": str,  # "early_recovery" / "mid" / "late_overheat" / "recession"
        }
    """
    if not commodity_data:
        return "neutral"

    scores = {"bullish": 0, "bearish": 0}

    # 油价趋势
    oil = commodity_data.get("oil_price_trend", "stable")
    if oil == "up":
        scores["bullish"] += 1
    elif oil == "down":
        scores["bearish"] += 1

    # 铜库存
    copper = commodity_data.get("copper_inventory", "normal")
    if copper == "low":
        scores["bullish"] += 1
    elif copper == "high":
        scores["bearish"] += 1

    # 金价阶段
    gold = commodity_data.get("gold_price_phase", "range")
    if gold == "rising":
        scores["bullish"] += 1
    elif gold == "falling":
        scores["bearish"] += 1

    # 工业金属周期
    metal = commodity_data.get("industrial_metal_cycle", "mid")
    if metal in ("early_recovery",):
        scores["bullish"] += 2  # 复苏期权重更高
    elif metal in ("recession",):
        scores["bearish"] += 2

    if scores["bullish"] > scores["bearish"]:
        return "bullish"
    elif scores["bearish"] > scores["bullish"]:
        return "bearish"
    return "neutral"


def assess_monetary(monetary_data: dict) -> str:
    """评估货币政策宏观方向。

    参数:
        monetary_data: {
            "china_mlf_trend": str,    # "cut" / "stable" / "hike"
            "china_lpr_trend": str,    # "cut" / "stable" / "hike"
            "fed_rate_trend": str,     # "cut" / "stable" / "hike"
            "liquidity_level": str,    # "abundant" / "neutral" / "tight"
        }
    """
    if not monetary_data:
        return "neutral"

    scores = {"bullish": 0, "bearish": 0}

    # MLF
    mlf = monetary_data.get("china_mlf_trend", "stable")
    if mlf == "cut":
        scores["bullish"] += 2  # MLF降息权重高
    elif mlf == "hike":
        scores["bearish"] += 2

    # LPR
    lpr = monetary_data.get("china_lpr_trend", "stable")
    if lpr == "cut":
        scores["bullish"] += 1
    elif lpr == "hike":
        scores["bearish"] += 1

    # 美联储
    fed = monetary_data.get("fed_rate_trend", "stable")
    if fed == "cut":
        scores["bullish"] += 1
    elif fed == "hike":
        scores["bearish"] += 1

    # 流动性
    liquidity = monetary_data.get("liquidity_level", "neutral")
    if liquidity == "abundant":
        scores["bullish"] += 2
    elif liquidity == "tight":
        scores["bearish"] += 2

    if scores["bullish"] > scores["bearish"]:
        return "bullish"
    elif scores["bearish"] > scores["bullish"]:
        return "bearish"
    return "neutral"


def assess_reserve(reserve_data: dict) -> str:
    """评估储备政策宏观方向。

    参数:
        reserve_data: {
            "strategic_metal_stockpile": str,  # "expanding" / "stable" / "releasing"
            "export_control": str,              # "tightening" / "stable" / "loosening"
            "energy_supply_policy": str,        # "guarantee" / "neutral" / "liberalize"
        }
    """
    if not reserve_data:
        return "neutral"

    scores = {"bullish": 0, "bearish": 0}

    stockpile = reserve_data.get("strategic_metal_stockpile", "stable")
    if stockpile == "expanding":
        scores["bullish"] += 2
    elif stockpile == "releasing":
        scores["bearish"] += 2

    export = reserve_data.get("export_control", "stable")
    if export == "tightening":
        scores["bullish"] += 1
    elif export == "loosening":
        scores["bearish"] += 1

    energy = reserve_data.get("energy_supply_policy", "neutral")
    if energy == "guarantee":
        scores["bullish"] += 1
    elif energy == "liberalize":
        scores["bearish"] += 1

    if scores["bullish"] > scores["bearish"]:
        return "bullish"
    elif scores["bearish"] > scores["bullish"]:
        return "bearish"
    return "neutral"


def aggregate_macro_environment(
    commodity_verdict: str,
    monetary_verdict: str,
    reserve_verdict: str,
) -> str:
    """汇总三类宏观因子，输出综合宏观环境档位。

    加权: 大宗商品×0.4 + 货币政策×0.4 + 储备政策×0.2
    bullish=+1, neutral=0, bearish=-1
    """
    score_map = {"bullish": 1, "neutral": 0, "bearish": -1}
    weighted = (
        score_map.get(commodity_verdict, 0) * 0.4
        + score_map.get(monetary_verdict, 0) * 0.4
        + score_map.get(reserve_verdict, 0) * 0.2
    )
    if weighted >= 0.3:
        return "bullish"
    elif weighted <= -0.3:
        return "bearish"
    return "neutral"


# ===================== 主入口 =====================

class L0MacroHedgeChecker:
    """L0 宏观对冲校验层 — 顶层前置。"""

    def __init__(self):
        self.logger = logging.getLogger("L0-MACRO")

    def check(
        self,
        commodity_data: Optional[dict] = None,
        monetary_data: Optional[dict] = None,
        reserve_data: Optional[dict] = None,
    ) -> dict:
        """执行 L0 宏观对冲校验，输出全局修正系数。

        参数:
            commodity_data: 商品价格/库存宏观数据
            monetary_data:  货币/利率宏观数据
            reserve_data:   储备政策宏观数据

        返回:
            {
                "macro_verdict": str,          # "bullish" / "neutral" / "bearish"
                "macro_label": str,             # 中文标签
                "macro_coefficient": float,     # 0.7 / 1.0 / 1.3
                "detail": str,                  # 详细描述
                "commodity_verdict": str,
                "monetary_verdict": str,
                "reserve_verdict": str,
            }
        """
        commodity_verdict = assess_commodity(commodity_data or {})
        monetary_verdict = assess_monetary(monetary_data or {})
        reserve_verdict = assess_reserve(reserve_data or {})

        macro_verdict = aggregate_macro_environment(
            commodity_verdict, monetary_verdict, reserve_verdict
        )

        coefficient = MACRO_COEFFICIENT_MAP[macro_verdict]
        label = {
            "bullish": "🟢 宏观利好 (×0.7)",
            "neutral": "🟡 宏观中性 (×1.0)",
            "bearish": "🔴 宏观利空 (×1.3)",
        }[macro_verdict]

        detail_parts = []
        detail_parts.append(f"商品={COMMODITY_TIERS[commodity_verdict]['label']}")
        detail_parts.append(f"货币={MONETARY_TIERS[monetary_verdict]['label']}")
        detail_parts.append(f"储备={RESERVE_TIERS[reserve_verdict]['label']}")

        self.logger.info(f"  L0宏观判定: {label} | {' | '.join(detail_parts)}")

        return {
            "macro_verdict": macro_verdict,
            "macro_label": label,
            "macro_coefficient": coefficient,
            "commodity_verdict": commodity_verdict,
            "monetary_verdict": monetary_verdict,
            "reserve_verdict": reserve_verdict,
            "commodity_desc": COMMODITY_TIERS[commodity_verdict]["desc"],
            "monetary_desc": MONETARY_TIERS[monetary_verdict]["desc"],
            "reserve_desc": RESERVE_TIERS[reserve_verdict]["desc"],
            "detail": " | ".join(detail_parts),
        }


# ===================== 快捷入口 =====================

def run_l0_macro(
    commodity_data: Optional[dict] = None,
    monetary_data: Optional[dict] = None,
    reserve_data: Optional[dict] = None,
) -> dict:
    """一键执行 L0 宏观对冲校验。"""
    checker = L0MacroHedgeChecker()
    result = checker.check(
        commodity_data=commodity_data,
        monetary_data=monetary_data,
        reserve_data=reserve_data,
    )
    return result


# ===================== 自测 =====================

if __name__ == "__main__":
    print("=" * 60)
    print("  L0 宏观对冲校验层 自测")
    print("=" * 60)

    ck = L0MacroHedgeChecker()

    # 测试1: 宏观利好
    print("\n--- 测试1: 宏观利好(宽松+低库存+收储) ---")
    r1 = ck.check(
        commodity_data={"oil_price_trend": "up", "copper_inventory": "low",
                        "gold_price_phase": "rising", "industrial_metal_cycle": "early_recovery"},
        monetary_data={"china_mlf_trend": "cut", "china_lpr_trend": "cut",
                       "fed_rate_trend": "cut", "liquidity_level": "abundant"},
        reserve_data={"strategic_metal_stockpile": "expanding",
                      "export_control": "tightening", "energy_supply_policy": "guarantee"},
    )
    assert r1["macro_verdict"] == "bullish", f"预期bullish: {r1['macro_verdict']}"
    assert r1["macro_coefficient"] == 0.7, f"预期0.7: {r1['macro_coefficient']}"
    print(f"  {r1['macro_label']} | {r1['detail']}")
    print(f"  系数={r1['macro_coefficient']} ✅")

    # 测试2: 宏观利空
    print("\n--- 测试2: 宏观利空(紧缩+高库存+投放) ---")
    r2 = ck.check(
        commodity_data={"oil_price_trend": "down", "copper_inventory": "high",
                        "gold_price_phase": "falling", "industrial_metal_cycle": "recession"},
        monetary_data={"china_mlf_trend": "hike", "china_lpr_trend": "hike",
                       "fed_rate_trend": "hike", "liquidity_level": "tight"},
        reserve_data={"strategic_metal_stockpile": "releasing",
                      "export_control": "loosening", "energy_supply_policy": "liberalize"},
    )
    assert r2["macro_verdict"] == "bearish", f"预期bearish: {r2['macro_verdict']}"
    assert r2["macro_coefficient"] == 1.3, f"预期1.3: {r2['macro_coefficient']}"
    print(f"  {r2['macro_label']} | {r2['detail']}")
    print(f"  系数={r2['macro_coefficient']} ✅")

    # 测试3: 宏观中性 (所有因子全部取neutral/stable)
    print("\n--- 测试3: 宏观中性(全中性) ---")
    r3 = ck.check(
        commodity_data={"oil_price_trend": "stable", "copper_inventory": "normal",
                        "gold_price_phase": "range", "industrial_metal_cycle": "mid"},
        monetary_data={"china_mlf_trend": "stable", "china_lpr_trend": "stable",
                       "fed_rate_trend": "stable", "liquidity_level": "neutral"},
        reserve_data={"strategic_metal_stockpile": "stable",
                      "export_control": "stable", "energy_supply_policy": "neutral"},
    )
    assert r3["macro_verdict"] == "neutral", f"预期neutral: {r3['macro_verdict']}"
    assert r3["macro_coefficient"] == 1.0, f"预期1.0: {r3['macro_coefficient']}"
    print(f"  {r3['macro_label']} | {r3['detail']}")
    print(f"  系数={r3['macro_coefficient']} ✅")

    # 测试4: 边际利空 (commodity中性+货币紧缩+储备中性)
    print("\n--- 测试4: 边际利空(货币紧缩0.4→-0.3) ---")
    r4 = ck.check(
        commodity_data={},
        monetary_data={"china_mlf_trend": "hike", "china_lpr_trend": "hike",
                       "fed_rate_trend": "hike", "liquidity_level": "tight"},
        reserve_data={},
    )
    assert r4["macro_verdict"] == "bearish", f"预期bearish: {r4['macro_verdict']}"
    print(f"  {r4['macro_label']} | {r4['detail']}")
    print(f"  系数={r4['macro_coefficient']} ✅")

    print(f"\n{'='*60}")
    print("✅ L0全部测试通过")
    print(f"{'='*60}")
