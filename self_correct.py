# -*- coding: utf-8 -*-
"""
self_correct.py — 模型自适应自我修正机制
全链路自动纠偏：打分偏差修正 / 因子权重自适应 / 数据源动态降级 / 市场风格识别 / 输出自校验
"""
import pandas as pd
import numpy as np
import gc
import json
import traceback
from datetime import datetime, timedelta
from functools import lru_cache

# ── 兼容导入：有config则用，无则返回None供调用方兜底 ──
try:
    from config import pg_engine, TARGET_CODES
except ImportError:
    pg_engine = None
    TARGET_CODES = []


# ========================================================================
# MODULE 1: 打分偏差追踪修正
# ========================================================================

class ScoreDeviationTracker:
    """
    每日对比模型历史打分与后续真实股价走势，识别偏差因子。
    存储至 memory_deviation 表（每只股票每日一条因子偏差明细）。
    """

    def __init__(self, lookback_days=30, accuracy_threshold=0.40):
        self.lookback = lookback_days
        self.threshold = accuracy_threshold  # 低于此阈值为失效因子
        self.table = "memory_deviation"

    def ensure_table(self):
        """自动建表"""
        if pg_engine is None:
            return False
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.table} (
            id SERIAL PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            stat_date DATE NOT NULL,
            factor_name VARCHAR(50) NOT NULL,
            predicted_score NUMERIC(6,2),
            actual_return NUMERIC(8,4),
            deviation NUMERIC(8,4),
            factor_accuracy NUMERIC(6,4),
            accuracy_status VARCHAR(20) DEFAULT 'active',
            correction_weight NUMERIC(6,4) DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(ts_code, stat_date, factor_name)
        )
        """
        with pg_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        return True

    def compute_accuracy(self, code):
        """
        计算最近lookback天内各因子历史胜率。
        比对规则：打分>=60（看多）→ 后续5日涨幅>0为正确；
                  打分<=40（看空）→ 后续5日涨幅<0为正确。
        """
        if pg_engine is None:
            return {}, "no_db"

        # 读取历史打分
        sql_p = text("""
            SELECT p.trade_date, p.confidence,
                   d.close AS close_now,
                   LEAD(d.close, 5) OVER(ORDER BY d.trade_date) AS close_5d
            FROM stock_predict p
            JOIN stock_daily d ON d.ts_code = p.ts_code
                AND d.trade_date = p.trade_date
            WHERE p.ts_code = :code
            ORDER BY d.trade_date DESC
            LIMIT :lim
        """)
        df = pd.read_sql(sql_p, pg_engine, params={"code": code, "lim": self.lookback})
        if df.empty:
            return {}, "no_data"

        df["return_5d"] = (df["close_5d"].astype(float) - df["close_now"].astype(float)) / df["close_now"].astype(float)
        df["correct"] = ((df["confidence"] >= 60) & (df["return_5d"] > 0)) | \
                        ((df["confidence"] <= 40) & (df["return_5d"] < 0))
        total = len(df)
        correct = int(df["correct"].sum())
        accuracy = correct / total if total > 0 else 0.0

        result = {
            "total_predictions": total,
            "correct": correct,
            "accuracy": round(accuracy, 4),
            "status": "active" if accuracy >= self.threshold else "warning",
            "threshold": self.threshold,
        }
        del df
        return result, "ok"

    def get_adjustment_factor(self, code):
        """
        返回偏差修正系数 [0.5, 1.5]。
        accuracy<0.3 → 强制扣置信系数*0.7
        accuracy>0.75 → 加置信系数*1.2
        """
        acc_data, status = self.compute_accuracy(code)
        if status != "ok":
            return 1.0, status

        acc = acc_data["accuracy"]
        if acc < 0.20:
            adj = 0.5
        elif acc < 0.30:
            adj = 0.7
        elif acc < 0.40:
            adj = 0.85
        elif acc > 0.75:
            adj = 1.2
        elif acc > 0.60:
            adj = 1.1
        else:
            adj = 1.0

        return round(adj, 4), f"acc={acc:.1%}→adj={adj}"


# ========================================================================
# MODULE 2: 因子权重自适应修正
# ========================================================================

class FactorWeightCorrect:
    """
    连续判断失误时自动标记对应因子，生成临时权重修正参数。
    写入 factor_correction 表，供 scoring engine 读取覆盖原始权重。
    """

    FACTOR_LIST = [
        # [因子名, 默认权重, 分组]
        ["pe_valuation",    0.12, "valuation"],
        ["pb_discount",     0.06, "valuation"],
        ["volume_price",    0.10, "technical"],
        ["ma_system",       0.10, "technical"],
        ["earnings_moment", 0.10, "fundamental"],
        ["asset_quality",   0.08, "fundamental"],
        ["event_driven",    0.04, "fundamental"],
        ["sentiment_pe",    0.06, "sentiment"],
        ["northbound",      0.04, "sentiment"],
        ["xueqiu",          0.02, "sentiment"],
        ["flow_10d",        0.12, "capital"],
        ["big_order",       0.08, "capital"],
        ["sector_momentum", 0.06, "sector"],
        ["industry_pos",    0.04, "sector"],
    ]
    # 分组默认权重(修正因子不覆盖组上限)
    GROUP_CAP = {
        "valuation":   0.20,
        "technical":   0.22,
        "fundamental": 0.25,
        "sentiment":   0.15,
        "capital":     0.22,
        "sector":      0.12,
    }

    def __init__(self, min_history=5):
        self.min_history = min_history  # 最少样本数才启动修正
        self.table = "factor_correction"

    def ensure_table(self):
        if pg_engine is None:
            return False
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.table} (
            id SERIAL PRIMARY KEY,
            factor_name VARCHAR(50) UNIQUE NOT NULL,
            default_weight NUMERIC(6,4),
            corrected_weight NUMERIC(6,4),
            correction_reason TEXT,
            consecutive_errors INT DEFAULT 0,
            is_degraded BOOLEAN DEFAULT FALSE,
            valid_from DATE,
            valid_until DATE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
        with pg_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        return True

    def initialize_factors(self):
        """写入因子默认权重（首次运行）"""
        if pg_engine is None:
            return
        self.ensure_table()
        today = datetime.now().date()
        # 清空失效的旧记录
        with pg_engine.connect() as conn:
            conn.execute(text(f"DELETE FROM {self.table} WHERE valid_until < :d"),
                         {"d": today})
            conn.commit()

        for name, weight, _ in self.FACTOR_LIST:
            sql = text(f"""
                INSERT INTO {self.table}
                (factor_name, default_weight, corrected_weight, correction_reason,
                 consecutive_errors, is_degraded, valid_from, valid_until)
                VALUES (:n, :w, :w, 'initial', 0, FALSE, :f, :u)
                ON CONFLICT (factor_name)
                DO UPDATE SET default_weight = :w2,
                              corrected_weight = CASE
                                  WHEN {self.table}.is_degraded THEN {self.table}.corrected_weight
                                  ELSE :w3
                              END,
                              updated_at = NOW()
            """)
            next_year = today.replace(year=today.year + 1)
            with pg_engine.connect() as conn:
                conn.execute(sql, {"n": name, "w": weight, "f": today, "u": next_year,
                                    "w2": weight, "w3": weight})
                conn.commit()

    def degrade_factor(self, factor_name, reason):
        """标记因子降级并降低权重"""
        if pg_engine is None:
            return
        sql = text(f"""
            UPDATE {self.table}
            SET corrected_weight = default_weight * 0.3,
                is_degraded = TRUE,
                consecutive_errors = consecutive_errors + 1,
                correction_reason = :reason,
                updated_at = NOW()
            WHERE factor_name = :name
        """)
        with pg_engine.connect() as conn:
            conn.execute(sql, {"name": factor_name, "reason": reason})
            conn.commit()

    def boost_factor(self, factor_name, reason):
        """因子表现优秀，提升权重乘1.2（不超过组上限）"""
        if pg_engine is None:
            return
        sql = text(f"""
            UPDATE {self.table}
            SET corrected_weight = LEAST(default_weight * 1.2, :cap),
                consecutive_errors = 0,
                correction_reason = :reason,
                updated_at = NOW()
            WHERE factor_name = :name
        """)
        # 找分组上限
        group_cap = 0.15
        for n, w, g in self.FACTOR_LIST:
            if n == factor_name:
                group_cap = self.GROUP_CAP.get(g, 0.15)
                break
        with pg_engine.connect() as conn:
            conn.execute(sql, {"name": factor_name, "cap": group_cap,
                                "reason": reason})
            conn.commit()

    def get_active_weights(self):
        """返回当前有效的 {因子名: 修正后权重} 字典"""
        if pg_engine is None:
            return {n: w for n, w, _ in self.FACTOR_LIST}
        sql = text(f"""
            SELECT factor_name, corrected_weight
            FROM {self.table}
            WHERE valid_from <= :today AND valid_until >= :today
        """)
        df = pd.read_sql(sql, pg_engine, params={"today": datetime.now().date()})
        if df.empty:
            return {n: w for n, w, _ in self.FACTOR_LIST}
        result = dict(zip(df["factor_name"], df["corrected_weight"].astype(float)))
        del df
        return result

    def auto_correct(self, code, accuracy_data):
        """
        根据deviation_tracker计算的accuracy，自动升降因子。
        accuracy < 0.25 → 整体降级
        accuracy 0.25-0.35 → 风控因子降级
        accuracy > 0.70 → 动量因子提升
        """
        if not accuracy_data or accuracy_data.get("status") != "ok":
            return {"corrected": False, "reason": "no_accuracy_data"}

        acc = accuracy_data.get("accuracy", 0.5)

        corrections = []
        if acc < 0.25:
            # 全面降级
            for name, _, group in self.FACTOR_LIST:
                if group != "fundamental":
                    self.degrade_factor(name,
                        f"全局acc={acc:.1%}<25%→全面降级")
                    corrections.append(name)
        elif acc < 0.35:
            # 风控/技术类降级
            for name, _, group in self.FACTOR_LIST:
                if group in ("technical", "sentiment"):
                    self.degrade_factor(name,
                        f"acc={acc:.1%}<35%→技术/情绪降级")
                    corrections.append(name)
        elif acc > 0.70:
            # 提升动量+资金因子
            for name, _, group in self.FACTOR_LIST:
                if group in ("capital", "sector"):
                    self.boost_factor(name,
                        f"acc={acc:.1%}>70%→资金/板块提升")
                    corrections.append(name)

        if acc > 0.85:
            for name, _, group in self.FACTOR_LIST:
                if group == "fundamental":
                    self.boost_factor(name,
                        f"acc={acc:.1%}>85%→基本面因子提升")
                    corrections.append(name)

        return {
            "corrected": len(corrections) > 0,
            "factors_adjusted": corrections,
            "reason": f"acc={acc:.1%}→{len(corrections)}项调整",
        }


# ========================================================================
# MODULE 3: 数据源动态自适应
# ========================================================================

class DataSourceAdaptive:
    """
    数据源动态自适应。
    追踪各接口调用成功率，自动调整实时数据与缓存数据的权重。
    写入 data_source_status 表。
    """

    def __init__(self, window=20):
        self.window = window
        self.table = "data_source_status"

    def ensure_table(self):
        if pg_engine is None:
            return False
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.table} (
            id SERIAL PRIMARY KEY,
            source_name VARCHAR(30) NOT NULL,
            call_date DATE NOT NULL,
            success_count INT DEFAULT 0,
            fail_count INT DEFAULT 0,
            consecutive_fail INT DEFAULT 0,
            status VARCHAR(20) DEFAULT 'active',
            realtime_weight NUMERIC(6,4) DEFAULT 1.0,
            cache_weight NUMERIC(6,4) DEFAULT 0.0,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(source_name, call_date)
        )
        """
        with pg_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        return True

    def record_call(self, source, success=True):
        """记录一次接口调用结果"""
        if pg_engine is None:
            return
        self.ensure_table()
        today = datetime.now().date()
        inc_s = 1 if success else 0
        inc_f = 0 if success else 1
        sql = text(f"""
            INSERT INTO {self.table}
            (source_name, call_date, success_count, fail_count, consecutive_fail, status)
            VALUES (:src, :d, :s, :f, CASE WHEN :f THEN 1 ELSE 0 END, 'active')
            ON CONFLICT (source_name, call_date)
            DO UPDATE SET
                success_count = {self.table}.success_count + :s2,
                fail_count = {self.table}.fail_count + :f2,
                consecutive_fail = CASE
                    WHEN :f3 THEN {self.table}.consecutive_fail + 1
                    ELSE 0
                END,
                status = CASE
                    WHEN {self.table}.consecutive_fail >= 5 THEN 'degraded'
                    WHEN {self.table}.consecutive_fail >= 10 THEN 'disabled'
                    ELSE 'active'
                END,
                updated_at = NOW()
        """)
        params = {"src": source, "d": today, "s": inc_s, "f": inc_f,
                  "s2": inc_s, "f2": inc_f, "f3": not success}
        with pg_engine.connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    def get_source_weights(self):
        """
        返回 {source_name: {"realtime_weight": 0.x, "cache_weight": 0.x}}
        """
        if pg_engine is None:
            return {"tushare": {"realtime_weight": 1.0, "cache_weight": 0.0}}
        today = datetime.now().date()
        start = today - timedelta(days=5)  # 近5日状态
        sql = text(f"""
            SELECT source_name,
                   SUM(success_count) as total_ok,
                   SUM(fail_count) as total_fail,
                   MAX(consecutive_fail) as max_consec
            FROM {self.table}
            WHERE call_date >= :start
            GROUP BY source_name
        """)
        df = pd.read_sql(sql, pg_engine, params={"start": start})
        results = {}
        for _, row in df.iterrows():
            src = row["source_name"]
            ok = int(row["total_ok"])
            fail = int(row["total_fail"])
            consec = int(row["max_consec"])
            total = ok + fail
            if total == 0:
                rw, cw = 1.0, 0.0
            elif consec >= 10:
                rw, cw = 0.0, 1.0  # 完全禁用实时
            elif consec >= 5:
                rw, cw = 0.3, 0.7  # 降级
            elif fail / total > 0.5:
                rw, cw = 0.5, 0.5  # 一半用缓存
            else:
                rw, cw = 1.0, 0.0

            results[src] = {
                "realtime_weight": rw,
                "cache_weight": cw,
                "status": "active" if rw >= 0.5 else ("degraded" if rw > 0 else "disabled"),
                "consecutive_fail": consec,
            }

        # 默认兜底
        if "tushare" not in results:
            results["tushare"] = {"realtime_weight": 1.0, "cache_weight": 0.0,
                                   "status": "active", "consecutive_fail": 0}
        if "xueqiu" not in results:
            results["xueqiu"] = {"realtime_weight": 0.0, "cache_weight": 1.0,
                                  "status": "disabled", "consecutive_fail": 99}
        del df
        return results

    def get_cache_fallback_data(self, code, field_group="daily"):
        """
        从本地缓存（stock_daily / stock_money_flow 已有历史数据）取权重
        返回: weight_multiplier (缓存不足时折扣)
        """
        if pg_engine is None:
            return 1.0
        # 检查stock_daily是否有足够历史数据(>=60条)
        sql = text("SELECT COUNT(*) as cnt FROM stock_daily WHERE ts_code = :code")
        cnt = pd.read_sql(sql, pg_engine, params={"code": code})["cnt"].iloc[0]
        if int(cnt) >= 60:
            return 1.0
        elif int(cnt) >= 20:
            return 0.7
        else:
            return 0.3


# ========================================================================
# MODULE 4: 市场风格识别
# ========================================================================

class MarketRegimeDetector:
    """
    自动识别市场风格切换：资金投机行情 vs 基本面价值行情。
    用8只标的的整体表现作为市场状态代理指标。
    输出: regime (momentum/fundamental/neutral) + 置信度
    """

    def __init__(self, lookback=10):
        self.lookback = lookback
        self.regime_table = "market_regime"

    def ensure_table(self):
        if pg_engine is None:
            return False
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.regime_table} (
            id SERIAL PRIMARY KEY,
            stat_date DATE UNIQUE NOT NULL,
            regime VARCHAR(20) NOT NULL,
            confidence NUMERIC(6,4),
            avg_return NUMERIC(8,4),
            vol_ratio NUMERIC(8,4),
            hot_count INT DEFAULT 0,
            large_premium NUMERIC(6,4),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
        with pg_engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        return True

    def detect(self):
        """
        风格判断算法：
        1. 读取所有8只标的最近lookback日涨幅
        2. 计算平均涨幅、涨幅标准差、>5%占比
        3. 动量行情(momentum): 平均涨幅>2%且>5%占比高 (>30%)
        4. 价值行情(fundamental): 平均涨幅<0.5%且低波动
        5. 否则 neutral
        """
        if pg_engine is None or not TARGET_CODES:
            return {"regime": "neutral", "confidence": 0.5,
                    "reason": "no_data_available"}

        codes_tuple = tuple(TARGET_CODES)
        sql = text(f"""
            SELECT ts_code, trade_date, pct_chg, amount
            FROM stock_daily
            WHERE ts_code IN :codes
            ORDER BY trade_date DESC
            LIMIT :lim
        """)
        df = pd.read_sql(sql, pg_engine,
                          params={"codes": codes_tuple, "lim": self.lookback * len(TARGET_CODES)})
        if df.empty:
            return {"regime": "neutral", "confidence": 0.5,
                    "reason": "no_daily_data"}

        # 按日期聚合
        df["pct_chg"] = df["pct_chg"].astype(float)
        daily_avg = df.groupby("trade_date")["pct_chg"].agg(["mean", "std", "count"]).reset_index()
        daily_avg = daily_avg.sort_values("trade_date", ascending=False).head(min(self.lookback, len(daily_avg)))

        if daily_avg.empty:
            return {"regime": "neutral", "confidence": 0.5, "reason": "empty_after_group"}

        avg_return = float(daily_avg["mean"].mean())
        avg_std = float(daily_avg["std"].mean())
        # 热门占比：单日涨幅>5%的股票占比
        hot_ratio = float((df.groupby("trade_date")["pct_chg"].apply(
            lambda x: (x > 5).mean())).mean())

        # 量能比：最新日与前20日均量对比
        latest = df[df["trade_date"] == df["trade_date"].max()]
        vol_ratio = 1.0
        if not latest.empty:
            latest_vol = float(latest["amount"].astype(float).mean())
            older = df[df["trade_date"] < df["trade_date"].max()]
            if not older.empty:
                avg_vol = float(older["amount"].astype(float).mean())
                vol_ratio = latest_vol / avg_vol if avg_vol > 0 else 1.0

        # 判定逻辑
        if avg_return > 1.5 and hot_ratio > 0.25 and vol_ratio > 1.3:
            regime = "momentum"
            confidence = min(0.95, 0.5 + avg_return / 10 + hot_ratio)
        elif avg_return < 0.3 and avg_std < 2.0:
            regime = "fundamental"
            confidence = min(0.85, 0.5 + (2.0 - avg_std) / 5)
        else:
            regime = "neutral"
            confidence = 0.5

        self._store_regime(regime, confidence, avg_return, vol_ratio, hot_ratio)
        del df, daily_avg

        return {
            "regime": regime,
            "confidence": round(confidence, 4),
            "avg_return": round(avg_return, 4),
            "vol_ratio": round(vol_ratio, 4),
            "hot_ratio": round(hot_ratio, 4),
            "reason": f"avg_ret={avg_return:.1f}% hot={hot_ratio:.0%} vol_ratio={vol_ratio:.2f}",
        }

    def _store_regime(self, regime, confidence, avg_return, vol_ratio, hot_ratio):
        if pg_engine is None:
            return
        self.ensure_table()
        sql = text(f"""
            INSERT INTO {self.regime_table}
            (stat_date, regime, confidence, avg_return, vol_ratio, hot_count, large_premium)
            VALUES (:d, :r, :c, :ar, :vr, :hc, :lp)
            ON CONFLICT (stat_date)
            DO UPDATE SET regime = :r2, confidence = :c2, updated_at = NOW()
        """)
        with pg_engine.connect() as conn:
            conn.execute(sql, {
                "d": datetime.now().date(),
                "r": regime, "c": confidence,
                "ar": avg_return, "vr": vol_ratio,
                "hc": int(hot_ratio * 100),
                "lp": round(avg_return * 0.3, 4),
                "r2": regime, "c2": confidence,
            })
            conn.commit()

    def get_regime_adjustment(self):
        """
        返回基于市场风格的因子侧重调整系数。
        momentum → capital/flow因子权重×1.3, fundamental×0.7
        fundamental → fundamental×1.2, flow×0.8
        neutral → 不调整
        """
        regime_data = self.detect()
        regime = regime_data["regime"]
        reasons = [f"regime={regime}({regime_data.get('reason','')[:30]})"]

        if regime == "momentum":
            # 投机行情：资金流/情绪因子权重提升
            result = {
                "flow_multiplier": 1.3,
                "sentiment_multiplier": 1.2,
                "fundamental_multiplier": 0.7,
                "valuation_multiplier": 0.8,
                "reason": "; ".join(reasons + ["投机行情→资金流+30%基本面-30%"]),
            }
        elif regime == "fundamental":
            result = {
                "flow_multiplier": 0.8,
                "sentiment_multiplier": 0.8,
                "fundamental_multiplier": 1.2,
                "valuation_multiplier": 1.1,
                "reason": "; ".join(reasons + ["价值行情→基本面+20%资金流-20%"]),
            }
        else:
            result = {
                "flow_multiplier": 1.0,
                "sentiment_multiplier": 1.0,
                "fundamental_multiplier": 1.0,
                "valuation_multiplier": 1.0,
                "reason": "; ".join(reasons + ["中性市场, 不调整"]),
            }
        return result


# ========================================================================
# MODULE 5: 输出自校验器
# ========================================================================

class PreOutputValidator:
    """
    每次输出报告/代码前自动执行全套自校验。
    检查项：
    - 数值钳位：confidence [0,100], position >=0, weight [0,1]
    - 规则一致性：pred=-1必须pos=0%, pred=1必须pos>0%
    - 逻辑闭环：风险分高(>40)则仓位≤3%
    - NaN/Inf检测
    - 自动修复：发现冲突自动修正+日志
    """

    def __init__(self):
        self.errors = []
        self.fixes = []
        self.warnings = []

    def validate_numeric(self, val, name, lo=0, hi=100, default=50):
        """钳位数值，超范围自动修正"""
        if val is None:
            self.fixes.append(f"{name}: None→{default}")
            return default
        try:
            v = float(val)
        except (ValueError, TypeError):
            self.fixes.append(f"{name}: {val}→{default}")
            return default
        if np.isnan(v) or np.isinf(v):
            self.fixes.append(f"{name}: {v}→{default}")
            return default
        if v < lo or v > hi:
            orig = v
            v = max(lo, min(hi, v))
            self.fixes.append(f"{name}: {orig}→{v} (钳位[{lo},{hi}])")
        return v

    def validate_rule_consistency(self, score, pred, position, risk_score):
        """
        规则一致性校验：
        - pred=-1 → position=0%
        - pred=1  → position>0%
        - risk>40 → position<=3%
        - score>=80 → pred=1
        - score<40  → pred=-1
        """
        issues = []

        if pred == -1 and position != "0%":
            issues.append(f"pred=-1但仓位{position}→修正为0%")

        if pred == 1 and position == "0%":
            issues.append(f"pred=1但仓位0%→修正为3%")

        if risk_score is not None:
            rs = float(risk_score)
            if rs > 40:
                pos_val = float(position.replace("%", ""))
                if pos_val > 3:
                    issues.append(f"风险{rs}>40但仓位{position}→修正为3%")
                    position = "3%"

        if score is not None:
            s = float(score)
            if s >= 80 and pred != 1:
                issues.append(f"评分{s}>=80但pred={pred}→修正为pred=1")
            if s < 40 and pred != -1:
                issues.append(f"评分{s}<40但pred={pred}→修正为pred=-1")

        for issue in issues:
            self.fixes.append(issue)

        # 执行修正
        if pred == -1 and position != "0%":
            position = "0%"
        if pred == 1 and position == "0%":
            position = "3%"
        if risk_score and float(risk_score) > 40:
            pos_val = float(position.replace("%", ""))
            if pos_val > 3:
                position = "3%"
        if score and float(score) >= 80 and pred != 1:
            pred = 1
        if score and float(score) < 40 and pred != -1:
            pred = -1

        return pred, position

    def validate_result_dict(self, d):
        """对scoring result dict做全量校验"""
        if not isinstance(d, dict):
            self.errors.append("result不是dict")
            return d

        # confidence 钳位
        if "confidence" in d:
            d["confidence"] = self.validate_numeric(d["confidence"], "confidence", 0, 100, 50)

        # base_score
        if "base_score" in d:
            d["base_score"] = self.validate_numeric(d["base_score"], "base_score", 0, 100, 50)

        # position 校验
        if "position" in d:
            pos = d["position"]
            if isinstance(pos, str) and "%" in pos:
                pv = float(pos.replace("%", ""))
                d["position"] = self.validate_numeric(pv, "position", 0, 100, 0)
                d["position"] = f"{d['position']}%"
            else:
                d["position"] = "0%"

        # 规则一致性
        pred = d.get("predict_result")
        position = d.get("position", "0%")
        risk = d.get("risk_score")
        pred_fixed, pos_fixed = self.validate_rule_consistency(
            d.get("confidence"), pred, position, risk)
        d["predict_result"] = pred_fixed
        d["position"] = pos_fixed

        # scenario_stress 子结构校验
        if "scenario_stress" in d and isinstance(d["scenario_stress"], dict):
            ss = d["scenario_stress"]
            for key in ("scenarios", "stress_tests"):
                if key in ss and isinstance(ss[key], dict):
                    for sub_key, sub_val in ss[key].items():
                        if isinstance(sub_val, dict) and "score" in sub_val:
                            sub_val["score"] = self.validate_numeric(
                                sub_val["score"], f"{key}.{sub_key}.score", 0, 100, 50)

        return d

    def validate_report_json(self, report_dict):
        """对完整JSON报告做全量校验"""
        if not isinstance(report_dict, dict):
            self.errors.append("report不是dict")
            return report_dict

        # 检查必需模块
        required_keys = [
            "table_market", "tech_signal", "fundamental", "market_sentiment",
            "capital_flow", "sector_track", "multibear_logic", "risk_control",
            "quant_control", "conclusion",
        ]
        for k in required_keys:
            if k not in report_dict:
                self.warnings.append(f"缺少模块: {k}")

        # 数值钳位
        for num_key in ["final_score", "risk_score"]:
            if num_key in report_dict:
                report_dict[num_key] = self.validate_numeric(
                    report_dict[num_key], num_key, 0, 100, 50)

        return report_dict

    def report(self):
        """输出校验报告"""
        n_errors = len(self.errors)
        n_fixes = len(self.fixes)
        n_warnings = len(self.warnings)
        total = n_errors + n_fixes + n_warnings
        ok = n_errors == 0

        lines = [f"[PreOutputValidator] 校验{'通过' if ok else '未通过'} "
                 f"(errors={n_errors}, fixes={n_fixes}, warnings={n_warnings})"]
        for e in self.errors:
            lines.append(f"  ❌ {e}")
        for f in self.fixes:
            lines.append(f"  🔧 {f}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")

        return {
            "ok": ok,
            "total_issues": total,
            "errors": self.errors,
            "fixes": self.fixes,
            "warnings": self.warnings,
            "text": "\n".join(lines),
        }


# ========================================================================
# MODULE 6: 全链路自适应主控
# ========================================================================

class SelfCorrectPipeline:
    """
    主控：组装所有修正模块，一次调用完成全链路校正。
    在 scoring / trading / reporting 阶段分别调用对应方法。
    """

    def __init__(self):
        self.deviation = ScoreDeviationTracker()
        self.weights = FactorWeightCorrect()
        self.source = DataSourceAdaptive()
        self.regime = MarketRegimeDetector()
        self.validator = PreOutputValidator()
        self._init_tables()

    def _init_tables(self):
        """延迟初始化：建表只在有DB时"""
        if pg_engine:
            try:
                self.deviation.ensure_table()
                self.weights.ensure_table()
                self.weights.initialize_factors()
                self.source.ensure_table()
                self.regime.ensure_table()
            except Exception as e:
                print(f"[self_correct] init_warning: {e}")

    def correct_scoring(self, code, score_result):
        """
        打分阶段自适应修正流程：
        1. data_source adaptive → 记录tushare调用
        2. deviation_tracker → 计算历史偏差修正系数
        3. factor_weights → 按偏差修正权重
        4. market_regime → 按市场风格调整因子侧重
        5. validator → 输出前自校验+自动修复
        """
        # 1. 数据源记录
        self.source.record_call("tushare", success=True)
        source_weights = self.source.get_source_weights()
        cache_weight = source_weights.get("tushare", {}).get("cache_weight", 0.0)

        # 2. 偏差修正系数
        adj_factor, adj_reason = self.deviation.get_adjustment_factor(code)
        if score_result and "confidence" in score_result:
            original_conf = score_result["confidence"]
            score_result["confidence"] = max(0, min(100,
                round(original_conf * adj_factor)))
            score_result.setdefault("deviation_adjustment", {})
            score_result["deviation_adjustment"] = {
                "original_confidence": original_conf,
                "adjustment_factor": adj_factor,
                "reason": adj_reason,
                "cache_weight": cache_weight,
            }

        # 3. 因子权重修正
        acc_data, acc_status = self.deviation.compute_accuracy(code)
        if acc_status == "ok":
            weight_correction = self.weights.auto_correct(code, acc_data)
            score_result["weight_correction"] = weight_correction

        # 4. 市场风格调整
        regime_adjust = self.regime.get_regime_adjustment()
        score_result["regime_adjustment"] = regime_adjust

        # 5. 输出自校验
        score_result = self.validator.validate_result_dict(score_result)

        return score_result

    def correct_trading(self, code, trade_signal):
        """交易信号阶段自适应（缩短版）"""
        # 数据源自适应
        source_weights = self.source.get_source_weights()
        cache_w = source_weights.get("tushare", {}).get("cache_weight", 0.0)
        if cache_w > 0.5 and trade_signal:
            # 缓存权重高 → 信号置信度打折扣
            if "confidence" in trade_signal:
                trade_signal["confidence"] = max(0, min(100,
                    round(float(trade_signal["confidence"]) * (1 - cache_w * 0.3))))

        # 市场风格检查
        regime_data = self.regime.detect()
        if regime_data["regime"] == "momentum" and trade_signal:
            # 投机行情下放宽信号阈值
            trade_signal.setdefault("regime_note", "momentum: 信号阈值放宽")

        # 输出自校验
        if trade_signal:
            trade_signal = self.validator.validate_result_dict(trade_signal)
        return trade_signal

    def correct_reporting(self, report_dict):
        """报告阶段自校验"""
        report_dict = self.validator.validate_report_json(report_dict)
        return report_dict

    def run_full_check(self, code=None):
        """
        全链路自检，用于开机/cron前健康检查。
        输出每个模块的状态。
        """
        checks = {}

        # 数据源检查
        try:
            sw = self.source.get_source_weights()
            checks["data_source"] = {
                "status": "ok",
                "details": sw,
            }
        except Exception as e:
            checks["data_source"] = {"status": "error", "error": str(e)}

        # 市场风格
        try:
            rd = self.regime.detect()
            checks["market_regime"] = {
                "status": "ok",
                "details": rd,
            }
        except Exception as e:
            checks["market_regime"] = {"status": "error", "error": str(e)}

        # 因子权重
        try:
            fw = self.weights.get_active_weights()
            checks["factor_weights"] = {
                "status": "ok",
                "factor_count": len(fw),
                "weights": fw,
            }
        except Exception as e:
            checks["factor_weights"] = {"status": "error", "error": str(e)}

        # 如果有具体代码，做打分偏差检查
        if code:
            try:
                acc, status = self.deviation.compute_accuracy(code)
                checks["score_deviation"] = {
                    "status": status,
                    "details": acc,
                }
            except Exception as e:
                checks["score_deviation"] = {"status": "error", "error": str(e)}

        # 校验器自检
        v = PreOutputValidator()
        checks["validator"] = {
            "status": "ok",
            "has_errors": len(v.errors) > 0,
            "has_warnings": len(v.warnings) > 0,
        }

        all_ok = all(c.get("status") == "ok" for c in checks.values())
        return {"all_ok": all_ok, "checks": checks}


# ========================================================================
# 全局单例（供 import 方直接使用）
# ========================================================================
self_correct = SelfCorrectPipeline()

# 便捷函数
def correct_scoring(code, score_result):
    return self_correct.correct_scoring(code, score_result)

def correct_trading(code, trade_signal):
    return self_correct.correct_trading(code, trade_signal)

def correct_reporting(report_dict):
    return self_correct.correct_reporting(report_dict)

def run_full_check(code=None):
    return self_correct.run_full_check(code)


# ── 独立运行：健康检查 ──
if __name__ == "__main__":
    print("=" * 60)
    print("【全链路自适应自我修正 - 健康检查】")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    check_result = run_full_check()
    print(f"\n总体状态: {'✅ 全部通过' if check_result['all_ok'] else '❌ 存在异常'}")
    for module, info in check_result["checks"].items():
        s = info.get("status", "unknown")
        icon = "✅" if s == "ok" else "❌"
        print(f"  {icon} {module}: {s}")
        if "details" in info and isinstance(info["details"], dict):
            for k, v in info["details"].items():
                print(f"      {k}: {v}")
        if "error" in info:
            print(f"      error: {info['error']}")

    # 模拟校验
    v = PreOutputValidator()
    test_result = {"confidence": 150, "position": "25%", "predict_result": 0}
    fixed = v.validate_result_dict(test_result)
    v_report = v.report()
    print(f"\n自校验模拟: confidence 150→{fixed['confidence']}")
    print(v_report["text"])
    print("=" * 60)
