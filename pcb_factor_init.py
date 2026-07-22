#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcb_factor_init.py — 300476 胜宏科技 PCB赛道13因子IC权重初始化

用途: 为factor_weekly_iterate.py提供PCB制造赛道专属因子IC权重初始化表。
在factor_weekly_iterate.py中导入此模块，用于PCB制造赛道(SECTOR_GROUPS['PCB制造'])的因子权重初始化。

使用方法:
    from pcb_factor_init import PCB_FACTOR_INIT, PCB_5CAT_MAP
    # 读取PCB赛道初始权重
    pcb_weights = PCB_5CAT_MAP
    # 写入weight_snapshot供后续迭代

更新规则:
    - 每周一08:00 factor_weekly_iterate.py运行后, 回写IC权重至pcb_factor_ic_log.csv
    - IC≥0.03保留, <0.03淘汰写入llm_invalid_factor.csv
    - 120日滑动窗口滚动重训(evolution_engine.py:274-298)
"""

# ── PCB制造赛道13因子IC权重初始化表 ──
# 来源: 300476 胜宏科技完整AI训练案例 (training-case-complete.md §九)
PCB_FACTOR_INIT = {
    # 因子名称: [IC权重, 方向, 说明]
    # 基于3年242天回测IC指标矫正(2026-07-19):
    #   7冲突因子=半权重矫正, 5无冲突高IR因子保持不变
    "pe_forward_growth": [0.1038, 1, "前瞻PE与增速匹配度(正向)"],
    "customer_concentration": [0.1661, -1, "客户集中度折价(负向,英伟达70%依赖)"],
    "gross_margin_trend": [0.1661, 1, "毛利率趋势(季度环比,正向)"],
    "revenue_yoy": [0.0692, 1, "营收同比增速(正向)"],
    "capex_intensity": [0.0554, -1, "资本开支强度(负向,重资产)"],
    "ar_turnover_days": [0.1107, -1, "应收周转天数(负向,95天)"],
    "inventory_turnover": [0.0484, 1, "存货周转(正向)"],
    "raw_material_index": [0.0484, -1, "原材料价格指数铜+CCL(负向)"],
    "overseas_capacity": [0.0450, 1, "海外产能占比(正向,东南亚)"],
    "r_d_intensity": [0.0831, 1, "研发投入强度(正向)"],
    "industry_supply_gap": [0.0692, 1, "行业供需缺口(正向,当前20%)"],
    "asic_diversification": [0.0345, 1, "ASIC客户拓展进度(正向)"],
}

# ── 13因子→5大类映射 (用于factor_weekly_iterate权重迭代) ──
# valuation(0.25) → pe_forward_growth(0.15) + customer_concentration(0.12) = 0.27
# momentum(0.20)  → revenue_yoy(0.10) + industry_supply_gap(0.05) = 0.15
# flow(0.25)      → 沿用通用资金流权重
# fundamental(0.15) → gross_margin_trend(0.12) + capex_intensity(0.08) + 
#                      ar_turnover_days(0.08) + inventory_turnover(0.07) + 
#                      r_d_intensity(0.06) + overseas_capacity(0.06) = 0.47 → 归一化
# sentiment(0.15)  → raw_material_index(0.07) + asic_diversification(0.04) = 0.11

def compute_pcb_5cat_weights():
    """
    将13因子IC权重归一化为5大类别权重（用于factor_weekly_iterate.py）
    
    Returns:
        dict: {sector_name: {valuation, momentum, flow, fundamental, sentiment}}
    """
    # 因子到类别的映射
    cat_map = {
        "valuation": ["pe_forward_growth", "customer_concentration"],
        "momentum": ["revenue_yoy", "industry_supply_gap"],
        "fundamental": ["gross_margin_trend", "capex_intensity", 
                        "ar_turnover_days", "inventory_turnover", 
                        "r_d_intensity", "overseas_capacity"],
        "sentiment": ["raw_material_index", "asic_diversification"],
    }
    # flow单独保留不做映射
    
    raw_cat_weights = {}
    for cat, factors in cat_map.items():
        raw_cat_weights[cat] = sum(PCB_FACTOR_INIT[f][0] for f in factors)
    
    # flow保留默认0.25
    raw_cat_weights["flow"] = 0.25
    
    # 归一化
    total = sum(raw_cat_weights.values())
    normalized = {k: round(v / total, 4) for k, v in raw_cat_weights.items()}
    
    # 确保总和=1.0
    diff = 1.0 - sum(normalized.values())
    if abs(diff) > 0.0001:
        # 补到最大的类别
        max_cat = max(normalized, key=normalized.get)
        normalized[max_cat] = round(normalized[max_cat] + diff, 4)
    
    return {"PCB制造": normalized}


# ── 预计算5类权重（用于直接导入） ──
PCB_5CAT_MAP = compute_pcb_5cat_weights()


def init_pcb_factor_snapshot(snap_dir="/opt/stock_agent/weight_snapshots"):
    """保存PCB赛道初始因子权重快照"""
    import json
    from datetime import date
    from pathlib import Path
    
    snap_dir = Path(snap_dir)
    snap_dir.mkdir(exist_ok=True)
    
    fp = snap_dir / f"pcb_factor_init_{date.today().isoformat()}.json"
    data = {
        "sector": "PCB制造",
        "stock_code": "300476",
        "description": "300476胜宏科技-PCB赛道13因子IC权重初始化",
        "timestamp": date.today().isoformat(),
        "factor_ic_weights_original": {k: v[0] for k, v in PCB_FACTOR_INIT.items()},
        "factor_direction": {k: v[1] for k, v in PCB_FACTOR_INIT.items()},
        "factor_descriptions": {k: v[2] for k, v in PCB_FACTOR_INIT.items()},
        "mapped_5cat_weights": PCB_5CAT_MAP,
        "total_ic_weight": sum(v[0] for v in PCB_FACTOR_INIT.values()),
    }
    
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    return str(fp)


def print_pcb_factor_summary():
    """打印PCB因子IC权重摘要"""
    print("=" * 65)
    print(f"{'PCB制造赛道 — 13因子IC权重初始化表':^63}")
    print("=" * 65)
    print(f"{'因子名称':<24} {'IC权重':>8} {'方向':>4} {'说明'}")
    print("-" * 65)
    for name, (weight, direction, desc) in sorted(
        PCB_FACTOR_INIT.items(), key=lambda x: -x[1][0]
    ):
        arrow = "↑" if direction == 1 else "↓"
        print(f"{name:<24} {weight:>8.2f} {arrow:>4} {desc}")
    print("-" * 65)
    print(f"{'合计':<24} {sum(v[0] for v in PCB_FACTOR_INIT.values()):>8.2f}")
    print()
    print("映射为5大类权重:")
    for sector, w in PCB_5CAT_MAP.items():
        for cat, weight in w.items():
            print(f"  {cat}: {weight:.4f}")
    print("=" * 65)
    print(f"13因子IC权重总计={sum(v[0] for v in PCB_FACTOR_INIT.values()):.2f} ✅")


if __name__ == "__main__":
    print_pcb_factor_summary()
    fp = init_pcb_factor_snapshot()
    print(f"\n快照已保存: {fp}")
