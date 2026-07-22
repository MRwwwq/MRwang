#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer0 数据采集层 — 原始输入汇聚与信号标准化

时序: Module04 定策略之后, Layer1 特征校验之前
输入来源:
  1. 上游四定论业务模块输出 (Module01~04)
  2. 外部原始市场数据源 (Tushare行情/基本面/舆情等)
  3. 全局共享数据 psy_hit_codes

输出:
  标准化五类信号: 技术面信号 / 基本面信号 / 情绪面信号 / 指标面信号 / 宏观面信号
  向下送入 Layer1 特征校验层
"""

import logging
from datetime import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [L0] %(message)s", datefmt="%H:%M:%S")

# ====================== 信号类型定义 ======================

SIGNAL_TYPES = {
    "technical": "技术面信号",
    "fundamental": "基本面信号",
    "sentiment": "情绪面信号",
    "indicator": "指标面信号",
    "macro": "宏观面信号",
}

SIGNAL_DIRECTION = {
    "bullish": "🟢利多",
    "bearish": "🔴利空",
    "neutral": "🟡中性",
}


# ====================== 信号数据结构 ======================

class MarketSignal:
    """单一市场信号结构"""

    def __init__(self, signal_type: str, name: str, value: float,
                 direction: str, detail: str, source: str, weight: float = 1.0):
        self.signal_type = signal_type       # technical/fundamental/sentiment/indicator/macro
        self.name = name                     # 信号名称
        self.value = value                   # 信号值 (0~100)
        self.direction = direction           # bullish/bearish/neutral
        self.detail = detail                 # 信号详细描述
        self.source = source                 # 数据来源
        self.weight = weight                 # 信号权重 (0~1)
        self.timestamp = datetime.now().strftime("%H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "signal_type": self.signal_type,
            "name": self.name,
            "value": round(self.value, 2),
            "direction": self.direction,
            "direction_cn": SIGNAL_DIRECTION.get(self.direction, "未知"),
            "detail": self.detail,
            "source": self.source,
            "weight": self.weight,
            "timestamp": self.timestamp,
        }

    def __repr__(self) -> str:
        return f"[{self.signal_type}] {self.name}={self.value:.1f} {self.direction_cn}"


# ====================== Layer0 采集器 ======================

class Layer0Collector:
    """
    Layer0 数据采集层主类。

    收集三类输入来源，标准化输出五类信号。
    """

    def __init__(self):
        self.signals: list[MarketSignal] = []

    # -------------------- 输入1: 上游业务模块 --------------------

    def ingest_module01(self, module01_result: dict) -> None:
        """采集 Module01 定风格输出"""
        active_style = module01_result.get("active_style", "D")
        style_name = module01_result.get("style_name", "兜底混合")
        position_cap = module01_result.get("position_cap", {})

        self._add_signal("macro", f"交易风格({style_name})",
                         80 if active_style in ("A", "B") else 50,
                         "neutral" if active_style == "D" else "bullish",
                         f"active_style={active_style}, 总仓{position_cap.get('total')}%, "
                         f"单票{position_cap.get('per_stock')}%, 止损{position_cap.get('stop_loss')}%",
                         "Module01")

        # 风格匹配状况作为宏观信号
        matched = module01_result.get("matched", False)
        if not matched:
            self._add_signal("macro", "风格匹配异常(兜底D)", 30, "bearish",
                             "交易周期与资金类型无完美匹配, 系统自动兜底D, 仓位受限",
                             "Module01")

    def ingest_module02(self, module02_result: dict) -> None:
        """采集 Module02 定情绪输出"""
        sentiment_label = module02_result.get("sentiment_label", "recovery")
        sentiment_cn = module02_result.get("sentiment_cn", "回暖修复")
        final_total_cap = module02_result.get("final_total_cap", 25)

        # 情绪阶段 → 情绪面信号
        sentiment_map = {
            "ice":       ("冰点恐慌", "bearish", 15),
            "recovery":  ("回暖修复", "bullish", 60),
            "boom":      ("高潮亢奋", "bearish", 85),  # 高潮偏空(过热)
            "recession": ("退潮分歧", "bearish", 25),
        }
        cn, direction, base_score = sentiment_map.get(sentiment_label, ("未知", "neutral", 50))
        self._add_signal("sentiment", f"市场情绪({cn})", base_score, direction,
                         f"情绪阶段{sentiment_label}, 总仓约束{final_total_cap}%, "
                         f"判定依据: {module02_result.get('judge_reason', '')}",
                         "Module02")

        massacre = module02_result.get("has_massacre", False)
        if massacre:
            self._add_signal("sentiment", "亏钱效应(核按钮)", 15, "bearish",
                             module02_result.get("massacre_note", "批量高位大面"),
                             "Module02", weight=1.5)

    def ingest_module03(self, module03_result: dict) -> None:
        """采集 Module03 定方向输出"""
        main_line = module03_result.get("main_line", "无主线混沌")
        driver_type = module03_result.get("driver_type", "")
        driver_validity = module03_result.get("driver_validity", "短期(≤2周)")

        if main_line and main_line != "无主线(混沌)":
            self._add_signal("indicator", f"主线板块({main_line})", 75, "bullish",
                             f"驱动:{driver_type}, 有效周期:{driver_validity}",
                             "Module03")
        else:
            self._add_signal("indicator", "主线板块(无主线混沌)", 25, "bearish",
                             "市场无明确主线, 资金分散轮动, 风险上升",
                             "Module03", weight=1.3)

        # 驱动类型 → 基本面信号
        driver_scores = {"政策": 80, "业绩": 75, "技术": 70, "题材": 35}
        ds = driver_scores.get(driver_type, 50)
        direction = "bearish" if driver_type == "题材" else "bullish"
        self._add_signal("fundamental", f"驱动逻辑({driver_type})", ds, direction,
                         module03_result.get("driver_detail", ""),
                         "Module03")

        # 选股违规
        violations = module03_result.get("violations", [])
        if violations:
            self._add_signal("indicator", "选股风格违规", 20, "bearish",
                             "; ".join(violations),
                             "Module03", weight=1.5)

    def ingest_module04(self, module04_result: dict) -> None:
        """采集 Module04 定策略输出"""
        pool = module04_result.get("tradeable_pool", [])
        blocked_black = module04_result.get("blacklist_blocked", [])
        blocked_fail = module04_result.get("failure_blocked", [])

        total_blocked = len(blocked_black) + len(blocked_fail)
        total_candidates = module04_result.get("candidates_before_blacklist", 0)

        if total_blocked > 0:
            self._add_signal("indicator", "黑名单拦截", 20 - min(total_blocked * 5, 20), "bearish",
                             f"黑名单拦截{len(blocked_black)}只 + 失效信号拦截{len(blocked_fail)}只",
                             "Module04", weight=1.5)

        # 可交易池规模作为流动性指标
        pool_size = len(pool)
        pool_score = min(pool_size * 15, 100) if pool_size > 0 else 0
        self._add_signal("indicator", "可交易池规模", pool_score,
                         "bullish" if pool_size >= 2 else "bearish",
                         f"{pool_size}只可交易标的",
                         "Module04")

    # -------------------- 输入2: 外部原始市场数据 --------------------

    def ingest_technical_data(self, tech_data: dict) -> None:
        """接入技术面信号 (Tushare/行情波动数据)"""
        ma_status = tech_data.get("ma_status", "unknown")       # 均线多头/空头/震荡
        volume_ratio = tech_data.get("volume_ratio", 1.0)       # 量比
        kdj_signal = tech_data.get("kdj_signal", "unknown")    # KDJ信号
        macd_signal = tech_data.get("macd_signal", "unknown")  # MACD信号

        # 均线信号
        ma_score = {"bullish": 80, "bearish": 20, "oscillation": 50, "unknown": 50}
        ma_dir = {"bullish": "bullish", "bearish": "bearish", "oscillation": "neutral", "unknown": "neutral"}
        self._add_signal("technical", f"均线形态({ma_status})",
                         ma_score.get(ma_status, 50), ma_dir.get(ma_status, "neutral"),
                         f"5/10/20日均线{ma_status}", "Tushare行情")

        # 量能信号
        vol_score = min(volume_ratio * 40, 100) if volume_ratio > 1.0 else max(volume_ratio * 30, 10)
        vol_dir = "bullish" if volume_ratio > 1.5 else ("bearish" if volume_ratio < 0.7 else "neutral")
        self._add_signal("technical", f"量能(量比{volume_ratio:.2f})",
                         round(vol_score, 1), vol_dir,
                         f"当日量比{volume_ratio:.2f}", "Tushare行情")

        # KDJ信号
        kdj_score = {"金叉": 75, "死叉": 25, "超买": 30, "超卖": 70, "unknown": 50}
        kdj_dir = {"金叉": "bullish", "死叉": "bearish", "超买": "bearish", "超卖": "bullish", "unknown": "neutral"}
        self._add_signal("technical", f"KDJ({kdj_signal})",
                         kdj_score.get(kdj_signal, 50), kdj_dir.get(kdj_signal, "neutral"),
                         f"KDJ指标{kdj_signal}", "Tushare行情")

        # MACD信号
        macd_score = {"金叉": 75, "死叉": 25, "零上": 65, "零下": 35, "unknown": 50}
        macd_dir = {"金叉": "bullish", "死叉": "bearish", "零上": "bullish", "零下": "bearish", "unknown": "neutral"}
        self._add_signal("technical", f"MACD({macd_signal})",
                         macd_score.get(macd_signal, 50), macd_dir.get(macd_signal, "neutral"),
                         f"MACD指标{macd_signal}", "Tushare行情")

    def ingest_fundamental_data(self, fund_data: dict) -> None:
        """接入基本面信号 (财报/估值)"""
        pe_ttm = fund_data.get("pe_ttm", 0)
        pb = fund_data.get("pb", 0)
        roe = fund_data.get("roe", 0)
        profit_growth = fund_data.get("profit_growth", 0)

        # PE估值信号
        pe_score = 70 if 0 < pe_ttm < 30 else (40 if pe_ttm >= 50 else 50)
        pe_dir = "bullish" if 0 < pe_ttm < 30 else ("bearish" if pe_ttm < 0 else "neutral")
        self._add_signal("fundamental", f"PE_TTM({pe_ttm:.1f})",
                         pe_score, pe_dir,
                         f"PE_TTM={pe_ttm:.1f}", "Tushare基本面")

        # ROE信号
        roe_score = min(roe * 10, 100) if roe > 0 else 20
        roe_dir = "bullish" if roe > 10 else ("bearish" if roe < 3 else "neutral")
        self._add_signal("fundamental", f"ROE({roe:.1f}%)",
                         round(roe_score, 1), roe_dir,
                         f"ROE={roe:.1f}%", "Tushare基本面")

        # 利润增长信号
        g_score = min(50 + profit_growth, 100) if profit_growth > 0 else max(50 + profit_growth, 5)
        g_dir = "bullish" if profit_growth > 10 else ("bearish" if profit_growth < -10 else "neutral")
        self._add_signal("fundamental", f"利润增速({profit_growth:+.1f}%)",
                         round(g_score, 1), g_dir,
                         f"净利润增长{profit_growth:+.1f}%", "Tushare基本面")

    def ingest_sentiment_data(self, sent_data: dict) -> None:
        """接入情绪面信号 (舆情/新闻/股吧)"""
        news_score = sent_data.get("news_sentiment", 50)
        guba_score = sent_data.get("guba_sentiment", 50)
        research_score = sent_data.get("research_sentiment", 50)

        # 综合舆情
        avg_sent = (news_score * 0.4 + guba_score * 0.3 + research_score * 0.3)
        sent_dir = "bullish" if avg_sent > 60 else ("bearish" if avg_sent < 40 else "neutral")
        self._add_signal("sentiment", f"综合舆情({avg_sent:.0f})",
                         round(avg_sent, 1), sent_dir,
                         f"新闻{news_score:.0f}/股吧{guba_score:.0f}/研报{research_score:.0f}",
                         "舆情流水线")

        # 研报一致性
        if research_score:
            r_dir = "bullish" if research_score > 60 else ("bearish" if research_score < 40 else "neutral")
            self._add_signal("sentiment", f"研报情感({research_score:.0f})",
                             research_score, r_dir,
                             f"机构研报评级情感{research_score:.0f}", "舆情流水线")

    def ingest_indicator_data(self, ind_data: dict) -> None:
        """接入指标面信号 (技术指标时序统计)"""
        rsi = ind_data.get("rsi", 50)
        rsi_score = min(100, max(0, 50 + (50 - abs(rsi - 50)) * 0.5))
        rsi_dir = "bearish" if rsi > 75 else ("bearish" if rsi < 25 else "neutral")
        self._add_signal("indicator", f"RSI({rsi:.0f})",
                         round(rsi_score, 1), rsi_dir,
                         f"RSI={rsi:.1f}", "Tushare行情")

        boll_pos = ind_data.get("boll_position", 0.5)  # 0下轨 ~ 1上轨
        boll_score = 70 if 0.3 < boll_pos < 0.7 else (20 if boll_pos > 0.9 else 50)
        boll_dir = "bearish" if boll_pos > 0.85 else ("bullish" if boll_pos < 0.15 else "neutral")
        self._add_signal("indicator", f"BOLL位置({boll_pos:.2f})",
                         boll_score, boll_dir,
                         f"布林带位置{boll_pos:.2f} (0下~1上)", "Tushare行情")

    def ingest_macro_data(self, macro_data: dict) -> None:
        """接入宏观面信号 (宏观政策/资金变量)"""
        shibor = macro_data.get("shibor_1w", 0)
        market_sentiment = macro_data.get("market_sentiment", 50)
        industry_flow = macro_data.get("industry_flow", "unknown")

        # 流动性信号
        liq_score = 70 if shibor < 2.0 else (30 if shibor > 3.0 else 50)
        liq_dir = "bullish" if shibor < 2.0 else ("bearish" if shibor > 3.0 else "neutral")
        self._add_signal("macro", f"流动性(SHIBOR 1W={shibor:.2f}%)",
                         liq_score, liq_dir,
                         f"SHIBOR 1周={shibor:.2f}%", "Tushare宏观")

        # 行业资金流向
        flow_scores = {"inflow": 70, "outflow": 30, "neutral": 50, "unknown": 50}
        flow_dir = {"inflow": "bullish", "outflow": "bearish", "neutral": "neutral", "unknown": "neutral"}
        self._add_signal("macro", f"行业资金流({industry_flow})",
                         flow_scores.get(industry_flow, 50), flow_dir.get(industry_flow, "neutral"),
                         f"所属行业资金{industry_flow}", "Tushare资金流")

    # -------------------- 输入3: psy_hit_codes --------------------

    def ingest_psy_hit_codes(self, psy_codes: list[str]) -> None:
        """接入全局 psy_hit_codes 心理误判编码"""
        count = len(psy_codes)
        if count > 0:
            self._add_signal("sentiment", f"心理误判累积({count}条)", max(10, 100 - count * 4), "bearish",
                             f"触发编码: {', '.join(psy_codes[:8])}{'...' if count > 8 else ''}",
                             "psy_hit_manager", weight=min(1.0 + count * 0.1, 2.0))
        else:
            self._add_signal("sentiment", "心理误判累积(0条)", 100, "bullish",
                             "无心理误判触发", "psy_hit_manager")

    # -------------------- 内部方法 --------------------

    def _add_signal(self, signal_type: str, name: str, value: float,
                    direction: str, detail: str, source: str, weight: float = 1.0) -> None:
        """添加一条标准化信号"""
        signal = MarketSignal(
            signal_type=signal_type,
            name=name,
            value=value,
            direction=direction,
            detail=detail,
            source=source,
            weight=weight,
        )
        self.signals.append(signal)
        logging.info(f"  📡 [{signal_type.upper():12s}] {name:30s} | value={value:5.1f} | {SIGNAL_DIRECTION.get(direction,'')}")

    def collect_all(self,
                    module01_result: dict,
                    module02_result: dict,
                    module03_result: dict,
                    module04_result: dict,
                    psy_codes: list[str],
                    tech_data: Optional[dict] = None,
                    fund_data: Optional[dict] = None,
                    sent_data: Optional[dict] = None,
                    ind_data: Optional[dict] = None,
                    macro_data: Optional[dict] = None,
                    ) -> dict:
        """
        全量采集入口。

        返回标准化五类信号字典，直接送入 Layer1。
        """
        logging.info("=" * 60)
        logging.info("Layer0 数据采集层 启动")
        logging.info(f"  输入源: Module01~04 + 外部市场数据 + psy_hit_codes")
        self.signals = []

        # 1. 业务模块输入
        self.ingest_module01(module01_result)
        self.ingest_module02(module02_result)
        self.ingest_module03(module03_result)
        self.ingest_module04(module04_result)

        # 2. 外部市场数据
        if tech_data:
            self.ingest_technical_data(tech_data)
        if fund_data:
            self.ingest_fundamental_data(fund_data)
        if sent_data:
            self.ingest_sentiment_data(sent_data)
        if ind_data:
            self.ingest_indicator_data(ind_data)
        if macro_data:
            self.ingest_macro_data(macro_data)

        # 3. psy_hit_codes
        self.ingest_psy_hit_codes(psy_codes)

        # 汇总输出
        result = self._build_output()
        logging.info(f"  ✅ 信号采集完成: 共{len(self.signals)}条 | "
                     f"技术{len(result['technical'])} 基本面{len(result['fundamental'])} "
                     f"情绪{len(result['sentiment'])} 指标{len(result['indicator'])} "
                     f"宏观{len(result['macro'])}")
        logging.info("Layer0 数据采集层 完成")
        logging.info("=" * 60)
        return result

    def _build_output(self) -> dict:
        """构建标准化五类信号输出"""
        grouped = {"technical": [], "fundamental": [], "sentiment": [], "indicator": [], "macro": []}
        for s in self.signals:
            st = s.signal_type
            if st in grouped:
                grouped[st].append(s.to_dict())
        return {
            "signals_raw": [s.to_dict() for s in self.signals],
            "total_signals": len(self.signals),
            "technical": grouped["technical"],
            "fundamental": grouped["fundamental"],
            "sentiment": grouped["sentiment"],
            "indicator": grouped["indicator"],
            "macro": grouped["macro"],
            "collected_at": datetime.now().strftime("%H:%M:%S"),
        }


# ====================== 测试 ======================

if __name__ == "__main__":
    from psy_hit_manager import clear_all_psy_codes
    clear_all_psy_codes()

    print("\n=== Layer0 数据采集层 测试 ===\n")

    collector = Layer0Collector()

    # 模拟 Module01 结果
    m01 = {
        "active_style": "B", "style_name": "首板套利",
        "position_cap": {"total": 40, "per_stock": 15, "stop_loss": 2.5},
        "matched": True,
    }
    m02 = {
        "sentiment_label": "recovery", "sentiment_cn": "回暖修复",
        "final_total_cap": 30, "has_massacre": False,
        "judge_reason": "涨跌均衡 连板3板",
    }
    m03 = {
        "main_line": "固态电池", "driver_type": "业绩",
        "driver_detail": "2026H1预增262~334%",
        "driver_validity": "长期(≥6月)",
        "violations": [],
    }
    m04 = {
        "candidates_before_blacklist": 3,
        "blacklist_blocked": [],
        "failure_blocked": [],
        "tradeable_pool": [
            {"code": "600884", "name": "杉杉股份", "role": "核心龙头",
             "alloc_pct": 10, "stop_loss": 2.5},
        ],
    }

    tech_data = {
        "ma_status": "bullish", "volume_ratio": 1.6,
        "kdj_signal": "金叉", "macd_signal": "金叉",
    }
    fund_data = {
        "pe_ttm": 18.5, "pb": 1.2, "roe": 12.5, "profit_growth": 35.0,
    }
    sent_data = {
        "news_sentiment": 72, "guba_sentiment": 65, "research_sentiment": 80,
    }
    ind_data = {
        "rsi": 62, "boll_position": 0.65,
    }
    macro_data = {
        "shibor_1w": 1.8, "market_sentiment": 55,
        "industry_flow": "inflow",
    }

    result = collector.collect_all(
        module01_result=m01, module02_result=m02,
        module03_result=m03, module04_result=m04,
        psy_codes=[],
        tech_data=tech_data, fund_data=fund_data,
        sent_data=sent_data, ind_data=ind_data,
        macro_data=macro_data,
    )

    print(f"\n采集信号总数: {result['total_signals']}")
    for stype, signals in [("technical", "技术"), ("fundamental", "基本面"),
                           ("sentiment", "情绪"), ("indicator", "指标"), ("macro", "宏观")]:
        sigs = result[stype]
        print(f"\n  {signals}({len(sigs)}条):")
        for s in sigs:
            print(f"    {s['direction_cn']} {s['name']:30s} | value={s['value']:5.1f} | w={s['weight']}")
