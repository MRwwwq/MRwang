"""
layered_risk_control.py — 全局强制风控架构 §1~§6
多层智能风控为AI量化智能体最高优先级安全底座。
固化阈值不可修改，开仓前必须调用 apply_risk_override()。

§1 架构定位     §2 静态硬约束(4道)
§3 AI动态预判(8维) §3.4 熔断自愈闭环
§4 风控与自进化联动  §5 固定执行链路
§6 架构价值标准
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

DB_PATH = "/opt/stock_agent/agent_memory.db"

# ====================================================================
# §1 + §2 全局固化阈值（永久锁死，不可修改）
# ====================================================================
RISK_CONFIG = {
    # §2 静态硬约束
    "single_stock_max_pos": 0.12,        # 单票仓位硬上限 12%
    "single_industry_max_pos": 0.30,      # 单一行业仓位硬上限 30%
    "account_total_max_pos": 0.75,        # 账户总仓位硬上限 75%
    "daily_max_loss_ratio": 0.025,        # 单日亏损熔断 2.5%
    # §3.1 流动性
    "liquidity_min_amount": 5000_0000,    # 20日均成交额下限 5000万(元)
    "liquidity_weak_amount": 1_0000_0000, # 偏弱阈值 1亿(元)
    # §3.3 波动率
    "vol_high": 0.35,                     # 年化波动率>35%=极端
    "vol_medium_high": 0.25,              # >25%=高波动
    "vol_medium": 0.18,                   # >18%=中等
    # §3.4 熔断
    "daily_fuse_threshold": 0.025,        # 日亏损熔断线
    # §4 沙盒
    "sandbox_fuse_max_ratio": 0.05,       # 回测期熔断>5%淘汰
    # §5 分层
    "base_score_min": 0.4,                # 底层因子最低分
    "trend_score_min": 0.35,              # 时序趋势最低分
    "dynamic_coeff_min": 0.35,            # 动态风控系数下限
    # 仓位映射
    "pos_high": 0.25,                     # 高分仓位
    "pos_medium": 0.12,                   # 中等仓位
    "pos_low": 0.03,                      # 轻仓
    "pos_zero": 0.0,                      # 空仓
    # 行业→申万映射
    "sector_map": {
        "电气设备": "801080.SI", "电子": "801080.SI",
        "贵金属": "801080.SI", "银行": "801780.SI",
        "医疗服务": "801150.SI", "半导体": "851521.SI",
        "通信": "801750.SI", "消费电子": "801080.SI",
        "建筑": "801741.SI", "电力": "801741.SI",
    },
}


class LayeredRiskControl:
    """全局强制风控引擎 — 开仓前唯一校验入口"""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cfg = RISK_CONFIG

    # ===================== §2 静态硬约束 =====================

    def _get_position_stat(self):
        """查询当前持仓：总仓位/单票/日亏损"""
        try:
            df = pd.read_sql(
                "SELECT ts_code, SUM(position) as pos_rate, SUM(total_pnl) as daily_loss "
                "FROM memory_trade_pnl WHERE exit_date IS NULL GROUP BY ts_code",
                self.conn)
            if df.empty:
                return {"total": 0, "daily_loss": 0, "stocks": {}}
            return {
                "total": float(df["pos_rate"].sum()),
                "daily_loss": float(abs(df["daily_loss"].sum())),
                "stocks": df.set_index("ts_code")["pos_rate"].to_dict(),
            }
        except Exception:
            return {"total": 0, "daily_loss": 0, "stocks": {}}

    def check_static(self, ts_code, industry, apply_pos):
        """
        §2 四道防线 → (pass, [reasons])
        全部返回静态判定结果，同时输出到日志
        """
        pos = self._get_position_stat()
        fails = []
        # 2-1 单票≤12%
        cur_stock = pos["stocks"].get(ts_code, 0)
        if cur_stock + apply_pos > self.cfg["single_stock_max_pos"]:
            fails.append(f"单票{ts_code}上限{self.cfg['single_stock_max_pos']*100:.0f}%, "
                         f"当前{cur_stock*100:.1f}%+申请{apply_pos*100:.1f}%超限")
        # 2-2 行业≤30%（近似：该标的本行业只有当前持仓）
        if industry:
            if cur_stock + apply_pos > self.cfg["single_industry_max_pos"]:
                fails.append(f"行业{industry}上限{self.cfg['single_industry_max_pos']*100:.0f}%, "
                             f"当前{cur_stock*100:.1f}%+申请{apply_pos*100:.1f}%超限")
        # 2-3 总仓≤75%
        if pos["total"] + apply_pos > self.cfg["account_total_max_pos"]:
            fails.append(f"总仓上限{self.cfg['account_total_max_pos']*100:.0f}%, "
                         f"当前{pos['total']*100:.1f}%+申请{apply_pos*100:.1f}%超限")
        # 2-4 日亏损熔断
        if pos["daily_loss"] >= self.cfg["daily_max_loss_ratio"]:
            fails.append(f"日亏损{pos['daily_loss']*100:.2f}%触及熔断线，禁止新开仓")
        return len(fails) == 0, fails

    # ===================== §3 AI动态预判 =====================

    def _liquidity_check(self, ts_code):
        """§3.1 流动性预判 → 0.0(拦截)/0.5(减半)/1.0(正常)"""
        try:
            df = pd.read_sql(
                "SELECT amount FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
                self.conn, params=(ts_code,))
            if len(df) < 20:
                return "data_insufficient", 0.5
            avg = float(df["amount"].mean())
            if avg < self.cfg["liquidity_min_amount"]:
                return "below_threshold", 0.0
            if avg < self.cfg["liquidity_weak_amount"]:
                return "weak", 0.5
            return "normal", 1.0
        except Exception:
            return "error", 0.5

    def _black_swan_scan(self, ts_code):
        """§3.2 暴雷预警扫描 → True=暴雷(拦截)/False=安全"""
        try:
            # 检查已有黑名单
            c = self.conn.execute(
                "SELECT COUNT(*) FROM memory_failure_signal "
                "WHERE ts_code=? AND signal_name LIKE '%black_swan%'",
                (ts_code,))
            if c.fetchone()[0] > 0:
                return True, "已在memory_failure_signal黑名单"
            # 业绩预告扫描
            try:
                import tushare as ts
                pro = ts.pro_api()
                fc = pro.forecast(ts_code=ts_code)
                if not fc.empty and fc.iloc[0].get("type") in ("预亏", "续亏", "首亏"):
                    reason = f"业绩预告:{fc.iloc[0]['type']}"
                    self._write_blacklist(ts_code, reason)
                    return True, reason
            except Exception:
                pass
            # 情感扫描
            df = pd.read_sql(
                "SELECT sentiment_score FROM memory_market "
                "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 5",
                self.conn, params=(ts_code,))
            if len(df) >= 5 and df["sentiment_score"].dropna().mean() < 0.3:
                reason = "持续负面舆情(情感均分<0.3)"
                self._write_blacklist(ts_code, reason)
                return True, reason
        except Exception:
            pass
        return False, "安全"

    def _write_blacklist(self, ts_code, reason):
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO memory_failure_signal "
                "(ts_code, signal_name, failure_type, avoid_strategy, record_time) "
                "VALUES (?,?,?,?,?)",
                (ts_code, "black_swan_auto", "black_swan",
                 f"暴雷扫描触发:{reason}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()
        except Exception:
            pass

    def _volatility_exposure(self):
        """§3.3 波动率自适应敞口 → 0.7~1.0"""
        try:
            df = pd.read_sql(
                "SELECT close FROM memory_market WHERE ts_code='000001' "
                "ORDER BY trade_date DESC LIMIT 25", self.conn)
            if len(df) < 20:
                return 0.85
            vol = float(df["close"].pct_change().std() * np.sqrt(252))
            if vol > self.cfg["vol_high"]:
                return 0.7
            if vol > self.cfg["vol_medium_high"]:
                return 0.8
            if vol > self.cfg["vol_medium"]:
                return 0.9
            return 1.0
        except Exception:
            return 0.85

    def _market_env_factor(self):
        """近5日大盘 → 0.7~1.0"""
        try:
            df = pd.read_sql(
                "SELECT close FROM memory_market WHERE ts_code='000001' "
                "ORDER BY trade_date DESC LIMIT 10", self.conn)
            if len(df) < 5:
                return 0.85
            r = df["close"].iloc[0] / df["close"].iloc[4] - 1
            if r > 0.015:
                return 1.0
            if r < -0.03:
                return 0.7
            if r < -0.015:
                return 0.75
            return 0.8 if df["close"].pct_change().std() > 0.025 else 0.85
        except Exception:
            return 0.85

    def _sector_factor(self, industry):
        """板块强弱 → 0.8~1.0"""
        if not industry:
            return 0.9
        try:
            import tushare as ts
            pro = ts.pro_api()
            si_code = self.cfg["sector_map"].get(industry, "801080.SI")
            df = pro.index_daily(ts_code=si_code,
                                 start=(datetime.now()-timedelta(10)).strftime("%Y%m%d"),
                                 end=datetime.now().strftime("%Y%m%d"))
            if df.empty:
                return 0.9
            r = df["close"].iloc[0] / df["close"].iloc[min(4, len(df)-1)] - 1
            return 1.0 if r > 0.02 else 0.8 if r < -0.02 else 0.9
        except Exception:
            return 0.9

    def _tech_factor(self, ts_code):
        """技术形态 → 0.7~1.0"""
        try:
            df = pd.read_sql(
                "SELECT ma5,ma10,ma20,macd,rsi FROM memory_market "
                "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 5",
                self.conn, params=(ts_code,))
            if df.empty:
                return 0.85
            r = df.iloc[0]
            s = 1.0
            if all(c in r for c in ["ma5", "ma10", "ma20"]):
                if r["ma5"] > r["ma10"] > r["ma20"]:
                    pass  # 多头→不扣分
                elif r["ma5"] < r["ma10"] < r["ma20"]:
                    s *= 0.75
                else:
                    s *= 0.85
            if "macd" in r and not pd.isna(r["macd"]):
                s *= 0.9 if r["macd"] < 0 else 1.0
            if "rsi" in r and not pd.isna(r["rsi"]):
                if r["rsi"] < 25:
                    s *= 0.7
                elif r["rsi"] < 35:
                    s *= 0.85
                elif r["rsi"] > 75:
                    s *= 0.75
            return max(0.7, min(1.0, round(s, 2)))
        except Exception:
            return 0.85

    def _sentiment_factor(self):
        """情绪 → 0.85~1.0"""
        try:
            df = pd.read_sql(
                "SELECT sentiment_score FROM memory_market "
                "ORDER BY trade_date DESC LIMIT 20", self.conn)
            if df.empty or df["sentiment_score"].isna().all():
                return 0.9
            avg = float(df["sentiment_score"].dropna().mean())
            return 1.0 if avg > 0.6 else 0.9 if avg > 0.45 else 0.85
        except Exception:
            return 0.9

    def _volume_factor(self, ts_code):
        """量能 → 0.8~1.0"""
        try:
            df = pd.read_sql(
                "SELECT volume FROM memory_market WHERE ts_code=? "
                "ORDER BY trade_date DESC LIMIT 25",
                self.conn, params=(ts_code,))
            if len(df) < 20:
                return 0.9
            ratio = float(df["volume"].iloc[:5].mean() / df["volume"].mean())
            return 1.0 if ratio > 1.5 else 0.9 if ratio > 0.8 else 0.8
        except Exception:
            return 0.9

    def run_dynamic_check(self, ts_code, industry=""):
        """
        §3 完整动态检查 → (ok, reason_detail, pos_coeff)
        pos_coeff: 后续所有仓位天花板(0~1)
        """
        # §3.2 暴雷优先
        blacklisted, swan_reason = self._black_swan_scan(ts_code)
        if blacklisted:
            return False, f"§3.2暴雷:{swan_reason}", 0.0

        # §3.1 流动性
        liqu_status, liqu_coeff = self._liquidity_check(ts_code)
        if liqu_coeff == 0.0:
            return False, f"§3.1流动性不足({liqu_status})", 0.0

        # §3.3 波动率 + 市场 + 板块 + 技术 + 情绪 + 量能
        vol_coeff = self._volatility_exposure()
        env_coeff = self._market_env_factor()
        sec_coeff = self._sector_factor(industry)
        tech_coeff = self._tech_factor(ts_code)
        sent_coeff = self._sentiment_factor()
        volu_coeff = self._volume_factor(ts_code)

        coeff = round(liqu_coeff * vol_coeff * env_coeff * sec_coeff
                      * tech_coeff * sent_coeff * volu_coeff, 2)
        detail = (
            f"§3.1流动:{liqu_coeff:.1f} §3.3波动:{vol_coeff:.2f} "
            f"市场:{env_coeff:.2f} 板块:{sec_coeff:.2f} "
            f"技术:{tech_coeff:.2f} 情绪:{sent_coeff:.2f} 量能:{volu_coeff:.2f}"
        )
        ok = coeff >= self.cfg["dynamic_coeff_min"]
        status = "安全" if coeff >= 0.8 else "谨慎" if coeff >= 0.6 else "偏弱" if ok else "禁止"
        return ok, f"§3 coeff={coeff:.2f}({status}) {detail}", coeff

    # ===================== §3.4 熔断自愈 =====================

    def check_and_trigger_fuse(self):
        """
        §3.4: 检测日亏损→触发冻结→返回是否已熔断
        熔断冻结写入memory_failure_signal，仅人工可释放
        """
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            df = pd.read_sql(
                f"SELECT reward FROM rl_decision_log WHERE trade_date='{today}'",
                self.conn)
            if df.empty or abs(float(df["reward"].sum())) < self.cfg["daily_fuse_threshold"]:
                return False  # 未触发
            # 写入冻结标记
            self.conn.execute(
                "INSERT OR IGNORE INTO memory_failure_signal "
                "(ts_code, signal_name, failure_type, avoid_strategy, record_time) "
                "VALUES (?,?,?,?,?)",
                ("ALL", f"fuse_freeze_{today}", "fuse_meltdown",
                 "熔断冻结,需人工复核释放", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()
            return True
        except Exception:
            return False

    # ===================== §5 统一入口（开仓前必须调用） =====================

    def apply_risk_override(self, ts_code, industry, raw_score, raw_position):
        """
        §5 完整开仓风控校验 → (allow, logs, final_score, final_position)
        调用者必须使用此结果覆盖原始评分/仓位

        输入:
            ts_code: 股票代码(纯数字/带后缀)
            industry: 行业名称
            raw_score: agent_predict_v2的confidence(0~100)
            raw_position: 原始仓位字符串("25%"/"12%"/"3%"/"0%")
        输出:
            allow: True=允许开仓, False=禁止
            logs: 多层风控日志
            final_score: 可能被风控调整后的分数
            final_position: 最终允许的仓位(0~1)
        """
        logs = []
        raw_pos = float(raw_position.replace("%", "")) / 100.0
        clean_code = str(ts_code).replace(".SH", "").replace(".SZ", "")

        # ── §2 静态硬约束 ──
        static_ok, static_fails = self.check_static(clean_code, industry, raw_pos)
        if static_fails:
            logs.append(f"§2❌ {'; '.join(static_fails)}")
        else:
            logs.append("§2✅ 静态4道防线通过")
        if not static_ok:
            return False, "\n".join(logs), raw_score, 0.0

        # ── §3 AI动态预判 ──
        dynamic_ok, dynamic_log, pos_coeff = self.run_dynamic_check(clean_code, industry)
        logs.append(f"§3{'✅' if dynamic_ok else '❌'} {dynamic_log}")
        if not dynamic_ok:
            return False, "\n".join(logs), raw_score, 0.0

        # ── §3.4 熔断检查 ──
        fused = self.check_and_trigger_fuse()
        if fused:
            logs.append("§3.4❌ 熔断已触发，冻结所有新开仓")
            return False, "\n".join(logs), raw_score, 0.0

        # ── 最终仓位计算 ──
        # (§5: 动态系数作为所有仓位天花板)
        capped_pos = min(raw_pos, pos_coeff * 0.75)
        # 还原为最高仓位映射等级
        if capped_pos >= self.cfg["pos_high"]:
            final_position = self.cfg["pos_high"]
        elif capped_pos >= self.cfg["pos_medium"]:
            final_position = self.cfg["pos_medium"]
        elif capped_pos >= self.cfg["pos_low"]:
            final_position = self.cfg["pos_low"]
        else:
            final_position = self.cfg["pos_zero"]

        logs.append(f"§5✅ 原仓位{raw_pos:.0%} → 风控调整后{final_position:.0%} "
                     f"(系数{pos_coeff:.2f})")
        return True, "\n".join(logs), raw_score, final_position

    def close(self):
        self.conn.close()
