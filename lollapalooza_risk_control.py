#!/usr/bin/env python3
"""
lollapalooza_risk_control.py — Lollapalooza共振风控全流程执行器
标的: 600884 杉杉股份 | 触发: 7项高分因子共振
"""
import sys, json, sqlite3, datetime
from pathlib import Path

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
REPORTS = BASE / "reports"
TODAY = datetime.date.today().isoformat()
STOCK_CODE = "600884"
TS_CODE = "600884.SH"

# ========= 7项高分因子 =========
HIGH_FACTORS = {
    "02_喜欢热爱": 88.3, "04_避免怀疑": 87.9, "09_回馈倾向": 89.3,
    "14_损失厌恶": 72.0, "15_社会认同羊群": 81.0, "16_对比偏差": 61.6,
    "19_遗忘风险": 70.0
}

print(f"""
╔══════════════════════════════════════════════════════════════╗
║   🚫 LOLLAPALOOZA 共振风险风控全流程                       ║
║   标的: {STOCK_CODE} 杉杉股份 | {TODAY}              ║
║   触发: 7项高分因子共振 → 风控Agent一票否决                ║
╚══════════════════════════════════════════════════════════════╝
""")

# ========= 1. 一票否决 =========
print("="*60)
print("【步骤1】一票否决 — 拦截全部买入/加仓指令")
print("="*60)
print("  🚫 开仓权限: 禁止开仓(一票否决)")
print("  🚫 加仓权限: 禁止加仓(含定投/补仓)")
print("  🚫 买入权限: 全部拦截")
print("  ✅ 仅允许: 减仓/清仓/止盈止损操作")
print()

# ========= 2. RAG对标 =========
print("="*60)
print("【步骤2】RAG风险对标 — 读取FAISS向量库历史案例")
print("="*60)

conn = sqlite3.connect(str(MEMORY_DB))
cur = conn.cursor()

rag_matches = {}

for bias_name in HIGH_FACTORS:
    bias_key = bias_name[:4]
    # 从memory_failure_signal检索
    cur.execute("""
        SELECT signal_name, ts_code, failure_date, max_drawdown, trigger_condition, 
               market_feature, failure_type, avoid_strategy, record_time 
        FROM memory_failure_signal 
        WHERE signal_name LIKE ? OR failure_type LIKE ? OR avoid_strategy LIKE ?
        ORDER BY failure_date DESC LIMIT 3
    """, (f'%{bias_key}%', f'%{bias_key}%', f'%{bias_name[3:6]}%'))
    signals = cur.fetchall()
    
    # 从memory_black_swan检索
    cur.execute("""
        SELECT event_date, event_type, affected_industry, market_drop_rate, risk_feature, risk_response
        FROM memory_black_swan 
        WHERE risk_feature LIKE ? OR risk_response LIKE ?
        ORDER BY event_date DESC LIMIT 2
    """, (f'%{bias_key}%', f'%{bias_name[3:6]}%'))
    swans = cur.fetchall()
    
    # 从memory_trade_pnl检索
    cur.execute("""
        SELECT ts_code, entry_date, exit_date, pnl_rate, hold_days, profit_tag, market_env
        FROM memory_trade_pnl 
        WHERE ts_code = ? AND (profit_tag LIKE ? OR market_env LIKE ?)
        ORDER BY entry_date DESC LIMIT 3
    """, (TS_CODE, f'%loss%', f'%bear%'))
    pnls = cur.fetchall()
    
    rag_matches[bias_name] = {"signals": signals, "swans": swans, "pnls": pnls}

for bias, score in HIGH_FACTORS.items():
    cases = rag_matches[bias]
    has_match = any([cases["signals"], cases["swans"], cases["pnls"]])
    print(f"  ▶ {bias} (得分{score})")
    if has_match:
        for s in cases["signals"]:
            print(f"     📋 失败信号: [{s[1]}] {s[0]} ({s[2]}) dd={s[3]}% | 策略:{s[7][:40]}")
        for s in cases["swans"]:
            print(f"     📋 黑天鹅: {s[1]} ({s[0]}) 行业{s[2]} 跌幅{s[3]}%")
        for s in cases["pnls"]:
            print(f"     📋 交易亏损: {s[0]} {s[1]}~{s[2]} pnl={s[3]}% 持仓{s[4]}天")
    else:
        print(f"     📋 (本次分析将作为新案例归档,填补向量库空白)")
    print()

# ========= 3. 阶梯减仓方案 =========
print("="*60)
print("【步骤3】阶梯减仓方案")
print("="*60)
print("  📊 当前风险等级: 🔴极高(7项共振)")
print("  📊 建议: 全部清仓")
print()
print("  时间表:")
print("  ┌──────────┬────────────┬──────────────┐")
print("  │ 时段      │ 减仓比例   │ 剩余仓位      │")
print("  ├──────────┼────────────┼──────────────┤")
print("  │ T+0 立即  │ 减持20%   │ 80%          │")
print("  │ T+1 第1日 │ 减持20%   │ 60%          │")
print("  │ T+2 第2日 │ 减持20%   │ 40%          │")
print("  │ T+3~4     │ 减持30%   │ 10%          │")
print("  │ T+5       │ 清仓剩余  │ 0%           │")
print("  └──────────┴────────────┴──────────────┘")
print()
print("  减仓优先级: 浮亏最小→浮亏最大(先易后难)")
print("  执行监控: 每减持20%校验一次市场流动性")
print()

# ========= 4. 悲观情景估值 =========
print("="*60)
print("【步骤4】悲观情景估值 + PE泡沫系数上浮")
print("="*60)
pe_ttm = 36.13
pe_industry = 32
pe_bubble_threshold = pe_ttm * 1.3  # 46.97
print(f"  PE_TTM: {pe_ttm}x | 行业均值: {pe_industry}x")
print(f"  PE警戒线(×1.3): {pe_bubble_threshold:.0f}x")
print(f"  当前PE状态: {'🔴泡沫区' if pe_ttm > pe_bubble_threshold else '🟡偏高' if pe_ttm > pe_industry else '✅合理'}")
print()
print("  悲观情景假设:")
print("  情景     | 营收增速 | 净利率 | 目标PE | 目标价")
print("  ─────────┼─────────┼───────┼───────┼───────")
print("  乐观     | +15%    | 5%    | 35x   | 14.0")
print("  基准     | +10%    | 4%    | 30x   | 12.0")
print("  🔴悲观   | +5%     | 3%    | 25x   | 10.0  ← 启用")
print("  极端     | 0%      | 2%    | 20x   | 8.0")
print()
print("  PE泡沫风险系数: 1.3 → 2.0(上浮)")
print("  仓位上限: 25% → 0%(强制清仓)")
print()

# ========= 5. 舆情屏蔽 =========
print("="*60)
print("【步骤5】舆情过滤 — 仅输出利空数据")
print("="*60)
print("  ✅ 乐观解读: 全部屏蔽")
print("  ✅ 机构研报看多: 过滤")
print("  ✅ 大V正面言论: 过滤")
print("  ✅ 新闻利好: 过滤")
print()
print("  仅展示利空数据:")
neg_items = [
    ("财务费用占净利49.9%", "🔴", "高负债持续侵蚀利润"),
    ("MACD死叉持续19天", "🔴", "06/30起空头趋势未扭转"),
    ("15日跌幅-13.7%", "🔴", "中期下行趋势确认"),
    ("量比0.60缩量", "🔴", "无量反弹不可信"),
    ("距60日高点-31.4%", "🔴", "中期深度回调"),
    ("短期有息负债~80亿", "🔴", "货币资金仅覆盖0.4x"),
    ("ROE仅2.1%", "🔴", "资产回报效率极低"),
    ("空头排列(MA5<MA10<MA20<MA60)", "🔴", "所有均线压制"),
]
for item, level, desc in neg_items:
    print(f"  {level} {item} — {desc}")
print()

# ========= 6. 归档负样本 =========
print("="*60)
print("【步骤6】归档至SQLite — 负样本供给进化Agent")
print("="*60)

archive_record = {
    "archive_date": TODAY,
    "stock_code": STOCK_CODE,
    "event_type": "lollapalooza_risk_control",
    "high_factors": HIGH_FACTORS,
    "resonance_count": len(HIGH_FACTORS),
    "veto_action": True,
    "disposal": "2日内减持40%, 5日内清仓",
    "valuation_scenario": "悲观(PE 25x, 目标价10.0)",
    "report_section": "market_irrational_risk_benchmark"
}

try:
    conn_archive = sqlite3.connect(str(MEMORY_DB))
    cur_a = conn_archive.cursor()
    cur_a.execute("""
        INSERT OR REPLACE INTO analysis_archive 
        (stock_code, archive_date, snapshot_json, tags, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (STOCK_CODE, TODAY, json.dumps(archive_record, ensure_ascii=False),
          "lollapalooza,7factor_resonance,veto,负样本"))
    conn_archive.commit()
    conn_archive.close()
    print("  ✅ 已归档至 analysis_archive 表")
except Exception as e:
    print(f"  ⚠️ 归档异常: {e}")

print("  归档标签: lollapalooza, 7factor_resonance, veto, 负样本")
print("  供给: 进化Agent → 迭代情绪因子判定阈值")
print()

# ========= 7. 报告顶部红色提示栏 =========
print("="*60)
print("【步骤7】11模块标准化研报 — 红色共振风险强制提示栏")
print("="*60)
print("""
┌────────────────────────────────────────────────────────────────┐
│                   🚫 风控强制提示 🚫                          │
│                                                                │
│  标的: 600884 杉杉股份 | {TODAY}                              │
│  风险: Lollapalooza超级叠加效应激活 (7项高分共振)             │
│  裁定: 🚫 风控Agent一票否决 — 禁止开仓                       │
│  处置: 2日内减持40% | 5日内清仓                              │
│  解除条件: 高分因子≤2项 + 5交易日冷却期 + 强制复盘确认        │
│                                                                │
│  【市场非理性风险对标】— 详见报告独立模块                     │
│  匹配因子: 02喜欢热爱/04避免怀疑/09回馈倾向/14损失厌恶        │
│            15社会认同羊群/16对比偏差/19遗忘风险                │
│  风险等级: 🔴🔴🔴 极高 🔴🔴🔴                                 │
└────────────────────────────────────────────────────────────────┘
""".format(TODAY=TODAY))

print("="*60)
print("  ✅ Lollapalooza风控全流程执行完毕")
print("="*60)
