#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_rule_score_engine.py — RULE_SCORE_ENGINE 四层联动规则打分服务【核心链路】

规格映射:
  3.RULE_SCORE_ENGINE: 四层联动规则打分引擎【核心链路】
  L0: 宏观对冲系数(0.7/1.0/1.3)
  L1: Rule021双分支基础打分+阶梯加分+兑现对冲+赛道雷区+宏观修正
  L2: 双赛道动态权重矩阵 + 时效衰减 + 正向对冲 + FAISS修正 + 降噪自愈
  L3: Lollapalooza分级判定(中度/重度)
"""

import logging
from typing import Optional
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RULE_ENG] %(message)s",
    datefmt="%H:%M:%S",
)

# L1核心引擎
from rule021_dual_branch import Rule021DualBranchEngine
from service_fault_degradation import (
    apply_degradation_before_scoring,
    apply_degradation_after_l1,
    get_degrade_risk_override,
    reset_degradation,
)
from service_factor_drift import (
    get_drift_monitor,
    run_drift_check,
    RISK_SIGNAL_17,
)
from service_shap_trace import build_and_log_shap, get_shap_logger
from service_faiss_memory import get_faiss, build_feature_vector
from service_weight_dispatch import get_dispatch

_rule021_engine = Rule021DualBranchEngine()


def run_l0_macro(matched_result: dict, macro_data: dict = None) -> dict:
    """L0: 宏观对冲校验层。

    规格:
        positive_macro_coeff: 0.7
        neutral_macro_coeff: 1.0
        bearish_macro_coeff: 1.3
        factor: [大宗商品价格中枢, 货币政策松紧, 战略资源收储/投放政策]
    """
    if not macro_data:
        return {"coefficient": 1.0, "macro_status": "neutral",
                "verdict": "无宏观数据→系数1.0", "label": "neutral"}

    commodity = macro_data.get("commodity_price", "neutral")
    monetary = macro_data.get("monetary_policy", "neutral")
    reserve = macro_data.get("reserve_policy", "neutral")

    factors = [commodity, monetary, reserve]
    bearish_count = sum(1 for f in factors if f == "bearish")
    positive_count = sum(1 for f in factors if f == "positive")

    if bearish_count >= 2:
        macro_status = "bearish"
        coeff = 1.3
        label = "bearish"
        verdict = "宏观偏空: 多因子利空"
    elif positive_count >= 2:
        macro_status = "positive"
        coeff = 0.7
        label = "positive"
        verdict = "宏观偏多: 多因子利好"
    else:
        macro_status = "neutral"
        coeff = 1.0
        label = "neutral"
        verdict = "宏观中性"

    neg_err = matched_result.get("total_negative_error", 0)
    if neg_err >= 30:
        coeff = min(coeff * 1.2, 1.3)
        verdict += " (负误差≥30→系数上浮)"

    return {
        "coefficient": round(coeff, 2),
        "macro_status": macro_status,
        "label": label,
        "verdict": verdict,
        "factors": {"commodity": commodity, "monetary": monetary, "reserve": reserve},
        "bearish_count": bearish_count,
        "positive_count": positive_count,
    }


def run_l1_rule021(matched_result: dict, l0_result: dict,
                   stock_type: str = "concept") -> dict:
    """L1: Rule021基础打分 — 委托 Rule021DualBranchEngine 核心引擎。"""
    bias_count = matched_result.get("bias_count", 0)
    neg_err = matched_result.get("total_negative_error", 0)
    macro_status = l0_result.get("macro_status", "neutral")
    track_type = {"resource": "cycle_stock", "bluechip": "blue_chip",
                  "concept": "theme_stock"}.get(stock_type, "theme_stock")

    bias_codes = matched_result.get("matched_bias_codes", [])
    dim_scores = [
        min(10, bias_count * 2.0) if any("简单联想" in c for c in bias_codes) else 2.0,
        min(10, bias_count * 1.8) if any("社会认同" in c for c in bias_codes) else 2.0,
        min(10, bias_count * 1.5) if any("废话" in c for c in bias_codes) else 2.0,
        min(10, neg_err / 5) if any(c in " ".join(bias_codes) for c in ["_04_", "_14_"]) else 2.0,
        min(10, bias_count * 1.6) if any("过度乐观" in c for c in bias_codes) else 2.0,
    ]
    is_danger = neg_err >= 25

    payload = {
        "stock_code": matched_result.get("stock_code", ""),
        "track_type": track_type,
        "dim_scores": dim_scores,
        "real_announcement_count": 0,
        "is_danger_track": is_danger,
        "macro_status": macro_status,
    }
    engine_result = _rule021_engine.run_rule021_calc(payload)

    if engine_result.get("status") != "success":
        logging.warning(f"  ⚠️ Rule021引擎返回错误: {engine_result.get('message','')}")
        return {
            "status": "fail",
            "error": engine_result.get("message", "Rule021引擎未知错误"),
            "stock_code": engine_result.get("stock_code", ""),
            "branch": "Rule021-1题材概念股" if stock_type == "concept" else "Rule021-2周期资源股",
            "stock_type": stock_type,
            "base_score": 0,
            "macro_adjusted_score": 0,
            "dimensions": {"bias_count_score": min(10, bias_count),
                           "high_risk_dimensions": 0,
                           "negative_error_total": neg_err},
        }

    return {
        "status": "success",
        "branch": "Rule021-1题材概念股" if stock_type == "concept" else "Rule021-2周期资源股",
        "stock_type": stock_type,
        "track_type": track_type,
        "base_score": engine_result["L1_final_score"],
        "dim_scores_raw": dim_scores,
        "dim_sum": engine_result["base_sum"],
        "high_risk_count": engine_result["high_risk_count"],
        "step_bonus": engine_result["ladder_add"],
        "announcement_deduct": engine_result["deduct_total"],
        "track_multiplier": engine_result["track_coeff"],
        "macro_coefficient": engine_result["macro_coeff"],
        "macro_adjusted_score": engine_result["L1_final_score"],
        "l1_detail": engine_result,
        "dimensions": {
            "bias_count_score": min(10, bias_count),
            "high_risk_dimensions": engine_result["high_risk_count"],
            "negative_error_total": neg_err,
        },
    }


def run_l2_weighted(l1_result: dict, matched_result: dict,
                    faiss_adjustment: dict = None,
                    stock_type: str = "concept") -> dict:
    """§3 L2【核心升级】动态加权运算层。

    四大机制同时生效:
      1. 双赛道独立动态权重矩阵 (通过 §5.4 调度)
      2. 信号时效衰减函数
      3. 正向对冲机制
      4. FAISS同类历史案例风险权重修正
    """
    base = l1_result.get("macro_adjusted_score", 0)
    neg_err = matched_result.get("total_negative_error", 0)
    bias_codes = matched_result.get("matched_bias_codes", [])
    bias_count = matched_result.get("bias_count", 0)

    # 1. 获取动态权重矩阵(§5.4)
    dispatch = get_dispatch()
    weights = dispatch.get_dynamic_weights(stock_type)

    # 2. 时效衰减(§5.4)
    #   - 短期情绪信号按short_term衰减
    #   - 基本面/周期按medium_term衰减
    short_decay = dispatch.get_decay_factor("short_term", elapsed_hours=2)
    medium_decay = dispatch.get_decay_factor("medium_term", elapsed_hours=4)

    # 3. 双赛道权重加权基础分
    #    权重矩阵影响bias→score映射
    main_w = weights["main_dim_weights"]
    #    取前三个维度的加权均值作为基础调整
    if bias_count > 0:
        weighted_bias = bias_count * (
            main_w.get("policy_catalyst", 0.20) * 0.3 +
            main_w.get("sector_heat", 0.20) * 0.3 +
            main_w.get("fund_stability", 0.20) * 0.4
        ) if stock_type == "concept" else bias_count * (
            main_w.get("commodity_3y_percentile", 0.25) * 0.4 +
            main_w.get("capacity_utilization", 0.20) * 0.3 +
            main_w.get("pe_historical_percentile", 0.20) * 0.3
        )
    else:
        weighted_bias = 0

    # 带权重的原始分
    weighted_base = base + weighted_bias * 1.5
    weighted_base = min(100, weighted_base)

    # 4. 同源信号降噪
    if neg_err >= 30:
        noise_reduced = weighted_base * 1.1
    else:
        noise_reduced = weighted_base

    # 5. FAISS同类历史案例风险权重修正
    faiss_coeff = 1.0
    faiss_note = ""
    if faiss_adjustment:
        faiss_coeff = faiss_adjustment.get("coefficient", 1.0)
        faiss_note = faiss_adjustment.get("note", "")
    score_with_faiss = noise_reduced * faiss_coeff

    # 6. 正向对冲(§5.4)
    #    正向信号来自: matched_result中的positive_signals或外部
    positive_signals = matched_result.get("positive_signals", [])
    if stock_type == "concept":
        # 题材: 检查政策利好信号
        if any("政策利好" in c for c in bias_codes):
            positive_signals = positive_signals or [5.0]
    else:
        # 周期: 检查供需反转信号
        if any("供需" in c for c in bias_codes):
            positive_signals = positive_signals or [4.0]

    hedge_result = dispatch.apply_hedge(score_with_faiss, positive_signals)
    hedged_score = hedge_result["hedged_score"]

    # 7. 衰减后总分
    total = round(hedged_score * short_decay * medium_decay, 1)
    total = min(total, 100)

    return {
        "base_score": base,
        "weighted_base": round(weighted_base, 1),
        "weight_info": {
            "stock_type": stock_type,
            "main_dim_weights": main_w,
        },
        "decay_factor": round(short_decay * medium_decay, 3),
        "short_decay": round(short_decay, 3),
        "medium_decay": round(medium_decay, 3),
        "noise_reduced_score": round(noise_reduced, 1),
        "faiss_adjustment": faiss_coeff,
        "faiss_note": faiss_note,
        "hedge": hedge_result,
        "total_weighted_score": total,
        "neg_error_weight": neg_err,
        "positive_signals_count": len(positive_signals),
    }


def run_l3_lollapalooza(l2_result: dict, matched_result: dict,
                        final_score: float, risk_tier: str) -> dict:
    """§3 L3【核心升级】Lollapalooza共振分级判定。

    区分中度、重度两级共振，差异化风控:
      1. 中度 Lollapalooza:
         - bias_count ≥ 4 + 总分在YELLOW区间
         - 仓位上限系数0.3; 禁止重仓新建; 允许滚动,不强制清仓
      2. 重度 Lollapalooza:
         - bias_count ≥ 6 + 总分在RED区间
         - 系数强制0.0; 禁止任何新头寸; 强制清仓
    """
    bias_count = matched_result.get("bias_count", 0)
    neg_err = matched_result.get("total_negative_error", 0)
    bias_codes = matched_result.get("matched_bias_codes", [])

    total = l2_result.get("total_weighted_score", 0)

    # ——— 计算最终偏离分 ———
    # 综合bias_count负向信号数量 & matched_bias_codes重叠度
    optimism_count = sum(1 for c in bias_codes if "过度乐观" in c or "简单联想" in c)

    lolla_level = "无"
    lolla_action = ""
    lolla_reason = ""
    position_override = None

    # —— 判定: 重度(≥6 + RED) ——
    if bias_count >= 6 and risk_tier == "RED":
        lolla_level = "重度"
        lolla_action = "系数强制0.0; 禁止新建任何头寸; 强制清仓"
        lolla_reason = (
            f"重度Lollapalooza: bias={bias_count}≥6 + {risk_tier}区间"
        )
        position_override = {
            "coefficient": 0.0,
            "force_liquidate": True,
            "new_open_allowed": False,
            "note": "重度Lollapalooza: 强制清仓",
        }

    # —— 判定: 中度(≥4 + YELLOW) ——
    elif bias_count >= 4 and risk_tier == "YELLOW":
        lolla_level = "中度"
        lolla_action = (
            "仓位上限系数0.3; 禁止新建重仓; 原有持仓允许滚动, 不强制清仓"
        )
        lolla_reason = (
            f"中度Lollapalooza: bias={bias_count}≥4 + {risk_tier}区间"
        )
        position_override = {
            "coefficient": 0.3,
            "force_liquidate": False,
            "new_open_allowed": False,
            "per_stock_max_pct": 3,
            "note": "中度Lollapalooza: 上限系数0.3, 禁止重仓",
        }

    # —— 无共振 ——
    else:
        lolla_level = "无"
        lolla_action = "正常处理, 无需Lollapalooza干预"
        lolla_reason = (
            f"bias={bias_count} + tier={risk_tier} → 未达共振阈值"
        )

    return {
        "original_score": total,
        "final_score": total,  # L3不改分值, 只改仓位约束
        "bias_count": bias_count,
        "neg_error_total": neg_err,
        "optimism_bias_count": optimism_count,
        "lollapalooza_level": lolla_level,
        "lollapalooza_action": lolla_action,
        "lollapalooza_reason": lolla_reason,
        "lollapalooza_override": position_override,
    }


def run_rule_score_engine(matched_result: dict,
                           stock_type: str = "concept",
                           macro_data: dict = None) -> dict:
    """RULE_SCORE_ENGINE 主入口: L0→L1→L2→L3 全链路。

    返回:
        {final_risk_score, risk_tier, lollapalooza_level, l0, l1, l2, l3, ...}
    """
    logging.info("  🎯 RULE_SCORE_ENGINE 启动")

    # §1.2 因子漂移监控节点 (Step3前置执行, 不阻断交易)
    drift_signals = {}
    if matched_result:
        neg_err = matched_result.get("total_negative_error", 0)
        bias_cnt = matched_result.get("bias_count", 0)
        drift_signals = {
            "sentiment_score": min(10, bias_cnt),
            "fund_net_inflow": min(10, neg_err / 5),
            "large_order_ratio": min(10, len(matched_result.get("matched_bias_codes", [])) * 1.2),
        }
        for name, val in drift_signals.items():
            get_drift_monitor().record_signal(name, val)
    drift_events = get_drift_monitor().run_daily_check()
    if drift_events:
        get_drift_monitor().push_alert(drift_events)
        logging.warning(f"  ⚠️ §1.2因子漂移: {len(drift_events)}项(仅预警,不阻断)")

    # L0: 宏观对冲
    l0 = run_l0_macro(matched_result, macro_data)
    logging.info(f"  L0宏观: coeff={l0['coefficient']} status={l0['macro_status']} ({l0['label']})")

    # 故障降级探测 (在L0之后L1之前)
    degrade_before = apply_degradation_before_scoring(
        rag_available=macro_data.get("rag_available", True) if macro_data else True,
        financial_available=macro_data.get("financial_available", True) if macro_data else True,
        multi_module_failure=macro_data.get("multi_module_failure", False) if macro_data else False,
    )
    if degrade_before["current_level"] > 0:
        logging.warning(f"  ⚠️ 当前降级等级: {degrade_before['level_desc']}")

    # L1: Rule021基础打分 (委托引擎)
    l1 = run_l1_rule021(matched_result, l0, stock_type)
    if l1.get("status") != "success":
        logging.error(f"  ❌ L1 Rule021引擎异常: {l1.get('error','')}")
        return {
            "status": "fail", "error": f"L1 Rule021引擎: {l1.get('error','')}",
            "stock_code": l1.get("stock_code", ""),
            "l0": l0, "l1": l1, "final_risk_score": 0, "risk_tier": "UNKNOWN",
            "lollapalooza_level": "无",
            "degrade_level": degrade_before.get("current_level", 0),
        }
    logging.info(f"  L1 Rule021: base={l1['base_score']} → macro_adj={l1['macro_adjusted_score']}")

    # L1后降级判定
    degrade_after = apply_degradation_after_l1(l1)
    skip_l2 = degrade_after.get("skip_l2", False)
    skip_l3 = degrade_after.get("skip_l3", False)
    force_static = degrade_after.get("force_static_risk", False)

    if force_static:
        final_score = 85
        tier = "RED"
        logging.warning(f"  🚨 三级降级: 强制RED, 得分={final_score}")
        return {
            "status": "success",
            "final_risk_score": final_score,
            "risk_tier": tier,
            "lollapalooza_level": "无",
            "l0": l0, "l1": l1, "l2": {}, "l3": {},
            "degrade_level": 3,
            "degrade_note": degrade_after.get("degrade_note", ""),
            "force_static_risk": True,
            "stock_code": l1.get("stock_code", ""),
        }

    # FAISS同类历史案例风险权重修正(异步,不阻塞)
    faiss_adj = None
    try:
        fv = build_feature_vector(l1, matched_result)
        faiss_adj = get_faiss().calc_risk_adjustment(fv,
                     track_type=l1.get("track_type", "theme_stock"))
    except Exception as e:
        logging.warning(f"  ⚠️ FAISS修正异常(降级跳过): {e}")
        faiss_adj = None

    # L2: 动态加权运算层
    if not skip_l2:
        l2 = run_l2_weighted(l1, matched_result,
                             faiss_adjustment=faiss_adj,
                             stock_type=stock_type)
        logging.info(f"  L2加权: {l2['base_score']}→{l2['total_weighted_score']} "
                     f"(decay={l2['decay_factor']}, faiss={l2.get('faiss_adjustment',1.0)}, "
                     f"hedge={l2['hedge']['hedge_ratio']})")
    else:
        l2 = {"base_score": l1["base_score"],
              "total_weighted_score": l1["base_score"],
              "decay_factor": 1.0,
              "weight_info": {},
              "hedge": {"hedge_ratio": 0, "hedged_score": l1["base_score"]},
              "degrade_skipped": True}
        logging.info(f"  ⏭️ 一级降级: 跳过L2, 直接使用L1={l1['base_score']}")

    # 赛道差异化阈值判定(三色等级)
    l2_score = l2["total_weighted_score"]
    if stock_type == "resource":
        y_low, r_line = 60, 80
    elif stock_type == "bluechip":
        y_low, r_line = 70, 90
    else:
        y_low, r_line = 50, 75

    if l2_score >= r_line:
        tier = "RED"
    elif l2_score >= y_low:
        tier = "YELLOW"
    else:
        tier = "GREEN"

    # L3: Lollapalooza共振分级判定
    if not skip_l3:
        l3 = run_l3_lollapalooza(l2, matched_result, l2_score, tier)
        logging.info(f"  L3 Lollapalooza: bias={l3['bias_count']} "
                     f"level={l3['lollapalooza_level']} tier={tier}")
    else:
        l3 = {"original_score": l2_score, "final_score": l2_score,
              "lollapalooza_level": "无",
              "lollapalooza_action": "降级跳过",
              "lollapalooza_override": None,
              "degrade_skipped": True}
        logging.info(f"  ⏭️ 降级模式: 跳过L3 Lollapalooza")

    final_score = l3["final_score"]
    final_tier = tier

    # Lollapalooza覆盖: 如果L3有仓位覆盖, 调整最终等级/系数标记
    lolla_override = l3.get("lollapalooza_override")
    lolla_level = l3.get("lollapalooza_level", "无")

    # SHAP全链路溯源日志 (§6)
    stock_code_final = l1.get("stock_code", "")
    run_mode = macro_data.get("run_mode", "eod_full") if macro_data else "eod_full"
    trade_date = (macro_data.get("trade_date", datetime.now().strftime("%Y%m%d"))
                  if macro_data else datetime.now().strftime("%Y%m%d"))
    tier_reason = f"{stock_type}得分{final_score}≥{r_line if final_score>=r_line else y_low} → {final_tier}"
    if lolla_level != "无":
        tier_reason += f" | Lollapalooza:{lolla_level}({l3.get('lollapalooza_reason','')})"

    try:
        build_and_log_shap(
            stock_code=stock_code_final or "unknown",
            run_mode=run_mode,
            trade_date=trade_date,
            l0_result=l0,
            l1_result=l1,
            l2_result=l2,
            l3_result=l3,
            faiss_adjustment=faiss_adj,
            final_score=final_score,
            risk_tier=final_tier,
            tier_reason=tier_reason,
            lollapalooza_level=lolla_level,
        )
    except Exception as e:
        logging.warning(f"  ⚠️ SHAP日志异常(不阻断): {e}")

    result = {
        "final_risk_score": final_score,
        "risk_tier": final_tier,
        "lollapalooza_level": lolla_level,
        "lollapalooza_override": lolla_override,
        "l0": l0,
        "l1": l1,
        "l2": l2,
        "l3": l3,
    }
    logging.info(f"  ✅ RULE_SCORE_ENGINE完成: score={final_score} tier={final_tier} "
                 f"lolla={lolla_level}")
    return result


# ===================== MQ消息处理器 =====================

def handle_rule_score_engine(msg: dict) -> Optional[dict]:
    """MQ消息处理器: 消费matched_signal,生产risk_score。"""
    from mq_bus import get_bus
    payload = msg.get("payload", {})
    stock_code = msg.get("stock_code", "")
    task_uuid = msg.get("task_uuid", "")

    stock_type = payload.get("stock_type", "concept")
    macro_data = {
        "commodity_price": payload.get("commodity", "neutral"),
        "monetary_policy": payload.get("monetary", "neutral"),
        "reserve_policy": payload.get("reserve", "neutral"),
    }

    result = run_rule_score_engine(payload, stock_type, macro_data)
    result["stock_code"] = stock_code
    result["source_matched_msg_id"] = msg.get("msg_id")

    bus = get_bus()

    if result.get("status") == "fail":
        logging.error(f"  🗑️ RULE_SCORE_ENGINE失败→DLQ: {stock_code} {result.get('error','')}")
        bus.dlq.append({
            "msg_id": msg.get("msg_id"),
            "stock_code": stock_code,
            "service": "RULE_SCORE_ENGINE",
            "error": result.get("error", ""),
            "payload": result,
            "timestamp": __import__("datetime").datetime.now().strftime("%H:%M:%S"),
        })
        return None

    bus.produce_from_service(
        "topic.risk.score", "RULE_SCORE_ENGINE",
        stock_code, result,
        task_uuid=task_uuid,
    )
    return result


if __name__ == "__main__":
    # 自测
    test_matched = {
        "matched_bias_codes": ["code_13_过度乐观", "code_10_简单联想",
                               "code_15_社会认同羊群", "code_08_嫉妒猜忌",
                               "code_14_损失厌恶"],
        "bias_count": 5,
        "total_negative_error": 35,
        "negative_error": {
            "details": [
                {"code": "code_13", "severity": 7},
            ]
        },
    }
    result = run_rule_score_engine(test_matched, "concept")
    print(f"  ✅ 最终分: {result['final_risk_score']} tier={result['risk_tier']} "
          f"lolla={result['lollapalooza_level']}")
    print(f"  ✅ L2 decay={result['l2']['decay_factor']} hedge_ratio={result['l2']['hedge']['hedge_ratio']}")
    print(f"  ✅ L3 override={result['lollapalooza_override']}")
