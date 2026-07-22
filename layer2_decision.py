#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer2 风控决策层 — 分级输出最终交易指令

时序: Layer1 特征校验层之后
输入: Layer1 全部校验结果
输出: RED/YELLOW/GREEN 三级管控指令

基于 Layer1 全部校验结果，输出三类分级管控指令，同步下发给:
  - Module04 定策略 (修正可交易池/仓位)
  - Module05 买卖离场模块 (持仓处置)

分级标准:
  🔴 RED   红色高风险:
    心理误判≥23条 / 多维度利空共振 / Lollapalooza效应生效

  🟡 YELLOW 黄色预警:
    3 ≤ 心理误判数量 ≤22 / 信号局部矛盾 / 单一维度存在利空

  🟢 GREEN  绿色合规:
    心理误判≤2条 / 多市场信号共振走强 / 无明显情绪化偏差
"""

import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [L2] %(message)s", datefmt="%H:%M:%S")

# ====================== 风控等级完整定义 ======================

RISK_LEVELS = {
    "RED": {
        "label": "🔴 RED 红色高风险",
        "action": "拦截禁止新开仓；已有持仓启动强制减仓/止损逻辑",
        "new_open_allowed": False,
        "allow_add_position": False,       # 禁止加仓
        "total_cap_factor": 0.0,           # 新开仓总仓归零
        "existing_position_action": "强制减仓或执行止损",
        "high_elasticity_ban": True,        # 禁止高弹性标的
        "consecutive_board_ban": True,      # 禁止连板票
        "convertible_bond_ban": True,       # 禁止可转债
        "reduce_existing_pct": 0.5,         # 已有持仓减至50%
    },
    "YELLOW": {
        "label": "🟡 YELLOW 黄色预警",
        "action": "限制总仓位、禁止加仓、降低单笔投入规模",
        "new_open_allowed": True,
        "allow_add_position": False,       # 禁止追加仓位
        "total_cap_factor": 0.5,           # 总仓上限压缩50%
        "existing_position_action": "维持现状，不增仓",
        "high_elasticity_ban": True,        # 不允许高弹性标的重仓
        "consecutive_board_ban": True,      # 连板不重仓
        "convertible_bond_ban": True,       # 可转债不超过5%
        "reduce_existing_pct": 0.0,         # 不强制减仓
        "per_trade_reduce": 0.5,            # 单笔投入降低50%
    },
    "GREEN": {
        "label": "🟢 GREEN 绿色合规",
        "action": "放开约束，正常执行 Module04 预设交易策略与进场计划",
        "new_open_allowed": True,
        "allow_add_position": True,
        "total_cap_factor": 1.0,
        "existing_position_action": "正常持有",
        "high_elasticity_ban": False,
        "consecutive_board_ban": False,
        "convertible_bond_ban": False,
        "reduce_existing_pct": 0.0,
    },
}


# ====================== 判定逻辑 ======================

def judge_decision_level(layer1_result: dict) -> str:
    """
    基于 Layer1 校验结果判定风控等级。

    判定树 (越靠前优先级越高):
      1. lolla_direct_red=True                   → RED
      2. rule_021 五维全利空 且 共振强度≥0.8       → RED
      3. psyche_count ≥ 23                        → RED
      4. psyche_count ≥ 3 且 ≤22                  → YELLOW
      5. rule_021 混合但含利空 (mixed_bearish)     → YELLOW
      6. 三层联动分歧 (divergent)                  → YELLOW
      7. composite_score < 0.3                    → YELLOW
      8. 以上都不满足, 且 psyche_count ≤ 2          → GREEN
      9. 兜底判定: 强度不明确                       → GREEN
    """
    composite_score = layer1_result.get("composite_score", 0.5)
    psy_count = layer1_result.get("psy_count", 0)
    lolla_red = layer1_result.get("lolla_direct_red", False)
    rule_021 = layer1_result.get("rule_021", {})
    linkage = layer1_result.get("three_layer_linkage", {})
    composite_signal = layer1_result.get("composite_signal", {})

    # 1. Lollapalooza ≥23 直接 RED
    if lolla_red:
        return "RED"

    # 2. 五维全利空共振
    if rule_021.get("all_bearish", False) and rule_021.get("resonance_strength", 0) >= 0.8:
        return "RED"

    # 3. psyche ≥ 23
    if psy_count >= 23:
        return "RED"

    # 4. 心理误判 3~22
    if 3 <= psy_count <= 22:
        return "YELLOW"

    # 5. 混合但含利空
    if rule_021.get("resonance_direction") == "mixed_bearish":
        return "YELLOW"

    # 6. 三层联动分歧
    if linkage.get("linkage_status") == "divergent":
        return "YELLOW"

    # 7. 综合得分过低
    if composite_score < 0.3:
        return "YELLOW"

    # 8. psyche ≤ 2
    if psy_count <= 2:
        return "GREEN"

    # 9. 兜底
    return "GREEN"


# ====================== 分级指令生成 ======================

def generate_decision(layer1_result: dict) -> dict:
    """
    生成最终风控决策指令。

    参数:
        layer1_result: Layer1 特征校验层完整输出

    返回:
        {
            "level": "RED"|"YELLOW"|"GREEN",
            "label": "🔴 RED 红色高风险",
            "action": "拦截...",
            "rule": {...},              # RISK_LEVELS 对应完整规则
            "reason": "判定理由",
            "key_metrics": {...},       # 关键指标
            "outputs": {               # 同步下发模块
                "module04": {...},     # → 定策略修正指令
                "module05": {...},     # → 离场模块指令
            },
            "timestamp": "HH:MM:SS",
        }
    """
    logging.info("=" * 60)
    logging.info("Layer2 风控决策层 启动")

    # 1. 判定等级
    level = judge_decision_level(layer1_result)
    rule = RISK_LEVELS[level]

    # 2. 构建判定理由
    reason_parts = []
    composite_score = layer1_result.get("composite_score", 0.5)
    psy_count = layer1_result.get("psy_count", 0)
    lolla_red = layer1_result.get("lolla_direct_red", False)
    rule_021 = layer1_result.get("rule_021", {})
    linkage = layer1_result.get("three_layer_linkage", {})

    if lolla_red:
        reason_parts.append(f"Lollapalooza触发(psy_hit≥23)")
    if psy_count >= 23:
        reason_parts.append(f"心理误判{psy_count}条≥23阈值")
    if rule_021.get("all_bearish", False):
        reason_parts.append("五维全利空共振")
    if rule_021.get("resonance_direction") == "mixed_bearish":
        bearish_count = sum(1 for d in rule_021.get("dimensions", []) if d.get("direction") == "bearish")
        reason_parts.append(f"五维中含{bearish_count}维利空")
    if linkage.get("linkage_status") == "divergent":
        reason_parts.append("技术/基本面/情绪三维信号分歧")
    if composite_score < 0.3:
        reason_parts.append(f"综合得分{composite_score:.3f}过低")

    reason = "; ".join(reason_parts) if reason_parts else "无显著异常信号"

    # 3. 关键指标快照
    key_metrics = {
        "composite_score": composite_score,
        "psy_count": psy_count,
        "active_features": layer1_result.get("active_features", []),
        "resonance_direction": rule_021.get("resonance_direction", "unknown"),
        "resonance_strength": rule_021.get("resonance_strength", 0),
        "linkage_status": linkage.get("linkage_status", "unknown"),
        "reliability": linkage.get("reliability", 0),
    }

    # 4. 同步下发 Module04 指令
    module04_output = {
        "tradeable_pool_action": rule["action"],
        "total_cap_factor": rule["total_cap_factor"],
        "new_open_allowed": rule["new_open_allowed"],
        "allow_add_position": rule["allow_add_position"],
        "per_trade_reduce": rule.get("per_trade_reduce", 1.0),
        "high_elasticity_ban": rule["high_elasticity_ban"],
        "consecutive_board_ban": rule["consecutive_board_ban"],
        "convertible_bond_ban": rule["convertible_bond_ban"],
    }

    # 5. 同步下发 Module05 指令
    module05_output = {
        "existing_position_action": rule["existing_position_action"],
        "reduce_existing_pct": rule["reduce_existing_pct"],
        "force_close_trigger": level == "RED",
        "stop_loss_tighten": level == "RED",  # RED下止损收严
    }

    # 6. 整合输出
    result = {
        "level": level,
        "label": rule["label"],
        "action": rule["action"],
        "rule": rule,
        "reason": reason,
        "key_metrics": key_metrics,
        "layer1_source": {
            "composite_score": composite_score,
            "psy_count": psy_count,
        },
        "outputs": {
            "module04": module04_output,
            "module05": module05_output,
        },
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "market_open_decision": _build_market_open_message(level, rule, reason),
    }

    # 日志输出
    logging.info(f"  → 判定等级: {rule['label']}")
    logging.info(f"  → 判定理由: {reason}")
    logging.info(f"  → 动作: {rule['action']}")
    logging.info(f"  → 新开仓: {'允许' if rule['new_open_allowed'] else '禁止'}")
    logging.info(f"  → 总仓乘数: {rule['total_cap_factor']:.0%}")
    logging.info(f"  → → Module04: 可交易池{module04_output['tradeable_pool_action'][:30]}...")
    logging.info(f"  → → Module05: {module05_output['existing_position_action']}")
    logging.info("Layer2 风控决策层 完成")
    logging.info("=" * 60)

    return result


def _build_market_open_message(level: str, rule: dict, reason: str) -> str:
    """生成面向交易员的开仓决策消息"""
    if level == "RED":
        return f"🔴 放弃当日新开仓计划 — {reason}。持仓启动强制减仓/止损。"
    elif level == "YELLOW":
        factor = rule["total_cap_factor"]
        return f"🟡 限仓执行，总仓上限压缩至{factor:.0%}。禁止加仓，禁止高弹性标的重仓。原因: {reason}"
    else:
        return "🟢 正常执行，按 Module04 预设策略与进场计划操作。"


# ====================== 快捷入口 ======================

def run_layer2(layer1_result: dict) -> dict:
    """Layer2 快捷执行入口"""
    return generate_decision(layer1_result)


# ====================== 测试 ======================

if __name__ == "__main__":
    print("\n=== Layer2 风控决策层 测试 ===\n")

    test_cases = [
        ("RED-Lolla≥23", {
            "composite_score": 0.15,
            "psy_count": 23,
            "active_features": ["F01", "F05", "F06", "F07", "F11"],
            "lolla_direct_red": True,
            "psy_category_matched": 8,
            "rule_021": {
                "resonance_direction": "mixed_bearish",
                "resonance_strength": 1.0,
                "all_bullish": False,
                "all_bearish": False,
                "dimensions": [
                    {"name": "技术面", "score": 25, "direction": "bearish", "signal_count": 4},
                    {"name": "基本面", "score": 30, "direction": "bearish", "signal_count": 2},
                    {"name": "情绪面", "score": 28, "direction": "bearish", "signal_count": 3},
                    {"name": "指标面", "score": 35, "direction": "bearish", "signal_count": 2},
                    {"name": "宏观面", "score": 40, "direction": "bearish", "signal_count": 2},
                ],
            },
            "three_layer_linkage": {
                "linkage_status": "cooperative_bearish",
                "reliability": 0.85,
                "details": {"technical": "bearish", "fundamental": "bearish", "sentiment": "bearish"},
            },
            "composite_signal": {"direction": "bearish_strong", "level": "high_risk", "reason": ""},
        }),
        ("YELLOW-3psy+分歧", {
            "composite_score": 0.45,
            "psy_count": 4,
            "active_features": ["F01", "F03", "F09"],
            "lolla_direct_red": False,
            "psy_category_matched": 3,
            "rule_021": {
                "resonance_direction": "mixed_bearish",
                "resonance_strength": 0.4,
                "all_bullish": False,
                "all_bearish": False,
                "dimensions": [
                    {"name": "技术面", "score": 65, "direction": "bullish", "signal_count": 4},
                    {"name": "基本面", "score": 72, "direction": "bullish", "signal_count": 2},
                    {"name": "情绪面", "score": 35, "direction": "bearish", "signal_count": 3},
                    {"name": "指标面", "score": 50, "direction": "neutral", "signal_count": 2},
                    {"name": "宏观面", "score": 55, "direction": "neutral", "signal_count": 2},
                ],
            },
            "three_layer_linkage": {
                "linkage_status": "divergent",
                "reliability": 0.30,
                "details": {"technical": "bullish", "fundamental": "bullish", "sentiment": "bearish"},
            },
            "composite_signal": {"direction": "uncertain", "level": "medium_risk", "reason": ""},
        }),
        ("GREEN-全部正常", {
            "composite_score": 0.78,
            "psy_count": 0,
            "active_features": ["F01", "F03", "F13", "F14"],
            "lolla_direct_red": False,
            "psy_category_matched": 0,
            "rule_021": {
                "resonance_direction": "bullish",
                "resonance_strength": 0.9,
                "all_bullish": True,
                "all_bearish": False,
                "dimensions": [
                    {"name": "技术面", "score": 78, "direction": "bullish", "signal_count": 4},
                    {"name": "基本面", "score": 82, "direction": "bullish", "signal_count": 2},
                    {"name": "情绪面", "score": 72, "direction": "bullish", "signal_count": 3},
                    {"name": "指标面", "score": 65, "direction": "bullish", "signal_count": 2},
                    {"name": "宏观面", "score": 70, "direction": "bullish", "signal_count": 2},
                ],
            },
            "three_layer_linkage": {
                "linkage_status": "cooperative_bullish",
                "reliability": 0.92,
                "details": {"technical": "bullish", "fundamental": "bullish", "sentiment": "bullish"},
            },
            "composite_signal": {"direction": "bullish_strong", "level": "low_risk", "reason": ""},
        }),
    ]

    for desc, l1_result in test_cases:
        print(f"\n--- {desc} ---")
        decision = generate_decision(l1_result)
        print(f"  等级: {decision['level']} | {decision['action'][:40]}...")
        print(f"  理由: {decision['reason']}")
        print(f"  new_open: {decision['rule']['new_open_allowed']} | 仓位乘数: {decision['rule']['total_cap_factor']:.0%}")
        print(f"  Module04: new_open={decision['outputs']['module04']['new_open_allowed']}")
        print(f"  Module05: force_close={decision['outputs']['module05']['force_close_trigger']}")
