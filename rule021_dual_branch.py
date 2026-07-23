#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rule021 双分支打分核心引擎
四层联动归属：L0宏观对冲层 + L1 Rule021基础打分层
文件路径：/opt/stock_agent/rule021_dual_branch.py

生产级｜异常隔离｜结构化日志｜边界锁死
"""

from typing import Dict, List, Union
import logging
import json

# 结构化日志初始化
logger = logging.getLogger("Rule021DualBranchEngine")
logger.setLevel(logging.INFO)


def json_log(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Rule021CalcError(Exception):
    """业务自定义异常"""
    pass


class Rule021DualBranchEngine:
    """
    四层联动：L0宏观 + L1双分支打分
    生产级｜异常隔离｜结构化日志｜边界锁死
    """

    def __init__(self):
        # 阶梯加分规则
        self.ladder_score_map = {0: 0, 1: 0, 2: 5, 3: 10, 4: 15, 5: 20}
        self.announcement_deduct = 10
        self.danger_track_multiplier = 1.5
        self.safe_track_multiplier = 1.0
        self.high_risk_dim_threshold = 7
        self.macro_coeff_map = {
            "positive": 0.7,
            "neutral": 1.0,
            "bearish": 1.3
        }

    def calc_macro_coefficient(self, macro_status: str) -> float:
        coeff = self.macro_coeff_map.get(macro_status, 1.0)
        logger.debug(json_log({
            "func": "calc_macro_coefficient",
            "macro_status": macro_status,
            "coeff": coeff
        }))
        return coeff

    def count_high_risk_dim(self, dim_scores: List[float]) -> int:
        cnt = sum(1 for s in dim_scores if s >= self.high_risk_dim_threshold)
        logger.debug(json_log({
            "func": "count_high_risk_dim",
            "high_risk_count": cnt
        }))
        return cnt

    def run_rule021_calc(self, payload: Dict) -> Dict:
        stock_code = payload.get("stock_code", "unknown")

        try:
            logger.info(json_log({
                "event": "rule021_calc_start",
                "stock_code": stock_code,
                "payload": payload
            }))

            # 入参强校验
            required = [
                "stock_code", "track_type", "dim_scores",
                "real_announcement_count", "is_danger_track", "macro_status"
            ]
            for k in required:
                if k not in payload:
                    raise Rule021CalcError(f"缺失字段:{k}")

            dims = payload["dim_scores"]
            if len(dims) != 5:
                raise Rule021CalcError("维度必须5项")
            for v in dims:
                if not (0 <= v <= 10):
                    raise Rule021CalcError(f"维度分值越界:{v}")

            announce_cnt = payload["real_announcement_count"]
            if not isinstance(announce_cnt, int) or announce_cnt < 0:
                raise Rule021CalcError("公告数量非法")

            # 核心计算链路
            base_sum = sum(dims)
            high_cnt = self.count_high_risk_dim(dims)
            ladder_add = self.ladder_score_map[high_cnt]
            s1 = base_sum + ladder_add

            deduct = announce_cnt * self.announcement_deduct
            s2 = max(0.0, s1 - deduct)

            track_coeff = self.danger_track_multiplier if payload["is_danger_track"] else self.safe_track_multiplier
            s3 = s2 * track_coeff

            macro_coeff = self.calc_macro_coefficient(payload["macro_status"])
            final_L1 = s3 * macro_coeff

            res = {
                "status": "success",
                "stock_code": stock_code,
                "track_type": payload["track_type"],
                "base_sum": round(base_sum, 2),
                "high_risk_count": high_cnt,
                "ladder_add": ladder_add,
                "deduct_total": deduct,
                "track_coeff": track_coeff,
                "macro_coeff": macro_coeff,
                "L1_final_score": round(final_L1, 2)
            }

            logger.info(json_log({
                "event": "rule021_calc_ok",
                "stock_code": stock_code,
                "L1_score": final_L1
            }))
            return res

        except Rule021CalcError as e:
            logger.error(json_log({
                "event": "rule021_input_error",
                "stock_code": stock_code,
                "msg": str(e)
            }))
            return {
                "status": "fail",
                "err_type": "input_error",
                "msg": str(e),
                "stock_code": stock_code
            }

        except Exception as e:
            logger.error(json_log({
                "event": "rule021_runtime_error",
                "stock_code": stock_code,
                "msg": str(e)
            }), exc_info=True)
            return {
                "status": "fail",
                "err_type": "runtime_exception",
                "msg": str(e),
                "stock_code": stock_code
            }


# ===================== 调用Demo =====================
if __name__ == "__main__":
    engine = Rule021DualBranchEngine()

    test_payload = {
        "stock_code": "600547",
        "track_type": "cycle_stock",
        "dim_scores": [7.2, 6.5, 8.1, 5.0, 4.3],
        "real_announcement_count": 1,
        "is_danger_track": True,
        "macro_status": "neutral"
    }
    resp = engine.run_rule021_calc(test_payload)
    print(json.dumps(resp, indent=2))
