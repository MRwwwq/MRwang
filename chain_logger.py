"""
chain_logger.py — §6.3 全链路可解释日志
每笔交易完整保存SHAP因子贡献权重、5 Agent全链路推理日志
按标的+日期分库持久化, 支持一键回溯
"""
import sqlite3
import json
import os
from datetime import datetime

LOG_DB = "/opt/stock_agent/chain_log.db"


class ChainLogger:
    """§6.3 全链路可解释日志"""

    def __init__(self, db_path=LOG_DB):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS chain_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT, trade_date TEXT, agent_name TEXT,
                log_type TEXT, payload TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chain_log
                ON chain_log(ts_code, trade_date, agent_name);
            CREATE TABLE IF NOT EXISTS shap_contrib (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT, trade_date TEXT,
                factor_name TEXT, contrib_value REAL,
                base_value REAL, prediction REAL, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shap
                ON shap_contrib(ts_code, trade_date);
            CREATE TABLE IF NOT EXISTS agent_perf (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT, trade_date TEXT,
                latency_ms REAL, status TEXT,
                detail TEXT, created_at TEXT
            );
        """)
        self.conn.commit()

    # ── 全链路日志写入 ──

    def log_chain(self, ts_code, agent_name, log_type, payload_dict):
        """写入选股/仓位/风控/执行/进化各环节日志"""
        self.conn.execute(
            "INSERT INTO chain_log (ts_code, trade_date, agent_name, log_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts_code, datetime.now().strftime("%Y-%m-%d"), agent_name, log_type,
             json.dumps(payload_dict, ensure_ascii=False, default=str),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    def log_agent_perf(self, agent_name, latency_ms, status="ok", detail=""):
        """记录各Agent推理性能"""
        self.conn.execute(
            "INSERT INTO agent_perf (agent_name, trade_date, latency_ms, status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_name, datetime.now().strftime("%Y-%m-%d"),
             round(latency_ms, 2), status, detail[:500],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    # ── SHAP因子贡献 ──

    def log_shap(self, ts_code, base_value, prediction, factor_contribs):
        """
        记录SHAP因子贡献权重
        factor_contribs: [{"factor_name":..., "contrib_value":...}, ...]
        """
        td = datetime.now().strftime("%Y-%m-%d")
        for fc in factor_contribs:
            self.conn.execute(
                "INSERT INTO shap_contrib (ts_code, trade_date, factor_name, contrib_value, base_value, prediction, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts_code, td, fc["factor_name"], fc["contrib_value"],
                 base_value, prediction,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    # ── 回溯查询 ──

    def query_chain(self, ts_code, date=None, agent_name=None):
        """一键回溯完整AI决策链路"""
        sql = "SELECT * FROM chain_log WHERE ts_code=?"
        params = [ts_code]
        if date:
            sql += " AND trade_date=?"
            params.append(date)
        if agent_name:
            sql += " AND agent_name=?"
            params.append(agent_name)
        sql += " ORDER BY id"
        rows = self.conn.execute(sql, params).fetchall()
        return rows

    def query_shap(self, ts_code, date=None):
        """回溯特定标的SHAP贡献"""
        sql = "SELECT * FROM shap_contrib WHERE ts_code=?"
        params = [ts_code]
        if date:
            sql += " AND trade_date=?"
            params.append(date)
        rows = self.conn.execute(sql, params).fetchall()
        return rows

    def query_latency_issues(self, threshold_ms=100):
        """查询时延异常记录"""
        rows = self.conn.execute(
            "SELECT * FROM agent_perf WHERE latency_ms > ? ORDER BY latency_ms DESC",
            (threshold_ms,)).fetchall()
        return rows

    def close(self):
        self.conn.close()
