#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_fault_degradation.py — §1.1 三级故障降级机制

规约:
  一级降级: RAG映射模块(MISJUDGE_MATCH)故障
    策略: 跳过误判心理学动态加权,直接使用L1静态原始风险分,持续告警,允许正常开平仓

  二级降级: 财务数据接口中断
    策略: 剔除所有依赖财务数据源的风险信号;保留量价/资金/题材周期信号,打分逻辑缩减子集

  三级降级: 全链路大规模异常
    策略: 停用动态四层打分;强制静态硬风控(单票上限/行业集中度/总仓位上限);禁止新开仓,仅减仓/持仓

  降级原则:
    1. 自动故障探测,日志标记降级等级,全链路埋点
    2. 故障恢复后自动平滑切回正常模式
    3. 降级状态永久写入链路日志
    4. 降级策略不可突破基础风控底线
"""

import logging
import time
import json
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DEGRADE] %(message)s",
    datefmt="%H:%M:%S",
)

# ===================== 降级状态定义 =====================

DEGRADE_LEVELS = {
    0: "正常",
    1: "一级降级 — RAG/MISJUDGE_MATCH故障",
    2: "二级降级 — 财务数据中断",
    3: "三级降级 — 全链路大规模异常",
}

# 静态硬风控阈值（三级降级时启用）
HARD_RISK_LIMITS = {
    "per_stock_max_pct": 12,       # 单票上限12%
    "industry_max_pct": 30,        # 行业集中度上限30%
    "total_account_max_pct": 75,   # 账户总仓位上限75%
}


class FaultDegradationManager:
    """三级故障降级管理器。

    职责:
      - 自动探测模块故障(通过外部健康检查)
      - 根据故障类型选择降级等级
      - 执行降级策略(跳过/缩减/强制)
      - 故障恢复后自动切回正常
      - 全链路日志埋点
    """

    def __init__(self):
        self.current_level = 0       # 0=正常, 1/2/3=降级等级
        self.fault_log = []          # 故障历史
        self.recovery_pending = False  # 待恢复标记
        self._last_fault_time = 0
        self._last_recovery_time = 0

    # ─────── 故障探测 ───────

    def detect_fault_level(self,
                           rag_available: bool = True,
                           financial_available: bool = True,
                           multi_module_failure: bool = False) -> int:
        """自动探测故障等级。

        优先级: 三级 > 二级 > 一级
        返回: 降级等级 (0/1/2/3)
        """
        if multi_module_failure:
            proposed = 3
        elif not financial_available:
            proposed = 2
        elif not rag_available:
            proposed = 1
        else:
            proposed = 0

        # 等级变更时记录
        if proposed != self.current_level:
            now = time.time()
            if proposed > 0 and self.current_level == 0:
                # 正常→降级
                self._last_fault_time = now
                self._log_fault(proposed, "故障触发")
            elif proposed == 0 and self.current_level > 0:
                # 降级→恢复
                self._last_recovery_time = now
                self.recovery_pending = False
                self._log_recovery()

            self.current_level = proposed

        return proposed

    def force_degrade(self, level: int):
        """强制设定降级等级(外部手动/监控触发)。"""
        if level not in DEGRADE_LEVELS:
            logging.warning(f"  ⚠️ 非法降级等级: {level}")
            return
        old = self.current_level
        self.current_level = level
        if level > 0:
            self._log_fault(level, f"强制降级 (原等级{old})")
        else:
            self._log_recovery()

    # ─────── 降级策略执行 ───────

    def apply_degradation(self, l1_result: dict,
                          l2_enabled: bool = True,
                          l3_enabled: bool = True) -> dict:
        """根据当前降级等级调整打分结果。

        返回修正后的(l1_result, l2_enabled, l3_enabled, degrade_note)
        """
        level = self.current_level
        result = {
            "l1_score": l1_result.get("L1_final_score", 0) if isinstance(l1_result, dict) else 0,
            "l2_enabled": l2_enabled,
            "l3_enabled": l3_enabled,
            "degrade_level": level,
            "degrade_note": DEGRADE_LEVELS.get(level, "未知"),
        }

        if level == 0:
            # 正常模式: 不做任何干预
            result["degrade_note"] = "正常模式, 无降级"
            return result

        if level == 1:
            # 一级: 跳过L2动态加权,直接使用L1原始分
            result["l2_enabled"] = False
            result["l3_enabled"] = True    # L3反转仍可运行
            logging.info(f"  ⚠️ 一级降级生效: 跳过L2动态加权, 使用L1原始分={result['l1_score']}")

        elif level == 2:
            # 二级: 缩减打分子集,剔除财务信号
            # L1原始分已不含财务维度(上游应剔除财务dim)
            result["l2_enabled"] = True     # L2仍运行(但输入已缩减)
            result["l3_enabled"] = True     # L3仍可用
            logging.info(f"  ⚠️ 二级降级生效: 剔除财务信号, 量价/资金/题材子集运行")

        elif level == 3:
            # 三级: 停用动态打分,强制静态硬风控
            result["l2_enabled"] = False
            result["l3_enabled"] = False
            result["force_static_risk"] = True
            result["static_limits"] = HARD_RISK_LIMITS
            result["l1_score"] = 75  # 强制高风险(触发RED,禁止开仓)
            logging.warning(f"  🚨 三级降级生效: 停用动态四层, 强制静态硬风控, "
                           f"单票≤{HARD_RISK_LIMITS['per_stock_max_pct']}% "
                           f"行业≤{HARD_RISK_LIMITS['industry_max_pct']}% "
                           f"总仓≤{HARD_RISK_LIMITS['total_account_max_pct']}%")

        # 埋点日志
        self._log_degrade_step(level, result)
        return result

    def get_risk_action_override(self, original_tier: str) -> dict:
        """三级降级特有: 强制覆盖仓位决策。"""
        if self.current_level < 3:
            return {"override": False, "reason": ""}

        return {
            "override": True,
            "force_tier": "RED",
            "force_action": "禁止一切新开仓操作，仅允许减仓、持仓维持",
            "force_coefficient": 0.0,
            "reason": "三级降级生效: 全链路异常, 静态硬风控强制RED",
        }

    def check_recovery(self,
                       rag_available: bool = True,
                       financial_available: bool = True,
                       multi_module_failure: bool = False) -> bool:
        """检查故障是否恢复,自动切回正常模式。

        恢复条件: 所有故障源已修复 且 稳定探测通过(连续2次探测正常)
        """
        if self.current_level == 0:
            return True  # 已在正常模式

        all_ok = (rag_available and financial_available and not multi_module_failure)

        if all_ok:
            if not self.recovery_pending:
                # 第一次探测到恢复
                self.recovery_pending = True
                logging.info(f"  🔄 降级恢复探测: 故障消失, 待稳定确认")
                return False

            # 连续两次正常→确认恢复
            self.current_level = 0
            self.recovery_pending = False
            self._log_recovery()
            logging.info(f"  ✅ 故障已恢复, 自动切回正常模式")
            return True

        # 仍有故障
        self.recovery_pending = False
        return False

    # ─────── 日志埋点 ───────

    def _log_fault(self, level: int, reason: str):
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": "degrade_fault",
            "level": level,
            "level_desc": DEGRADE_LEVELS.get(level, ""),
            "reason": reason,
        }
        self.fault_log.append(entry)
        logging.warning(json.dumps(entry, ensure_ascii=False))

    def _log_recovery(self):
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": "degrade_recovery",
            "level": 0,
            "level_desc": "恢复正常",
        }
        self.fault_log.append(entry)
        logging.info(json.dumps(entry, ensure_ascii=False))

    def _log_degrade_step(self, level: int, result: dict):
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": "degrade_step",
            "level": level,
            "action": result,
        }
        logging.debug(json.dumps(entry, ensure_ascii=False))

    def get_fault_history(self) -> list:
        """返回全量故障历史(用于事后复盘)。"""
        return list(self.fault_log)

    def status_report(self) -> dict:
        """降级状态报告。"""
        return {
            "current_level": self.current_level,
            "level_desc": DEGRADE_LEVELS.get(self.current_level, "未知"),
            "fault_count": len(self.fault_log),
            "last_fault": self._last_fault_time,
            "last_recovery": self._last_recovery_time,
            "recovery_pending": self.recovery_pending,
        }


# ===================== 全局单例 =====================

_degrade_manager = None


def get_degradation_manager() -> FaultDegradationManager:
    global _degrade_manager
    if _degrade_manager is None:
        _degrade_manager = FaultDegradationManager()
    return _degrade_manager


def reset_degradation():
    global _degrade_manager
    _degrade_manager = FaultDegradationManager()


# ===================== 集成入口 =====================

def apply_degradation_before_scoring(
    rag_available: bool = True,
    financial_available: bool = True,
    multi_module_failure: bool = False,
) -> dict:
    """在 RULE_SCORE_ENGINE 开始打分前调用, 返回降级状态。"""
    mgr = get_degradation_manager()
    level = mgr.detect_fault_level(rag_available, financial_available, multi_module_failure)

    # 检查恢复
    mgr.check_recovery(rag_available, financial_available, multi_module_failure)

    return mgr.status_report()


def apply_degradation_after_l1(l1_result: dict) -> dict:
    """L1打分完成后调用, 根据降级等级决定后续链路。"""
    mgr = get_degradation_manager()
    if mgr.current_level == 0:
        return {"skip_l2": False, "skip_l3": False,
                "degrade_level": 0, "degrade_note": "正常模式"}

    result = mgr.apply_degradation(l1_result)
    return {
        "skip_l2": not result["l2_enabled"],
        "skip_l3": not result["l3_enabled"],
        "degrade_level": result["degrade_level"],
        "degrade_note": result["degrade_note"],
        "force_static_risk": result.get("force_static_risk", False),
        "static_limits": result.get("static_limits", {}),
    }


def get_degrade_risk_override(original_tier: str) -> dict:
    """三级降级时强制覆盖仓位。"""
    mgr = get_degradation_manager()
    return mgr.get_risk_action_override(original_tier)


# ===================== 自测 =====================

if __name__ == "__main__":
    reset_degradation()
    mgr = get_degradation_manager()

    # 测试正常模式
    print("=== 测试1: 正常模式 ===")
    level = mgr.detect_fault_level(True, True, False)
    assert level == 0, f"应0,实际{level}"
    r = mgr.apply_degradation({"L1_final_score": 30.0})
    assert r["l2_enabled"] and r["l3_enabled"]
    print(f"  ✅ 正常: level={level} l2={r['l2_enabled']} l3={r['l3_enabled']}")

    # 测试一级降级(RAG故障)
    print("=== 测试2: 一级降级 ===")
    reset_degradation()
    mgr = get_degradation_manager()
    mgr.detect_fault_level(rag_available=False, financial_available=True)
    assert mgr.current_level == 1
    r = mgr.apply_degradation({"L1_final_score": 35.0})
    assert not r["l2_enabled"], "一级应跳过L2"
    assert r["l3_enabled"], "一级仍运行L3"
    print(f"  ✅ 一级: l2={r['l2_enabled']} l3={r['l3_enabled']} note={r['degrade_note'][:30]}")

    # 测试二级降级(财务中断)
    print("=== 测试3: 二级降级 ===")
    reset_degradation()
    mgr = get_degradation_manager()
    mgr.detect_fault_level(True, financial_available=False)
    assert mgr.current_level == 2
    r = mgr.apply_degradation({"L1_final_score": 40.0})
    assert r["l2_enabled"]  # 二级, L2仍运行(输入已缩减)
    print(f"  ✅ 二级: l2={r['l2_enabled']} l3={r['l3_enabled']}")

    # 测试三级降级
    print("=== 测试4: 三级降级 ===")
    reset_degradation()
    mgr = get_degradation_manager()
    mgr.detect_fault_level(False, False, multi_module_failure=True)
    assert mgr.current_level == 3
    r = mgr.apply_degradation({"L1_final_score": 30.0})
    assert not r["l2_enabled"] and not r["l3_enabled"]
    assert r.get("force_static_risk")
    override = mgr.get_risk_action_override("GREEN")
    assert override["override"]
    assert override["force_tier"] == "RED"
    print(f"  ✅ 三级: l2={r['l2_enabled']} l3={r['l3_enabled']} "
          f"static={r.get('force_static_risk')} force={override['force_tier']}")

    # 测试恢复
    print("=== 测试5: 自动恢复 ===")
    recovered = mgr.check_recovery(True, True, False)
    assert not recovered
    recovered = mgr.check_recovery(True, True, False)
    assert recovered
    assert mgr.current_level == 0
    print(f"  ✅ 恢复: level={mgr.current_level}")

    print(f"\n故障历史: {len(mgr.get_fault_history())}条")
    print("✅ 三级降级全部测试通过")
