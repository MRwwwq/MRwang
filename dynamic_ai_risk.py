"""
AI动态预判风控 — 8维强制能力：流动性/暴雷预警/波动率/市场/板块/技术/情绪/量能
§3.1 流动性预判  §3.2 暴雷预警  §3.3 波动率自适应
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

DB_PATH = "agent_memory.db"
LIQUIDITY_THRESHOLD = 5000_0000  # 日均成交额5000万合规阈值(元)
LIQUIDITY_WEAK_THRESHOLD = 1_0000_0000  # 偏弱阈值1亿


class DynamicAIRiskControl:
    """AI动态预判风控 — 8维仓位系数"""
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    # ── §3.1 流动性预判 ──
    def _liquidity_check(self, ts_code: str) -> float:
        """流动性判定: 20日均成交额 → 0.0(拦截) / 0.5(减半) / 1.0(正常)"""
        try:
            df = pd.read_sql(
                "SELECT amount FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
                self.conn, params=(ts_code,))
            if len(df) < 20:
                return 0.5  # 数据不足，保守减半
            avg = float(df["amount"].mean())
            if avg < LIQUIDITY_THRESHOLD:
                return 0.0  # §3.1 永久拦截
            if avg < LIQUIDITY_WEAK_THRESHOLD:
                return 0.5  # §3.1 减半
            return 1.0
        except:
            return 0.5

    # ── §3.2 暴雷预警自动扫描 ──
    def _black_swan_scan(self, ts_code: str) -> bool:
        """
        暴雷扫描：负面舆情/减持/业绩预亏 → 自动写入memory_failure_signal
        返回 True=暴雷(禁止开仓) / False=安全
        """
        triggers = []
        try:
            # 检查 memory_failure_signal 是否有该标的活跃黑名单
            c = self.conn.execute(
                "SELECT COUNT(*) FROM memory_failure_signal WHERE ts_code=? AND signal_name LIKE '%black_swan%'",
                (ts_code,))
            if c.fetchone()[0] > 0:
                return True  # 已在黑名单
        except:
            pass

        # 接入 Tushare 公告/业绩预告扫描
        try:
            import tushare as ts
            pro = ts.pro_api()
            # 业绩预告
            fc = pro.forecast(ts_code=ts_code)
            if not fc.empty:
                latest = fc.iloc[0]
                if latest.get("type") in ("预亏", "续亏", "首亏", "略减"):
                    triggers.append(f"业绩{latest['type']}")
            # 解禁扫描
            sf = pro.share_float(ts_code=ts_code)
            if not sf.empty:
                upcoming = sf[sf["float_date"] > datetime.now().strftime("%Y%m%d")]
                if len(upcoming) > 0 and upcoming.iloc[0]["float_ratio"] > 5:
                    triggers.append(f"大额解禁{upcoming.iloc[0]['float_ratio']}%")
        except:
            pass

        # 检查近5日新闻情感(本地)
        try:
            df = pd.read_sql(
                "SELECT sentiment_score FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 5",
                self.conn, params=(ts_code,))
            if len(df) >= 5 and df["sentiment_score"].dropna().mean() < 0.3:
                triggers.append("持续负面舆情")
        except:
            pass

        if triggers:
            try:
                reason = "; ".join(triggers)
                self.conn.execute("""
                    INSERT INTO memory_failure_signal (ts_code, signal_name, failure_type, avoid_strategy, record_time)
                    VALUES (?, ?, ?, ?, ?)
                """, (ts_code, "black_swan_auto", "black_swan",
                      f"暴雷扫描触发: {reason}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                self.conn.commit()
            except:
                pass
            return True
        return False

    # ── §3.3 波动率自适应敞口 ──
    def _volatility_exposure(self) -> float:
        """大盘20日波动率 → 0.7~1.0 仓位系数"""
        try:
            df = pd.read_sql(
                "SELECT close FROM memory_market WHERE ts_code='000001' ORDER BY trade_date DESC LIMIT 25",
                self.conn)
            if len(df) < 20:
                return 0.85
            vol = float(df["close"].pct_change().std() * np.sqrt(252))
            if vol > 0.35:
                return 0.7   # 极端高波动
            if vol > 0.25:
                return 0.8   # 高波动
            if vol > 0.18:
                return 0.9   # 中等
            return 1.0       # 低波动
        except:
            return 0.85

    # ── 原有5维因子（精简保留） ──
    def _market_env_factor(self) -> float:
        try:
            df = pd.read_sql("SELECT close FROM memory_market WHERE ts_code='000001' ORDER BY trade_date DESC LIMIT 10", self.conn)
            if len(df) < 5: return 0.85
            r = df["close"].iloc[0]/df["close"].iloc[4]-1
            if r > 0.015: return 1.0
            if r < -0.03: return 0.7
            if r < -0.015: return 0.75
            return 0.8 if df["close"].pct_change().std()>0.025 else 0.85
        except: return 0.85

    def _sector_factor(self, industry: str) -> float:
        if not industry: return 0.9
        try:
            import tushare as ts; pro=ts.pro_api()
            sm={"电气设备":"801080.SI","电子":"801080.SI","贵金属":"801080.SI","银行":"801780.SI","医疗服务":"801150.SI","半导体":"851521.SI","通信":"801750.SI","消费电子":"801080.SI","建筑":"801741.SI","电力":"801741.SI"}
            si=sm.get(industry,"801080.SI")
            df=pro.index_daily(ts_code=si,start=(datetime.now()-timedelta(10)).strftime("%Y%m%d"),end=datetime.now().strftime("%Y%m%d"))
            if df.empty: return 0.9
            r=df["close"].iloc[0]/df["close"].iloc[min(4,len(df)-1)]-1
            return 1.0 if r>0.02 else 0.8 if r<-0.02 else 0.9
        except: return 0.9

    def _tech_factor(self, ts_code: str) -> float:
        try:
            df=pd.read_sql("SELECT close,ma5,ma10,ma20,macd,rsi FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 5",self.conn,params=(ts_code,))
            if df.empty: return 0.85
            r=df.iloc[0]; s=1.0
            if all(c in r for c in ["ma5","ma10","ma20"]):
                s*=1.0 if r["ma5"]>r["ma10"]>r["ma20"] else 0.75 if r["ma5"]<r["ma10"]<r["ma20"] else 0.85
            if "macd" in r and not pd.isna(r["macd"]): s*=0.9 if r["macd"]<0 else 1.0
            if "rsi" in r and not pd.isna(r["rsi"]):
                s*=0.7 if r["rsi"]<25 else 0.85 if r["rsi"]<35 else 0.75 if r["rsi"]>75 else 1.0
            return max(0.7,min(1.0,round(s,2)))
        except: return 0.85

    def _sentiment_factor(self) -> float:
        try:
            df=pd.read_sql("SELECT sentiment_score FROM memory_market ORDER BY trade_date DESC LIMIT 20",self.conn)
            if df.empty or df["sentiment_score"].isna().all(): return 0.9
            a=float(df["sentiment_score"].dropna().mean())
            return 1.0 if a>0.6 else 0.9 if a>0.45 else 0.85
        except: return 0.9

    def _volume_factor(self, ts_code: str) -> float:
        try:
            df=pd.read_sql("SELECT volume FROM memory_market WHERE ts_code=? ORDER BY trade_date DESC LIMIT 25",self.conn,params=(ts_code,))
            if len(df)<20: return 0.9
            r=df["volume"].iloc[:5].mean()/df["volume"].mean()
            return 1.0 if r>1.5 else 0.9 if r>0.8 else 0.8
        except: return 0.9

    # ── 统一入口（8维合并） ──
    def full_dynamic_risk_check(self, ts_code: str, industry: str = ""):
        """
        8维AI动态风控 → (ok, log, coeff)
        coeff = 流动性 × 波动率敞口 × 市场环境 × 板块 × 技术 × 情绪 × 量能
        暴雷扫描单独提前拦截
        """
        # 暴雷扫描优先（§3.2）
        blacklisted = self._black_swan_scan(ts_code)
        if blacklisted:
            return False, "  ❌ 暴雷预警触发，已归档 memory_failure_signal，禁止开仓", 0.0

        factors = {
            "liquidity": self._liquidity_check(ts_code),
            "volatility": self._volatility_exposure(),
            "market_env": self._market_env_factor(),
            "sector": self._sector_factor(industry),
            "technical": self._tech_factor(ts_code),
            "sentiment": self._sentiment_factor(),
            "volume": self._volume_factor(ts_code),
        }

        # 流动性拦截（§3.1）
        if factors["liquidity"] == 0.0:
            return False, f"  ❌ 流动性不足(20日均成交额<{LIQUIDITY_THRESHOLD/10000:.0f}万)，永久拦截", 0.0

        coeff = round(float(np.prod(list(factors.values()))), 2)
        # 流动性偏弱自动减半（已在返回值中体现）
        lines = [
            f"  💧 流动性:{factors['liquidity']:.1f}",
            f"  🌊 波动率敞口:{factors['volatility']:.2f}",
            f"  🌍 市场环境:{factors['market_env']:.2f}",
            f"  🏭 板块强弱:{factors['sector']:.2f}",
            f"  📈 技术形态:{factors['technical']:.2f}",
            f"  💬 市场情绪:{factors['sentiment']:.2f}",
            f"  📊 量能确认:{factors['volume']:.2f}",
            f"  → 综合系数:{coeff:.2f}",
        ]
        ok = coeff >= 0.35
        lines.append("  ❌ 禁止开仓(系数<0.35)" if not ok else
                      ("  🟢 安全" if coeff>=0.8 else "  🟡 谨慎" if coeff>=0.6 else "  🟠 偏弱"))
        return ok, "\n".join(lines), coeff

    def close(self):
        self.conn.close()
