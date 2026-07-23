#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
module01_config_520.py — M01 520参数配置池

固化520算法全局参数。支持盘中调参（触发模式B全链重跑）。
"""


# ===================== 520基础参数（固化） =====================

CONFIG_520 = {
    # --- 周期参数 ---
    "short_window": 5,          # 短周期: MA5
    "long_window": 20,          # 长周期: MA20
    "trend_lookback": 3,        # 趋势斜率回溯周期

    # --- 阈值参数 ---
    "trend_slope_min": 0.0,     # MA20斜率 ≥ 0（走平/向上为有效趋势）
    "volume_ratio_min": 1.3,    # 放量阈值：当日量 ≥ 5日均量 × 1.3
    "liquidity_min": 50_000_000, # 最低日均成交额 5000万
    "turnover_max": 25.0,       # 最高换手率 25%

    # --- 权重参数 ---
    "weight_bull_market": 1.0,  # 多头环境520权重
    "weight_oscillation": 0.4,  # 震荡环境520权重(降级60%)
    "weight_bear_market": 0.0,  # 熊市环境520权重(完全屏蔽)

    # --- M04 D模式参数 ---
    "d_mode_per_stock_max": 3,  # D模式单票≤3%
    "d_mode_520_required": True,# D模式强制校验520信号
}


# ===================== 情绪→520权重映射 =====================

SENTIMENT_TO_520_WEIGHT = {
    # sentiment_label: (weight, description)
    "ice":        (0.0, "冰点期: 完全屏蔽520金叉信号"),
    "recovery":   (0.4, "回暖修复: 520权重降级60%, 仅观察"),
    "boom":       (0.4, "高潮亢奋: 520权重降级60%, 防止追高"),
    "recession":  (0.0, "退潮分歧: 完全屏蔽520金叉信号"),
}


# ===================== 熊市判定阈值 =====================

BEAR_MARKET_THRESHOLDS = {
    "down_count_min": 3000,     # 下跌家数≥3000
    "highest_board_max": 2,     # 连板高度≤2
    "seal_rate_max": 40,        # 封板率≤40%
    "blow_rate_min": 30,        # 炸板率≥30%
}


# ===================== 模式A/B/C调度绑定 =====================

SCHEDULE_MODES = {
    "A": {
        "name": "全流水线(每日开盘1次)",
        "trigger": "每日开盘前固定执行",
        "scope": "M00完整重算→M01→M02→M03→M04→M05",
        "note": "完整重算MA5/MA20全市场基准, 生成当日初始趋势池",
    },
    "B": {
        "name": "盘中参数调整",
        "trigger": "修改520阈值/放量比例/斜率规则",
        "scope": "自动M00起全链重跑",
        "note": "参数改动属于上层参数变更, 触发模式B判定逻辑",
    },
    "C": {
        "name": "单模块重跑(盘中默认最优)",
        "trigger": "行情滚动更新",
        "scope": "仅重跑M02→M03→M04→M05, 复用M00/M01缓存",
        "note": "低延迟刷新520选股结果",
    },
}


# ===================== 快捷查询 =====================

def get_520_weight(sentiment_label: str) -> float:
    """根据情绪标签获取520信号权重。"""
    return SENTIMENT_TO_520_WEIGHT.get(sentiment_label, (0.4, ""))[0]


def get_520_weight_desc(sentiment_label: str) -> str:
    """获取权重描述。"""
    return SENTIMENT_TO_520_WEIGHT.get(sentiment_label, ("", "未知情绪"))[1]


if __name__ == "__main__":
    print("=" * 60)
    print("  M01 520参数配置池 自测")
    print("=" * 60)
    print(f"  短周期: MA{CONFIG_520['short_window']}")
    print(f"  长周期: MA{CONFIG_520['long_window']}")
    print(f"  放量阈值: {CONFIG_520['volume_ratio_min']}x")
    print(f"  情绪→权重: ", {k: v[0] for k, v in SENTIMENT_TO_520_WEIGHT.items()})
    print(f"  调度模式: {len(SCHEDULE_MODES)}种")
    print("  ✅")
