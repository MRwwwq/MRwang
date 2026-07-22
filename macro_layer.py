#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macro_layer.py — 宏观层综合分析 v1.0
================================================================
覆盖5维度:
  1. 利率: SHIBOR/LPR/美债 (Tushare)
  2. 汇率: USD/CNY 宏观储备数据 (AkShare)
  3. 大盘: 上证/深证/沪深300/科创板 (Tushare index_daily)
  4. 板块轮动: 申万行业指数排行+资金流向 (Tushare+AkShare)
  5. 行业联动: 目标标的所在行业指数走势 (申万)

用法:
  python3 macro_layer.py 600547                            # 含标的所在行业
  python3 macro_layer.py 600547 --sectors 贵金属,银行,半导体  # 自定义行业对比
"""

import sys, os, json
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════
#  标的 → 申万行业映射
# ═══════════════════════════════════════════════

# 自选股 → 申万行业代码映射
STOCK_SECTOR_MAP = {
    "600884": ("杉杉股份", "801080.SI", "贵金属/锂电"),
    "002617": ("露笑科技", "851521.SI", "半导体/碳化硅"),
    "600547": ("山东黄金", "801080.SI", "贵金属"),
    "002044": ("美年健康", "801780.SI", "医疗服务"),
    "300098": ("高新兴", "851521.SI", "通信/车联网"),
    "300693": ("胜宏科技", "801080.SI", "PCB/电子"),
    "300433": ("蓝思科技", "801080.SI", "消费电子"),
    "601868": ("中国能建", "801741.SI", "建筑/电力"),
}

# 常用行业对比池
DEFAULT_SECTORS = {
    "贵金属": "801080.SI",
    "银行": "801780.SI",
    "证券": "801790.SI", 
    "有色": "801050.SI",
    "房地产": "801710.SI",
    "汽车": "801726.SI",
    "电力": "801741.SI",
    "半导体": "851521.SI",
    "医药生物": "801150.SI",
    "计算机": "801750.SI",
    "食品饮料": "801120.SI",
}

# 大盘指数
MARKET_INDICES = {
    "上证指数": "000001.SH",
    "深证成指": "399001.SZ",
    "沪深300": "000300.SH",
    "上证50": "000016.SH",
    "科创50": "000688.SH",
    "创业板指": "399006.SZ",
}


# ═══════════════════════════════════════════════
#  数据获取
# ═══════════════════════════════════════════════

def get_market_indices(days: int = 20) -> dict:
    """大盘指数近期表现"""
    import tushare as ts
    pro = ts.pro_api()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    results = {}
    for name, code in MARKET_INDICES.items():
        try:
            df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                last = df.iloc[-1]
                first = df.iloc[0]
                period_chg = round((last["close"] / first["close"] - 1) * 100, 2)
                results[name] = {
                    "close": float(last["close"]),
                    "pct_chg": float(last.get("pct_chg", 0)),
                    "period_chg_pct": period_chg,
                    "volume": float(last.get("vol", 0)),
                    "date": str(last.get("trade_date", "")),
                    "trend": "up" if period_chg > 0 else "down",
                }
        except Exception:
            continue
    return results


def get_interest_rates() -> dict:
    """利率环境"""
    import tushare as ts
    pro = ts.pro_api()
    today = datetime.now().strftime("%Y%m%d")

    results = {}
    try:
        df = pro.shibor(start_date=today, end_date=today)
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            results["shibor_on"] = float(last.get("on", 0))
            results["shibor_1w"] = float(last.get("1w", 0))
            results["shibor_1m"] = float(last.get("1m", 0))
            results["shibor_1y"] = float(last.get("1y", 0))
            results["shibor_env"] = _shibor_env(results["shibor_on"])
        else:
            # 取最近一日
            df = pro.shibor(start_date=(datetime.now()-timedelta(days=7)).strftime("%Y%m%d"), end_date=today)
            if df is not None and len(df) > 0:
                last = df.iloc[-1]
                results["shibor_on"] = float(last.get("on", 0))
                results["shibor_1w"] = float(last.get("1w", 0))
                results["shibor_1m"] = float(last.get("1m", 0))
                results["shibor_1y"] = float(last.get("1y", 0))
                results["shibor_env"] = _shibor_env(results["shibor_on"])
    except Exception:
        results["error"] = "SHIBOR unavailable"

    return results


def _shibor_env(on: float) -> str:
    """利率环境判定"""
    if on < 1.0:
        return "偏宽松"
    elif on < 1.5:
        return "中性偏松"
    elif on < 2.0:
        return "中性偏紧"
    else:
        return "偏紧"


def get_exchange_rate() -> dict:
    """汇率环境(黄金储备+外汇储备)"""
    results = {}
    try:
        import akshare as ak
        df = ak.macro_china_fx_gold()
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            results["gold_reserve"] = float(last.get("黄金储备-数值", 0))
            results["gold_reserve_change"] = float(last.get("黄金储备-环比", 0))
            results["fx_reserve"] = float(last.get("国家外汇储备-数值", 0))
            results["fx_reserve_change"] = float(last.get("国家外汇储备-环比", 0))
            results["month"] = str(last.get("月份", ""))
            # 判定
            if results["gold_reserve_change"] > 0:
                results["gold_trend"] = "增持"
            else:
                results["gold_trend"] = "减持"
    except Exception:
        results["error"] = "FX/gold data unavailable"
    return results


def get_sector_performance(sectors: dict = None, days: int = 20) -> dict:
    """
    行业板块近期表现排行

    :param sectors: {name: sw_code, ...}，默认 ALL
    :param days: 统计区间天数
    :return: {name: {close, pct_chg, period_chg, ...}, ...}
    """
    import tushare as ts
    pro = ts.pro_api()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    if sectors is None:
        sectors = DEFAULT_SECTORS

    results = {}
    for name, code in sectors.items():
        try:
            df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                last = df.iloc[-1]
                first = df.iloc[0]
                period_chg = round((last["close"] / first["close"] - 1) * 100, 2)
                results[name] = {
                    "close": float(last["close"]),
                    "pct_chg": float(last.get("pct_chg", 0)),
                    "period_chg_pct": period_chg,
                    "date": str(last.get("trade_date", "")),
                }
        except Exception:
            continue

    # 按区间涨幅排序
    ranked = sorted(results.items(), key=lambda x: x[1]["period_chg_pct"], reverse=True)
    return {name: data for name, data in ranked}


def get_sector_fund_flow(top_n: int = 10) -> dict:
    """
    行业资金流向排行

    :param top_n: 返回前N个流入/流出
    :return: {"inflow_top": [...], "outflow_top": [...]}
    """
    try:
        import akshare as ak
        df = ak.stock_fund_flow_industry()
        if df is None or len(df) == 0:
            return {}

        # 排序
        df_sorted = df.sort_values("净额", ascending=False)
        inflow = []
        outflow = []
        for _, row in df_sorted.head(top_n).iterrows():
            inflow.append({
                "sector": str(row.get("行业", "")),
                "index": float(row.get("行业指数", 0)),
                "chg_pct": float(row.get("行业-涨跌幅", 0)),
                "net_inflow": float(row.get("净额", 0)),
            })
        for _, row in df_sorted.tail(top_n).iterrows():
            outflow.append({
                "sector": str(row.get("行业", "")),
                "index": float(row.get("行业指数", 0)),
                "chg_pct": float(row.get("行业-涨跌幅", 0)),
                "net_inflow": float(row.get("净额", 0)),
            })
        outflow.reverse()
        return {"inflow_top": inflow, "outflow_top": outflow, "total_sectors": len(df)}
    except Exception as e:
        return {"error": str(e)}


def get_stock_sector_info(symbol: str) -> dict:
    """
    获取标的所在行业信息

    :param symbol: 股票代码
    :return: {name, sector, sector_code, sector_performance, ...}
    """
    info = STOCK_SECTOR_MAP.get(symbol)
    if info is None:
        return {"symbol": symbol, "sector": "未知"}

    stock_name, sector_code, sector_name = info
    result = {
        "symbol": symbol,
        "name": stock_name,
        "sector": sector_name,
        "sector_code": sector_code,
    }

    # 获取行业指数表现
    perf = get_sector_performance({sector_name: sector_code}, days=20)
    if sector_name in perf:
        result["sector_performance"] = perf[sector_name]

    # 行业资金流
    try:
        import akshare as ak
        df = ak.stock_fund_flow_industry()
        if df is not None and len(df) > 0:
            mask = df["行业"].str.contains(sector_name[:2]) if "行业" in df.columns else None
            if mask is not None:
                sector_flow = df[mask]
                if len(sector_flow) > 0:
                    row = sector_flow.iloc[0]
                    result["sector_fund_flow"] = {
                        "net_inflow": float(row.get("净额", 0)),
                        "inflow": float(row.get("流入资金", 0)),
                        "outflow": float(row.get("流出资金", 0)),
                    }
    except Exception:
        pass

    return result


# ═══════════════════════════════════════════════
#  全流程
# ═══════════════════════════════════════════════

def run(symbol: Optional[str] = None, show_sectors: list = None) -> dict:
    """
    宏观层全量分析

    :param symbol: 标的代码(可选，用于显示所属行业)
    :param show_sectors: 额外行业对比列表
    :return: 汇总dict
    """
    print(f"\n{'='*60}")
    print(f"  🌍 宏观层综合分析")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    result = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 1. 大盘指数
    print("\n📊 大盘指数:")
    indices = get_market_indices(20)
    result["market_indices"] = indices
    for name, data in indices.items():
        arrow = "🟢" if data["period_chg_pct"] > 0 else "🔴"
        print(f"  {name:<10} {data['close']:<10.1f} {arrow} {data['period_chg_pct']:>+.2f}% (日{data['pct_chg']:>+.2f}%)")

    # 2. 利率
    print("\n💰 利率环境:")
    rates = get_interest_rates()
    result["interest_rates"] = rates
    if "shibor_on" in rates:
        print(f"  SHIBOR: 隔夜{rates['shibor_on']}% 1周{rates['shibor_1w']}% 1月{rates['shibor_1m']}% 1年{rates['shibor_1y']}%")
        print(f"  流动性判断: {rates.get('shibor_env', 'N/A')}")

    # 3. 汇率/黄金储备
    print("\n💱 汇率/储备:")
    fx = get_exchange_rate()
    result["exchange_rate"] = fx
    if "gold_reserve" in fx:
        print(f"  黄金储备: {fx['gold_reserve']:.0f}吨 ({fx['gold_reserve_change']:>+.2f}%环比) {fx.get('gold_trend', '')}")
        print(f"  外汇储备: {fx['fx_reserve']:.0f}亿美元 ({fx['fx_reserve_change']:>+.2f}%环比)")

    # 4. 板块轮动
    print("\n🔄 板块轮动(20日涨幅排行):")
    sectors_to_check = DEFAULT_SECTORS.copy()
    if show_sectors:
        extra = {s: DEFAULT_SECTORS.get(s, "") for s in show_sectors if s in DEFAULT_SECTORS}
        sectors_to_check.update(extra)
    
    perf = get_sector_performance(sectors_to_check, 20)
    result["sector_rotation"] = perf
    rank = 1
    for name, data in perf.items():
        arrow = "🟢" if data["period_chg_pct"] > 0 else "🔴"
        print(f"  {rank}. {name:<10} {data['close']:<10.1f} {arrow} {data['period_chg_pct']:>+.2f}% (日{data['pct_chg']:>+.2f}%)")
        rank += 1

    # 5. 行业资金流向
    print("\n💰 行业资金流向TOP5:")
    fund_flow = get_sector_fund_flow(5)
    result["sector_fund_flow"] = fund_flow
    if "inflow_top" in fund_flow:
        print("  流入TOP:")
        for s in fund_flow["inflow_top"]:
            print(f"    🟢 {s['sector']:<10} 净额{s['net_inflow']:>+8.1f}亿 ({s['chg_pct']:>+.2f}%)")
        print("  流出TOP:")
        for s in fund_flow["outflow_top"]:
            print(f"    🔴 {s['sector']:<10} 净额{s['net_inflow']:>+8.1f}亿 ({s['chg_pct']:>+.2f}%)")

    # 6. 标的所属行业
    if symbol:
        print(f"\n🎯 标的行业: {symbol}")
        stock_sector = get_stock_sector_info(symbol)
        result["stock_sector"] = stock_sector
        print(f"  {stock_sector.get('name','')} → {stock_sector.get('sector','未知')}")
        if "sector_performance" in stock_sector:
            sp = stock_sector["sector_performance"]
            print(f"  行业指数: {sp['close']:.1f} (20日{sp['period_chg_pct']:>+.2f}%)")
        if "sector_fund_flow" in stock_sector:
            sf = stock_sector["sector_fund_flow"]
            print(f"  行业资金: 净流入{sf['net_inflow']:+.1f}亿 (流入{sf['inflow']:.1f}亿/流出{sf['outflow']:.1f}亿)")

    # 综合评估
    print(f"\n{'='*60}")
    bullish_sectors = sum(1 for v in perf.values() if v["period_chg_pct"] > 0)
    bearish_sectors = sum(1 for v in perf.values() if v["period_chg_pct"] <= 0)
    total_sectors = len(perf)
    ratio = bullish_sectors / max(1, total_sectors)

    if ratio > 0.6:
        macro_sentiment = "🟢 偏积极 (多数行业上涨)"
    elif ratio < 0.3:
        macro_sentiment = "🔴 偏消极 (多数行业下跌)"
    else:
        macro_sentiment = "🟡 结构性行情 (行业分化)"

    print(f"  行业上涨比例: {bullish_sectors}/{total_sectors} ({ratio*100:.0f}%)")
    print(f"  宏观情绪: {macro_sentiment}")
    result["macro_sentiment"] = macro_sentiment
    result["bullish_sectors"] = bullish_sectors
    result["total_sectors"] = total_sectors
    print(f"{'='*60}")

    return result


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="宏观层综合分析")
    parser.add_argument("symbol", type=str, nargs="?", default=None, help="股票代码(可选)")
    parser.add_argument("--sectors", type=str, default="", help="额外行业对比,逗号分隔")
    args = parser.parse_args()

    extra = [s.strip() for s in args.sectors.split(",") if s.strip()] if args.sectors else None
    result = run(symbol=args.symbol, show_sectors=extra)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
