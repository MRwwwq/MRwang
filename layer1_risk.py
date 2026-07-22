#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer1 风控引擎 — 综合判定

时序: 在 Module04 之后, 挂单/交易之前
输入: psy_hit_count (来自 psy_hit_manager) + 个股25因子Lollapalooza状态
输出: GREEN / YELLOW / RED 三级风控等级 + 对应权限规则
"""

import logging
from psy_hit_manager import get_psy_hit_count, psy_hit_codes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Layer1] %(message)s", datefmt="%H:%M:%S")

# ====================== 风险等级定义 ======================

RISK_LEVEL = {
    "GREEN": {
        "label": "🟢 GREEN 绿色合规",
        "action": "正常执行交易策略",
        "new_open": True,
        "max_total_cap_reduce": 0,  # 不额外减仓
    },
    "YELLOW": {
        "label": "🟡 YELLOW 黄色预警",
        "action": "限仓执行，禁止新开加仓",
        "new_open": False,
        "max_total_cap_reduce": 0.5,  # 总仓砍半
    },
    "RED": {
        "label": "🔴 RED 红色高风险拦截",
        "action": "禁止一切新开仓，持仓择机执行离场规则",
        "new_open": False,
        "max_total_cap_reduce": 1.0,  # 总仓归零(新开仓)
    },
}


def judge_risk_level(
    psy_hit_count: int,
    lolla_triggered: bool = False,
    lolla_high_count: int = 0,
) -> dict:
    """
    综合判定风控等级。

    判定规则:
        RED:   Lollapalooza共振已触发(>=3项高分)  OR  psy_hit_count >= 5
        YELLOW: psy_hit_count >= 3  OR  Lollapalooza共振计数>=3但未达RED边界
        GREEN:  其他正常情况

    返回:
        {
            "level": "GREEN"|"YELLOW"|"RED",
            "label": "🟢 GREEN 绿色合规",
            "action": "正常执行交易策略",
            "psy_hit_count": int,
            "lolla_triggered": bool,
            "lolla_high_count": int,
            "new_open_allowed": bool,
            "total_cap_factor": float,  # 仓位乘数
        }
    """
    logging.info("=" * 50)
    logging.info("Layer1 风控引擎 启动")
    logging.info(f"  psy_hit_count: {psy_hit_count}")
    logging.info(f"  Lollapalooza触发: {lolla_triggered} (高分{lolla_high_count}项)")
    logging.info(f"  psy_hit_codes: {psy_hit_codes}")

    # 判定
    if lolla_triggered or psy_hit_count >= 5:
        level = "RED"
    elif psy_hit_count >= 3 or lolla_high_count >= 3:
        level = "YELLOW"
    else:
        level = "GREEN"

    rule = RISK_LEVEL[level]

    result = {
        "level": level,
        "label": rule["label"],
        "action": rule["action"],
        "psy_hit_count": psy_hit_count,
        "lolla_triggered": lolla_triggered,
        "lolla_high_count": lolla_high_count,
        "new_open_allowed": rule["new_open"],
        "total_cap_factor": 1.0 - rule["max_total_cap_reduce"],
    }

    logging.info(f"  → 判定: {rule['label']}")
    logging.info(f"  → 动作: {rule['action']}")
    logging.info(f"  → 新开仓: {'允许' if rule['new_open'] else '禁止'}")
    logging.info(f"  → 仓位乘数: {result['total_cap_factor']:.0%}")
    logging.info("Layer1 风控引擎 完成")
    logging.info("=" * 50)

    return result


if __name__ == "__main__":
    from psy_hit_manager import add_psy_code, clear_all_psy_codes

    test_cases = [
        (0, False, 0, "GREEN-无触发"),
        (3, False, 0, "YELLOW-3项累计"),
        (5, False, 0, "RED-5项累计"),
        (2, True, 4, "RED-Lolla共振"),
        (1, False, 3, "YELLOW-个股高分3项"),
    ]

    for cnt, lolla, high, desc in test_cases:
        clear_all_psy_codes()
        # 模拟添加对应数量的psy_hit
        for i in range(cnt):
            add_psy_code(f"code_test_{i}")
        print(f"\n--- {desc} ---")
        r = judge_risk_level(cnt, lolla, high)
        print(f"  {r['level']} | 新开仓:{r['new_open_allowed']} | 仓位:{r['total_cap_factor']:.0%}")
