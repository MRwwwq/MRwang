#!/usr/bin/env python3
"""全A股批量扫描 - 建仓标的筛选"""
import tushare as ts
import pandas as pd
import time, sys

pd.set_option('display.max_rows', 40)
pd.set_option('display.width', 250)

pro = ts.pro_api()
today = '20260723'

# Phase 1: 全市场粗筛
print("=== Phase1: 全市场基本面粗筛 (Lollapalooza阈值≥6) ===")
db = pro.daily_basic(trade_date=today, fields='ts_code,pe_ttm,pb,circ_mv,close')
print(f"全市场: {len(db)}只")

mask = (db['pe_ttm'] >= 5) & (db['pe_ttm'] <= 25) & (db['pb'] >= 0.8) & (db['circ_mv'] > 5e5)  # 流通市值>50亿(单位:万元)
phase1 = db[mask].copy()
print(f"Phase1通过: {len(phase1)}只 (PE5-25, PB>0.8, 流通市值>50亿)")

basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,industry,market')
phase1 = phase1.merge(basic, on='ts_code', how='left')
for kw in ['银行', '证券', '保险', '信托']:
    phase1 = phase1[~phase1['industry'].fillna('').str.contains(kw)]
print(f"排除金融后: {len(phase1)}只")

# Phase 2: 近期日线数据
print("\n=== Phase2: 技术面筛选 ===")
codes = phase1['ts_code'].tolist()
results = []

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
            above = cn > ma20
            ret_20d = (cn / sub.iloc[min(19, len(sub)-1)]['close'] - 1) * 100 if len(sub) >= 20 else 0
            avg_vol = sub.head(5)['vol'].mean() if len(sub) >= 5 else 1
            vr = lat['vol'] / avg_vol if avg_vol > 0 else 0
            results.append({
                'ts_code': code, 'close': round(cn, 2),
                'pct_20d': round(ret_20d, 2), 'above_ma20': above,
                'vol_ratio': round(vr, 2)
            })
    except Exception as e:
        print(f"  批次{i} err: {e}")
    time.sleep(0.2)
    if i % 500 == 0:
        print(f"  进度: {i}/{len(codes)}")

df2 = pd.DataFrame(results)
merged = phase1.merge(df2, on='ts_code', how='inner')

# 技术过滤
mask2 = (merged['above_ma20'] == True) & (merged['pct_20d'] > -5) & (merged['vol_ratio'] > 0.5)
phase2 = merged[mask2].sort_values('pe_ttm', ascending=True)
print(f"Phase2通过(站MA20+非暴跌+有量): {len(phase2)}只")

# 提前保存
phase2.to_csv('/tmp/phase2_candidates.csv', index=False)
print(f"已保存到 /tmp/phase2_candidates.csv ({len(phase2)}只)")

# Top 20
available_cols = ['ts_code', 'pe_ttm', 'pb', 'pct_20d', 'vol_ratio']
for c in ['name', 'industry', 'close', 'circ_mv']:
    if c in phase2.columns:
        available_cols.append(c)

print("\n=== Top20 (低PE+技术面好) ===")
print(phase2[available_cols].head(20).to_string())

print("\n=== Top20 (涨幅好+低PE) ===")
phase2_by_ret = phase2.sort_values('pct_20d', ascending=False)
print(phase2_by_ret[available_cols].head(20).to_string())

print(f"\n已保存到 /tmp/phase2_candidates.csv ({len(phase2)}只)")
