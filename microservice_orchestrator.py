#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
microservice_orchestrator.py — 微服务编排器

架构: 消息队列异步流转,五服务并行独立运行
  1.SIGNAL_EXTRACT → topic.signal.raw
  2.MISJUDGE_MATCH → topic.signal.matched
  3.RULE_SCORE_ENGINE → topic.risk.score  【核心链路】
  4.POSITION_DECISION → topic.order.decision
  5.EVOLUTION_AGENT: 旁路异步消费topic.risk.score,不阻塞主链路

隔离规则:
  - 单条消息异常→DLQ; 单模块超时不阻塞其他标的
  - 幂等键: task_uuid+stock_code+snapshot_time
  - 单任务超时1200ms
"""

import logging
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MS_Orch] %(message)s",
    datefmt="%H:%M:%S",
)


def register_all_services():
    """注册全部微服务到消息总线。

    订阅关系:
      SIGNAL_EXTRACT(producer) → topic.signal.raw
        [MISJUDGE_MATCH] subscribes topic.signal.raw
        → produces topic.signal.matched
        [RULE_SCORE_ENGINE] subscribes topic.signal.matched
        → produces topic.risk.score
        [POSITION_DECISION] subscribes topic.risk.score
        → produces topic.order.decision
        [EVOLUTION_AGENT] subscribes topic.risk.score (异步旁路)
    """
    from mq_bus import get_bus, MSG_TOPICS
    from service_misjudge_match import handle_misjudge_match
    from service_rule_score_engine import handle_rule_score_engine
    from service_position_decision import handle_position_decision
    from service_evolution_agent import handle_evolution_agent

    bus = get_bus()

    # 订阅拓扑
    bus.subscribe(MSG_TOPICS["raw_signal"], handle_misjudge_match)
    bus.subscribe(MSG_TOPICS["matched_signal"], handle_rule_score_engine)
    bus.subscribe(MSG_TOPICS["risk_score"], handle_position_decision)
    bus.subscribe(MSG_TOPICS["risk_score"], handle_evolution_agent)  # 旁路

    svcs = {
        "SIGNAL_EXTRACT": {"type": "producer", "topic": MSG_TOPICS["raw_signal"]},
        "MISJUDGE_MATCH": {"type": "consumer+producer",
                           "consume": MSG_TOPICS["raw_signal"],
                           "produce": MSG_TOPICS["matched_signal"]},
        "RULE_SCORE_ENGINE": {"type": "consumer+producer",
                              "consume": MSG_TOPICS["matched_signal"],
                              "produce": MSG_TOPICS["risk_score"]},
        "POSITION_DECISION": {"type": "consumer+producer",
                              "consume": MSG_TOPICS["risk_score"],
                              "produce": MSG_TOPICS["order_decision"]},
        "EVOLUTION_AGENT": {"type": "async_consumer",
                            "consume": MSG_TOPICS["risk_score"],
                            "bypass": True},
    }
    logging.info(f"  ✅ 5微服务注册完成")
    for name, info in svcs.items():
        logging.info(f"    {name:20s} → {info}")
    return svcs


def run_one_stock(stock_code: str, stock_data: dict,
                  m02_result: dict = None,
                  m03_result: dict = None,
                  macro_data: dict = None,
                  stock_type: str = "concept",
                  current_position_pct: float = 0.0,
                  task_uuid: str = None) -> dict:
    """对单只标的执行全流程: 信号提取→误判匹配→规则打分→仓位决策→进化旁路。

    返回: 全链路最终决策
    """
    from mq_bus import get_bus, MSG_TOPICS

    bus = get_bus()
    code = stock_code

    logging.info(f"\n{'='*55}")
    logging.info(f"  🎯 微服务流水线 [{code}] 启动")
    logging.info(f"{'='*55}")

    # Step1: SIGNAL_EXTRACT → topic.signal.raw
    from service_signal_extract import run_signal_extract
    raw_signal = run_signal_extract(
        code, stock_data, m02_result, m03_result, macro_data
    )
    sig_msg = bus.produce_from_service(
        MSG_TOPICS["raw_signal"], "SIGNAL_EXTRACT",
        code, {**raw_signal, "psy_hit_codes": stock_data.get("psy_hit_codes", [])},
        task_uuid=task_uuid,
    )

    # Step2: MISJUDGE_MATCH (消费raw_signal → 生产matched_signal)
    bus.process_topic(MSG_TOPICS["raw_signal"])

    # Step3: RULE_SCORE_ENGINE (消费matched_signal → 生产risk_score)
    bus.process_topic(MSG_TOPICS["matched_signal"])

    # Step4: POSITION_DECISION (消费risk_score → 生产order_decision)
    bus.process_topic(MSG_TOPICS["risk_score"])

    # 消费order_decision获取最终决策
    decisions = bus.consume_all(MSG_TOPICS["order_decision"])

    # Step5: EVOLUTION_AGENT 旁路已消费risk_score
    # (异步,不阻塞上面的主链路)

    # 输出
    from mq_bus import SINGLE_TASK_TIMEOUT_MS
    stats = bus.stats()
    final = None
    if decisions:
        final = decisions[-1].get("payload", decisions[-1])

    result = {
        "stock_code": code,
        "pipeline_complete": True,
        "pipeline_type": "微服务流水线并行解耦 + 消息队列异步流转",
        "services": list(SERVICE_MAP.keys()),
        "messages_produced": stats["produced"],
        "messages_consumed": stats["consumed"],
        "dlq_count": stats["dlq_count"],
        "final_decision": final,
        "pipeline_version": "1.0.0",
    }

    if final:
        logging.info(f"\n{'='*55}")
        logging.info(f"  ✅ 全链路完成 [{code}]")
        logging.info(f"  风险等级: {final.get('risk_tier', 'N/A')}")
        logging.info(f"  风险评分: {final.get('risk_score', 'N/A')}")
        logging.info(f"  指令: {final.get('order_instruction', '')[:80]}")
        logging.info(f"{'='*55}")

    return result


SERVICE_MAP = {
    "SIGNAL_EXTRACT": "service_signal_extract.py",
    "MISJUDGE_MATCH": "service_misjudge_match.py",
    "RULE_SCORE_ENGINE": "service_rule_score_engine.py",
    "POSITION_DECISION": "service_position_decision.py",
    "EVOLUTION_AGENT": "service_evolution_agent.py",
}


def mq_service_status() -> dict:
    """查询微服务运行状态。"""
    from mq_bus import get_bus
    bus = get_bus()
    stats = bus.stats()
    return {
        "services": list(SERVICE_MAP.keys()),
        "service_count": len(SERVICE_MAP),
        "topic_depth": stats["topics_depth"],
        "consumers_count": stats["consumers_count"],
        "dlq": stats["dlq_count"],
        "idempotent": stats["idempotent_cache_size"],
        "timeout": stats["timeout"],
    }


# ===================== 全链路测试 =====================

def run_quick_test():
    """快速全链路测试(回暖环境)。"""
    from mq_bus import reset_bus

    reset_bus()
    register_all_services()

    # 模拟标的: 杉杉股份 回暖环境
    stock_data = {
        "name": "杉杉股份",
        "ma5": 12.5, "ma20": 11.8, "ma20_slope": 2.3,
        "ma20_trend": True, "ma20_flat_or_up": True,
        "gold_cross": True, "dead_cross": False,
        "above_ma20": True, "vol_ratio": 1.8, "vol_ok": True,
        "close": 12.6,
        "pe_ttm": 18.5, "pb": 1.2, "roe": 12.5, "profit_growth": 35.0,
        "rsi": 62, "boll_position": 0.65, "macd_signal": "金叉",
        "psy_hit_codes": [],
    }
    m02 = {
        "sentiment_label": "recovery",
        "signal_520_weight": 0.4,
        "has_massacre": False,
        "bear_market_override": False,
    }
    m03 = {
        "main_line": "固态电池",
        "driver_type": "业绩",
        "driver_validity": "长期(≥6月)",
    }

    result = run_one_stock(
        stock_code="600884.SH",
        stock_data=stock_data,
        m02_result=m02,
        m03_result=m03,
        stock_type="concept",
        current_position_pct=5.0,
    )

    print(f"\n{'='*55}")
    print(f"  全链路自测报告")
    print(f"{'='*55}")
    print(f"  标的:      {result['stock_code']}")
    print(f"  完成:      {result['pipeline_complete']}")
    print(f"  服务数:    {result['services']}")

    final = result.get("final_decision", {})
    print(f"  风险等级:  {final.get('risk_tier', '?')}")
    print(f"  风险评分:  {final.get('risk_score', '?')}")
    print(f"  仓位系数:  {final.get('decision', {}).get('coefficient', '?')}")
    print(f"  新开仓:    {final.get('decision', {}).get('new_open_allowed', '?')}")
    print(f"  强清仓:    {final.get('decision', {}).get('force_liquidate', '?')}")
    print(f"  指令:      {final.get('order_instruction', '?')[:80]}")

    status = mq_service_status()
    print(f"  话题深度:  {status['topic_depth']}")
    print(f"  DLQ:       {status['dlq']}")
    print(f"  幂等:      {status['idempotent']}")
    print(f"{'='*55}")
    return result


def run_bear_market_test():
    """熊市屏蔽测试。"""
    from mq_bus import reset_bus

    reset_bus()
    register_all_services()

    stock_data = {
        "name": "杉杉股份",
        "ma5": 10.5, "ma20": 11.8, "ma20_slope": -2.0,
        "ma20_trend": False, "ma20_flat_or_up": False,
        "gold_cross": False, "above_ma20": False,
        "close": 10.2, "vol_ratio": 0.6, "vol_ok": False,
        "psy_hit_codes": ["code_14_损失厌恶", "code_04_避免怀疑"],
    }
    m02 = {
        "sentiment_label": "ice",
        "signal_520_weight": 0.0,
        "has_massacre": True,
        "bear_market_override": True,
    }
    m03 = {
        "main_line": "",
        "driver_type": "题材",
        "driver_validity": "短期(≤2周)",
    }

    result = run_one_stock(
        stock_code="600884.SH",
        stock_data=stock_data,
        m02_result=m02,
        m03_result=m03,
        stock_type="concept",
        current_position_pct=8.0,
    )

    final = result.get("final_decision", {})
    print(f"\n{'='*55}")
    print(f"  熊市场景测试报告")
    print(f"{'='*55}")
    print(f"  风险等级:  {final.get('risk_tier', '?')}")
    print(f"  风险评分:  {final.get('risk_score', '?')}")
    print(f"  强清仓:    {final.get('decision', {}).get('force_liquidate', '?')}")
    print(f"  指令:      {final.get('order_instruction', '')[:100]}")
    print(f"{'='*55}")
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        s = mq_service_status()
        print(f"\n📋 微服务状态")
        print(f"{'='*45}")
        print(f"  注册服务: {len(s['services'])}")
        for svc in s['services']:
            print(f"    ✅ {svc}")
        print(f"  话题深度: {s['topic_depth']}")
        print(f"  消费者:   {s['consumers_count']}")
        print(f"  DLQ:      {s['dlq']}")
        print(f"  幂等:     {s['idempotent']}")
        print(f"  超时:     {s['timeout']}")
        print(f"{'='*45}")

    elif len(sys.argv) > 1 and sys.argv[1] == "bear":
        run_bear_market_test()

    else:
        # 默认: 注册服务+全链路测试
        register_all_services()
        r = run_quick_test()
