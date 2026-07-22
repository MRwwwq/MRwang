"""
factor_drift_monitor.py — §6.4 因子漂移实时监控告警
实时监控因子分布/IC/胜率变化, 3级预警机制
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os

DB_PATH = "/opt/stock_agent/agent_memory.db"
DRIFT_STATE_PATH = "/opt/stock_agent/factor_drift_state.json"


class FactorDriftMonitor:
    """§6.4 因子漂移监控 — 3级告警"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.history = self._load_history()

    def _load_history(self):
        """加载历史基准统计"""
        try:
            with open(DRIFT_STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {"baseline": {}, "alerts": []}

    def _save_history(self):
        try:
            with open(DRIFT_STATE_PATH, "w") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── 因子分布监控 ──

    def check_factor_distribution(self, factor_name, current_values):
        """
        检查因子分布vs历史基线
        返回: (level, message)  level=0(正常)/1(一级)/2(二级)/3(三级)
        """
        baseline = self.history.get("baseline", {}).get(factor_name, {})
        if not baseline:
            # 首次: 建立基线
            self.history["baseline"][factor_name] = {
                "mean": float(np.mean(current_values)),
                "std": float(np.std(current_values)),
                "p5": float(np.percentile(current_values, 5)),
                "p95": float(np.percentile(current_values, 95)),
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }
            self._save_history()
            return 0, f"{factor_name}首次建立基线"

        cur_mean = float(np.mean(current_values))
        cur_std = float(np.std(current_values))
        base_mean = baseline["mean"]
        base_std = max(baseline["std"], 1e-6)
        z_score = abs(cur_mean - base_mean) / base_std

        if z_score > 3.0:
            msg = f"§6.4-3 🔴 {factor_name}均值漂移{z_score:.1f}σ,触发三级熔断"
            self._record_alert(factor_name, 3, msg)
            return 3, msg
        if z_score > 2.0:
            msg = f"§6.4-2 🟡 {factor_name}均值漂移{z_score:.1f}σ,限制新开仓"
            self._record_alert(factor_name, 2, msg)
            return 2, msg
        if z_score > 1.5:
            msg = f"§6.4-1 ⚠ {factor_name}均值漂移{z_score:.1f}σ,日志提示"
            self._record_alert(factor_name, 1, msg)
            return 1, msg
        return 0, f"{factor_name}分布稳定(z={z_score:.2f})"

    # ── IC监控 ──

    def check_ic(self, factor_name, ic_value):
        """IC断崖监控"""
        baseline = self.history.get("baseline", {}).get(f"{factor_name}_ic", {})
        if not baseline:
            self.history["baseline"][f"{factor_name}_ic"] = {
                "last_ic": ic_value,
                "ma_ic": ic_value,
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }
            self._save_history()
            return 0, f"{factor_name} IC首次记录={ic_value:.4f}"

        old_ma = baseline["ma_ic"]
        new_ma = old_ma * 0.9 + ic_value * 0.1
        baseline["ma_ic"] = new_ma
        baseline["last_ic"] = ic_value
        baseline["updated"] = datetime.now().strftime("%Y-%m-%d")
        self._save_history()

        if ic_value < 0 and old_ma > 0:
            msg = f"§6.4-2 🟡 {factor_name} IC由正转负({old_ma:.3f}→{ic_value:.3f}),因子失效预警"
            self._record_alert(factor_name, 2, msg)
            return 2, msg
        if abs(ic_value) < 0.01 and abs(old_ma) > 0.03:
            msg = f"§6.4-2 🟡 {factor_name} IC趋零({ic_value:.4f}),因子失效预警"
            self._record_alert(factor_name, 2, msg)
            return 2, msg
        return 0, f"{factor_name} IC={ic_value:.4f}, MA_IC={new_ma:.4f}"

    # ── 胜率监控 ──

    def check_win_rate(self, recent_win_rate, threshold=0.35):
        """胜率监控"""
        if recent_win_rate < threshold:
            msg = f"§6.4-2 🟡 近期胜率{recent_win_rate:.1%}低于{threshold:.0%},策略失效预警"
            self._record_alert("win_rate", 2, msg)
            return 2, msg
        return 0, f"胜率{recent_win_rate:.1%}正常"

    # ── 告警记录 ──

    def _record_alert(self, factor, level, message):
        """告警自动写入memory_failure_signal"""
        self.history.setdefault("alerts", []).append({
            "factor": factor, "level": level, "message": message,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save_history()
        # 同步写入memory_failure_signal (3级必须写入)
        if level >= 2:
            try:
                self.conn.execute(
                    "INSERT INTO memory_failure_signal (ts_code, signal_name, failure_type, avoid_strategy, record_time) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("ALL", f"drift_alert_{factor}", "factor_drift",
                     message[:200], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                self.conn.commit()
            except Exception:
                pass
        print(f"[DriftMonitor] L{level} {message}")

    # ── 全量检查 ──

    def full_check(self, factor_data=None):
        """
        全量因子漂移检查
        factor_data: {factor_name: [values]}
        返回: max_level, [所有告警]
        """
        alerts = []
        max_level = 0

        if factor_data:
            for fname, values in factor_data.items():
                lvl, msg = self.check_factor_distribution(fname, values)
                max_level = max(max_level, lvl)
                if lvl > 0:
                    alerts.append(msg)

        # 检查IC (从chain_log.shap_contrib)
        try:
            df = pd.read_sql(
                "SELECT factor_name, contrib_value FROM shap_contrib "
                "ORDER BY id DESC LIMIT 100", self.conn)
            if not df.empty:
                for fname in df["factor_name"].unique():
                    subset = df[df["factor_name"] == fname]["contrib_value"]
                    ic = subset.corr(pd.Series(range(len(subset))))
                    if not pd.isna(ic):
                        lvl, msg = self.check_ic(fname, round(float(ic), 4))
                        max_level = max(max_level, lvl)
                        if lvl > 0:
                            alerts.append(msg)
        except Exception:
            pass

        return max_level, alerts

    def close(self):
        self.conn.close()
