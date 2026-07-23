#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_misjudge_match.py — MISJUDGE_MATCH 误判匹配微服务

职责: 使用芒格25种误判心理学RAG检索,将五类原始信号与心理误判知识库匹配。
统计独立负误差项,输出topic.signal.matched。

规格映射:
  2.MISJUDGE_MATCH: 芒格25种误判心理学RAG检索,统计独立负误差项
"""

import logging
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MIS_MATCH] %(message)s",
    datefmt="%H:%M:%S",
)

# ===================== 芒格25误判映射表 =====================

MUNGER_25_MAP = {
    "code_01_低估奖励高估惩罚": "低估奖励/高估惩罚",
    "code_02_喜欢热爱倾向": "喜欢/热爱倾向",
    "code_03_讨厌憎恨倾向": "讨厌/憎恨倾向",
    "code_04_避免怀疑": "避免怀疑倾向",
    "code_05_避免不一致": "避免不一致倾向",
    "code_06_好奇心倾向": "好奇心倾向",
    "code_07_康德式公平倾向": "康德式公平倾向",
    "code_08_嫉妒猜忌": "嫉妒/猜忌倾向",
    "code_09_回馈倾向": "回馈倾向",
    "code_10_简单联想": "简单联想倾向",
    "code_11_简单避免痛苦心理否认": "简单的、避免痛苦的心理否认",
    "code_12_自视过高": "自视过高倾向",
    "code_13_过度乐观": "过度乐观倾向",
    "code_14_损失厌恶": "损失厌恶倾向",
    "code_15_社会认同羊群": "社会认同/羊群效应",
    "code_16_对比误导倾向": "对比误导倾向",
    "code_17_压力影响倾向": "压力影响倾向",
    "code_18_易得性误导": "易得性误导",
    "code_19_不用就忘": "不用就忘倾向",
    "code_20_化学物质依赖": "化学物质依赖",
    "code_21_衰老误导倾向": "衰老误导倾向",
    "code_22_权威误导倾向": "权威误导倾向",
    "code_23_废话倾向": "废话倾向",
    "code_24_重视理由倾向": "重视理由倾向",
    "code_25_Lollapalooza": "Lollapalooza效应",
}

# 信号→误判偏差映射规则
SIGNAL_TO_BIAS_MAP = {
    "gold_cross_true": ["code_13_过度乐观", "code_10_简单联想"],
    "gold_cross_false_ma20_down": ["code_14_损失厌恶", "code_04_避免怀疑"],
    "bear_market_override": ["code_12_自视过高", "code_19_不用就忘"],
    "high_sentiment_boom": ["code_13_过度乐观", "code_15_社会认同羊群"],
    "massacre_mode": ["code_14_损失厌恶", "code_15_社会认同羊群"],
    "strong_trend_up": ["code_13_过度乐观", "code_01_低估奖励"],
    "strong_trend_down": ["code_14_损失厌恶", "code_04_避免怀疑", "code_08_嫉妒猜忌"],
    "no_main_line": ["code_23_废话倾向"],
    "short_driver": ["code_10_简单联想"],
    "high_volume_surge": ["code_15_社会认同羊群", "code_08_嫉妒猜忌"],
    "low_liquidity": ["code_19_不用就忘"],
    "bearish_macro": ["code_14_损失厌恶"],
}

# 评分规则: 每条偏差的负误差强度
BIAS_SEVERITY = {
    "code_04_避免怀疑": 8,
    "code_08_嫉妒猜忌": 7,
    "code_10_简单联想": 5,
    "code_12_自视过高": 6,
    "code_13_过度乐观": 7,
    "code_14_损失厌恶": 9,
    "code_15_社会认同羊群": 8,
    "code_19_不用就忘": 4,
    "code_23_废话倾向": 3,
    "default": 5,
}


def detect_signal_patterns(signals: dict) -> list:
    """从五类信号中检测特征模式,返回匹配的偏差编码列表。"""
    matched_codes = []
    tech = signals.get("tech", {})
    sent = signals.get("sentiment", {})

    # 模式1: 金叉 + MA20上行 → 过度乐观
    if tech.get("gold_cross") and tech.get("ma20_trend"):
        matched_codes.append("code_13_过度乐观")

    # 模式2: 金叉但MA20下行 → 避免怀疑+损失厌恶
    if tech.get("gold_cross") and tech.get("ma20_trend") is False:
        matched_codes.extend(["code_04_避免怀疑", "code_14_损失厌恶"])

    # 模式3: 熊市覆盖 → 自视过高
    if sent.get("bear_market_override"):
        matched_codes.append("code_12_自视过高")

    # 模式4: 高潮情绪 → 过度乐观+羊群
    if sent.get("sentiment_label") == "boom":
        matched_codes.extend(["code_13_过度乐观", "code_15_社会认同羊群"])

    # 模式5: 亏钱效应 → 损失厌恶+羊群
    if sent.get("has_massacre"):
        matched_codes.extend(["code_14_损失厌恶", "code_15_社会认同羊群"])

    # 模式6: 无主线 → 废话倾向
    if sent.get("main_line", "") == "" or sent.get("main_line") == "无主线(混沌)":
        matched_codes.append("code_23_废话倾向")

    # 模式7: 短期题材 → 简单联想
    if sent.get("driver_validity") == "短期(≤2周)":
        matched_codes.append("code_10_简单联想")

    # 去重
    seen = set()
    unique = []
    for c in matched_codes:
        if c not in seen and c in MUNGER_25_MAP:
            seen.add(c)
            unique.append(c)
    return unique


def calculate_negative_error(bias_codes: list) -> dict:
    """统计独立负误差项: 每条偏差计算强度得分。"""
    total_neg = 0
    details = []
    for code in bias_codes:
        sev = BIAS_SEVERITY.get(code, BIAS_SEVERITY["default"])
        total_neg += sev
        details.append({
            "code": code,
            "name": MUNGER_25_MAP.get(code, "未知"),
            "severity": sev,
            "description": f"{code} — 负误差强度{sev}",
        })
    return {
        "total_negative_error": total_neg,
        "bias_count": len(bias_codes),
        "details": details,
        "severity_level": "HIGH" if total_neg >= 30 else "MEDIUM" if total_neg >= 15 else "LOW",
    }


def run_misjudge_match(signals: dict, psy_hit_codes: list = None) -> dict:
    """MISJUDGE_MATCH 主入口。

    参数:
        signals: SIGNAL_EXTRACT输出的五类信号
        psy_hit_codes: 全局psy_hit_codes (可选)

    返回:
        {
            "matched_bias_codes": [编码列表],
            "negative_error": {...},
            "bias_count": int,
            "total_negative_error": int,
            "severity_level": str,
        }
    """
    logging.info("  🧠 MISJUDGE_MATCH RAG检索")

    # 步骤1: 从信号检测偏差模式
    detected = detect_signal_patterns(signals or {})
    logging.info(f"  → 模式检测: {len(detected)}条偏差")

    # 步骤2: 合并外部psy_hit_codes
    all_codes = list(detected)
    if psy_hit_codes:
        for c in psy_hit_codes:
            if c not in all_codes and c in MUNGER_25_MAP:
                all_codes.append(c)
    logging.info(f"  → 合并后: {len(all_codes)}条偏差(含全局{len(psy_hit_codes or [])}条)")

    # 步骤3: 统计独立负误差项
    neg_err = calculate_negative_error(all_codes)
    logging.info(f"  → 负误差合计: {neg_err['total_negative_error']} "
                  f"({neg_err['severity_level']})")

    result = {
        "matched_bias_codes": all_codes,
        "negative_error": neg_err,
        "bias_count": len(all_codes),
        "total_negative_error": neg_err["total_negative_error"],
        "severity_level": neg_err["severity_level"],
    }
    logging.info(f"  ✅ MISJUDGE_MATCH完成: {result['bias_count']}项偏差 "
                  f"负误差={result['total_negative_error']}")
    return result


# ===================== MQ消息处理器 =====================

def handle_misjudge_match(msg: dict) -> Optional[dict]:
    """MQ消息处理器: 消费raw_signal,生产matched_signal。"""
    from mq_bus import get_bus
    payload = msg.get("payload", {})
    stock_code = msg.get("stock_code", "")
    task_uuid = msg.get("task_uuid", "")

    signals = payload.get("signals", {}) if isinstance(payload, dict) else {}
    psy_codes = payload.get("psy_hit_codes", [])

    result = run_misjudge_match(signals, psy_codes)
    result["stock_code"] = stock_code
    result["source_raw_msg_id"] = msg.get("msg_id")

    bus = get_bus()
    bus.produce_from_service(
        "topic.signal.matched", "MISJUDGE_MATCH",
        stock_code, result,
        task_uuid=task_uuid,
    )
    return result


if __name__ == "__main__":
    # 自测
    test_signals = {
        "tech": {"gold_cross": True, "ma20_trend": True, "close": 12.5},
        "sentiment": {"sentiment_label": "boom", "has_massacre": False,
                       "bear_market_override": False,
                       "main_line": "固态电池", "driver_validity": "短期(≤2周)",
                       "signal_520_weight": 0.4},
    }
    result = run_misjudge_match(test_signals)
    print(f"  ✅ 偏差匹配: {result['bias_count']}项 负误差: {result['total_negative_error']} "
          f"({result['severity_level']})")
    for d in result["negative_error"]["details"]:
        print(f"    {d['code']}: severity={d['severity']}")
