#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
psychological_bias_checker.py — 25类人类心理误判量化检测引擎

每分析一只个股，自动执行本模块：
- 25类误判因子实时量化评分
- 单因子高分→预警
- ≥6类同向因子共振→标记Lollapalooza高风险→风控一票否决
- 校验结果插入报告风险板块

用法: python3 psychological_bias_checker.py <stock_code>
"""
import json, sys, math
from datetime import date
from pathlib import Path

BASE = Path("/opt/stock_agent")
REPORTS = BASE / "reports"
TODAY = date.today().isoformat()

# ============================================================
# 25类误判因子检测函数
# ============================================================

class BiasChecker:
    """25类心理误判量化检测引擎"""
    
    def __init__(self, stock_code: str = "", market_data: dict = None):
        self.stock_code = stock_code
        self.d = market_data or {}
        self.results = {}
        self.lollapalooza_count = 0
        self.lollapalooza_direction = None  # "bull" or "bear"
        
    def check_all(self) -> dict:
        """执行全部25项检测"""
        checks = {}
        
        # 1. 奖励/惩罚超级反应
        checks["bias_01_reward_punishment"] = self._check_reward_punishment()
        
        # 2. 喜欢/热爱倾向
        checks["bias_02_liking"] = self._check_liking()
        
        # 3. 讨厌/憎恨倾向
        checks["bias_03_hating"] = self._check_hating()
        
        # 4. 避免怀疑倾向
        checks["bias_04_doubt_avoidance"] = self._check_doubt_avoidance()
        
        # 5. 避免不一致性(高权重)
        checks["bias_05_consistency"] = self._check_consistency()
        
        # 6. 好奇心倾向
        checks["bias_06_curiosity"] = self._check_curiosity()
        
        # 7. 公平倾向
        checks["bias_07_fairness"] = self._check_fairness()
        
        # 8. 嫉妒/猜忌倾向
        checks["bias_08_envy"] = self._check_envy()
        
        # 9. 回馈倾向
        checks["bias_09_reciprocation"] = self._check_reciprocation()
        
        # 10. 简单联想影响
        checks["bias_10_simple_association"] = self._check_simple_association()
        
        # 11. 避免痛苦的心理否认
        checks["bias_11_denial"] = self._check_denial()
        
        # 12. 自视过高倾向
        checks["bias_12_overconfidence"] = self._check_overconfidence()
        
        # 13. 过度乐观倾向
        checks["bias_13_optimism"] = self._check_optimism()
        
        # 14. 被剥夺超级反应(高权重)
        checks["bias_14_deprivation"] = self._check_deprivation()
        
        # 15. 社会认同(羊群效应)
        checks["bias_15_social_proof"] = self._check_social_proof()
        
        # 16. 对比错误反应
        checks["bias_16_contrast"] = self._check_contrast()
        
        # 17. 压力影响倾向
        checks["bias_17_stress"] = self._check_stress()
        
        # 18. 易得性误导
        checks["bias_18_availability"] = self._check_availability()
        
        # 19. 不用就忘倾向
        checks["bias_19_forgetting"] = self._check_forgetting()
        
        # 20. 化学物质错误影响
        checks["bias_20_chemical"] = self._check_chemical()
        
        # 21. 衰老错误影响
        checks["bias_21_aging"] = self._check_aging()
        
        # 22. 权威错误影响(高权重)
        checks["bias_22_authority"] = self._check_authority()
        
        # 23. 废话倾向
        checks["bias_23_nonsense"] = self._check_nonsense()
        
        # 24. 重视理由倾向
        checks["bias_24_reason_respecting"] = self._check_reason_respecting()
        
        # 25. Lollapalooza超级叠加
        checks["bias_25_lollapalooza"] = self._check_lollapalooza()
        
        self.results = checks
        return checks
    
    # ---------- 各因子检测 ----------
    
    def _check_reward_punishment(self) -> dict:
        """奖励/惩罚超级反应: 短期连续上涨→看多舆情激增→估值风险上浮"""
        score = 0
        ret_3d = self.d.get("ret_3d", 0)
        ret_5d = self.d.get("ret_5d", 0)
        ret_15d = self.d.get("ret_15d", 0)
        
        # 短期暴涨检测
        if ret_3d > 8: score += 3
        elif ret_5d > 12: score += 2
        elif ret_15d > 20: score += 1
        
        # 散户资金暴增(模拟)
        retail_surge = self.d.get("retail_flow_surge", 0)
        if retail_surge > 2: score += 2
        
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"3日涨幅{ret_3d:.1f}%/5日{ret_5d:.1f}%/15日{ret_15d:.1f}%",
            "rule": "禁止短期收益作为选股加分项;短期暴涨自动下调仓位上限"
        }
    
    def _check_liking(self) -> dict:
        """喜欢/热爱倾向: 持仓偏好→放宽标准"""
        score = 0
        pe = self.d.get("pe_ttm", 30)
        industry_pe = self.d.get("industry_pe", 25)
        neg_news = self.d.get("negative_news_count", 0)
        
        # PE显著高于行业均值→可能因偏好而忽略
        if pe > industry_pe * 1.5: score += 2
        if neg_news > 3 and pe > industry_pe: score += 2  # 有利空但容忍
        
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"PE {pe} vs 行业均值{industry_pe}, 负面公告{neg_news}条",
            "rule": "持仓与空仓同一套风控;强制展示近1年负面公告"
        }
    
    def _check_hating(self) -> dict:
        """讨厌/憎恨倾向: 历史亏损→全盘否定"""
        hist_loss = self.d.get("historical_loss_count", 0)
        industry_ignore = self.d.get("industry_ignored", False)
        score = 2 if hist_loss > 2 else (1 if hist_loss > 0 else 0)
        if industry_ignore: score += 2
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"历史亏损{hist_loss}次",
            "rule": "黑名单仅收录硬风险;行业轮动不跳过任何板块"
        }
    
    def _check_doubt_avoidance(self) -> dict:
        """避免怀疑倾向: 数据不足强行预判"""
        data_gap = self.d.get("data_missing_pct", 0)
        score = 2 if data_gap > 10 else (1 if data_gap > 5 else 0)
        if data_gap > 20: score += 5  # 强制禁止
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"数据缺失度{data_gap:.0f}%",
            "rule": "数据缺失>20%→能力圈外标的,禁止交易"
        }
    
    def _check_consistency(self) -> dict:
        """避免不一致性(高权重): 浮亏后找利多信息"""
        position_pnl = self.d.get("position_pnl_pct", 0)
        neg_news_ignored = self.d.get("neg_news_ignored", 0)
        score = 0
        if position_pnl < -10: score += 2  # 浮亏>10%
        if neg_news_ignored > 2: score += 3  # 忽略利空
        return {
            "score": min(10, score * 1.5),  # 高权重
            "triggered": score >= 2,
            "detail": f"浮亏{position_pnl:.1f}%, 忽略利空{neg_news_ignored}条",
            "rule": "每日重算,昨日结论不继承;趋势反转自动降仓"
        }
    
    def _check_curiosity(self) -> dict:
        """好奇心倾向: 新概念/次新股追逐"""
        list_years = self.d.get("listing_years", 10)
        concept_new = self.d.get("new_concept_score", 0)
        score = 0
        if list_years < 2: score += 4  # 上市未满2年
        if list_years < 1: score += 3
        if concept_new > 7: score += 2  # 新概念热度高
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"上市{list_years:.0f}年, 新概念热度{concept_new}/10",
            "rule": "次新股单独加高一级风控;新概念禁止重仓"
        }
    
    def _check_fairness(self) -> dict:
        """公平倾向: 情绪化报复抄底"""
        cap_outflow = self.d.get("capital_outflow_surge", False)
        score = 2 if cap_outflow else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"资金异动: {'是' if cap_outflow else '否'}",
            "rule": "不评判公平与否;主力异动仅作为风险因子"
        }
    
    def _check_envy(self) -> dict:
        """嫉妒/猜忌倾向: 踏空追高"""
        sector_ret_30d = self.d.get("sector_ret_30d", 0)
        score = 0
        if sector_ret_30d > 30: score += 3  # 板块短期>30%
        if sector_ret_30d > 50: score += 2
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"板块30日涨幅{sector_ret_30d:.0f}%",
            "rule": "板块涨幅>30%→提升情绪风险分位;强制降低单票仓位"
        }
    
    def _check_reciprocation(self) -> dict:
        """回馈倾向: 轻信研报/大V"""
        report_bullish = self.d.get("report_bullish_count", 0)
        score = min(3, report_bullish // 2)
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"近期看多研报{report_bullish}篇",
            "rule": "研报匹配反向利空交叉核验;正面权重下调30%"
        }
    
    def _check_simple_association(self) -> dict:
        """简单联想影响: 单一指标判定行情"""
        single_signal = self.d.get("single_kline_signal", False)
        score = 3 if single_signal else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"单K线信号: {'是' if single_signal else '否'}",
            "rule": "禁止单一信号生成指令;需≥3独立维度共振"
        }
    
    def _check_denial(self) -> dict:
        """避免痛苦的心理否认: 无视利空"""
        black_swan = self.d.get("black_swan_active", False)
        deep_loss = self.d.get("deep_loss_position", False)
        score = 0
        if black_swan: score += 4
        if deep_loss: score += 3
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"黑天鹅事件:{black_swan}, 深度套牢:{deep_loss}",
            "rule": "利空强制弹窗展示;浮亏超阈值自动减仓提醒"
        }
    
    def _check_overconfidence(self) -> dict:
        """自视过高倾向: 频繁满仓"""
        position = self.d.get("current_position_pct", 0)
        recent_wins = self.d.get("recent_consecutive_wins", 0)
        score = 0
        if position > 70: score += 2  # 几乎满仓
        if recent_wins >= 3: score += 2  # 连续盈利→自信心膨胀
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"当前仓位{position:.0f}%, 连续盈利{recent_wins}笔",
            "rule": "单票≤12%/总仓≤75%;连续盈利3笔→后续仓位降1/3"
        }
    
    def _check_optimism(self) -> dict:
        """过度乐观倾向: 上涨周期调高预期"""
        ret_60d = self.d.get("ret_from_60d_high", 0)
        bull_market = self.d.get("bull_market_flag", False)
        score = 0
        if bull_market and ret_60d > -5: score += 2  # 接近高点
        if self.d.get("analyst_upgrade_count", 0) > 3: score += 2
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"距60日高:{ret_60d:.1f}%, 牛市:{bull_market}",
            "rule": "上涨周期保守估值;景气赛道强制悲观情景为仓位基准"
        }
    
    def _check_deprivation(self) -> dict:
        """被剥夺超级反应(高权重): 亏损死扛"""
        loss_pct = self.d.get("max_loss_from_peak", 0)
        score = 0
        if loss_pct < -15: score += 3
        if loss_pct < -25: score += 3
        return {
            "score": min(10, score * 1.5),  # 高权重
            "triggered": score >= 2,
            "detail": f"距高点回撤{loss_pct:.1f}%",
            "rule": "固定止损止盈阈值;不随盈亏状态调整"
        }
    
    def _check_social_proof(self) -> dict:
        """社会认同(羊群效应)"""
        market_consensus = self.d.get("market_consensus_pct", 50)
        score = 0
        if market_consensus > 80: score += 3  # 一致性>80%
        if market_consensus > 90: score += 2
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"市场观点一致性{market_consensus:.0f}%",
            "rule": "一致性>80%标记羊群高风险;过滤后排跟风股"
        }
    
    def _check_contrast(self) -> dict:
        """对比错误反应: 短期跌幅大=便宜"""
        ret_15d = self.d.get("ret_15d", 0)
        pe_5y_pct = self.d.get("pe_5y_percentile", 50)
        score = 0
        if ret_15d < -15 and pe_5y_pct > 50: score += 3  # 跌了但不便宜
        if ret_15d < -25: score += 2
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"15日{ret_15d:.0f}%, PE 5年分位{pe_5y_pct:.0f}%",
            "rule": "估值基准为5年分位;禁止以跌幅大小判低估"
        }
    
    def _check_stress(self) -> dict:
        """压力影响倾向: 回撤后激进操作"""
        dd = self.d.get("account_drawdown", 0)
        score = 2 if dd > 8 else (1 if dd > 5 else 0)
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"账户回撤{dd:.1f}%",
            "rule": "回撤超阈值→限制新开仓;增加悲观情景权重"
        }
    
    def _check_availability(self) -> dict:
        """易得性误导: 近期行情高估概率"""
        recent_vol = self.d.get("recent_volatility", 0)
        score = 1 if recent_vol > 30 else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"近期波动率{recent_vol:.0f}%",
            "rule": "概率基于10年数据;RAG均衡调取牛熊震荡三类案例"
        }
    
    def _check_forgetting(self) -> dict:
        """不用就忘倾向: 忽略历史极端风险"""
        score = 0
        if self.d.get("bull_market_duration_months", 0) > 12: score += 2
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"牛市持续{self.d.get('bull_market_duration_months',0)}月",
            "rule": "向量库永久存储历史股灾案例;每周压力测试"
        }
    
    def _check_chemical(self) -> dict:
        """化学物质错误影响: 情绪化交易"""
        score = 2 if self.d.get("manual_intervention_recent", False) else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"近期人工干预:{self.d.get('manual_intervention_recent',False)}",
            "rule": "全部自动化执行;人工干预需双重校验"
        }
    
    def _check_aging(self) -> dict:
        """衰老错误影响: 策略固化"""
        strategy_age_days = self.d.get("strategy_last_update_days", 0)
        score = 2 if strategy_age_days > 30 else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"策略距上次更新{strategy_age_days}天",
            "rule": "进化Agent每周复盘;每月纳入新数据;淘汰旧策略"
        }
    
    def _check_authority(self) -> dict:
        """权威错误影响(高权重): 盲信大V"""
        authority_score = self.d.get("authority_bullish_score", 0)
        score = min(4, authority_score // 2)
        return {
            "score": min(10, score * 1.5),  # 高权重
            "triggered": score >= 2,
            "detail": f"权威看多强度{authority_score}/10",
            "rule": "权威观点权重≤20%;目标价与实际财报做误差校验"
        }
    
    def _check_nonsense(self) -> dict:
        """废话倾向: 噪音干扰"""
        noise_ratio = self.d.get("noise_content_ratio", 0)
        score = 1 if noise_ratio > 0.3 else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"噪音内容占比{noise_ratio:.0%}",
            "rule": "舆情自动过滤无数据支撑的主观文字;噪音权重≤10%"
        }
    
    def _check_reason_respecting(self) -> dict:
        """重视理由倾向: 话术理由信以为真"""
        reason_only = self.d.get("reason_without_data", False)
        score = 3 if reason_only else 0
        return {
            "score": min(10, score),
            "triggered": score >= 3,
            "detail": f"无数据支撑理由:{reason_only}",
            "rule": "涨跌逻辑需财报+资金+行业三类数据佐证;单一文字不触发信号"
        }
    
    def _check_lollapalooza(self) -> dict:
        """Lollapalooza超级叠加: ≥3类同向因子共振"""
        # 统计看多方向因子
        bullish_biases = []
        bearish_biases = []
        
        mappings = {
            # (因子名, 看多方向trigger, 看空方向trigger)
            "bias_01": ("reward_punishment", True, False),
            "bias_02": ("liking", True, False),
            "bias_08": ("envy", True, False),
            "bias_09": ("reciprocation", True, False),
            "bias_13": ("optimism", True, False),
            "bias_15": ("social_proof", True, True),
            "bias_22": ("authority", True, False),
            "bias_05": ("consistency", False, True),
            "bias_11": ("denial", False, True),
            "bias_14": ("deprivation", False, True),
            "bias_17": ("stress", False, True),
        }
        
        # 检查已计算的因子
        for i in range(1, 25):
            key = f"bias_{i:02d}"
            if key in self.results:
                r = self.results[key]
                if r.get("triggered"):
                    # 正向泡沫组合: reward + social_proof + optimism + envy
                    if key in ["bias_01","bias_02","bias_08","bias_09","bias_13","bias_15","bias_22"]:
                        bullish_biases.append(key)
                    # 反向崩盘组合: deprivation + stress + denial + panic
                    if key in ["bias_05","bias_11","bias_14","bias_17"]:
                        bearish_biases.append(key)
        
        result = {
            "score": 0,
            "triggered": False,
            "lollapalooza_active": False,
            "direction": None,
            "bullish_count": len(bullish_biases),
            "bearish_count": len(bearish_biases),
            "bullish_biases": bullish_biases,
            "bearish_biases": bearish_biases,
            "detail": ""
        }
        
        # 判定共振 (阈值已调整为>=6, 2026-07-23修改)
        if len(bullish_biases) >= 6:
            result["lollapalooza_active"] = True
            result["direction"] = "bull"
            result["score"] = len(bullish_biases) * 2
            result["detail"] = f"正向泡沫共振: {len(bullish_biases)}类同向因子(奖励反馈+社会认同+过度乐观+嫉妒)"
            
        if len(bearish_biases) >= 6:
            result["lollapalooza_active"] = True
            result["direction"] = "bear"
            result["score"] = max(result["score"], len(bearish_biases) * 2)
            result["detail"] += f" | 反向崩盘共振: {len(bearish_biases)}类同向因子(被剥夺厌恶+压力+否认)"
        
        if result["lollapalooza_active"]:
            result["triggered"] = True
            result["score"] = min(10, result["score"])
            result["rule"] = "Lollapalooza高风险→风控Agent一票否决开仓;已持仓自动阶梯减仓"
        
        self.lollapalooza_count = len(bullish_biases) + len(bearish_biases)
        self.lollapalooza_direction = result["direction"]
        
        return result
    
    def get_checklist(self) -> list:
        """生成25项校验清单"""
        checklist = []
        for i in range(1, 26):
            key = f"bias_{i:02d}"
            r = self.results.get(key, {"triggered": False, "score": 0})
            checklist.append({
                "id": i,
                "name": self._bias_name(i),
                "triggered": r.get("triggered", False),
                "score": r.get("score", 0),
                "detail": r.get("detail", "")
            })
        return checklist
    
    def summary_report(self) -> dict:
        """生成总体报告"""
        triggered = [c for c in self.get_checklist() if c["triggered"]]
        high_risk = [c for c in triggered if c["score"] >= 5]
        
        return {
            "total_biases": 25,
            "triggered_count": len(triggered),
            "high_risk_count": len(high_risk),
            "lollapalooza": {
                "active": self.results.get("bias_25_lollapalooza", {}).get("lollapalooza_active", False),
                "direction": self.lollapalooza_direction,
                "bullish_count": self.results.get("bias_25_lollapalooza", {}).get("bullish_count", 0),
                "bearish_count": self.results.get("bias_25_lollapalooza", {}).get("bearish_count", 0)
            },
            "triggered_list": triggered,
            "veto_active": self.lollapalooza_count >= 6,
            "conclusion": (
                "🚫 Lollapalooza高风险→风控一票否决" if self.lollapalooza_count >= 6
                else f"⚠️ 触发{len(triggered)}类偏差,需关注" if len(triggered) >= 2
                else "✅ 心理偏差风险可控"
            )
        }
    
    @staticmethod
    def _bias_name(i: int) -> str:
        names = [
            "奖励/惩罚超级反应","喜欢/热爱倾向","讨厌/憎恨倾向",
            "避免怀疑倾向","避免不一致性(高权重)","好奇心倾向",
            "公平倾向","嫉妒/猜忌倾向","回馈倾向",
            "简单联想影响","避免痛苦的心理否认","自视过高倾向",
            "过度乐观倾向","被剥夺超级反应(高权重)","社会认同(羊群效应)",
            "对比错误反应","压力影响倾向","易得性误导",
            "不用就忘倾向","化学物质错误影响","衰老错误影响",
            "权威错误影响(高权重)","废话倾向","重视理由倾向",
            "Lollapalooza超级叠加"
        ]
        return names[i-1] if 1 <= i <= 25 else f"bias_{i}"


# ============================================================
# 主入口
# ============================================================

def analyze_biases(stock_code: str, market_data: dict = None) -> dict:
    """对某只个股执行全套25类心理误判检测"""
    print(f"\n{'='*60}")
    print(f"🧠 25类心理误判检测 — {stock_code} | {TODAY}")
    print(f"{'='*60}")
    
    # 缺省数据
    if market_data is None:
        market_data = {
            "ret_3d": 0, "ret_5d": 0, "ret_15d": -13.7,
            "pe_ttm": 36.13, "industry_pe": 32, "negative_news_count": 1,
            "historical_loss_count": 0, "data_missing_pct": 5,
            "position_pnl_pct": 0, "neg_news_ignored": 0,
            "listing_years": 30, "new_concept_score": 2,
            "capital_outflow_surge": False,
            "sector_ret_30d": 5, "report_bullish_count": 2,
            "single_kline_signal": False,
            "black_swan_active": False, "deep_loss_position": False,
            "current_position_pct": 0, "recent_consecutive_wins": 0,
            "ret_from_60d_high": -31.4, "bull_market_flag": False,
            "analyst_upgrade_count": 0,
            "max_loss_from_peak": -31.4,
            "market_consensus_pct": 45,
            "pe_5y_percentile": 55,
            "account_drawdown": 0,
            "recent_volatility": 25,
            "bull_market_duration_months": 0,
            "manual_intervention_recent": False,
            "strategy_last_update_days": 0,
            "authority_bullish_score": 2,
            "noise_content_ratio": 0.2,
            "reason_without_data": False,
        }
    
    checker = BiasChecker(stock_code, market_data)
    results = checker.check_all()
    
    # 25项校验清单
    checklist = checker.get_checklist()
    print(f"\n{'─'*60}")
    print("25项心理偏差校验清单:")
    print(f"{'─'*60}")
    for c in checklist:
        mark = "🔴" if c["triggered"] and c["score"] >= 5 else ("🟡" if c["triggered"] else "✅")
        print(f"  {mark} {c['id']:02d}. {c['name']:<20} | 得分{c['score']}/10 | {c['detail'][:40]}")
    
    # 汇总
    summary = checker.summary_report()
    print(f"\n{'='*60}")
    print(f"📊 汇总: {summary['conclusion']}")
    print(f"  触发偏差: {summary['triggered_count']}/25 | 高分: {summary['high_risk_count']}")
    print(f"  Lollapalooza: {'🚫激活(一票否决)' if summary['lollapalooza']['active'] else '✅未激活'}")
    if summary['lollapalooza']['active']:
        print(f"  方向: {summary['lollapalooza']['direction']} | "
              f"正向{summary['lollapalooza']['bullish_count']}类 反向{summary['lollapalooza']['bearish_count']}类")
    print(f"{'='*60}")
    
    return summary


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "600884"
    result = analyze_biases(code)
    
    # 保存报告
    fp = REPORTS / f"bias_check_{code}_{TODAY}.json"
    with open(fp, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n报告已保存: {fp}")

    # 如果Lollapalooza激活→风控一票否决
    if result.get("lollapalooza", {}).get("active"):
        print("\n🚫 【风控Agent一票否决】Lollapalooza高风险,禁止开仓")
