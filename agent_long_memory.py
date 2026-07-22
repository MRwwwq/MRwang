import sqlite3
import pandas as pd
from datetime import datetime


class AgentLongMemory:
    def __init__(self, db_path="agent_memory.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._create_all_table()

    def _migrate_backward_compat_tables(self):
        """迁移旧架构表：检测到缺失列时重建为新schema"""
        cur = self.conn.cursor()
        # 兼容表迁移（列名变更）
        for table, expected_col in [("trade_memory", "signal"), ("global_rule", "risk_level")]:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='{}'".format(table))
            if cur.fetchone():
                cur.execute("PRAGMA table_info({})".format(table))
                cols = [r[1] for r in cur.fetchall()]
                if expected_col not in cols:
                    cur.execute("DROP TABLE {}".format(table))
        # 主表迁移（新增列）
        for table, expected_col in [("memory_market", "base_factor_score"), ("memory_market", "trend_score"),
                                     ("memory_market_archive", "base_factor_score"), ("memory_market_archive", "trend_score")]:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='{}'".format(table))
            if cur.fetchone():
                cur.execute("PRAGMA table_info({})".format(table))
                cols = [r[1] for r in cur.fetchall()]
                if expected_col not in cols:
                    cur.execute("DROP TABLE {}".format(table))
        self.conn.commit()

    def _create_all_table(self):
        # 检查旧兼容表是否需要迁移
        self._migrate_backward_compat_tables()

        create_sql_list = [
            # 原有 trading-memory-system 旧向量记忆表
            """
            CREATE TABLE IF NOT EXISTS trade_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT, signal TEXT, pnl_rate REAL, market_vec TEXT, record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trade_signal ON trade_memory(signal);
            CREATE TABLE IF NOT EXISTS global_rule (
                rule_id TEXT PRIMARY KEY, rule_content TEXT, risk_level INTEGER, record_time TEXT
            );
            """,
            # 新增结构化行情记忆主表
            """
            CREATE TABLE IF NOT EXISTS memory_market (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close REAL, high REAL, low REAL, open REAL, volume REAL, turnover REAL,
                macd REAL, rsi REAL, ma5 REAL, ma20 REAL, ma60 REAL,
                sentiment_score REAL, org_visit_flag INTEGER, guba_hot INTEGER,
                market_cap REAL, industry TEXT, base_factor_score REAL, trend_score REAL, record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_market_code_date ON memory_market(ts_code, trade_date);
            """,
            # 交易盈亏归档表
            """
            CREATE TABLE IF NOT EXISTS memory_trade_pnl (
                trade_id TEXT PRIMARY KEY,
                ts_code TEXT, entry_date TEXT, exit_date TEXT,
                entry_price REAL, exit_price REAL, position REAL,
                total_pnl REAL, pnl_rate REAL, hold_days INTEGER,
                trigger_signal TEXT, market_env TEXT, sentiment_tag TEXT,
                profit_tag TEXT, summary TEXT, record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pnl_code_signal ON memory_trade_pnl(ts_code, trigger_signal);
            """,
            # 策略失效信号风控库
            """
            CREATE TABLE IF NOT EXISTS memory_failure_signal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_name TEXT NOT NULL, ts_code TEXT, failure_date TEXT,
                max_drawdown REAL, trigger_condition TEXT, market_feature TEXT,
                failure_type TEXT, warning_level INTEGER, avoid_strategy TEXT, record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_failure_signal ON memory_failure_signal(signal_name);
            """,
            # 黑天鹅极端风险事件库
            """
            CREATE TABLE IF NOT EXISTS memory_black_swan (
                event_id TEXT PRIMARY KEY, event_date TEXT, event_type TEXT,
                affected_industry TEXT, affected_ts_codes TEXT, market_drop_rate REAL,
                sustain_days INTEGER, risk_feature TEXT, risk_response TEXT, record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_swan_date_type ON memory_black_swan(event_date, event_type);
            """,
            # 3年以上行情归档表（不参与实时相似度检索）
            """
            CREATE TABLE IF NOT EXISTS memory_market_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close REAL, high REAL, low REAL, open REAL, volume REAL, turnover REAL,
                macd REAL, rsi REAL, ma5 REAL, ma20 REAL, ma60 REAL,
                sentiment_score REAL, org_visit_flag INTEGER, guba_hot INTEGER,
                market_cap REAL, industry TEXT, base_factor_score REAL, trend_score REAL, record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_archive_code_date ON memory_market_archive(ts_code, trade_date);
            """,
            # PPO强化学习决策记录表
            """
            CREATE TABLE IF NOT EXISTS rl_decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT,
                trade_date TEXT,
                base_score REAL,
                trend_score REAL,
                market_change REAL,
                volatility REAL,
                action INT,
                reward REAL,
                position_rate REAL,
                stop_loss REAL,
                take_profit REAL,
                record_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_rl_date ON rl_decision_log(trade_date);
            """,
        ]
        for sql in create_sql_list:
            self.conn.executescript(sql)
        self.conn.commit()

    # 基础行情写入（承载多因子模型输出base_factor_score）
    def write_market_memory(self, df: pd.DataFrame):
        if "record_time" not in df.columns:
            df["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df.to_sql("memory_market", self.conn, if_exists="append", index=False)

    # 中层时序趋势分写入（承载TrendCaptureModel输出trend_score）
    def write_trend_score(self, df: pd.DataFrame):
        """将中层时序模型trend_score同步写入行情记忆库"""
        if "record_time" not in df.columns:
            df["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df.to_sql("memory_market", self.conn, if_exists="append", index=False)

    # 平仓盈亏归档 + 自动失效信号写入
    def after_close_archive(self, trade_info: dict, market_env: str):
        trade_info["market_env"] = market_env
        trade_info["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = pd.DataFrame([trade_info])
        df.to_sql("memory_trade_pnl", self.conn, if_exists="append", index=False)

        # 根据回撤自动计算风险等级
        drawdown = abs(trade_info["pnl_rate"])
        warn_level = int(drawdown * 100)
        if drawdown > 0.03:
            fail_record = {
                "signal_name": trade_info["trigger_signal"],
                "ts_code": trade_info["ts_code"],
                "failure_date": trade_info["exit_date"],
                "max_drawdown": drawdown,
                "trigger_condition": "基础打分{}".format(trade_info.get("base_factor_score", 0)),
                "market_feature": market_env,
                "failure_type": "多因子信号回撤",
                "warning_level": warn_level,
                "avoid_strategy": "降低同形态仓位阈值"
            }
            self.write_failure_signal(fail_record)

    def write_failure_signal(self, fail_dict: dict):
        if "record_time" not in fail_dict:
            fail_dict["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = pd.DataFrame([fail_dict])
        df.to_sql("memory_failure_signal", self.conn, if_exists="append", index=False)

    def write_black_swan(self, swan_dict: dict):
        if "record_time" not in swan_dict:
            swan_dict["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = pd.DataFrame([swan_dict])
        df.to_sql("memory_black_swan", self.conn, if_exists="append", index=False)

    # PPO强化学习决策记录写入
    def write_rl_decision(self, decision_dict: dict):
        """写入RL决策记录到rl_decision_log表，供历史复盘/回测核查"""
        if "record_time" not in decision_dict:
            decision_dict["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = pd.DataFrame([decision_dict])
        df.to_sql("rl_decision_log", self.conn, if_exists="append", index=False)

    # 开仓前置结构化风控校验（硬性拦截）
    def pre_open_check(self, tech_dict: dict, industry: str, sentiment_score: float,
                       signal_name: str, ts_code: str, base_score: float, trend_score: float = 0.5):
        log_list = []
        allow = True

        # 0. 新增中层时序趋势硬性门槛
        if trend_score < 0.35:
            log_list.append("❌ 时序趋势打分过低{:.4f}，短期无上涨趋势，拦截".format(trend_score))
            allow = False

        # 1. 失效信号黑名单校验
        fail_df = pd.read_sql(
            "SELECT * FROM memory_failure_signal WHERE signal_name='{}' AND ts_code='{}'".format(signal_name, ts_code),
            self.conn)
        if len(fail_df) > 0 and fail_df["warning_level"].max() >= 5:
            log_list.append("❌ 标的{}信号{}存在高等级失效记录，禁止开仓".format(ts_code, signal_name))
            allow = False

        # 2. 行业黑天鹅校验
        swan_df = pd.read_sql(
            "SELECT * FROM memory_black_swan WHERE affected_industry LIKE '%{}%'".format(industry),
            self.conn)
        if len(swan_df) > 0:
            log_list.append("⚠️ 行业{}存在未消除黑天鹅事件".format(industry))
            if base_score < 0.6:
                log_list.append("❌ 底层打分不足0.6，黑天鹅环境直接拦截")
                allow = False

        # 3. 历史胜率校验
        pnl_df = pd.read_sql(
            "SELECT pnl_rate FROM memory_trade_pnl WHERE trigger_signal='{}'".format(signal_name),
            self.conn)
        if len(pnl_df) >= 10:
            win_rate = len(pnl_df[pnl_df["pnl_rate"] > 0]) / len(pnl_df)
            log_list.append("历史胜率{:.2%}".format(win_rate))
            if win_rate < 0.45:
                log_list.append("❌ 历史胜率低于45%，拦截开仓")
                allow = False

        log_list.append("底层压舱打分：{:.4f}".format(base_score))
        log_list.append("中层时序趋势打分：{:.4f}".format(trend_score))
        return allow, "\n".join(log_list)

    # 双模型打分联合仓位计算
    def calc_dual_score_position(self, base_score: float, trend_score: float) -> dict:
        """
        双模型打分联合仓位规则 (全局统一执行总约束指令 §3)
        base_score≥0.7 & trend_score≥0.7 → 高仓位(25%)
        base_score≥0.5 & trend_score≥0.5 → 中仓位(12%)
        base_score∈[0.4,0.5) & trend_score∈[0.35,0.5) → 轻仓(3%)
        任意一项低于阈值 → 剔除(0%)
        """
        if base_score >= 0.7 and trend_score >= 0.7:
            level = "高仓位"
            pct = 0.25
        elif base_score >= 0.5 and trend_score >= 0.5:
            level = "中等仓位"
            pct = 0.12
        elif base_score >= 0.4 and trend_score >= 0.35:
            level = "轻仓试错"
            pct = 0.03
        else:
            level = "剔除"
            pct = 0.0
        return {"level": level, "position_pct": pct,
                "base_score": base_score, "trend_score": trend_score}

    # 板块联动风控预警：板块内trend_score标准差过大→限制板块总持仓
    def check_industry_correlation_risk(self, industry_trend_map: dict) -> dict:
        """
        同行业板块联动强度过高时自动预警 (全局统一执行总约束指令 §5)
        输入 {ts_code: trend_score}, 输出板块联动风险等级
        """
        scores = list(industry_trend_map.values())
        if len(scores) < 2:
            return {"risk": "低", "std": 0.0, "n_stocks": len(scores),
                    "detail": "板块内标的不足2只，无法计算联动强度"}
        mean_s = sum(scores) / len(scores)
        var_s = sum((s - mean_s) ** 2 for s in scores) / len(scores)
        std_s = var_s ** 0.5
        # 标准差<0.05=高度联动(板块一致性强), 0.05~0.1=中等分化, >0.1=严重分化
        if std_s < 0.05:
            risk = "低"
            limit = "无限制"
        elif std_s < 0.1:
            risk = "中"
            limit = "板块总仓位上限降至50%"
        else:
            risk = "高"
            limit = "板块总仓位上限降至25%"
        return {"risk": risk, "std": round(std_s, 4), "mean_trend": round(mean_s, 4),
                "n_stocks": len(scores), "position_limit": limit,
                "detail": "板块联动强度{:.4f}({}), {}".format(std_s, risk, limit)}

    # 轻量化相似度筛选（SQL差值加权）
    def query_similar_market(self, ts_code: str, rsi: float, macd: float, sent: float, top_n=10):
        sql = """
            SELECT *, ABS(rsi-({rsi}))+ABS(macd-({macd}))+ABS(sentiment_score-({sent})) AS dist
            FROM memory_market WHERE ts_code='{code}'
            ORDER BY dist ASC LIMIT {n}
        """.format(rsi=rsi, macd=macd, sent=sent, code=ts_code, n=top_n)
        return pd.read_sql(sql, self.conn)

    # ========== 兼容原有 trading-memory-system 向量记忆读写接口 ==========
    def write_old_trade_memory(self, vec_record: dict):
        if "record_time" not in vec_record:
            vec_record["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = pd.DataFrame([vec_record])
        df.to_sql("trade_memory", self.conn, if_exists="append", index=False)

    def query_old_trade_vec(self, signal_name: str):
        sql = "SELECT * FROM trade_memory WHERE signal = '{}'".format(signal_name)
        return pd.read_sql(sql, self.conn)

    def write_global_rule(self, rule_dict: dict):
        if "record_time" not in rule_dict:
            rule_dict["record_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = pd.DataFrame([rule_dict])
        df.to_sql("global_rule", self.conn, if_exists="append", index=False)

    def query_global_rule(self, risk_level=None):
        if risk_level:
            sql = "SELECT * FROM global_rule WHERE risk_level >= {}".format(risk_level)
        else:
            sql = "SELECT * FROM global_rule ORDER BY risk_level DESC"
        return pd.read_sql(sql, self.conn)

    # 记忆老化清理：3年前行情迁移归档，压缩数据库
    def memory_aging_clean(self, archive_years=3):
        cutoff_date = (datetime.now() - pd.Timedelta(days=archive_years * 365)).strftime("%Y-%m-%d")
        cursor = self.conn.cursor()
        # 1. 迁移至归档表
        move_sql = "INSERT INTO memory_market_archive SELECT * FROM memory_market WHERE trade_date < '{}'".format(cutoff_date)
        cursor.execute(move_sql)
        move_rows = cursor.rowcount
        # 2. 删除主表过期数据
        del_sql = "DELETE FROM memory_market WHERE trade_date < '{}'".format(cutoff_date)
        cursor.execute(del_sql)
        del_rows = cursor.rowcount
        # 3. 先提交事务，再VACUUM
        self.conn.commit()
        cursor.execute("VACUUM;")
        self.conn.commit()
        return {"moved": move_rows, "deleted": del_rows, "cutoff": cutoff_date}

    # 全库备份CSV
    def backup_all_memory(self, save_dir="memory_backup"):
        import os
        os.makedirs(save_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        tables = ["trade_memory", "global_rule", "memory_market", "memory_trade_pnl",
                  "memory_failure_signal", "memory_black_swan", "memory_market_archive"]
        for tbl in tables:
            try:
                df = pd.read_sql("SELECT * FROM {}".format(tbl), self.conn)
                df.to_csv("{}/{}_{}.csv".format(save_dir, tbl, date_str), index=False, encoding="utf-8-sig")
            except Exception:
                pass

    def close(self):
        self.conn.close()
