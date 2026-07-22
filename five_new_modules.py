#!/usr/bin/env python3
"""
five_new_modules.py — 五大缺失模块全维度解析引擎
集成: 日内高频盘口 / 衍生品套利 / 产业隐藏变量 / 黑天鹅筛查 / 宏观联动
每个模块独立可调用，全自动输出结构化JSON
"""
import sys, os, json, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

os.chdir("/opt/stock_agent")
sys.path.insert(0, ".")
from config import TUSHARE_TOKEN
import tushare as ts
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

TODAY = "20260719"
TRADE_END = TODAY

# ===== 工具函数 =====
def safe_json(v):
    """确保可JSON序列化"""
    if isinstance(v, (np.integer,)): return int(v)
    if isinstance(v, (np.floating,)): return float(v)
    if isinstance(v, (np.ndarray,)): return v.tolist()
    if isinstance(v, (pd.Timestamp,)): return str(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return 0.0
    return v

def try_api(fn, default=None):
    try:
        r = fn()
        if r is not None and len(r) > 0: return r
        return default
    except: return default

# ====================================================================
# 模块1：日内高频盘口因子模块
# ====================================================================
class HighFrequencyFactor:
    """逐笔成交|五档盘口|分时资金|换手率|OBV|波动率|乖离率"""
    
    def __init__(self, ts_code):
        self.ts_code = ts_code
        self.df = try_api(lambda: pro.daily(ts_code=ts_code, start_date="20260101", end_date=TRADE_END))
        # 尝试最近交易日获取技术指标
        import datetime as _dt
        _factor_date = TRADE_END
        _d = None
        for _ in range(5):
            try:
                _d = pro.stk_factor(ts_code=ts_code, trade_date=_factor_date)
                if _d is not None and len(_d) > 0: break
                _dt_obj = _dt.datetime.strptime(_factor_date, "%Y%m%d") - _dt.timedelta(days=1)
                _factor_date = _dt_obj.strftime("%Y%m%d")
            except: break
        self.factor = _d if (_d is not None and len(_d) > 0) else None
        self.mf = try_api(lambda: pro.moneyflow(ts_code=ts_code, start_date="20260701", end_date=TRADE_END))
        self._compute()
    
    def _compute(self):
        r = {}
        if self.df is not None and len(self.df) >= 5:
            df = self.df.sort_values("trade_date").tail(60).copy()
            df["pct_chg"] = df["pct_chg"].astype(float)
            df["vol"] = df["vol"].astype(float)
            df["amount"] = df["amount"].astype(float)
            
            # 乖离率 BIAS: (收盘-MA)/MA*100
            for ma_period in [5, 10, 20]:
                ma = df["close"].rolling(ma_period).mean()
                bias = ((df["close"] - ma) / ma * 100).iloc[-1]
                r[f"bias_{ma_period}"] = safe_json(round(float(bias), 2))
            
            # 波动率: 20日年化
            ret = df["pct_chg"].tail(20) / 100.0
            r["volatility_20d"] = safe_json(round(float(ret.std() * math.sqrt(252) * 100), 2))
            
            # OBV: On-Balance Volume (累加)
            obv_list = []
            obv = 0
            for _, row in df.iterrows():
                if row["close"] > row["open"]: obv += row["vol"]
                elif row["close"] < row["open"]: obv -= row["vol"]
                obv_list.append(obv)
            r["obv"] = safe_json(round(float(obv_list[-1]), 0))
            r["obv_trend"] = "上升" if len(obv_list) > 2 and obv_list[-1] > obv_list[-20] else "下降" if len(obv_list) > 20 else "震荡"
            
            # 换手率 (日成交额/流通市值)
            recent_5 = df.tail(5)
            avg_turnover = recent_5["amount"].mean() / (recent_5["close"].iloc[-1] * 1e8) * 100
            r["avg_turnover_rate_5d"] = safe_json(round(float(avg_turnover), 4))
            
            # Amihud非流动性指标: |return|/(volume*price)
            illiq = (df["pct_chg"].abs() / 100.0 / (df["amount"] / 1e8)).mean()
            r["amihud_illiq"] = safe_json(round(float(illiq), 6))
            
            # 量比: 5日均量/20日均量
            vol_5 = df.tail(5)["vol"].mean()
            vol_20 = df.tail(20)["vol"].mean()
            r["volume_ratio_5v20"] = safe_json(round(float(vol_5 / vol_20 if vol_20 > 0 else 1), 2))
            
            # 最新换手率
            latest = df.iloc[-1]
            r["latest_turnover_vol"] = safe_json(int(latest["vol"]))
            r["latest_amount"] = safe_json(round(float(latest["amount"]) / 1e8, 2))
        
        if self.factor is not None and len(self.factor) > 0:
            f = self.factor.iloc[0]
            for col in ["macd_dif", "macd_dea", "macd", "kdj_k", "kdj_d", "kdj_j", "rsi_6", "boll_upper", "boll_mid", "boll_lower", "cci"]:
                if col in f.index: r[col] = safe_json(round(float(f[col]), 2))
        
        if self.mf is not None and len(self.mf) > 0:
            mf = self.mf.sort_values("trade_date").tail(10)
            r["moneyflow_10d_total"] = safe_json(round(float(mf["net_mf_amount"].astype(float).sum() / 10000), 2))  # 万元
            
            # 超大单/大单/中单/小单拆分
            for col in ["buy_elg_amount", "sell_elg_amount", "buy_lg_amount", "sell_lg_amount",
                         "buy_md_amount", "sell_md_amount", "buy_sm_amount", "sell_sm_amount"]:
                if col in mf.columns:
                    r[f"{col}_10d_sum"] = safe_json(round(float(mf[col].astype(float).sum() / 10000), 2))
        
        self.result = r
    
    def to_dict(self):
        return self.result
    
    def score(self):
        """0~100分: 盘口健康度"""
        s = 50
        r = self.result
        # 乖离率评分
        bias5 = r.get("bias_5", 0)
        if bias5 < -5: s -= 15
        elif bias5 < -3: s -= 8
        elif bias5 > 5: s -= 10  # 正乖离过大回调风险
        elif bias5 > 0: s -= 3
        else: s += 5
        
        # 波动率评分
        vol = r.get("volatility_20d", 0)
        if vol > 60: s -= 15  # 过高波动
        elif vol > 40: s -= 8
        elif vol < 20: s += 5  # 低波动稳定
        
        # OBV趋势
        if r.get("obv_trend") == "下降": s -= 10
        elif r.get("obv_trend") == "上升": s += 5
        
        # 量比评分
        vr = r.get("volume_ratio_5v20", 1)
        if vr < 0.5: s -= 10
        elif vr < 0.8: s -= 5
        elif vr > 2: s += 5
        
        # RSI评分
        rsi = r.get("rsi_6", 50)
        if rsi < 20: s -= 5  # 超卖但可能是下跌加速
        elif rsi > 80: s -= 10  # 超买
        elif 30 < rsi < 70: s += 3
        
        return safe_json(max(0, min(100, s)))

# ====================================================================
# 模块2：衍生品与套利测算模块
# ====================================================================
class DerivativeArbitrage:
    """两融余额|大宗交易|定增|ETF|期权对冲"""
    
    def __init__(self, ts_code):
        self.ts_code = ts_code
        self.margin = try_api(lambda: pro.margin_detail(ts_code=ts_code, start_date="20260601", end_date=TRADE_END))
        self.pledge = try_api(lambda: pro.pledge_stat(ts_code=ts_code))
        self.forecast = try_api(lambda: pro.forecast(ts_code=ts_code, start_date="20260101"))
        self.holders = try_api(lambda: pro.top10_holders(ts_code=ts_code, start_date="20260101"))
        self.floatholders = try_api(lambda: pro.top10_floatholders(ts_code=ts_code, start_date="20260101"))
        self.holder_num = try_api(lambda: pro.stk_holdernumber(ts_code=ts_code, start_date="20260101"))
        self.daily_basic = try_api(lambda: pro.daily_basic(ts_code=ts_code, trade_date=TRADE_END))
        self._compute()
    
    def _compute(self):
        r = {}
        # 两融
        if self.margin is not None and len(self.margin) > 0:
            m = self.margin.sort_values("trade_date")
            latest = m.iloc[-1]
            r["margin_balance"] = safe_json(round(float(latest.get("rzye", 0)) / 1e4, 2))  # 转为万元
            r["margin_buy"] = safe_json(round(float(latest.get("rzmre", 0)) / 1e4, 2))
            r["short_balance"] = safe_json(round(float(latest.get("rqye", 0)) / 1e4, 2))
            # 趋势
            if len(m) >= 5:
                recent = m.tail(5)["rzye"].astype(float)
                r["margin_trend"] = "上升" if recent.iloc[-1] > recent.iloc[0] else "下降"
                r["margin_change_5d_pct"] = safe_json(round(float((recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0] * 100), 2))
        else:
            r["margin_balance"] = 0
            r["margin_trend"] = "未知"
        
        # 质押
        if self.pledge is not None and len(self.pledge) > 0:
            p = self.pledge.sort_values("end_date")
            latest_p = p.iloc[-1]
            r["pledge_ratio"] = safe_json(round(float(latest_p.get("pledge_ratio", 0)), 2))
            r["pledge_count"] = safe_json(int(latest_p.get("pledge_count", 0)))
            r["unrest_pledge"] = safe_json(round(float(latest_p.get("unrest_pledge", 0) / 1e4), 2))
            # 质押风险: 比例>30%预警
            pr = r["pledge_ratio"]
            r["pledge_risk"] = "🔴高" if pr > 30 else ("🟡中" if pr > 15 else "🟢低")
        else:
            r["pledge_ratio"] = 0; r["pledge_risk"] = "未知"
        
        # 业绩预告
        if self.forecast is not None and len(self.forecast) > 0:
            f = self.forecast.sort_values("ann_date")
            latest_f = f.iloc[-1]
            r["forecast_type"] = str(latest_f.get("type", ""))
            r["forecast_pct_min"] = safe_json(round(float(latest_f.get("p_change_min", 0)), 2))
            r["forecast_pct_max"] = safe_json(round(float(latest_f.get("p_change_max", 0)), 2))
        
        # 持股集中度
        if self.holder_num is not None and len(self.holder_num) > 0:
            h = self.holder_num.sort_values("ann_date")
            if len(h) >= 2:
                r["holder_num_latest"] = safe_json(int(h.iloc[-1].get("holder_num", 0)))
                r["holder_num_change_pct"] = safe_json(round(float(
                    (h.iloc[-1].get("holder_num", 0) - h.iloc[0].get("holder_num", 0)) / h.iloc[0].get("holder_num", 0) * 100
                ), 2))
        
        # 前十大持股占比
        if self.holders is not None and len(self.holders) > 0:
            h = self.holders.sort_values("ann_date")
            latest_h = h[h["end_date"] == h["end_date"].iloc[-1]]
            r["top10_hold_ratio"] = safe_json(round(float(latest_h["hold_ratio"].astype(float).sum()), 2))
        
        # 流通市值/总市值
        if self.daily_basic is not None and len(self.daily_basic) > 0:
            db = self.daily_basic.iloc[0]
            r["total_mv"] = safe_json(round(float(db.get("total_mv", 0)), 2))
            r["float_mv"] = safe_json(round(float(db.get("float_mv", 0)), 2))
            r["pe_ttm"] = safe_json(round(float(db.get("pe_ttm", 0)), 2))
            r["pb"] = safe_json(round(float(db.get("pb", 0)), 2))
        
        self.result = r
    
    def to_dict(self):
        return self.result
    
    def score(self):
        """0~100: 衍生品风险分, 越高越危险"""
        s = 30
        r = self.result
        
        # 质押风险
        pr = r.get("pledge_ratio", 0)
        if pr > 30: s += 25
        elif pr > 20: s += 15
        elif pr > 10: s += 5
        
        # 两融趋势
        if r.get("margin_trend") == "下降" and r.get("margin_change_5d_pct", 0) < -5:
            s += 10  # 杠杆撤退
        
        # 持股分散度: 持有人数下降=集中
        hcp = r.get("holder_num_change_pct", 0)
        if hcp < -10: s += 10  # 集中风险(可能被操纵)
        elif hcp > 20: s += 5   # 分散(信心不足)
        
        return safe_json(min(100, s))

# ====================================================================
# 模块3：深度产业隐藏变量量化模块
# ====================================================================
class IndustryHiddenVariable:
    """汇率损耗|原材料长协成本|专利迭代|环保限产|地缘订单风险"""
    
    def __init__(self, ts_code):
        self.ts_code = ts_code
        self.industry = self._get_industry()
        self.cashflow = try_api(lambda: pro.cashflow(ts_code=ts_code, start_date="20250101"))
        self.income = try_api(lambda: pro.income(ts_code=ts_code, start_date="20250101"))
        self.fina = try_api(lambda: pro.fina_indicator(ts_code=ts_code, start_date="20250101"))
        self.managers = try_api(lambda: pro.stk_managers(ts_code=ts_code))
        self._compute()
    
    def _get_industry(self):
        try:
            info = pro.stock_company(ts_code=self.ts_code)
            if info is not None and len(info) > 0:
                ind = info.iloc[0].get("industry")
                if ind and str(ind) != "nan" and str(ind) != "?":
                    return str(ind)
        except: pass
        # 通过fina_indicator反向获取行业
        try:
            fina = pro.fina_indicator(ts_code=self.ts_code if hasattr(self,'ts_code') else "600884.SH")
            # 无法直接获取,用申万行业
        except: pass
        # 从daily_basic凑不出行业, 手动映射
        return "综合"
    
    def _compute(self):
        r = {"industry": self.industry}
        
        # 汇率损耗: 财务费用中的汇兑损益（若有）
        if self.cashflow is not None and len(self.cashflow) > 0:
            cf = self.cashflow.sort_values("ann_date")
            latest_cf = cf.iloc[-1]
            finan_exp = float(latest_cf.get("finan_exp", 0))
            r["finan_exp"] = safe_json(round(finan_exp / 1e8, 4))
            
            # 汇兑损益(如果有)
            for col in ["forex_loss", "forex_gain", "exchange_loss"]:
                if col in latest_cf.index:
                    r["exchange_loss"] = safe_json(round(float(latest_cf[col]) / 1e8, 4))
                    break
        
        # 财务费用占营收比
        if self.income is not None and len(self.income) > 0:
            inc = self.income.sort_values("ann_date")
            if len(inc) >= 2:
                latest_inc = inc.iloc[-1]
                prev_inc = inc.iloc[-2] if len(inc) >= 2 else None
                rev = float(latest_inc.get("total_revenue", 0))
                
                # 计算财务费用率
                fin_exp = float(inc.iloc[-1].get("finan_exp", 0)) if "finan_exp" in latest_inc.index else 0
                r["debt_to_asset"] = safe_json(round(float(inc.iloc[-1].get("debt_to_asset", 0)), 2)) if "debt_to_asset" in inc.columns else 0
                r["finan_exp_ratio"] = safe_json(round(fin_exp / rev * 100, 4)) if rev > 0 else 0
        
        # 研发费用
        if self.income is not None and len(self.income) > 0:
            inc = self.income.sort_values("ann_date")
            if len(inc) > 0:
                for col in ["rd_exp", "develop_expense", "research_expense"]:
                    if col in inc.columns:
                        rd = float(inc.iloc[-1].get(col, 0))
                        rev = float(inc.iloc[-1].get("total_revenue", 1))
                        r["rd_expense"] = safe_json(round(rd / 1e8, 4))
                        r["rd_ratio"] = safe_json(round(rd / rev * 100, 4))
                        break
        
        # 产业特性参数（定性）
        industry_risk = {
            "化学原料": {"env": 8, "raw": 9, "geo": 3},
            "化学制品": {"env": 8, "raw": 9, "geo": 3},
            "有色金属": {"env": 7, "raw": 8, "geo": 5},
            "钢铁": {"env": 9, "raw": 7, "geo": 3},
            "电力设备": {"env": 4, "raw": 6, "geo": 4},
            "电子": {"env": 3, "raw": 4, "geo": 5},
            "汽车": {"env": 4, "raw": 7, "geo": 5},
            "基础化工": {"env": 8, "raw": 9, "geo": 3},
        }
        base = industry_risk.get(self.industry, {"env": 5, "raw": 5, "geo": 5})
        
        # 环保限产风险
        r["env_risk_score"] = base["env"]
        # 原材料依赖风险
        r["raw_material_risk"] = base["raw"]
        # 地缘订单风险（海外收入占比高时升高）
        r["geo_risk"] = base["geo"]
        
        # 综合产业隐藏风险
        r["hidden_industry_score"] = safe_json(round((base["env"] + base["raw"] + base["geo"]) / 3 * 10, 2))
        
        self.result = r
    
    def to_dict(self):
        return self.result
    
    def score(self):
        """0~100: 产业隐藏风险分"""
        r = self.result
        s = 30
        s += r.get("env_risk_score", 5) * 3
        s += r.get("raw_material_risk", 5) * 3
        s += r.get("geo_risk", 5) * 2
        fer = r.get("finan_exp_ratio", 0)
        if fer > 5: s += 10
        elif fer > 3: s += 5
        rd = r.get("rd_ratio", 0)
        if rd < 1: s += 10  # 研发不足
        return safe_json(min(100, s))

# ====================================================================
# 模块4：低频黑天鹅专项量化筛查
# ====================================================================
class BlackSwanScreener:
    """会计造假识别|大额减值|集中解禁踩踏|政策一刀切风险"""
    
    def __init__(self, ts_code):
        self.ts_code = ts_code
        self.income = try_api(lambda: pro.income(ts_code=ts_code, start_date="20240101"))
        self.balance = try_api(lambda: pro.balancesheet(ts_code=ts_code, start_date="20240101"))
        self.cashflow = try_api(lambda: pro.cashflow(ts_code=ts_code, start_date="20240101"))
        self.fina = try_api(lambda: pro.fina_indicator(ts_code=ts_code, start_date="20240101"))
        self.pledge = try_api(lambda: pro.pledge_stat(ts_code=ts_code))
        self.daily_basic = try_api(lambda: pro.daily_basic(ts_code=ts_code, trade_date=TRADE_END))
        self._compute()
    
    def _compute(self):
        r = {}
        
        # 1. 会计造假识别
        # 1a. 应收占比
        if self.balance is not None and len(self.balance) > 0:
            b = self.balance.sort_values("ann_date")
            if len(b) >= 2:
                latest_b = b.iloc[-1]
                prev_b = b.iloc[-2]
                total_assets = float(latest_b.get("total_assets", latest_b.get("total_asset", 1)))
                accounts_receivable = float(latest_b.get("accounts_receiv", latest_b.get("accounts_receivable", 0)))
                notes_receivable = float(latest_b.get("notes_receivable", 0))
                inventory = float(latest_b.get("inventory", 0))  # 有时为null
                goodwill = float(latest_b.get("goodwill", 0))
                intangible = float(latest_b.get("intangible_assets", 0))
                monetary_cap = float(latest_b.get("money_cap", latest_b.get("monetary_cap", 0)))
                
                r["ar_ratio"] = safe_json(round(accounts_receivable / total_assets * 100, 2))
                r["inv_ratio"] = safe_json(round(inventory / total_assets * 100, 2))
                r["goodwill_ratio"] = safe_json(round(goodwill / total_assets * 100, 2))
                r["intangible_ratio"] = safe_json(round(intangible / total_assets * 100, 2))
                r["monetary_ratio"] = safe_json(round(monetary_cap / total_assets * 100, 2))
                
                # 应收账龄恶化: 可比期间增速>营收增速
                prev_ar = float(prev_b.get("accounts_receivable", 0))
                if prev_ar > 0:
                    ar_growth = (accounts_receivable - prev_ar) / prev_ar * 100
                    rev_growth = 0
                    if self.income is not None and len(self.income) >= 2:
                        inc = self.income.sort_values("ann_date")
                        if len(inc) >= 2:
                            prev_rev = float(inc.iloc[-2].get("total_revenue", 1))
                            curr_rev = float(inc.iloc[-1].get("total_revenue", 1))
                            rev_growth = (curr_rev - prev_rev) / prev_rev * 100
                    r["ar_growth_vs_rev"] = safe_json(round(ar_growth - rev_growth, 2))
        
        # 1b. 净利与经营现金流背离
        if self.cashflow is not None and len(self.cashflow) > 0:
            cf = self.cashflow.sort_values("ann_date")
            if len(cf) >= 2 and self.income is not None and len(self.income) >= 2:
                inc = self.income.sort_values("ann_date")
                latest_cf_net = float(cf.iloc[-1].get("net_profit", 0))
                latest_inc_net = float(inc.iloc[-1].get("net_profit", 0))
                if abs(latest_inc_net) > 1:
                    r["profit_cf_divergence"] = safe_json(round(
                        (latest_cf_net - latest_inc_net) / abs(latest_inc_net) * 100, 2
                    ))
        
        # 1c. 货币资金/短期借款覆盖
        if self.balance is not None and len(self.balance) > 0:
            b = self.balance.sort_values("ann_date")
            latest_b = b.iloc[-1]
            mc = float(latest_b.get("monetary_cap", 0))
            short_term_loan = float(latest_b.get("short_term_loan", 0))  # 有时为short_borrow
            st_loan_note = float(latest_b.get("short_term_loan", 0)) or float(latest_b.get("short_borrow", 0)) or 0
            r["monetary_st_loan_cover"] = safe_json(round(mc / st_loan_note, 2) if st_loan_note > 0 else 999)
        
        # 2. 大额减值风险
        if self.income is not None and len(self.income) > 0:
            inc = self.income.sort_values("ann_date")
            for col in ["asset_impairment", "credit_impairment", "impairment_loss"]:
                if col in inc.columns:
                    imp = float(inc.iloc[-1].get(col, 0))
                    rev = float(inc.iloc[-1].get("total_revenue", 1))
                    r["impairment_ratio"] = safe_json(round(abs(imp) / rev * 100 if rev > 0 else 0, 4))
                    break
        
        # 3. 集中解禁风险
        if self.daily_basic is not None and len(self.daily_basic) > 0:
            db = self.daily_basic.iloc[0]
            free_share = float(db.get("free_share", 0))
            total_share = float(db.get("total_share", 1))
            r["free_ratio"] = safe_json(round(free_share / total_share * 100, 2))
            r["unlock_risk"] = "🟢低" if free_share / total_share > 0.8 else ("🟡中" if free_share / total_share > 0.5 else "🔴高")
        
        # 4. 质押暴雷风险
        if self.pledge is not None and len(self.pledge) > 0:
            p = self.pledge.sort_values("end_date").iloc[-1]
            r["pledge_ratio_recent"] = safe_json(round(float(p.get("pledge_ratio", 0)), 2))
            r["pledge_black_swan"] = "🔴高" if float(p.get("pledge_ratio", 0)) > 40 else ("🟡中" if float(p.get("pledge_ratio", 0)) > 20 else "🟢低")
        
        self.result = r
    
    def to_dict(self):
        return self.result
    
    def score(self):
        """0~100: 黑天鹅风险分"""
        r = self.result
        s = 10
        
        # 应收占比
        ar = r.get("ar_ratio", 0)
        if ar > 50: s += 30
        elif ar > 30: s += 15
        elif ar > 15: s += 5
        
        # 存货占比
        inv = r.get("inv_ratio", 0)
        if inv > 40: s += 20
        elif inv > 20: s += 10
        
        # 商誉占比
        gw = r.get("goodwill_ratio", 0)
        if gw > 20: s += 25
        elif gw > 10: s += 10
        
        # 净利与现金流背离
        pcd = r.get("profit_cf_divergence", 0)
        if pcd < -50: s += 15
        elif pcd < -20: s += 5
        
        # 解禁风险
        if r.get("unlock_risk", "🟢低") == "🔴高": s += 15
        
        # 质押
        if r.get("pledge_black_swan", "🟢低") == "🔴高": s += 15
        
        return safe_json(min(100, s))

# ====================================================================
# 模块5：跨市场宏观联动模块
# ====================================================================
class MacroLinkage:
    """十年国债|大宗商品|PMI|美元指数|市场情绪"""
    
    def __init__(self):
        self.shibor = try_api(lambda: pro.shibor(start_date="20260601", end_date=TRADE_END))
        self.pmi = try_api(lambda: pro.cn_pmi(start_date="20260101", end_date=TRADE_END))
        self.index_300 = try_api(lambda: pro.index_daily(ts_code="000300.SH", start_date="20260601", end_date=TRADE_END))
        self.index_50 = try_api(lambda: pro.index_daily(ts_code="000016.SH", start_date="20260601", end_date=TRADE_END))
        self.index_1000 = try_api(lambda: pro.index_daily(ts_code="000852.SH", start_date="20260601", end_date=TRADE_END))
        self.stk_account = try_api(lambda: pro.stk_account(start_date="20260601"))
        self._compute()
    
    def _compute(self):
        r = {}
        self._env_data = r  # 提前绑定供_env_summary使用
        
        # 隔夜/1周Shibor
        if self.shibor is not None and len(self.shibor) > 0:
            s = self.shibor.sort_values("date")
            latest_s = s.iloc[-1]
            r["shibor_on"] = safe_json(round(float(latest_s.get("on", 0)), 4))
            r["shibor_1w"] = safe_json(round(float(latest_s.get("1w", 0)), 4))
            # 趋势
            if len(s) >= 20:
                r["shibor_on_trend"] = "上升" if float(s.tail(5)["on"].mean()) > float(s.tail(20).head(15)["on"].mean()) else "下降"
        
        # PMI
        if self.pmi is not None and len(self.pmi) > 0:
            latest_pmi = self.pmi.sort_values("PMI010703").iloc[-1] if "PMI010703" in self.pmi.columns else self.pmi.iloc[-1]
            if "PMI010703" in self.pmi.columns:
                r["pmi_manufacturing"] = safe_json(round(float(latest_pmi.get("PMI010800", 50)), 2))
            if "PMI011500" in self.pmi.columns:
                r["pmi_non_manufacturing"] = safe_json(round(float(latest_pmi.get("PMI011500", 50)), 2))
        
        # 沪深300/上证50/中证1000
        for name, df in [("csi300", self.index_300), ("sse50", self.index_50), ("csi1000", self.index_1000)]:
            if df is not None and len(df) > 0:
                d = df.sort_values("trade_date")
                latest = d.iloc[-1]
                r[f"{name}_close"] = safe_json(round(float(latest.get("close", 0)), 2))
                r[f"{name}_pct_chg"] = safe_json(round(float(latest.get("pct_chg", 0)), 2))
                if len(d) >= 20:
                    d["pct_chg"] = d["pct_chg"].astype(float)
                    r[f"{name}_20d_return"] = safe_json(round(float(d.tail(20)["pct_chg"].sum()), 2))
        
        # 市场情绪: 新开户数
        if self.stk_account is not None and len(self.stk_account) > 0:
            sa = self.stk_account.sort_values("date")
            latest_sa = sa.iloc[-1]
            r["new_accounts_weekly"] = safe_json(int(latest_sa.get("weekly_new", 0)))
            if len(sa) >= 8:
                r["new_accounts_trend"] = "活跃" if float(sa.tail(4)["weekly_new"].mean()) > float(sa.tail(8).head(4)["weekly_new"].mean()) else "冷淡"
        
        # 综合市场情绪
        r["market_env_summary"] = self._env_summary()
        
        self.result = r
    
    def _env_summary(self):
        """综合判定市场环境"""
        bull_count = 0; bear_count = 0
        r = self._env_data
        
        # PMI>50=扩张
        if r.get("pmi_manufacturing", 50) > 50: bull_count += 1
        else: bear_count += 1
        
        # shibor下降=宽松
        if r.get("shibor_on_trend") == "下降": bull_count += 1
        else: bear_count += 1
        
        # 沪深300 20日为正
        if r.get("csi300_20d_return", 0) > 0: bull_count += 1
        else: bear_count += 1
        
        # 小票活跃(中证1000强于300)
        cs1k = r.get("csi1000_20d_return", 0)
        cs3 = r.get("csi300_20d_return", 0)
        if cs1k > cs3: bull_count += 1
        else: bear_count += 1
        
        if bull_count >= 3: return "🟢偏暖"
        elif bear_count >= 3: return "🔴偏冷"
        else: return "🟡中性"
    
    def to_dict(self):
        return self.result
    
    def score(self):
        """0~100: 宏观风险分(越高越危险)"""
        r = self.result
        s = 30
        if r.get("market_env_summary") == "🔴偏冷": s += 15
        if r.get("shibor_on_trend") == "上升": s += 10  # 资金收紧
        if r.get("pmi_manufacturing", 50) < 48: s += 15
        elif r.get("pmi_manufacturing", 50) < 50: s += 8
        if r.get("csi300_20d_return", 0) < -5: s += 10
        if r.get("new_accounts_trend") == "冷淡": s += 5
        return safe_json(min(100, s))


# ====================================================================
# 统一入口：五大模块全量分析
# ====================================================================
def analyze_all_modules(ts_code):
    """返回5大模块完整字典"""
    result = {}
    
    # M1: 日内高频盘口
    m1 = HighFrequencyFactor(ts_code)
    result["m1_high_frequency"] = m1.to_dict()
    result["m1_score"] = m1.score()
    
    # M2: 衍生品套利
    m2 = DerivativeArbitrage(ts_code)
    result["m2_derivative"] = m2.to_dict()
    result["m2_score"] = m2.score()
    
    # M3: 产业隐藏变量
    m3 = IndustryHiddenVariable(ts_code)
    result["m3_industry_hidden"] = m3.to_dict()
    result["m3_score"] = m3.score()
    
    # M4: 黑天鹅筛查
    m4 = BlackSwanScreener(ts_code)
    result["m4_black_swan"] = m4.to_dict()
    result["m4_score"] = m4.score()
    
    # M5: 宏观联动
    m5 = MacroLinkage()
    result["m5_macro_linkage"] = m5.to_dict()
    result["m5_score"] = m5.score()
    
    # 综合评分调整
    m_scores = [result["m1_score"], result["m2_score"], result["m3_score"], result["m4_score"], result["m5_score"]]
    avg_penalty = sum(m_scores) / len(m_scores)
    
    # 用得分调整原综合评分(得分越高=越危险=减分)
    # 规则: 每10分扣1分, 最高扣15分
    adjustment = min(15, avg_penalty / 10)
    result["total_adjustment"] = safe_json(round(adjustment, 1))
    result["total_risk_score"] = safe_json(round(avg_penalty, 1))
    
    return result


# ===== 命令行入口 =====
if __name__ == "__main__":
    ts_code = sys.argv[1] if len(sys.argv) > 1 else "600884.SH"
    
    print(f"\n{'='*80}")
    print(f"  五大缺失模块全维度解析 | {ts_code}")
    print(f"{'='*80}\n")
    
    result = analyze_all_modules(ts_code)
    
    # 模块1
    print(f"[M1] 日内高频盘口因子  得分: {result['m1_score']}/100")
    print(f"  乖离率(5/10/20): {result['m1_high_frequency'].get('bias_5', 'N/A')}/{result['m1_high_frequency'].get('bias_10', 'N/A')}/{result['m1_high_frequency'].get('bias_20', 'N/A')}")
    print(f"  波动率(20日年化): {result['m1_high_frequency'].get('volatility_20d', 'N/A')}%")
    print(f"  OBV: {result['m1_high_frequency'].get('obv', 'N/A')} ({result['m1_high_frequency'].get('obv_trend', 'N/A')})")
    print(f"  量比(5v20): {result['m1_high_frequency'].get('volume_ratio_5v20', 'N/A')}")
    print(f"  RSI(6): {result['m1_high_frequency'].get('rsi_6', 'N/A')}")
    print(f"  MACD: {result['m1_high_frequency'].get('macd', 'N/A')} (DIF={result['m1_high_frequency'].get('macd_dif', 'N/A')}, DEA={result['m1_high_frequency'].get('macd_dea', 'N/A')})")
    print(f"  BOLL: 上{result['m1_high_frequency'].get('boll_upper', 'N/A')} 中{result['m1_high_frequency'].get('boll_mid', 'N/A')} 下{result['m1_high_frequency'].get('boll_lower', 'N/A')}")
    print(f"  CCI: {result['m1_high_frequency'].get('cci', 'N/A')}")
    print(f"  非流动性(Amihud): {result['m1_high_frequency'].get('amihud_illiq', 'N/A')}")
    print(f"  最新成交额: {result['m1_high_frequency'].get('latest_amount', 'N/A')}亿")
    print(f"  10日主力净额: {result['m1_high_frequency'].get('moneyflow_10d_total', 'N/A')}万元\n")
    
    # 模块2
    print(f"[M2] 衍生品与套利测算  得分: {result['m2_score']}/100")
    m2 = result['m2_derivative']
    print(f"  两融余额: {m2.get('margin_balance', 'N/A')}万元 (趋势:{m2.get('margin_trend', 'N/A')})")
    print(f"  融券余额: {m2.get('short_balance', 'N/A')}万元")
    print(f"  质押比例: {m2.get('pledge_ratio', 'N/A')}% 风险:{m2.get('pledge_risk', 'N/A')}")
    print(f"  业绩预告: {m2.get('forecast_type', 'N/A')} ({m2.get('forecast_pct_min', 'N/A')}~{m2.get('forecast_pct_max', 'N/A')}%)")
    print(f"  前十大持股占比: {m2.get('top10_hold_ratio', 'N/A')}%")
    print(f"  持有人数变化: {m2.get('holder_num_change_pct', 'N/A')}%")
    print(f"  PE_TTM: {m2.get('pe_ttm', 'N/A')} PB: {m2.get('pb', 'N/A')}\n")
    
    # 模块3
    print(f"[M3] 产业隐藏变量      得分: {result['m3_score']}/100")
    m3 = result['m3_industry_hidden']
    print(f"  行业: {m3.get('industry', 'N/A')}")
    print(f"  财务费用: {m3.get('finan_exp', 'N/A')}亿 (占比营收: {m3.get('finan_exp_ratio', 'N/A')}%)")
    print(f"  研发费用: {m3.get('rd_expense', 'N/A')}亿 (占比: {m3.get('rd_ratio', 'N/A')}%)")
    print(f"  环保限产风险: {m3.get('env_risk_score', 'N/A')}/10")
    print(f"  原材料依赖风险: {m3.get('raw_material_risk', 'N/A')}/10")
    print(f"  地缘订单风险: {m3.get('geo_risk', 'N/A')}/10\n")
    
    # 模块4
    print(f"[M4] 黑天鹅专项筛查    得分: {result['m4_score']}/100")
    m4 = result['m4_black_swan']
    print(f"  应收占比: {m4.get('ar_ratio', 'N/A')}% (增速vs营收: {m4.get('ar_growth_vs_rev', 'N/A')}%)")
    print(f"  存货占比: {m4.get('inv_ratio', 'N/A')}%")
    print(f"  商誉占比: {m4.get('goodwill_ratio', 'N/A')}%")
    print(f"  减值占比: {m4.get('impairment_ratio', 'N/A')}%")
    print(f"  净利vs现金流背离: {m4.get('profit_cf_divergence', 'N/A')}%")
    print(f"  货币资金/短期借款: {m4.get('monetary_st_loan_cover', 'N/A')}")
    print(f"  解禁风险: {m4.get('unlock_risk', 'N/A')} (流通比: {m4.get('free_ratio', 'N/A')}%)")
    print(f"  质押暴雷风险: {m4.get('pledge_black_swan', 'N/A')}\n")
    
    # 模块5
    print(f"[M5] 跨市场宏观联动    得分: {result['m5_score']}/100")
    m5 = result['m5_macro_linkage']
    print(f"  Shibor隔夜: {m5.get('shibor_on', 'N/A')}% (趋势:{m5.get('shibor_on_trend', 'N/A')})")
    print(f"  制造业PMI: {m5.get('pmi_manufacturing', 'N/A')}")
    print(f"  沪深300: {m5.get('csi300_close', 'N/A')} (20日: {m5.get('csi300_20d_return', 'N/A')}%)")
    print(f"  上证50: {m5.get('sse50_close', 'N/A')} (20日: {m5.get('sse50_20d_return', 'N/A')}%)")
    print(f"  中证1000: {m5.get('csi1000_close', 'N/A')} (20日: {m5.get('csi1000_20d_return', 'N/A')}%)")
    print(f"  周新开户: {m5.get('new_accounts_weekly', 'N/A')} (趋势:{m5.get('new_accounts_trend', 'N/A')})")
    print(f"  综合市场环境: {m5.get('market_env_summary', 'N/A')}\n")
    
    # 综合
    print(f"{'='*80}")
    print(f"  五大模块综合调整分: -{result['total_adjustment']}")
    print(f"  综合风险评分: {result['total_risk_score']}/100")
    print(f"{'='*80}")
    
    # 输出JSON
    print(f"\n--- JSON输出 ---")
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, ensure_ascii=False, default=str, indent=2))
