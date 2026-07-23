#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
module_review_520.py — 520复盘输出标准话术

每轮复盘自动输出:
  - 当日520有效金叉数量
  - 被MA20下行过滤假信号数量
  - 情绪降级过滤数量
  - 芒格风控拦截数量
  - 整体趋势环境评分
"""

import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [REV520] %(message)s",
                    datefmt="%H:%M:%S")


def build_review(
    total_candidates: int = 0,
    gold_cross_count: int = 0,
    ma20_down_filtered: int = 0,
    sentiment_downgraded: int = 0,
    lolla_blocked: int = 0,
    final_pool_size: int = 0,
    sentiment_label: str = "",
    signal_520_weight: float = 1.0,
    market_trend: str = "",
) -> dict:
    """构建520复盘报告。

    返回: dict + 自动打印到日志
    """
    # 环境评分: 基于权重换算
    if signal_520_weight >= 1.0:
        env_score = "🟢 良好"
        env_desc = "多头趋势环境, 520信号可正常采信"
    elif signal_520_weight >= 0.4:
        env_score = "🟡 谨慎"
        env_desc = f"情绪{ sentiment_label }, 520信号权重降级60%, 仅观察"
    else:
        env_score = "🔴 恶劣"
        env_desc = f"情绪{ sentiment_label }, 520信号完全屏蔽"

    report = {
        "report_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market_trend": market_trend or sentiment_label,
        "sentiment_label": sentiment_label,
        "signal_520_weight": signal_520_weight,
        "env_score": env_score,
        "env_desc": env_desc,
        "stats": {
            "total_candidates": total_candidates,
            "gold_cross_valid": gold_cross_count,
            "ma20_down_filtered": ma20_down_filtered,
            "sentiment_downgraded": sentiment_downgraded,
            "lolla_blocked": lolla_blocked,
            "final_pool_size": final_pool_size,
        },
        "summary": (
            f"📊 520复盘 | 环境:{env_score} | "
            f"金叉有效{gold_cross_count}/{total_candidates} | "
            f"MA20下行过滤{ma20_down_filtered} | "
            f"情绪降级{sentiment_downgraded} | "
            f"芒格拦截{lolla_blocked} | "
            f"最终池{final_pool_size}"
        ),
    }

    # 日志输出
    lines = [
        f"\n{'='*55}",
        f"  📊 520交易信号 复盘报告",
        f"  {report['report_time']}",
        f"  {'='*55}",
        f"  整体环境: {env_score}",
        f"  {env_desc}",
        f"",
        f"  【筛选统计】",
        f"  原始候选:          {total_candidates:>4} 只",
        f"  有效金叉:          {gold_cross_count:>4} 只",
        f"  MA20下行过滤:     {ma20_down_filtered:>4} 只(假信号)",
        f"  情绪降级过滤:     {sentiment_downgraded:>4} 只",
        f"  芒格风控拦截:     {lolla_blocked:>4} 只",
        f"  ─────────────────────",
        f"  最终可交易池:      {final_pool_size:>4} 只",
        f"",
        f"  【风控状态】",
        f"  520权重:           {signal_520_weight}",
        f"  环境评分:          {env_score}",
        f"  {'='*55}",
    ]
    logging.info("\n".join(lines))
    return report


if __name__ == "__main__":
    build_review(
        total_candidates=50,
        gold_cross_count=8,
        ma20_down_filtered=12,
        sentiment_downgraded=15,
        lolla_blocked=5,
        final_pool_size=10,
        sentiment_label="recovery",
        signal_520_weight=0.4,
        market_trend="震荡",
    )
