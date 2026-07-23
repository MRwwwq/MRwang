#!/usr/bin/env python3
"""
全A股批量扫描 v2 — 包含Lollapalooza ≥6阈值筛选
用法: python3 ashare_scan_lolla6.py [--top N]

流程:
  Phase1: PE 5-25, PB>0.8, 流通市值>50亿, 排除金融
  Phase2: 站MA20 + 非暴跌 + 有量
  Phase3: Lollapalooza 25类心理偏差检查 (阈值≥6才否决)
  Phase4: 输出通过名单
"""
import sys, json, time, os
import pandas as pd
import tushare as ts
from pathlib import Path

sys.path.insert(0, '/opt/stock_agent')
from psychological_bias_checker import BiasChecker

pd.set_option('display.max_rows', 50)
pd.set_option('display.width', 300)
pd.set_option('display.max_columns', 20)

pro = ts.pro_api()
today = '20260723'
TOP_N = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 100

print("=" * 70)
print("  🚀 全A扫描 v2 — Lollapalooza ≥6 阈值")
print(f"  日期: {today}  |  Top候选数: {TOP_N}")
print("=" * 70)

# ========== Phase1: 基本面粗筛 ==========
print("\n【Phase1】基本面粗筛")
db = pro.daily_basic(trade_date=today, fields='ts_code,pe_ttm,pb,circ_mv,close')
print(f"  全市场: {len(db)}只")

mask = (
    (db['pe_ttm'] >= 5) & (db['pe_ttm'] <= 25) &
    (db['pb'] >= 0.8) &
    (db['circ_mv'] > 5e5)  # 流通市值>50亿(万元)
)
phase1 = db[mask].copy()
print(f"  ✅ Phase1通过: {len(phase1)}只 (PE5-25, PB>0.8, 市值>50亿)")

# 合并行业信息
basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,industry,market')
phase1 = phase1.merge(basic, on='ts_code', how='left')

# 排除金融
fin_kw = ['银行', '证券', '保险', '信托', '期货']
fin_mask = phase1['industry'].fillna('').apply(lambda x: any(k in x for k in fin_kw))
phase1 = phase1[~fin_mask]
print(f"  ✅ 排除金融后: {len(phase1)}只")

# ========== Phase2: 技术面筛选 ==========
print("\n【Phase2】技术面筛选")
codes = phase1['ts_code'].tolist()
tech_records = []

for i in range(0, len(codes), 100):
    batch = codes[i:i+100]
    try:
        df = pro.daily(ts_code=','.join(batch), start_date='20260601', end_date=today)
        if df.empty:
            continue
        for code in batch:
            sub = df[df['ts_code'] == code].sort_values('trade_date', ascending=False)
            if len(sub) < 10:
                continue
            lat = sub.iloc[0]
            cn = lat['close']
            ma20 = sub.head(20)['close'].mean() if len(sub) >= 20 else 0
            above_ma20 = cn > ma20
            ret_20d = (cn / sub.iloc[min(19, len(sub)-1)]['close'] - 1) * 100 if len(sub) >= 20 else 0
            avg_vol = sub.head(5)['vol'].mean() if len(sub) >= 5 else 1
            vr = lat['vol'] / avg_vol if avg_vol > 0 else 0
            
            # 额外数据用于偏差检查
            ret_3d = (cn / sub.iloc[min(2, len(sub)-1)]['close'] - 1) * 100 if len(sub) >= 3 else 0
            ret_5d = (cn / sub.iloc[min(4, len(sub)-1)]['close'] - 1) * 100 if len(sub) >= 5 else 0
            ret_15d = (cn / sub.iloc[min(14, len(sub)-1)]['close'] - 1) * 100 if len(sub) >= 15 else 0
            high_60d = sub.head(60)['close'].max() if len(sub) >= 60 else cn
            ret_from_high = (cn / high_60d - 1) * 100
            
            # 波动率
            if len(sub) >= 20:
                returns = sub.head(20)['close'].pct_change().dropna()
                volatility = float(returns.std() * 100 * (252 ** 0.5))
            else:
                volatility = 25.0
            
            tech_records.append({
                'ts_code': code,
                'close': round(cn, 2),
                'pct_20d': round(ret_20d, 2),
                'above_ma20': above_ma20,
                'vol_ratio': round(vr, 2),
                'ret_3d': round(ret_3d, 2),
                'ret_5d': round(ret_5d, 2),
                'ret_15d': round(ret_15d, 2),
                'ret_from_high': round(ret_from_high, 2),
                'volatility': round(volatility, 1),
            })
    except Exception as e:
        print(f"  批次{i}错误: {e}")
    time.sleep(0.2)
    if i % 500 == 0:
        print(f"  进度: {i}/{len(codes)} ({len(tech_records)}条记录)")

print(f"  技术数据获取完成: {len(tech_records)}条")

df_tech = pd.DataFrame(tech_records)
merged = phase1.merge(df_tech, on='ts_code', how='inner')

# 技术面过滤
mask2 = (
    (merged['above_ma20'] == True) &
    (merged['pct_20d'] > -5) &
    (merged['vol_ratio'] > 0.5)
)
phase2 = merged[mask2].copy()
print(f"  ✅ Phase2通过(站MA20+非暴跌+有量): {len(phase2)}只")

# ========== Phase3: 资金流 + 排序取Top ==========
print("\n【Phase3】资金流排序取Top")
moneyflow_dir = Path("/opt/astock-data-toolkit/data/parquet/moneyflow")
top_candidates = []

if moneyflow_dir.exists():
    mf_files = sorted(moneyflow_dir.glob("*.parquet"))
    if mf_files:
        mf_latest = pd.read_parquet(mf_files[-1])
        # 资金净流入
        if 'net_amount' in mf_latest.columns:
            mf_sum = mf_latest.groupby('ts_code')['net_amount'].sum().reset_index()
            mf_sum.columns = ['ts_code', 'net_mf_15d']
            phase2 = phase2.merge(mf_sum, on='ts_code', how='left')
        if 'buy_sm_vol' in mf_latest.columns:
            small = mf_latest.groupby('ts_code')[['buy_sm_vol','sell_sm_vol']].sum().reset_index()
            small['retail_ratio'] = small['buy_sm_vol'] / (small['sell_sm_vol'] + 1)
            phase2 = phase2.merge(small[['ts_code','retail_ratio']], on='ts_code', how='left')

# 排序: PE低 + 涨幅好 + 资金流
phase2['score_rank'] = (
    phase2.groupby('pe_ttm')['pe_ttm'].rank(pct=True).fillna(0.5)
)
sort_cols = ['net_mf_15d'] if 'net_mf_15d' in phase2.columns else []
if sort_cols:
    phase2_sorted = phase2.sort_values(sort_cols + ['pe_ttm'], ascending=[False, True])
else:
    phase2_sorted = phase2.sort_values('pe_ttm', ascending=True)

top_df = phase2_sorted.head(TOP_N)
print(f"  Top {TOP_N}候选: 已筛选完成")

# 保存Phase2结果
phase2.to_csv('/tmp/phase2_candidates.csv', index=False)
top_df.to_csv('/tmp/top_candidates.csv', index=False)
print(f"  已保存到 /tmp/phase2_candidates.csv ({len(phase2)}只)")
print(f"          /tmp/top_candidates.csv (Top {TOP_N})")

# ========== Phase4: Lollapalooza偏差检查 ==========
print(f"\n【Phase4】Lollapalooza ≥6 偏差检查 (Top {TOP_N})")
print(f"{'='*70}")

passed_stocks = []
vetoed_stocks = []
industry_pe_map = phase1.groupby('industry')['pe_ttm'].mean().to_dict()

for idx, row in top_df.iterrows():
    code = row['ts_code']
    industry = row.get('industry', '')
    industry_pe = industry_pe_map.get(industry, 20)
    
    # 构建市场数据
    market_data = {
        "ret_3d": row.get('ret_3d', 0),
        "ret_5d": row.get('ret_5d', 0),
        "ret_15d": row.get('ret_15d', 0),
        "pe_ttm": float(row['pe_ttm']),
        "industry_pe": float(industry_pe),
        "negative_news_count": 0,
        "historical_loss_count": 0,
        "data_missing_pct": 5,
        "position_pnl_pct": 0,
        "neg_news_ignored": 0,
        "listing_years": 5,
        "new_concept_score": 3,
        "capital_outflow_surge": False,
        "sector_ret_30d": max(0, row.get('pct_20d', 0) * 1.2),
        "report_bullish_count": 1,
        "single_kline_signal": False,
        "black_swan_active": False,
        "deep_loss_position": False,
        "current_position_pct": 0,
        "recent_consecutive_wins": 0,
        "ret_from_60d_high": row.get('ret_from_high', 0),
        "bull_market_flag": row.get('pct_20d', 0) > 10,
        "analyst_upgrade_count": 1,
        "max_loss_from_peak": row.get('ret_from_high', 0),
        "market_consensus_pct": 50,
        "pe_5y_percentile": 40,
        "account_drawdown": 0,
        "recent_volatility": row.get('volatility', 25),
        "bull_market_duration_months": 1,
        "manual_intervention_recent": False,
        "strategy_last_update_days": 0,
        "authority_bullish_score": 2,
        "noise_content_ratio": 0.2,
        "reason_without_data": False,
    }
    
    # 运行偏差检查
    try:
        checker = BiasChecker(code, market_data)
        results = checker.check_all()
        summary = checker.summary_report()
        lolla = summary.get('lollapalooza', {})
        lolla_active = lolla.get('active', False)
        triggered_total = summary.get('triggered_count', 0)
        high_risk_count = summary.get('high_risk_count', 0)
        bullish_c = lolla.get('bullish_count', 0)
        bearish_c = lolla.get('bearish_count', 0)
        
        # 触发清单
        triggered_names = [c['name'] for c in summary.get('triggered_list', []) if c['triggered']]
    except Exception as e:
        print(f"  ⚠️ {code} 偏差检查异常: {e}")
        lolla_active = False
        triggered_total = 0
        bullish_c = 0
        bearish_c = 0
        triggered_names = []
    
    name = row.get('name', '')
    symbol = row.get('symbol', '')
    pe = row['pe_ttm']
    pb = row['pb']
    pct = row.get('pct_20d', 0)
    close = row.get('close', 0)
    net_mf = row.get('net_mf_15d', 0)
    mkt = row.get('circ_mv', 0) / 1e4  # 亿
    
    entry = {
        'code': code, 'name': name, 'symbol': symbol,
        'close': close, 'pe': pe, 'pb': pb,
        'pct_20d': pct, 'mkt_cap': round(mkt, 1),
        'net_mf_15d': net_mf,
        'triggered_total': triggered_total,
        'lolla_active': lolla_active,
        'bullish_c': bullish_c, 'bearish_c': bearish_c,
        'high_risk': high_risk_count,
        'triggered_names': triggered_names,
    }
    
    if lolla_active:
        vetoed_stocks.append(entry)
        mark = "🚫"
    else:
        passed_stocks.append(entry)
        mark = "✅"
    
    print(f"  {mark} {symbol}: {name} PE{pe:.0f} PB{pb:.1f} +{pct:.1f}% "
          f"触发{bullish_c+support_count if 'support_count' in dir() else triggered_total}/25 "
          f"Lolla:{'激活' if lolla_active else '正常'}")

# ========== Phase5: 结果 ==========
print(f"\n{'='*70}")
print(f"  📊 扫描结果汇总")
print(f"{'='*70}")
print(f"  Phase1通过: {len(phase1)}只")
print(f"  Phase2通过: {len(phase2)}只")
print(f"  Top{TOP_N}候选偏差检查:")
print(f"    ✅ Lollapalooza正常(可通过): {len(passed_stocks)}只")
print(f"    🚫 Lollapalooza激活(否决):  {len(vetoed_stocks)}只")
print()

# 按PE排序输出通过的
if passed_stocks:
    print("=" * 70)
    print("  🟢【通过标的 — Lollapalooza正常】")
    print("=" * 70)
    passed_sorted = sorted(passed_stocks, key=lambda x: x['pe'])
    print(f"{'代码':<12} {'名称':<10} {'PE':<6} {'PB':<6} {'20日%':<8} {'市值(亿)':<10} {'资金流':<12} {'触发/25':<8}")
    print("-" * 72)
    for s in passed_sorted:
        mf_str = f"{s['net_mf_15d']:+.1f}亿" if isinstance(s['net_mf_15d'], (int, float)) and abs(s['net_mf_15d']) < 1e5 else "-"
        print(f"{s['code']:<12} {s['name']:<10} {s['pe']:<6.1f} {s['pb']:<6.2f} "
              f"{s['pct_20d']:<+8.1f} {s['mkt_cap']:<10.1f} {mf_str:<12} {s['triggered_total']:<8}")

print()

if vetoed_stocks:
    print("=" * 70)
    print(f"  🚫【否决标的 — Lollapalooza激活({len(vetoed_stocks)}只)】")
    print("=" * 70)
    for s in vetoed_stocks[:10]:  # 只显示Top10
        print(f"  🚫 {s['name']}({s['code']}) PE{s['pe']:.1f} +{s['pct_20d']:.1f}% "
              f"触发{s['triggered_total']}类偏差")

# 保存最终结果
result = {
    'date': today,
    'threshold': '>=6',
    'phase1_pass': len(phase1),
    'phase2_pass': len(phase2),
    'top_n': TOP_N,
    'lolla_normal': len(passed_stocks),
    'lolla_vetoed': len(vetoed_stocks),
    'passed': passed_stocks,
    'vetoed': vetoed_stocks,
}
with open('/tmp/scan_lolla6_results.json', 'w') as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"\n结果已保存: /tmp/scan_lolla6_results.json")
