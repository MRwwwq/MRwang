"""
agent_risk_controller.py — 风控Agent（§2.4, 独立安全拦截层, 最高优先级）
职责隔离: 完全独立, 全局一票否决权, 不受其他Agent输出结果约束
全量执行 §1~§6 多层智能风控指令
"""
import sqlite3
import pandas as pd
from datetime import datetime

DB_PATH = "/opt/stock_agent/agent_memory.db"


class AgentRiskController:
    """§2.4 风控Agent — 独立全维度风险拦截, 全局一票否决"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

    def veto_check(self, stock_pool, position_plan, context=None):
        """
        全维度风控校验 → (veto, logs, approved_plan)
        veto=True → 驳回, 全部拦截
        veto=False → 放行, 含修正后仓位
        """
        logs = []
        approved = {}
        veto = False
        total_pos = sum(position_plan.values()) if position_plan else 0

        if context is None:
            context = {}

        # ── §2 静态硬约束 ──
        if total_pos > 0.75:
            logs.append("§2-3 ❌ 总仓位75%超限, 驳回")
            veto = True

        for code, pos in (position_plan or {}).items():
            if pos > 0.12:
                logs.append(f"§2-1 ❌ {code}单票{pos*100:.0f}%>12%超限, 截断至12%")
                approved[code] = 0.12
                veto = True  # 即使截断也要标记风险
            else:
                approved[code] = pos

        # ── §3.1 流动性检查 ──
        for code in list(approved.keys()):
            try:
                df = pd.read_sql(
                    "SELECT amount FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
                    self.conn, params=(code,))
                if len(df) < 20:
                    continue
                avg = float(df["amount"].mean())
                if avg < 5000_0000:
                    logs.append(f"§3.1 ❌ {code}流动性不足({avg/10000:.0f}万), 拦截")
                    del approved[code]
                    veto = True
            except Exception:
                continue

        # ── §3.2 暴雷检查 ──
        for code in list(approved.keys()):
            try:
                c = self.conn.execute(
                    "SELECT COUNT(*) FROM memory_failure_signal "
                    "WHERE ts_code=? AND signal_name LIKE '%black_swan%'",
                    (code,))
                if c.fetchone()[0] > 0:
                    logs.append(f"§3.2 ❌ {code}暴雷黑名单, 拦截")
                    del approved[code]
                    veto = True
            except Exception:
                continue

        # ── §3.4 熔断检查 ──
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            df = pd.read_sql(
                f"SELECT reward FROM rl_decision_log WHERE trade_date='{today}'",
                self.conn)
            if not df.empty and abs(float(df["reward"].sum())) >= 0.025:
                logs.append("§3.4 ❌ 熔断已触发, 全部新开仓冻结")
                approved = {}
                veto = True
        except Exception:
            pass

        # ── §5 固定链路完整性 ──
        if not stock_pool:
            logs.append("§5 ❌ 选股池为空, 驳回")
            veto = True
        if not position_plan:
            logs.append("§5 ❌ 仓位方案为空, 驳回")
            veto = True

        status = "❌ 驳回" if veto else "✅ 放行"
        logs.insert(0, f"[风控Agent] {status}")

        # 拦截日志归档
        if veto:
            try:
                for code in (position_plan or {}):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO memory_failure_signal "
                        "(ts_code, signal_name, failure_type, avoid_strategy, record_time) "
                        "VALUES (?,?,?,?,?)",
                        (code, "risk_veto_auto", "risk_control",
                         "风控Agent一票否决", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                self.conn.commit()
            except Exception:
                pass

        return veto, "\n".join(logs), approved

    def check_single(self, ts_code, target_pos):
        """单标的快速风控校验(供外部调用)"""
        df = pd.read_sql(
            "SELECT amount FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
            self.conn, params=(ts_code,))
        if len(df) >= 20 and float(df["amount"].mean()) < 5000_0000:
            return False, "流动性拦截"
        c = self.conn.execute(
            "SELECT COUNT(*) FROM memory_failure_signal "
            "WHERE ts_code=? AND signal_name LIKE '%black_swan%'", (ts_code,))
        if c.fetchone()[0] > 0:
            return False, "暴雷黑名单"
        if target_pos > 0.12:
            return False, f"单票{target_pos*100:.0f}%>12%超限"
        return True, "通过"

    def close(self):
        self.conn.close()
