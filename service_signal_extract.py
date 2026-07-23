#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_signal_extract.py — SIGNAL_EXTRACT 信号提取微服务

职责: 从M00-M05模块输出中提取原始特征,标准化五类信号:
  技术面, 基本面, 情绪面, 指标面, 宏观面

输出话题: topic.signal.raw

规格映射:
  1.SIGNAL_EXTRACT → 输出原始特征 topic.signal.raw
"""

import logging
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SIG_EXT] %(message)s",
    datefmt="%H:%M:%S",
)


def extract_tech_signals(stock_data: dict) -> dict:
    """从M00行情数据提取技术面信号。"""
    return {
        "ma5": stock_data.get("ma5"),
        "ma20": stock_data.get("ma20"),
        "ma20_slope": stock_data.get("ma20_slope"),
        "ma20_trend": stock_data.get("ma20_trend"),
        "ma20_flat_or_up": stock_data.get("ma20_flat_or_up"),
        "gold_cross": stock_data.get("gold_cross"),
        "dead_cross": stock_data.get("dead_cross"),
        "above_ma20": stock_data.get("above_ma20"),
        "vol_ratio": stock_data.get("vol_ratio"),
        "vol_ok": stock_data.get("vol_ok"),
        "close": stock_data.get("close"),
    }


def extract_fundamental_signals(stock_data: dict) -> dict:
    """从Tushare基本面数据提取。"""
    return {
        "pe_ttm": stock_data.get("pe_ttm"),
        "pb": stock_data.get("pb"),
        "roe": stock_data.get("roe"),
        "profit_growth": stock_data.get("profit_growth"),
        "debt_ratio": stock_data.get("debt_ratio"),
        "revenue_growth": stock_data.get("revenue_growth"),
    }


def extract_sentiment_signals(m02_result: dict, m03_result: dict) -> dict:
    """从M02/M03提取情绪面信号。"""
    return {
        "sentiment_label": (m02_result or {}).get("sentiment_label", ""),
        "sentiment_cn": (m02_result or {}).get("sentiment_cn", ""),
        "has_massacre": (m02_result or {}).get("has_massacre", False),
        "signal_520_weight": (m02_result or {}).get("signal_520_weight", 1.0),
        "bear_market_override": (m02_result or {}).get("bear_market_override", False),
        "main_line": (m03_result or {}).get("main_line", ""),
        "driver_type": (m03_result or {}).get("driver_type", ""),
        "driver_validity": (m03_result or {}).get("driver_validity", ""),
    }


def extract_indicator_signals(stock_data: dict) -> dict:
    """提取技术指标面信号。"""
    return {
        "rsi": stock_data.get("rsi"),
        "boll_position": stock_data.get("boll_position"),
        "macd_signal": stock_data.get("macd_signal"),
        "kdj_signal": stock_data.get("kdj_signal"),
    }


def extract_macro_signals(macro_data: dict) -> dict:
    """从L0宏观数据提取宏观面信号。"""
    if not macro_data:
        return {}
    return {
        "commodity_price": macro_data.get("commodity_price"),
        "monetary_policy": macro_data.get("monetary_policy"),
        "reserve_policy": macro_data.get("reserve_policy"),
        "shibor_1w": macro_data.get("shibor_1w"),
        "market_sentiment": macro_data.get("market_sentiment"),
        "industry_flow": macro_data.get("industry_flow"),
    }


def run_signal_extract(
    stock_code: str,
    stock_data: dict,
    m02_result: dict = None,
    m03_result: dict = None,
    macro_data: dict = None,
) -> dict:
    """SIGNAL_EXTRACT 主入口: 提取五类原始信号。

    参数:
        stock_code: 标的代码
        stock_data: dict, 含行情/基本面/指标数据
        m02_result: Module02输出
        m03_result: Module03输出
        macro_data: 宏观数据

    返回:
        {
            "stock_code": str,
            "stock_name": str,
            "signal_type": "raw",
            "signals": {
                "tech": {...},
                "fundamental": {...},
                "sentiment": {...},
                "indicator": {...},
                "macro": {...},
            },
            "raw_feature_count": int,
        }
    """
    logging.info(f"  📡 SIGNAL_EXTRACT [{stock_code}]")

    signals = {
        "tech": extract_tech_signals(stock_data),
        "fundamental": extract_fundamental_signals(stock_data),
        "sentiment": extract_sentiment_signals(m02_result, m03_result),
        "indicator": extract_indicator_signals(stock_data),
        "macro": extract_macro_signals(macro_data),
    }

    count = sum(1 for k, v in signals.items() if v)
    result = {
        "stock_code": stock_code,
        "stock_name": stock_data.get("name", ""),
        "signal_type": "raw",
        "signals": signals,
        "raw_feature_count": count,
    }
    logging.info(f"  ✅ 五类信号提取完成: {count}/5类, "
                  f"tech={bool(signals['tech'])} "
                  f"fund={bool(signals['fundamental'])} "
                  f"senti={bool(signals['sentiment'])} "
                  f"ind={bool(signals['indicator'])} "
                  f"macro={bool(signals['macro'])}")
    return result


# ===================== MQ消息处理器 =====================

def handle_signal_extract(msg: dict) -> Optional[dict]:
    """MQ消息处理器: 接收信号提取请求,生产raw_signal消息。"""
    from mq_bus import get_bus
    payload = msg.get("payload", {})
    stock_code = msg.get("stock_code", "")
    task_uuid = msg.get("task_uuid", "")

    result = run_signal_extract(
        stock_code=stock_code,
        stock_data=payload.get("stock_data", {}),
        m02_result=payload.get("m02_result"),
        m03_result=payload.get("m03_result"),
        macro_data=payload.get("macro_data"),
    )

    bus = get_bus()
    bus.produce_from_service(
        "topic.signal.raw", "SIGNAL_EXTRACT",
        stock_code, result,
        task_uuid=task_uuid,
    )
    return result


if __name__ == "__main__":
    # 自测
    test_stock = {
        "name": "杉杉股份",
        "ma5": 12.5, "ma20": 11.8, "ma20_slope": 2.3,
        "ma20_trend": True, "ma20_flat_or_up": True,
        "gold_cross": True, "dead_cross": False,
        "above_ma20": True, "vol_ratio": 1.8, "vol_ok": True,
        "close": 12.6, "pe_ttm": 18.5, "pb": 1.2, "roe": 12.5,
        "profit_growth": 35.0, "debt_ratio": 45.0,
        "rsi": 62, "boll_position": 0.65, "macd_signal": "金叉",
    }
    result = run_signal_extract("600884.SH", test_stock,
                                 m02_result={"sentiment_label": "recovery",
                                             "signal_520_weight": 0.4},
                                 m03_result={"main_line": "固态电池"})
    print(f"  ✅ 信号提取: {result['raw_feature_count']}/5类")
