#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_shap_trace.py — §6 SHAP全链路可解释溯源日志规范

触发时机:
  盘中30分钟刷新、收盘全量重算 — 所有标的强制输出异步落盘

完整日志字段(9字段):
  1. 17类风险信号分项原始得分
  2. L2实时动态权重、信号时效衰减系数、正向对冲抵扣分值(扩展)
  3. FAISS历史同类案例风险上浮权重贡献
  4. L1高危阶梯加分明细
  5. 利空公告扣分项明细
  6. 赛道雷区倍率偏移量
  7. L0宏观对冲缩放偏移量
  8. Lollapalooza分级标签: 无/中度/重度(新增)
  9. 总分分层归因占比、风险等级判定完整溯源
"""

import logging
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SHAP] %(message)s",
    datefmt="%H:%M:%S",
)

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"


class ShapTraceLogger:
    def __init__(self):
        self._ensure_table()

    @staticmethod
    def _ensure_table():
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shap_trace_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT,
                    trade_date TEXT,
                    run_mode TEXT,
                    timestamp TEXT,
                    final_score REAL,
                    risk_tier TEXT,
                    lollapalooza_level TEXT DEFAULT '无',
                    trace_detail TEXT,
                    create_time TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"  ⚠️ SHAP表创建失败: {e}")

    def build_trace(self,
                    stock_code: str,
                    run_mode: str,
                    trade_date: str,
                    # §6 字段1
                    signal_raw_scores: Dict[str, float],
                    # §6 字段2 (扩展)
                    l2_detail: Dict,
                    # §6 字段3
                    faiss_adjustment: Dict = None,
                    # §6 字段4~7: L1/L0
                    l1_ladder_detail: Dict = None,
                    announcement_deduct: float = 0,
                    track_penalty_offset: float = 0,
                    l0_macro_offset: float = 0,
                    # §6 字段8 (新增)
                    lollapalooza_level: str = "无",
                    # §6 字段9
                    final_score: float = 0,
                    risk_tier: str = "GREEN",
                    tier_reason: str = "",
                    ) -> dict:
        """构建完整SHAP溯源记录(9字段)。"""
        base_score = sum(signal_raw_scores.values()) if signal_raw_scores else 0
        lad = l1_ladder_detail or {}
        ladder = lad.get("ladder_add", 0)
        deduct = announcement_deduct
        track_mult = lad.get("track_coeff", 1.0)
        macro_coeff = lad.get("macro_coeff", 1.0)

        faiss_coeff_val = (faiss_adjustment or {}).get("coefficient", 1.0)
        faiss_case_count = (faiss_adjustment or {}).get("matched_cases", 0)

        # 字段2: L2实时动态权重 + 时效衰减 + 正向对冲
        l2_base = l2_detail or {}
        l2_weights_used = l2_base.get("weight_info", {}).get("main_dim_weights", {})
        l2_decay = l2_base.get("decay_factor", 1.0)
        l2_hedge = l2_base.get("hedge", {"hedge_ratio": 0, "hedge_detail": []})
        l2_faiss_coeff = l2_base.get("faiss_adjustment", 1.0)

        final_score_val = final_score
        tier_val = risk_tier

        trace = {
            "meta": {
                "stock_code": stock_code,
                "trade_date": trade_date,
                "run_mode": run_mode,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "shap_version": "2.0.0",
            },
            "final_result": {
                "final_score": round(final_score_val, 2),
                "risk_tier": tier_val,
                "tier_reason": tier_reason,
                # §6 字段8: Lollapalooza分级标签
                "lollapalooza_level": lollapalooza_level,
            },
            # §6 字段1: 信号原始分
            "field_1_signal_raw_scores": {
                k: round(v, 2) for k, v in signal_raw_scores.items()
            },
            # §6 字段2(扩展): L2动态权重 + 衰减 + 对冲 + FAISS
            "field_2_l2_dynamic_detail": {
                "dynamic_weights_used": l2_weights_used,
                "signal_decay_factor": l2_decay,
                "positive_hedge_ratio": l2_hedge.get("hedge_ratio", 0),
                "positive_hedge_detail": l2_hedge.get("hedge_detail", []),
                "faiss_coefficient": l2_faiss_coeff,
                "positive_signals_count": l2_hedge.get("positive_signals_count", 0),
                "note": (
                    f"L2: dec={l2_decay} hedge=×{l2_hedge.get('hedge_ratio', 1.0)}"
                    f" faiss=×{l2_faiss_coeff}"
                ),
            },
            # §6 字段3: FAISS
            "field_3_faiss_risk_adjustment": {
                "coefficient": faiss_coeff_val,
                "matched_cases": faiss_case_count,
                "adjustment_pct": round((faiss_coeff_val - 1.0) * 100, 1),
                "note": (faiss_adjustment or {}).get("note", ""),
            },
            # §6 字段4~7: L1/L0逐项
            "field_4_5_6_7_layer_contributions": {
                "base_signal_sum": round(base_score, 2),
                "l1_ladder_add": ladder,
                "l1_announcement_deduct": round(deduct, 2),
                "l1_track_multiplier": track_mult,
                "l0_macro_coefficient": macro_coeff,
                "l1_track_offset": round(base_score * (track_mult - 1.0) if track_mult > 0 else 0, 2),
                "l0_macro_offset": round(l0_macro_offset, 2),
            },
            # §6 字段9: 归因占比
            "field_9_attribution_pct": {
                "signal_base_pct": round(base_score / max(final_score_val, 1) * 100, 1) if final_score_val > 0 else 0,
                "ladder_contribution_pct": round(ladder / max(final_score_val, 1) * 100, 1) if final_score_val > 0 else 0,
                "track_amplify_pct": round((track_mult - 1.0) * 100, 1) if track_mult > 1.0 else 0,
                "macro_amplify_pct": round((macro_coeff - 1.0) * 100, 1) if macro_coeff > 1.0 else
                    round((1.0 - macro_coeff) * -100, 1) if macro_coeff < 1.0 else 0,
            },
            "calc_steps": (
                f"base={base_score:.1f} + "
                f"ladder=+{ladder} - "
                f"deduct=-{deduct:.0f} = "
                f"{max(0, base_score + ladder - deduct):.1f} "
                f"×track={track_mult} "
                f"×macro={macro_coeff} "
                f"→ L2(dec={l2_decay} hedge=×{l2_hedge.get('hedge_ratio',0)}) "
                f"= {final_score_val:.1f} → {tier_val} "
                f"[lolla={lollapalooza_level}]"
            ),
        }
        return trace

    def persist_trace(self, trace: dict) -> bool:
        try:
            meta = trace.get("meta", {})
            final = trace.get("final_result", {})
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO shap_trace_log
                (stock_code, trade_date, run_mode, timestamp,
                 final_score, risk_tier, lollapalooza_level,
                 trace_detail, create_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                meta.get("stock_code", ""),
                meta.get("trade_date", ""),
                meta.get("run_mode", ""),
                meta.get("timestamp", ""),
                final.get("final_score", 0),
                final.get("risk_tier", ""),
                final.get("lollapalooza_level", "无"),
                json.dumps(trace, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logging.warning(f"  ⚠️ SHAP持久化失败: {e}")
            return False

    def log_and_persist(self, trace: dict) -> dict:
        meta = trace.get("meta", {})
        final = trace.get("final_result", {})
        logging.info(json.dumps({
            "event": "shap_trace",
            "stock_code": meta.get("stock_code", ""),
            "run_mode": meta.get("run_mode", ""),
            "final_score": final.get("final_score"),
            "risk_tier": final.get("risk_tier"),
            "lollapalooza_level": final.get("lollapalooza_level", "无"),
            "calc_steps": trace.get("calc_steps", ""),
            "attribution": trace.get("field_9_attribution_pct", {}),
        }, ensure_ascii=False))
        self.persist_trace(trace)
        return trace

    def query_trace(self, stock_code: str = None,
                    trade_date: str = None,
                    limit: int = 10) -> List[dict]:
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            conditions = []
            params = []
            if stock_code:
                conditions.append("stock_code=?")
                params.append(stock_code)
            if trade_date:
                conditions.append("trade_date=?")
                params.append(trade_date)

            where = " AND ".join(conditions) if conditions else "1=1"
            cur.execute(f"""
                SELECT stock_code, trade_date, run_mode, timestamp,
                       final_score, risk_tier, lollapalooza_level, trace_detail
                FROM shap_trace_log
                WHERE {where}
                ORDER BY id DESC LIMIT ?
            """, params + [limit])

            rows = cur.fetchall()
            conn.close()
            return [
                {
                    "stock_code": r[0], "trade_date": r[1],
                    "run_mode": r[2], "timestamp": r[3],
                    "final_score": r[4], "risk_tier": r[5],
                    "lollapalooza_level": r[6],
                    "trace": json.loads(r[7]) if r[7] else {},
                }
                for r in rows
            ]
        except Exception as e:
            logging.warning(f"  ⚠️ SHAP查询失败: {e}")
            return []


# ===================== 全局单例 =====================

_shap_logger = None


def get_shap_logger() -> ShapTraceLogger:
    global _shap_logger
    if _shap_logger is None:
        _shap_logger = ShapTraceLogger()
    return _shap_logger


def reset_shap_logger():
    global _shap_logger
    _shap_logger = ShapTraceLogger()


# ===================== 快捷入口(接受lollapalooza_level) =====================

def build_and_log_shap(
    stock_code: str,
    run_mode: str,
    trade_date: str,
    # L0~L3完整结果
    l0_result: dict = None,
    l1_result: dict = None,
    l2_result: dict = None,
    l3_result: dict = None,
    # FAISS调整
    faiss_adjustment: dict = None,
    # 最终判定
    final_score: float = 0,
    risk_tier: str = "GREEN",
    tier_reason: str = "",
    # §6 字段8 (新增)
    lollapalooza_level: str = "无",
) -> dict:
    """一键: 从四层结果构建SHAP日志+持久化(含字段8 Lollapalooza)。"""
    logger = get_shap_logger()

    # 字段1: 信号原始分
    l1_detail = l1_result.get("l1_detail", {}) if l1_result else {}
    dim_scores = l1_detail.get("dim_scores", [])
    signal_raw = {
        "policy_catalyst": dim_scores[0] if len(dim_scores) > 0 else 0,
        "sector_heat": dim_scores[1] if len(dim_scores) > 1 else 0,
        "theme_purity": dim_scores[2] if len(dim_scores) > 2 else 0,
        "fund_stability": dim_scores[3] if len(dim_scores) > 3 else 0,
        "expectation_gap": dim_scores[4] if len(dim_scores) > 4 else 0,
    }

    # 字段2: L2详情
    l2_detail_data = l2_result or {}

    # 字段4~7: L1/L0
    ladder_detail = {
        "ladder_add": (l1_result.get("step_bonus") or l1_detail.get("ladder_add", 0)),
        "deduct_total": (l1_result.get("announcement_deduct") or l1_detail.get("deduct_total", 0)),
        "track_coeff": (l1_result.get("track_multiplier") or l1_detail.get("track_coeff", 1.0)),
        "macro_coeff": (l1_result.get("macro_coefficient") or l1_detail.get("macro_coeff", 1.0)),
    }
    announce_deduct = ladder_detail["deduct_total"]
    track_coeff = ladder_detail["track_coeff"]

    l0_coeff = l0_result.get("coefficient", 1.0) if l0_result else 1.0
    l0_offset = sum(signal_raw.values()) * (l0_coeff - 1.0)
    track_offset = sum(signal_raw.values()) * (track_coeff - 1.0)

    # 字段8: Lollapalooza (从l3_result获取或外部传入)
    if not lollapalooza_level or lollapalooza_level == "无":
        lollapalooza_level = (l3_result or {}).get("lollapalooza_level", "无")

    trace = logger.build_trace(
        stock_code=stock_code,
        run_mode=run_mode,
        trade_date=trade_date,
        signal_raw_scores=signal_raw,
        l2_detail=l2_detail_data,
        l1_ladder_detail=ladder_detail,
        announcement_deduct=announce_deduct,
        track_penalty_offset=track_offset,
        l0_macro_offset=l0_offset,
        faiss_adjustment=faiss_adjustment,
        lollapalooza_level=lollapalooza_level,
        final_score=final_score,
        risk_tier=risk_tier,
        tier_reason=tier_reason,
    )

    return logger.log_and_persist(trace)


if __name__ == "__main__":
    reset_shap_logger()

    # 测试: 完整9字段
    trace = build_and_log_shap(
        stock_code="600884.SH",
        run_mode="eod_full",
        trade_date="20260722",
        l0_result={"coefficient": 1.3, "macro_status": "bearish"},
        l1_result={
            "step_bonus": 10,
            "announcement_deduct": 20,
            "track_multiplier": 1.5,
            "macro_coefficient": 1.3,
            "l1_detail": {
                "dim_scores": [8.0, 7.5, 4.0, 8.2, 3.5],
                "ladder_add": 10, "deduct_total": 20,
                "track_coeff": 1.5, "macro_coeff": 1.3,
            },
        },
        l2_result={
            "decay_factor": 0.85,
            "weight_info": {"main_dim_weights": {"policy_catalyst": 0.25}},
            "hedge": {
                "hedge_ratio": 0.15,
                "hedge_detail": [{"signal_0": {"strength": 6, "hedge_applied": 0.15}}],
                "positive_signals_count": 1,
            },
            "faiss_adjustment": 1.08,
        },
        final_score=85.8,
        risk_tier="RED",
        tier_reason="题材股得分85.8≥75 → RED",
        lollapalooza_level="重度",
    )

    assert trace["final_result"]["lollapalooza_level"] == "重度"
    assert "field_2_l2_dynamic_detail" in trace
    assert "field_9_attribution_pct" in trace
    print(f"✅ SHAP v2.0: {trace['meta']['stock_code']} {trace['final_result']['risk_tier']}")
    print(f"  Lollapalooza: {trace['final_result']['lollapalooza_level']}")
    print(f"  L2动态详情: decay={trace['field_2_l2_dynamic_detail']['signal_decay_factor']} "
          f"hedge={trace['field_2_l2_dynamic_detail']['positive_hedge_ratio']}")
    print(f"  calc: {trace['calc_steps']}")

    # 查询
    results = get_shap_logger().query_trace("600884.SH", limit=5)
    print(f"✅ SHAP查询: {len(results)}条")

    print()
    print("✅ §6 SHAP全链路溯源日志(9字段+字段8 Lollapalooza) 全部测试通过")
