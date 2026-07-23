#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_position_decision.py — POSITION_DECISION 仓位决策分级服务

职责: 消费topic.risk.score, 根据风险等级+Lollapalooza输出交易约束指令。
同步下发至Module04定策略和Module05买卖离场模块。

输出话题: topic.order.decision

仓位系数(规格):
  GREEN:  coeff=1.0 仅遵循静态风控,正常开仓/加仓/持仓
  YELLOW: coeff=0.3 单标的上限3%,禁止新开仓/加仓,仅允许减仓
  RED:    coeff=0.0 持仓资金强制归零,强制清仓,最高优先级

Lollapalooza覆盖(§3 L3):
  中度: coeff=0.3, 允许滚动,不强制清仓(覆盖YELLOW默认)
  重度: coeff=0.0, 强制清仓(覆盖RED默认,最高优先级)
"""

import logging
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [POS_DEC] %(message)s",
    datefmt="%H:%M:%S",
)

POSITION_COEFF_MAP = {
    "GREEN": {
        "coefficient": 1.0,
        "constraint": "仅遵循全局静态风控，无单标的额外资金限制",
        "action": "正常开仓、加仓、持仓",
        "per_stock_max_pct": None,
        "new_open_allowed": True,
        "add_position_allowed": True,
        "force_liquidate": False,
    },
    "YELLOW": {
        "coefficient": 0.3,
        "constraint": "单标的占用总资金上限3%",
        "action": "禁止新开仓、禁止加仓，仅允许减仓",
        "per_stock_max_pct": 3,
        "new_open_allowed": False,
        "add_position_allowed": False,
        "force_liquidate": False,
    },
    "RED": {
        "coefficient": 0.0,
        "constraint": "持仓资金强制归零",
        "action": "强制清仓，禁止新建任何头寸，优先级最高",
        "per_stock_max_pct": 0,
        "new_open_allowed": False,
        "add_position_allowed": False,
        "force_liquidate": True,
    },
}


def decide_position(risk_score: float, risk_tier: str,
                    stock_code: str = "",
                    stock_name: str = "",
                    current_position_pct: float = 0,
                    lollapalooza_override: dict = None,
                    lollapalooza_level: str = "无") -> dict:
    """POSITION_DECISION 主入口，支持Lollapalooza仓位覆盖。

    Lollapalooza覆盖(优先级高于等级默认):
      中度: coefficient=0.3, force_liquidate=False, 允许滚动
      重度: coefficient=0.0, force_liquidate=True, 强制清仓
    """
    logging.info(f"  📋 POSITION_DECISION [{stock_code}] tier={risk_tier} "
                 f"score={risk_score} lolla={lollapalooza_level}")

    # 基础等级配置
    cfg = dict(POSITION_COEFF_MAP.get(risk_tier, POSITION_COEFF_MAP["GREEN"]))

    # Lollapalooza覆盖
    if lollapalooza_override:
        lolla_coeff = lollapalooza_override.get("coefficient")
        lolla_liquidate = lollapalooza_override.get("force_liquidate", False)
        if lolla_coeff is not None:
            cfg["coefficient"] = lolla_coeff
            cfg["force_liquidate"] = lolla_liquidate
            cfg["new_open_allowed"] = lollapalooza_override.get("new_open_allowed", False)
            cfg["lollapalooza_applied"] = True
            cfg["lollapalooza_note"] = lollapalooza_override.get("note", f"Lollapalooza{lollapalooza_level}")
        if lollapalooza_level == "中度":
            cfg["action"] = "中度Lollapalooza: 上限系数0.3, 禁止重仓新建, 允许滚动, 不强制清仓"
        elif lollapalooza_level == "重度":
            cfg["action"] = "重度Lollapalooza: 系数强制0.0, 禁止新建任何头寸, 强制清仓"
    else:
        cfg["lollapalooza_applied"] = False

    # 计算建议仓位
    suggested = round(current_position_pct * cfg["coefficient"], 1)

    if cfg.get("force_liquidate"):
        instruction = (
            f"🚫 强制清仓 {stock_code}({stock_name}): "
            f"风险等级{risk_tier}, Lollapalooza{lollapalooza_level}, "
            f"全部持仓归零"
        )
    elif not cfg["new_open_allowed"]:
        instruction = (
            f"🟡 禁止新开仓/加仓 {stock_code}: "
            f"风险等级{risk_tier}, Lollapalooza{lollapalooza_level}, "
            f"仅允许减仓至{suggested}%"
        )
    else:
        instruction = (
            f"🟢 正常执行 {stock_code}: "
            f"风险等级{risk_tier}, 仓位系数{cfg['coefficient']}"
        )

    decision = {
        **cfg,
        "risk_score": risk_score,
        "risk_tier": risk_tier,
        "lollapalooza_level": lollapalooza_level,
    }

    result = {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "risk_tier": risk_tier,
        "risk_score": risk_score,
        "lollapalooza_level": lollapalooza_level,
        "decision": decision,
        "current_position_pct": current_position_pct,
        "suggested_position_pct": suggested,
        "order_instruction": instruction,
    }
    logging.info(f"  → {instruction[:80]}...")
    return result


# ===================== MQ消息处理器 =====================

def handle_position_decision(msg: dict) -> Optional[dict]:
    from mq_bus import get_bus
    payload = msg.get("payload", {})
    stock_code = msg.get("stock_code", "")
    task_uuid = msg.get("task_uuid", "")

    final_score = payload.get("final_risk_score", 50)
    risk_tier = payload.get("risk_tier", "YELLOW")
    lolla_level = payload.get("lollapalooza_level", "无")
    lolla_override = payload.get("lollapalooza_override")
    current_pos = payload.get("current_position_pct", 0)

    result = decide_position(final_score, risk_tier, stock_code,
                              current_position_pct=current_pos,
                              lollapalooza_override=lolla_override,
                              lollapalooza_level=lolla_level)
    result["source_risk_msg_id"] = msg.get("msg_id")

    bus = get_bus()
    bus.produce_from_service(
        "topic.order.decision", "POSITION_DECISION",
        stock_code, result,
        task_uuid=task_uuid,
    )
    return result


if __name__ == "__main__":
    # 自测: 三个等级 + Lollapalooza覆盖
    for tier in ["GREEN", "YELLOW", "RED"]:
        r = decide_position(
            risk_score={"GREEN": 30, "YELLOW": 65, "RED": 90}[tier],
            risk_tier=tier,
            stock_code="600884.SH",
            stock_name="杉杉股份",
            current_position_pct=10.0,
        )
        print(f"  {tier:6s}: coeff={r['decision']['coefficient']} "
              f"new_open={r['decision']['new_open_allowed']} "
              f"suggested={r['suggested_position_pct']}% "
              f"lolla={r['lollapalooza_level']}")

    # 自测: 中度Lollapalooza覆盖
    r = decide_position(
        risk_score=65, risk_tier="YELLOW",
        stock_code="600884.SH", stock_name="杉杉股份",
        current_position_pct=10.0,
        lollapalooza_override={"coefficient": 0.3, "force_liquidate": False,
                                "new_open_allowed": False, "note": "中度Lollapalooza"},
        lollapalooza_level="中度",
    )
    print(f"  {'中度':6s}: coeff={r['decision']['coefficient']} "
          f"force_liquidate={r['decision']['force_liquidate']} "
          f"action={r['order_instruction'][:60]}")

    # 自测: 重度Lollapalooza覆盖
    r = decide_position(
        risk_score=88, risk_tier="RED",
        stock_code="600884.SH", stock_name="杉杉股份",
        current_position_pct=10.0,
        lollapalooza_override={"coefficient": 0.0, "force_liquidate": True,
                                "new_open_allowed": False, "note": "重度Lollapalooza"},
        lollapalooza_level="重度",
    )
    print(f"  {'重度':6s}: coeff={r['decision']['coefficient']} "
          f"force_liquidate={r['decision']['force_liquidate']} "
          f"action={r['order_instruction'][:60]}")
