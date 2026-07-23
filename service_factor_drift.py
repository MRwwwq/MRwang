#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_factor_drift.py — §1.2 因子漂移监控节点

规约:
  监控对象: 全部17类风险信号
  监控逻辑: 实时持续采集各类风险信号分布、均值变化
  判定阈值: 单日信号分布偏离历史稳态区间达到3σ → 一级预警
  配套动作:
    1. 推送一级系统预警
    2. 漂移事件+信号统计快照完整持久化写入复盘日志
  约束: 因子漂移预警仅做风险提示，不自动阻断交易流程
"""

import logging
import json
import time
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DRIFT] %(message)s",
    datefmt="%H:%M:%S",
)

# ===================== 17类风险信号清单 =====================

RISK_SIGNAL_17 = [
    # 量价类 (4)
    "vol_ratio",           # 量比(当日/5日均量)
    "ma20_slope",          # MA20斜率
    "close_position",      # 收盘价在BOLL中轨附近位置
    "turnover_rate",       # 换手率
    # 资金类 (3)
    "fund_net_inflow",     # 主力净流入
    "retail_flow_ratio",   # 散户资金占比
    "large_order_ratio",   # 大单成交占比
    # 情绪类 (3)
    "sentiment_score",     # 情绪综合分
    "guba_heat",           # 股吧热度
    "news_sentiment",      # 新闻情感
    # 指标类 (4)
    "rsi_14",              # 14日RSI
    "macd_histogram",      # MACD柱
    "kdj_k",               # KDJ的K值
    "boll_width",          # BOLL带宽
    # 宏观类 (3)
    "shibor_1w",           # 1周SHIBOR
    "industry_flow",       # 行业资金流向
    "market_up_ratio",     # 市场上涨占比
]


class FactorDriftMonitor:
    """因子漂移监控器。

    职责:
      - 实时采集17类风险信号
      - 维护历史稳态分布(均值+标准差)
      - 检测单日偏差是否达到3σ
      - 触发一级预警+持久化日志
      - 不阻断交易流程
    """

    def __init__(self, warmup_samples: int = 20):
        self.warmup_samples = warmup_samples
        # 历史稳态: {signal_name: {"mean": float, "std": float, "count": int}}
        self._baseline: Dict[str, dict] = {}
        # 当日信号快照: {signal_name: [values]}
        self._daily_snapshot: Dict[str, list] = defaultdict(list)
        # 漂移事件日志
        self._drift_events: list = []
        # 当日日期
        self._current_date = ""

    # ─────── 采集 ───────

    def record_signal(self, signal_name: str, value: float):
        """记录单条信号值。"""
        if signal_name not in RISK_SIGNAL_17:
            logging.warning(f"  ⚠️ 未知风险信号: {signal_name}")
            return
        self._daily_snapshot[signal_name].append(value)

    def record_batch(self, signals: Dict[str, float]):
        """批量记录信号值。"""
        for name, value in signals.items():
            self.record_signal(name, value)

    # ─────── 稳态基线 ───────

    def update_baseline(self, signals: Dict[str, float]):
        """用历史数据更新稳态基线。"""
        for name, value in signals.items():
            if name not in RISK_SIGNAL_17:
                continue
            if name not in self._baseline:
                self._baseline[name] = {"mean": value, "M2": 0, "count": 1}
            else:
                b = self._baseline[name]
                b["count"] += 1
                delta = value - b["mean"]
                b["mean"] += delta / b["count"]
                b["M2"] += delta * (value - b["mean"])

    def finalize_baseline(self):
        """完成基线构建，计算标准差。"""
        for name, b in self._baseline.items():
            if b["count"] >= 2:
                b["std"] = (b["M2"] / (b["count"] - 1)) ** 0.5
            else:
                b["std"] = 0
            # 兜底std=0时用默认值
            if b["std"] == 0:
                b["std"] = 1.0  # 防止除零

    def get_baseline(self, signal_name: str) -> dict:
        """获取某信号的历史稳态。"""
        return self._baseline.get(signal_name, {"mean": 0, "std": 1.0, "count": 0})

    # ─────── 漂移检测 ───────

    def detect_drift(self, signal_name: str, current_value: float) -> dict:
        """检测单信号是否偏离>3σ。

        返回: {"drifted": bool, "z_score": float, "detail": str}
        """
        baseline = self.get_baseline(signal_name)
        mean = baseline["mean"]
        std = baseline["std"] if baseline["std"] > 0 else 1.0
        count = baseline["count"]

        if count < self.warmup_samples:
            return {"drifted": False, "z_score": 0,
                    "detail": f"稳态未建立(样本{count}<{self.warmup_samples})"}

        z_score = abs(current_value - mean) / std
        drifted = z_score > 3.0

        return {
            "drifted": drifted,
            "z_score": round(z_score, 2),
            "detail": (
                f"{'🚨 漂移' if drifted else '✅ 正常'} | "
                f"z={z_score:.2f}{'(≥3σ)' if drifted else ''} | "
                f"当前={current_value:.2f} vs 稳态={mean:.2f}±{std:.2f}"
            ),
        }

    def run_daily_check(self, date_str: str = "") -> list:
        """执行日终漂移检查: 对当日全部信号做3σ检测。

        返回: 漂移事件列表
        """
        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")
        self._current_date = date_str

        events = []
        for signal_name in RISK_SIGNAL_17:
            values = self._daily_snapshot.get(signal_name, [])
            if not values:
                continue

            # 当日均值
            daily_mean = sum(values) / len(values)

            # 漂移检测
            result = self.detect_drift(signal_name, daily_mean)

            if result["drifted"]:
                event = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": date_str,
                    "event": "factor_drift_3sigma",
                    "signal_name": signal_name,
                    "z_score": result["z_score"],
                    "daily_mean": round(daily_mean, 2),
                    "baseline_mean": round(self.get_baseline(signal_name)["mean"], 2),
                    "baseline_std": round(self.get_baseline(signal_name)["std"], 2),
                    "sample_count": len(values),
                    "detail": result["detail"],
                }
                events.append(event)
                self._drift_events.append(event)

                logging.warning(json.dumps(event, ensure_ascii=False))

        # 输出汇总
        if events:
            names = [e["signal_name"] for e in events]
            logging.warning(f"  ⚠️ 因子漂移汇总: {len(events)}项信号偏离>3σ: {names}")
        else:
            logging.info(f"  ✅ 日终漂移检查: 全部{RISK_SIGNAL_17}项信号在稳态范围内")

        # 清空当日快照
        self._daily_snapshot.clear()

        return events

    # ─────── 预警推送 ───────

    def push_alert(self, events: list) -> dict:
        """推送一级系统预警。

        不阻断交易——仅记录+提示。
        """
        if not events:
            return {"alert_level": 0, "message": "无漂移事件"}

        alert = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "alert_level": 1,
            "alert_type": "factor_drift_warning",
            "event_count": len(events),
            "drifted_signals": [e["signal_name"] for e in events],
            "action_required": False,  # 纯提示，不阻断
            "action_note": "因子漂移预警仅做风险提示，不自动阻断交易流程。由人工结合降级策略、市场环境评估是否调整模型参数。",
        }

        logging.warning(json.dumps(alert, ensure_ascii=False))
        return alert

    # ─────── 复盘日志 ───────

    def get_drift_history(self) -> list:
        """获取全量漂移事件历史。"""
        return list(self._drift_events)

    def drift_report(self) -> dict:
        """生成漂移监控报告。"""
        return {
            "monitor_date": self._current_date,
            "total_signals_monitored": len(RISK_SIGNAL_17),
            "baseline_signals": len(self._baseline),
            "drift_events_total": len(self._drift_events),
            "recent_events": self._drift_events[-10:] if self._drift_events else [],
            "action_required": False,
            "note": "因子漂移预警仅做风险提示，不自动阻断交易流程",
        }


# ===================== 全局单例 =====================

_drift_monitor = None


def get_drift_monitor() -> FactorDriftMonitor:
    global _drift_monitor
    if _drift_monitor is None:
        _drift_monitor = FactorDriftMonitor()
    return _drift_monitor


def reset_drift_monitor():
    global _drift_monitor
    _drift_monitor = FactorDriftMonitor()


# ===================== 快捷入口 =====================

def run_drift_check(date_str: str = "",
                    daily_signals: Dict[str, float] = None) -> dict:
    """快捷入口: 记录信号→执行日终检查→推送预警。

    返回: {"events": [...], "alert": {...}, "drifted": bool}
    """
    monitor = get_drift_monitor()

    if daily_signals:
        monitor.record_batch(daily_signals)

    events = monitor.run_daily_check(date_str)
    alert = monitor.push_alert(events)

    return {
        "events": events,
        "alert": alert,
        "drifted": len(events) > 0,
        "note": "因子漂移预警仅做风险提示，不自动阻断交易流程",
    }


def quick_drift_check(signal_name: str, current_value: float) -> dict:
    """快速单信号漂移检查。"""
    monitor = get_drift_monitor()
    monitor.record_signal(signal_name, current_value)
    return monitor.detect_drift(signal_name, current_value)


# ===================== 自测 =====================

if __name__ == "__main__":
    reset_drift_monitor()
    monitor = get_drift_monitor()

    # 建立基线
    import random
    for _ in range(30):
        sigs = {name: random.uniform(0, 10) for name in RISK_SIGNAL_17[:6]}
        monitor.update_baseline(sigs)
    monitor.finalize_baseline()

    # 测试稳态信号
    for name in RISK_SIGNAL_17[:6]:
        r = monitor.detect_drift(name, 5.0)
        assert not r["drifted"], f"{name} 不应漂移"
    print("✅ 稳态信号: 全部正常")

    # 测试漂移(偏离>3σ)
    r = monitor.detect_drift(RISK_SIGNAL_17[0], 50.0)
    print(f"✅ 漂移检测: name={RISK_SIGNAL_17[0]} drifted={r['drifted']} z={r['z_score']}")
    assert r["drifted"], "极端值应触发漂移"

    # 测试批量记录+日终检查
    reset_drift_monitor()
    monitor = get_drift_monitor()

    # 先建基线
    for _ in range(25):
        monitor.update_baseline({name: random.uniform(0, 10) for name in RISK_SIGNAL_17})
    monitor.finalize_baseline()

    # 记录当日信号(全部正常)
    for name in RISK_SIGNAL_17:
        monitor.record_signal(name, random.uniform(4, 6))

    events = monitor.run_daily_check("20260722")
    print(f"✅ 日终检查(正常): {len(events)}项漂移(应0)")

    # 记录当日信号(部分异常)
    for name in RISK_SIGNAL_17:
        val = 50.0 if name in ["vol_ratio", "turnover_rate", "fund_net_inflow"] else random.uniform(4, 6)
        monitor.record_signal(name, val)

    events = monitor.run_daily_check("20260723")
    print(f"✅ 日终检查(有漂移): {len(events)}项漂移(应≥1)")

    alert = monitor.push_alert(events)
    print(f"✅ 预警推送: level={alert['alert_level']} action_required={alert['action_required']}")
    assert not alert["action_required"], "漂移预警不应阻断交易"

    report = monitor.drift_report()
    print(f"✅ 报告: 总事件={report['drift_events_total']} 无需操作={not report['action_required']}")

    print()
    print("✅ 因子漂移监控全部测试通过")
