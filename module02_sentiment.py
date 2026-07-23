#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module02 定情绪 — 盘前情绪判定

时序: Module01 定风格之后执行
依赖:
    - 全市场涨跌家数 (数据源: akshare stock_zh_a_spot_em / 东方财富)
    - 连板高度 / 封板率 / 炸板率 (数据源: 东方财富涨停板数据)
输入: 人工录入 or API接入
输出: sentiment_label, sentiment_cap, final_total_cap, psy_codes
"""

import logging
from psy_hit_manager import add_psy_code, psy_hit_codes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [M02] %(message)s", datefmt="%H:%M:%S")

# ====================== 情绪阶段判定标准 ======================

def judge_sentiment(
    up_count: int, down_count: int,
    highest_board: int,
    seal_rate: float,   # 封板率 %
    blow_rate: float,   # 炸板率 %
    has_massacre: bool, # 批量高位核按钮/大面
) -> tuple:
    """
    返回: (label, label_cn, reason)
    label: 'ice' | 'recovery' | 'boom' | 'recession'
    """
    total = up_count + down_count
    up_ratio = up_count / total * 100 if total > 0 else 50

    # —— 冰点 ————————
    if down_count > 3000 and highest_board <= 2 and seal_rate < 40 and blow_rate > 30 and has_massacre:
        return ("ice", "冰点", f"下跌{down_count}>3000 连板≤2 封板{seal_rate:.0f}%<40% 炸板{blow_rate:.0f}%>30% 核按钮")

    # —— 高潮 ————————
    if up_count > 3000 and highest_board >= 5 and seal_rate > 70 and blow_rate < 15 and not has_massacre:
        return ("boom", "高潮亢奋", f"上涨{up_count}>3000 连板≥{highest_board} 封板{seal_rate:.0f}%>70% 炸板{blow_rate:.0f}%<15% 无亏钱效应")

    # —— 退潮 ————————
    if highest_board >= 4 and blow_rate > 30 and has_massacre and seal_rate < 50:
        return ("recession", "退潮分歧", f"高位分歧 封板{seal_rate:.0f}%<50% 炸板{blow_rate:.0f}%>30% 核按钮")

    # —— 回暖修复(兜底) —
    return ("recovery", "回暖修复", f"涨跌均衡 连板{highest_board}板 封板{seal_rate:.0f}% 炸板{blow_rate:.0f}%")


# ====================== 情绪仓位约束 ======================

SENTIMENT_CAP = {
    "ice":       {"total": 30, "label": "冰点期 总仓≤30%"},
    "recovery":  {"total": 60, "label": "回暖修复 总仓50%~70%"},
    "boom":      {"total": 20, "label": "高潮亢奋 总仓≤20%"},
    "recession": {"total": 20, "label": "退潮分歧 总仓≤20%"},
}

# ====================== 心理误判编码绑定 ======================

def apply_psy_codes(sentiment_label: str, has_massacre: bool, is_boom: bool):
    """根据情绪阶段绑定心理误判编码"""
    if has_massacre:
        add_psy_code("code_14_损失厌恶")
        add_psy_code("code_15_社会认同羊群")
        logging.info(f"  🧠 核按钮→新增 code_14 + code_15")

    if is_boom:
        add_psy_code("code_13_过度乐观")
        logging.info(f"  🧠 高潮无亏钱→新增 code_13")


# ====================== 市场趋势判定（520联动） ======================

def judge_market_trend(up_count: int, down_count: int,
                       has_massacre: bool) -> tuple:
    """判定市场环境标签。"""
    total = up_count + down_count
    up_ratio = up_count / total * 100 if total > 0 else 50
    if up_ratio >= 60 and not has_massacre:
        return ("多头趋势", 1.0, f"上涨{up_count}家({up_ratio:.0f}%)≥60% 无亏钱效应")
    elif has_massacre or up_ratio < 40:
        return ("亏钱效应", 0.4, f"上涨{up_count}家({up_ratio:.0f}%) 或亏钱效应")
    else:
        return ("震荡行情", 0.4, f"涨跌均衡 上涨{up_count}家({up_ratio:.0f}%)")


def is_bear_market_override(
    up_count: int, down_count: int,
    highest_board: int, has_massacre: bool,
) -> tuple:
    """判定是否熊市单边下行环境（覆盖520权重=0.0）。

    触发条件（任一）:
      1. 下跌家数≥3500 且 连板高度≤2
      2. 上涨占比<25% 且 连板高度≤2 且 有核按钮
    返回: (override: bool, reason: str)
    """
    total = up_count + down_count
    up_ratio = up_count / total * 100 if total > 0 else 50
    if down_count >= 3500 and highest_board <= 2:
        return True, f"熊市单边下行: 下跌{down_count}≥3500 连板≤{highest_board}"
    if up_ratio < 25 and highest_board <= 2 and has_massacre:
        return True, f"熊市单边下行: 上涨占比{up_ratio:.0f}%<25% 连板≤{highest_board} 亏钱效应"
    return False, ""


def get_520_override_weight(
    sentiment_label: str,
    up_count: int, down_count: int,
    highest_board: int, blow_rate: float,
    has_massacre: bool,
) -> tuple:
    """获取520最终权重(含个股数据+情绪降级+熊市覆盖)。

    返回: (weight: float, reason: str)
    """
    from module01_config_520 import get_520_weight, get_520_weight_desc

    # 第1重: 熊市单边下行 → 直接屏蔽
    bear_override, bear_reason = is_bear_market_override(
        up_count, down_count, highest_board, has_massacre
    )
    if bear_override:
        return (0.0, f"熊市屏蔽: {bear_reason}")

    # 第2重: 高亏钱效应(炸板率≥35 + 连板<3) → 降级60%
    if blow_rate >= 35 and highest_board <= 2:
        return (0.4, f"亏钱效应降级: 炸板{blow_rate}%≥35% 连板≤{highest_board} → 520权重降至0.4, 仅观察")

    # 第3重: 情绪阶段映射
    base_weight = get_520_weight(sentiment_label)
    desc = get_520_weight_desc(sentiment_label)
    return (base_weight, desc)


# ====================== 主入口 ======================

def run_module02(
    up_count: int, down_count: int,
    highest_board: int,
    seal_rate: float,
    blow_rate: float,
    has_massacre: bool,
    module01_total_cap: int,  # 来自Module01的style_position_cap.total
) -> dict:
    """
    Module02 定情绪主入口

    返回:
        {
            "sentiment_label": "ice"|"recovery"|"boom"|"recession",
            "sentiment_cn": "冰点"|...,
            "sentiment_cap": XX,       # 情绪强制总仓上限%
            "style_cap": XX,           # 模块1风格总仓上限%
            "final_total_cap": XX,     # min(风格,情绪)
            "psy_codes_added": [...],
            "has_massacre": bool,
            "judge_reason": "判定依据",
            "massacre_note": "亏钱效应记录",
        }
    """
    logging.info("=" * 50)
    logging.info("Module02 定情绪 启动")
    logging.info(f"  数据: 上涨{up_count} 下跌{down_count} 连板{highest_board}板 封板{seal_rate}% 炸板{blow_rate}%")

    # 1. 情绪判定
    label, cn, reason = judge_sentiment(
        up_count, down_count, highest_board, seal_rate, blow_rate, has_massacre
    )
    is_boom = (label == "boom")
    sentiment_cap = SENTIMENT_CAP[label]["total"]

    logging.info(f"  → 情绪: {cn} | 依据: {reason}")
    logging.info(f"  → 情绪仓位上限: {sentiment_cap}%")

    # 2. 亏钱效应文本记录
    massacre_note = ""
    if has_massacre:
        massacre_note = "批量高位核按钮/大面 - 单日多只-10%以上"
        logging.info(f"  ⚠️ 亏钱效应: {massacre_note}")

    # 3. 心理误判编码
    before_count = len(psy_hit_codes)
    apply_psy_codes(label, has_massacre, is_boom)
    new_codes = psy_hit_codes[before_count:] if before_count < len(psy_hit_codes) else []

    # 4. 最终总仓 = min(风格仓位, 情绪仓位)
    final_total_cap = min(module01_total_cap, sentiment_cap)
    logging.info(f"  → 风格仓位{module01_total_cap}% vs 情绪仓位{sentiment_cap}%")
    logging.info(f"  → 最终总仓上限: {final_total_cap}% (取较小值)")

    # 5. 520信号权重判定（三重覆盖: 熊市屏蔽→亏钱效应降级→情绪阶段映射）
    from module01_config_520 import SENTIMENT_TO_520_WEIGHT
    signal_520_weight, weight_reason = get_520_override_weight(
        sentiment_label=label,
        up_count=up_count, down_count=down_count,
        highest_board=highest_board, blow_rate=blow_rate,
        has_massacre=has_massacre,
    )
    logging.info(f"  → 520权重: {signal_520_weight} ({weight_reason})")
    logging.info(f"  → psy_hit_codes 当前: {psy_hit_codes}")
    logging.info("Module02 定情绪 完成")
    logging.info("=" * 50)

    return {
        "sentiment_label": label,
        "sentiment_cn": cn,
        "sentiment_cap": sentiment_cap,
        "style_cap": module01_total_cap,
        "final_total_cap": final_total_cap,
        "psy_codes_added": new_codes,
        "has_massacre": has_massacre,
        "judge_reason": reason,
        "massacre_note": massacre_note,
        "market_trend_label": weight_reason.split(":")[0].strip(),
        "signal_520_weight": signal_520_weight,
        "signal_520_weight_desc": weight_reason,
        "bear_market_override": signal_520_weight == 0.0,
    }


# ====================== 测试 ======================
if __name__ == "__main__":
    from psy_hit_manager import clear_all_psy_codes
    clear_all_psy_codes()

    test_cases = [
        # (up, down, 连板, 封板%, 炸板%, 核按钮, 风格仓位, 场景)
        (800, 3500, 2, 30, 40, True,  60, "冰点"),
        (2800, 1200, 3, 50, 25, False, 60, "回暖"),
        (3800, 400,  6, 80, 10, False, 60, "高潮"),
        (1500, 2600, 5, 40, 40, True,  60, "退潮"),
    ]

    for up, down, board, seal, blow, massacre, style_cap, label in test_cases:
        clear_all_psy_codes()
        print(f"\n--- {label} ---")
        r = run_module02(up, down, board, seal, blow, massacre, style_cap)
        print(f"  {r['sentiment_cn']} | 情绪仓位{r['sentiment_cap']}% | "
              f"最终总仓{r['final_total_cap']}% | psy: {psy_hit_codes}")
