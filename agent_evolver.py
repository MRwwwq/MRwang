"""
agent_evolver.py — 进化Agent（§2.5, 自进化闭环运维层）
职责隔离: 仅离线迭代模型与参数，不参与盘中实时交易
"""
import sqlite3
import pandas as pd
import os
from datetime import datetime

DB_PATH = "/opt/stock_agent/agent_memory.db"
LOG_DIR = "/opt/stock_agent/evolution_log"
REVIEW_DIR = "/opt/stock_agent/review_report"


class AgentEvolver:
    """§2.5 进化Agent — 全权负责复盘/进化/沙盒/版本迭代"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(REVIEW_DIR, exist_ok=True)

    def daily_ingest(self):
        """
        每日收盘后: 归集全量交易/风控数据 → 供复盘与迭代
        返回: 统计摘要dict
        """
        try:
            # 当日决策统计
            today = datetime.now().strftime("%Y-%m-%d")
            df = pd.read_sql(
                f"SELECT * FROM rl_decision_log WHERE trade_date='{today}'",
                self.conn)
            trade_count = len(df)
            avg_reward = float(df["reward"].mean()) if trade_count > 0 else 0
            total_exposure = float(df["position_rate"].sum()) if trade_count > 0 else 0

            # 风控拦截统计
            fail_df = pd.read_sql(
                f"SELECT COUNT(*) as cnt FROM memory_failure_signal "
                f"WHERE record_time LIKE '{today}%'", self.conn)
            risk_blocks = int(fail_df["cnt"].iloc[0]) if not fail_df.empty else 0

            summary = {
                "date": today,
                "trade_count": trade_count,
                "avg_reward": round(avg_reward, 4),
                "total_exposure": round(total_exposure, 4),
                "risk_blocks": risk_blocks,
            }
            print(f"[Evolver] 每日归集: {trade_count}笔交易, {risk_blocks}次拦截")
            return summary
        except Exception as e:
            return {"error": str(e)}

    def run_review(self):
        """启动复盘"""
        try:
            from daily_auto_review import AutoDailyReview
            reviewer = AutoDailyReview()
            report = reviewer.generate_report(perf_days=30, fail_days=60)
            fname = f"{REVIEW_DIR}/review_{datetime.now().strftime('%Y-%m-%d')}.md"
            with open(fname, "w") as f:
                f.write(report)
            print(f"[Evolver] 复盘完成: {fname}")
            return report
        except Exception as e:
            print(f"[Evolver] 复盘异常: {e}")
            return None

    def run_evolution(self):
        """启动参数/因子/模型迭代"""
        try:
            from evolution_engine import AIEvolveEngine
            engine = AIEvolveEngine()
            result = engine.run_full_evolve_cycle()
            engine.close()
            print(f"[Evolver] 进化完成: params={len(result.get('top_param',[]))}, "
                  f"factors={len(result.get('new_valid_factor',[]))}")
            return result
        except Exception as e:
            print(f"[Evolver] 进化异常: {e}")
            return None

    def run_sandbox(self):
        """沙盒安全测试"""
        try:
            from sandbox_safe_test import SandboxSafeTest
            sandbox = SandboxSafeTest()
            result = sandbox.run_full_sandbox_flow()
            print(f"[Evolver] 沙盒决策: online_switch={result.get('online_switch')}")
            return result
        except Exception as e:
            print(f"[Evolver] 沙盒异常: {e}")
            return None

    def close(self):
        self.conn.close()
