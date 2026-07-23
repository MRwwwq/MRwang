#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重点跟踪标的日常监控脚本
5只固定池: 格力电器000651, 中国中车601766, 伊利股份600887, 中远海控601919, 三一重工600031
执行模式:
  python3 watch_tracker.py           # 收盘扫描(15:35) — 完整数据
  python3 watch_tracker.py --pre     # 盘前快照(09:25) — 轻量版
"""

import sys, os, json, time
sys.path.insert(0, '/opt/stock_agent')
import tushare as ts
from config import TUSHARE_TOKEN
import pandas as pd
import numpy as np
from datetime import datetime, date

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# ===== 固定跟踪池 =====
WATCH_LIST = [
    {"code": "000651.SZ", "name": "格力电器", "logic": "PE7.8行业低位，15日35亿资金流入，高分红白电周期", "risk": "地产需求疲软、行业价格内卷"},
    {"code": "601766.SH", "name": "中国中车", "logic": "PE12.7，资金持续加速流入，轨交大修+海外订单景气", "risk": "大盘千亿市值，股价上涨弹性弱"},
    {"code": "600887.SH", "name": "伊利股份", "logic": "消费底部资金持续回补，龙头业绩稳健", "risk": "乳品行业总量收缩，价格竞争激烈"},
    {"code": "601919.SH", "name": "中远海控", "logic": "PE9.2低位，博弈运价上行周期，现金流充裕", "risk": "航运强周期，运价、业绩波动极大"},
    {"code": "600031.SH", "name": "三一重工", "logic": "海外高毛利业务放量，15日资金持续布局", "risk": "国内地产链长期拖累内需"},
    {"code": "600884.SH", "name": "杉杉股份", "logic": "国资入主股权出清+双主业周期反转，半年报预增262~334%", "risk": "短期有息负债超百亿，存货周转慢，板块资金偏弱"},
    {"code": "600547.SH", "name": "山东黄金", "logic": "纯黄金龙头，金价弹性极强，降息+避险+产能释放三重催化", "risk": "金价单边下行风险，负债率偏高，估值高于同业"},
    {"code": "002044.SZ", "name": "美年健康", "logic": "民营体检龙头，AI筛查高增+下半年旺季弹性+数据要素", "risk": "上半年淡季亏损，PE~60偏高，阿里减持，质控舆情"},
    {"code": "300476.SZ", "name": "胜宏科技", "logic": "英伟达AI服务器PCB核心供应商，VR200放量+海外产能扩张", "risk": "英伟达客户集中，PE>50偏高，原材料涨价压制毛利率"},
    {"code": "300433.SZ", "name": "蓝思科技", "logic": "苹果折叠UTG玻璃核心供应商，车载+VR光学增量，财务健康", "risk": "汇兑亏损拖累短期业绩，客户集中苹果，折叠渗透率存不确定性"},
]

TRADE_DATE = datetime.now().strftime("%Y%m%d")
IS_MODE_PRE = "--pre" in sys.argv

def get_latest_trade_date():
    """获取最近交易日"""
    cal = pro.trade_cal(start_date='20260701', end_date=TRADE_DATE)
    open_days = cal[cal['is_open'] == 1]['cal_date'].tolist()
    return max(open_days) if open_days else TRADE_DATE

def fetch_stock_data(code, latest_trade):
    """获取单只跟踪标的完整数据"""
    result = {}
    
    # 日线 + 均线
    df = pro.daily(ts_code=code, start_date='20260401', end_date=latest_trade)
    if df.empty:
        return None
    df = df.sort_values('trade_date')
    last = df.iloc[-1]
    close = float(last['close'])
    pct_chg = float(last['pct_chg']) if 'pct_chg' in last else 0.0
    amount_b = float(last['amount']) / 10000  # 亿元
    
    # 均线
    closes = df['close'].values
    ma5 = float(pd.Series(closes).rolling(5).mean().iloc[-1])
    ma10 = float(pd.Series(closes).rolling(10).mean().iloc[-1])
    ma20 = float(pd.Series(closes).rolling(20).mean().iloc[-1])
    ma60 = float(pd.Series(closes).rolling(60).mean().iloc[-1]) if len(closes) >= 60 else None
    
    # 量比（近5日均量 / 近20日均量）
    vol = last['vol']  # 手
    avg_vol_5 = df['vol'].tail(5).mean()
    avg_vol_20 = df['vol'].tail(20).mean()
    vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 1.0
    
    # 20日涨幅
    pct_20d = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
    
    result['close'] = close
    result['pct_chg'] = pct_chg
    result['amount_b'] = round(amount_b, 1)
    result['ma5'] = round(ma5, 2)
    result['ma10'] = round(ma10, 2)
    result['ma20'] = round(ma20, 2)
    result['ma60'] = round(ma60, 2) if ma60 else None
    result['vol_ratio'] = round(vol_ratio, 2)
    result['pct_20d'] = round(pct_20d, 1)
    
    # 均线判定
    above_ma20 = close > ma20
    above_ma60 = ma60 is not None and close > ma60
    result['trend'] = '🟢多头' if (above_ma20 and above_ma60) else '🟡震荡' if above_ma20 else '🔴空头'
    
    # 估值
    basic = pro.daily_basic(ts_code=code, start_date='20260701', end_date=latest_trade)
    if not basic.empty:
        b = basic.iloc[-1]
        result['pe_ttm'] = round(float(b['pe_ttm']), 1) if pd.notna(b['pe_ttm']) else '—'
        result['pb'] = round(float(b['pb']), 2) if pd.notna(b['pb']) else '—'
        result['total_mv'] = round(float(b['total_mv']) / 10000, 0) if pd.notna(b['total_mv']) else '—'
        result['turnover_rate'] = round(float(b['turnover_rate']), 1) if pd.notna(b['turnover_rate']) else '—'
    else:
        result['pe_ttm'] = result['pb'] = result['total_mv'] = result['turnover_rate'] = '—'
    
    # 资金流向（15日 + 3日）
    mf_start = str(int(latest_trade) - 30)  # 往前推30天确保够15日数据
    mf = pro.moneyflow(ts_code=code, start_date=mf_start, end_date=latest_trade)
    if not mf.empty and len(mf) >= 3:
        mf = mf.sort_values('trade_date')
        net_15d = mf['net_mf_amount'].tail(15).sum() / 10000 if len(mf) >= 15 else mf['net_mf_amount'].sum() / 10000
        net_3d = mf['net_mf_amount'].tail(3).sum() / 10000 if len(mf) >= 3 else 0
        net_1d = mf['net_mf_amount'].tail(1).sum() / 10000 if len(mf) >= 1 else 0
        
        # 特大单（机构）净额
        elg_net_15d = (mf['buy_elg_amount'].tail(15).sum() - mf['sell_elg_amount'].tail(15).sum()) / 10000 if 'buy_elg_amount' in mf.columns and len(mf) >= 15 else 0
        
        result['net_15d'] = round(net_15d, 2)
        result['net_3d'] = round(net_3d, 2)
        result['net_1d'] = round(net_1d, 2)
        result['elg_net_15d'] = round(elg_net_15d, 2)
        
        # 资金判定
        if net_15d > 5:
            result['fund_status'] = '🟢大幅流入'
        elif net_15d > 1:
            result['fund_status'] = '🟢持续流入'
        elif net_15d > 0:
            result['fund_status'] = '🟡小幅流入'
        elif net_15d > -5:
            result['fund_status'] = '🟡小幅流出'
        else:
            result['fund_status'] = '🔴大幅流出'
    else:
        result['net_15d'] = result['net_3d'] = result['net_1d'] = result['elg_net_15d'] = '—'
        result['fund_status'] = '—'
    
    return result

def build_markdown_table(data_rows, mode='post'):
    """生成Markdown表格"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    
    if mode == 'pre':
        lines.append(f"## 📋 重点跟踪标的盘前总览 — {today}")
        lines.append("")
        lines.append(f"| 代码 | 名称 | 最新价 | PE_TTM | 15日主力净额 | 趋势判定 | 核心逻辑 | 风险点 |")
        lines.append(f"|:----:|:----:|:-----:|:------:|:----------:|:--------:|:---------|:--------|")
    else:
        lines.append(f"## 📊 重点跟踪标的收盘扫描 — {today}")
        lines.append("")
        lines.append(f"| 代码 | 名称 | 收盘价 | 涨跌幅 | 成交额 | 换手率 | PE_TTM | PB | 15日净额 | 3日净额 | 特大单净 | 资金判定 | 均线趋势 | MA5/MA20/MA60 | 核心逻辑 | 风险 |")
        lines.append(f"|:----:|:----:|:-----:|:-----:|:-----:|:-----:|:------:|:--:|:--------:|:-------:|:--------:|:--------:|:--------:|:-------------|:---------|:----|")

    for row in data_rows:
        if row is None:
            continue
        c = row['code']
        n = row['name']
        
        if mode == 'pre':
            # 盘前精简版
            close = row['data']['close'] if row['data'] else '—'
            pe = row['data']['pe_ttm'] if row['data'] else '—'
            net15 = f"{row['data']['net_15d']:+}亿" if row['data'] and row['data']['net_15d'] != '—' else '—'
            trend = row['data']['trend'] if row['data'] else '—'
            lines.append(f"| {c[:6]} | {n} | {close} | {pe} | {net15} | {trend} | {row['logic']} | {row['risk']} |")
        else:
            # 收盘完整版
            d = row['data']
            if d is None:
                lines.append(f"| {c[:6]} | {n} | — | — | — | — | — | — | — | — | — | — | — | — | {row['logic']} | {row['risk']} |")
                continue
            close = d['close']
            pct = f"{d['pct_chg']:+.2f}%"
            amt = f"{d['amount_b']}亿"
            turn = f"{d['turnover_rate']}%"
            pe = d['pe_ttm']
            pb = d['pb']
            net15 = f"{d['net_15d']:+}亿" if d['net_15d'] != '—' else '—'
            net3 = f"{d['net_3d']:+}亿" if d['net_3d'] != '—' else '—'
            elg = f"{d['elg_net_15d']:+}亿" if d['elg_net_15d'] != '—' else '—'
            fund = d['fund_status']
            trend = d['trend']
            ma = f"{d['ma5']}/{d['ma20']}/{d['ma60'] or '—'}"
            lines.append(f"| {c[:6]} | {n} | {close} | {pct} | {amt} | {turn} | {pe} | {pb} | {net15} | {net3} | {elg} | {fund} | {trend} | {ma} | {row['logic']} | {row['risk']} |")
    
    lines.append("")
    lines.append("---")
    lines.append(f"*自动生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据源: Tushare Pro*")
    
    return "\n".join(lines)

def save_to_file(content, mode):
    """保存Markdown到本地"""
    today = datetime.now().strftime("%Y%m%d")
    prefix = "pre" if mode == 'pre' else "close"
    path = f"/opt/stock_agent/tracker_reports/{prefix}_watch_{today}.md"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path

def main():
    latest_trade = get_latest_trade_date()
    mode = 'pre' if IS_MODE_PRE else 'post'
    
    print(f"{'='*60}")
    print(f"重点跟踪标的 {'盘前快照' if IS_MODE_PRE else '收盘扫描'}")
    print(f"交易日: {latest_trade}")
    print(f"{'='*60}")
    
    data_rows = []
    for item in WATCH_LIST:
        code = item['code']
        print(f"  采集 {item['name']} ({code})...", end=' ', flush=True)
        try:
            data = fetch_stock_data(code, latest_trade)
            if data:
                print(f"✅ @{data['close']} PE={data['pe_ttm']}")
            else:
                print(f"❌ 无数据")
            data_rows.append({**item, 'data': data})
        except Exception as e:
            print(f"❌ {e}")
            data_rows.append({**item, 'data': None})
        time.sleep(0.5)
    
    # 生成Markdown
    markdown = build_markdown_table(data_rows, mode)
    print(f"\n{markdown}")
    
    # 保存本地
    path = save_to_file(markdown, mode)
    print(f"\n已保存: {path}")
    
    # 输出JSON供cron调度解析
    json_out = []
    for row in data_rows:
        if row['data']:
            json_out.append({
                'code': row['code'], 'name': row['name'],
                'close': row['data']['close'], 'pct_chg': row['data']['pct_chg'],
                'pe_ttm': row['data']['pe_ttm'], 'net_15d': row['data']['net_15d'],
                'net_3d': row['data']['net_3d'], 'fund_status': row['data']['fund_status'],
                'trend': row['data']['trend']
            })
    
    json_path = path.replace('.md', '.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'date': latest_trade,
            'mode': mode,
            'data': json_out
        }, f, ensure_ascii=False, indent=2)
    print(f"JSON已保存: {json_path}")
    
    print(f"\n{'='*60}")
    print(f"完成")

if __name__ == '__main__':
    main()
