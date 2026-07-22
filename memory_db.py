# memory_db.py
import sqlite3
import json
import numpy as np
from config_memory import DB_PATH

class TradingMemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_table()

    def _create_table(self):
        # 交易记忆主表：每一笔成交永久存储
        sql = """
        CREATE TABLE IF NOT EXISTS trade_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            market_env TEXT,             -- 牛市/震荡/熊市/高波动
            industry TEXT,
            feature_json TEXT,            -- 因子向量json字符串
            open_price REAL,
            close_price REAL,
            profit_rate REAL,
            hold_days INTEGER,
            memory_tag TEXT,              -- good/bad/normal
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        self.cursor.execute(sql)

        # 全局规则库：黑名单、失效因子永久固化
        sql_rule = """
        CREATE TABLE IF NOT EXISTS global_rule (
            rule_type TEXT,               -- black_stock / black_factor / market_param
            content TEXT,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(rule_type, content)
        )
        """
        self.cursor.execute(sql_rule)
        self.conn.commit()

    # 写入单条交易记忆
    def insert_trade(self, trade_data: dict):
        feature_str = json.dumps(trade_data["feature"])
        sql = """
        INSERT INTO trade_memory
            (stock_code, market_env, industry, feature_json, open_price, close_price, profit_rate, hold_days, memory_tag)
        VALUES (?,?,?,?,?,?,?,?,?)
        """
        self.cursor.execute(sql, (
            trade_data["stock_code"],
            trade_data["market_env"],
            trade_data["industry"],
            feature_str,
            trade_data["open_price"],
            trade_data["close_price"],
            trade_data["profit_rate"],
            trade_data["hold_days"],
            trade_data["memory_tag"]
        ))
        self.conn.commit()

    # 查询全部历史样本（蒸馏训练用）
    def get_all_samples(self):
        self.cursor.execute("SELECT * FROM trade_memory")
        rows = self.cursor.fetchall()
        samples = []
        for row in rows:
            feat = json.loads(row[4])
            samples.append({
                "stock_code": row[1],
                "market_env": row[2],
                "feature": np.array(feat, dtype=np.float32),
                "profit_rate": row[7],
                "tag": row[9]
            })
        return samples

    # 全局规则操作：新增黑名单
    def add_global_rule(self, rule_type, content):
        try:
            self.cursor.execute(
                "INSERT OR IGNORE INTO global_rule (rule_type, content) VALUES (?,?)",
                (rule_type, content)
            )
            self.conn.commit()
        except:
            pass

    # 加载黑名单
    def get_black_list(self, rule_type="black_stock"):
        self.cursor.execute(
            "SELECT content FROM global_rule WHERE rule_type=?", (rule_type,)
        )
        res = self.cursor.fetchall()
        return [i[0] for i in res]

    def close(self):
        self.conn.close()
