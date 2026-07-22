#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simulation_stress.py — 300476 胜宏科技 极端冲突场景批量压力测试
=============================================================
用途: 8大极端冲突场景因子评分 + 多层风控叠加压测
依赖: pcb_factor_init.py (因子权重), layered_risk_control.py (风控逻辑)
输出: /opt/stock_agent/reports/simulation_stress_YYYY-MM-DD.json
       /opt/stock_agent/weight_snapshots/stress_scenario_YYYY-MM-DD.json
"""

import json
import sys
import os
from datetime import date, datetime
from pathlib import Path

# ── 路径 ──
BASE_DIR = Path("/opt/stock_agent")
REPORT_DIR = BASE_DIR / "reports"
SNAP_DIR = BASE_DIR / "weight_snapshots"
REPORT_DIR.mkdir(exist_ok=True)
SNAP_DIR.mkdir(exist_ok=True)

# ── 导入PCB因子权重 ──
sys.path.insert(0, str(BASE_DIR))
try:
    from pcb_factor_init import PCB_FACTOR_INIT, PCB_5CAT_MAP
    print("✅ 已加载 pcb_factor_init.py — 13因子IC权重表")
except ImportError as e:
    print(f"❌ 导入 pcb_factor_init.py 失败: {e}")
    sys.exit(1)

# 因子列表 (按IC权重降序)
FACTORS = sorted(PCB_FACTOR_INIT.keys(), key=lambda k: -PCB_FACTOR_INIT[k][0])
FACTOR_WEIGHTS = {k: v[0] for k, v in PCB_FACTOR_INIT.items()}
FACTOR_DIR = {k: v[1] for k, v in PCB_FACTOR_INIT.items()}
FACTOR_DESC = {k: v[2] for k, v in PCB_FACTOR_INIT.items()}

# ── 风控配置（与layered_risk_control.py §2 一致）──
RISK_CFG = {
    "single_stock_max_pos": 0.12,
    "single_industry_max_pos": 0.30,
    "account_total_max_pos": 0.75,
    "pos_high": 0.25,
    "pos_medium": 0.12,
    "pos_low": 0.03,
    "pos_zero": 0.0,
    "base_score_min": 0.40,
    "trend_score_min": 0.35,
    "dynamic_coeff_min": 0.35,
    "pe_normal": 57.0,        # 300476 正常前瞻PE
    "pe_risk_multiplier": 1.3, # PE×1.3 警戒线
}

# ====================================================================
# §1 极端冲突场景定义 (8 scenarios)
# ====================================================================
SCENARIOS = {
    "SC01": {
        "name": "算力资本开支骤降30%+英伟达砍单+铜价暴涨20%",
        "type": "bear",
        "pe_impact_multiplier": 1.8,  # PE从57x飙升
        "description": "AI算力周期下行: 北美CSP资本开支削减30%, 英伟达砍单20%, LME铜价暴涨20%压缩毛利率",
        "factor_scores": {
            "pe_forward_growth": 0.10,
            "customer_concentration": 0.05,
            "gross_margin_trend": 0.15,
            "revenue_yoy": 0.10,
            "capex_intensity": 0.60,
            "ar_turnover_days": 0.25,
            "inventory_turnover": 0.20,
            "raw_material_index": 0.10,
            "overseas_capacity": 0.50,
            "r_d_intensity": 0.50,
            "industry_supply_gap": 0.15,
            "asic_diversification": 0.10,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": True,
            "factor_base_below_min": True,
            "trend_below_min": True,
            "liquidity_risk": True,
        }
    },
    "SC02": {
        "name": "海外基地因地缘停产+订单转移+汇率损失",
        "type": "bear",
        "pe_impact_multiplier": 1.3,
        "description": "东南亚地缘冲突: 泰国/越南PCB工厂停产3个月, 订单加速转移至日韩台, 人民币升值5%造成汇兑损失",
        "factor_scores": {
            "pe_forward_growth": 0.25,
            "customer_concentration": 0.40,
            "gross_margin_trend": 0.15,
            "revenue_yoy": 0.20,
            "capex_intensity": 0.30,
            "ar_turnover_days": 0.30,
            "inventory_turnover": 0.25,
            "raw_material_index": 0.40,
            "overseas_capacity": 0.10,
            "r_d_intensity": 0.50,
            "industry_supply_gap": 0.40,
            "asic_diversification": 0.40,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": True,
            "factor_base_below_min": True,
            "trend_below_min": True,
            "liquidity_risk": True,
        }
    },
    "SC03": {
        "name": "ASIC客户大幅放量+毛利率修复+估值修复",
        "type": "bull",
        "pe_impact_multiplier": 0.7,
        "description": "ASIC定制芯片爆发: 谷歌TPU/亚马逊Trainium PCB订单翻倍, 产品结构优化毛利率恢复至32%+, PE中枢修复",
        "factor_scores": {
            "pe_forward_growth": 0.85,
            "customer_concentration": 0.75,
            "gross_margin_trend": 0.85,
            "revenue_yoy": 0.80,
            "capex_intensity": 0.50,
            "ar_turnover_days": 0.60,
            "inventory_turnover": 0.65,
            "raw_material_index": 0.50,
            "overseas_capacity": 0.55,
            "r_d_intensity": 0.60,
            "industry_supply_gap": 0.70,
            "asic_diversification": 0.90,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": False,
            "factor_base_below_min": False,
            "trend_below_min": False,
            "liquidity_risk": False,
        }
    },
    "SC04": {
        "name": "全行业产能过剩+价格战+毛利跌破25%",
        "type": "bear",
        "pe_impact_multiplier": 1.5,
        "description": "PCB行业周期下行: 2027年全行业产能过剩30%, 价格战白热化, 胜宏毛利率从30%+暴跌至22%以下",
        "factor_scores": {
            "pe_forward_growth": 0.15,
            "customer_concentration": 0.40,
            "gross_margin_trend": 0.05,
            "revenue_yoy": 0.15,
            "capex_intensity": 0.20,
            "ar_turnover_days": 0.20,
            "inventory_turnover": 0.10,
            "raw_material_index": 0.40,
            "overseas_capacity": 0.30,
            "r_d_intensity": 0.40,
            "industry_supply_gap": 0.05,
            "asic_diversification": 0.35,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": True,
            "factor_base_below_min": True,
            "trend_below_min": True,
            "liquidity_risk": True,
        }
    },
    "SC05": {
        "name": "英伟达Rubin平台超预期+单机PCB价值量翻倍",
        "type": "bull",
        "pe_impact_multiplier": 0.65,
        "description": "英伟达Rubin架构GPU PCB层数增至24层+Ultra Ethernet交换机PCB需求爆发, 单机PCB价值量从$3000→$6000+",
        "factor_scores": {
            "pe_forward_growth": 0.80,
            "customer_concentration": 0.60,
            "gross_margin_trend": 0.80,
            "revenue_yoy": 0.90,
            "capex_intensity": 0.40,
            "ar_turnover_days": 0.55,
            "inventory_turnover": 0.60,
            "raw_material_index": 0.45,
            "overseas_capacity": 0.55,
            "r_d_intensity": 0.65,
            "industry_supply_gap": 0.80,
            "asic_diversification": 0.60,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": False,
            "factor_base_below_min": False,
            "trend_below_min": False,
            "liquidity_risk": False,
        }
    },
    "SC06": {
        "name": "大股东减持+北向资金单月流出超5亿",
        "type": "bear",
        "pe_impact_multiplier": 1.1,
        "description": "资金面恶化: 控股股东公告减持3%, 北向资金单月净流出5.2亿元, 融资余额下降15%",
        "factor_scores": {
            "pe_forward_growth": 0.35,
            "customer_concentration": 0.45,
            "gross_margin_trend": 0.45,
            "revenue_yoy": 0.40,
            "capex_intensity": 0.45,
            "ar_turnover_days": 0.45,
            "inventory_turnover": 0.45,
            "raw_material_index": 0.45,
            "overseas_capacity": 0.45,
            "r_d_intensity": 0.45,
            "industry_supply_gap": 0.40,
            "asic_diversification": 0.40,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": False,
            "factor_base_below_min": True,
            "trend_below_min": True,
            "liquidity_risk": True,
        }
    },
    "SC07": {
        "name": "中报净利增速<20%+商誉减值预警",
        "type": "bear",
        "pe_impact_multiplier": 1.4,
        "description": "基本面恶化: 2026年中报净利润增速仅12%(预期25%), 子公司商誉减值测试预警, 应收账款周转恶化",
        "factor_scores": {
            "pe_forward_growth": 0.20,
            "customer_concentration": 0.45,
            "gross_margin_trend": 0.30,
            "revenue_yoy": 0.25,
            "capex_intensity": 0.35,
            "ar_turnover_days": 0.30,
            "inventory_turnover": 0.30,
            "raw_material_index": 0.40,
            "overseas_capacity": 0.40,
            "r_d_intensity": 0.40,
            "industry_supply_gap": 0.35,
            "asic_diversification": 0.35,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": True,
            "factor_base_below_min": True,
            "trend_below_min": True,
            "liquidity_risk": True,
        }
    },
    "SC08": {
        "name": "AI算力泡沫破裂+全板块暴跌30%+",
        "type": "bear",
        "pe_impact_multiplier": 2.0,
        "description": "系统性风险: AI算力叙事崩塌, PCB板块指数一个月暴跌32%, 胜宏科技作为高贝塔标的跌幅超40%, 流动性枯竭",
        "factor_scores": {
            "pe_forward_growth": 0.05,
            "customer_concentration": 0.05,
            "gross_margin_trend": 0.10,
            "revenue_yoy": 0.05,
            "capex_intensity": 0.40,
            "ar_turnover_days": 0.10,
            "inventory_turnover": 0.10,
            "raw_material_index": 0.25,
            "overseas_capacity": 0.30,
            "r_d_intensity": 0.30,
            "industry_supply_gap": 0.05,
            "asic_diversification": 0.10,
        },
        "risk_triggers": {
            "pe_exceed_1_3x": True,
            "factor_base_below_min": True,
            "trend_below_min": True,
            "liquidity_risk": True,
        }
    },
}

# ====================================================================
# §2 核心计算
# ====================================================================

def compute_factor_scores(scenario_data):
    """计算因子评分明细: 每个因子的加权贡献"""
    scores = scenario_data["factor_scores"]
    details = {}
    weighted_sum = 0.0
    for f in FACTORS:
        w = FACTOR_WEIGHTS[f]
        s = scores.get(f, 0.5)
        # 方向调整: direction=1(正向因子) 得分越高越好,
        #            direction=-1(负向因子) 得分越低越好 → 反转
        adj = FACTOR_DIR[f]
        if adj == -1:
            s = 1.0 - s  # 反转: 低原始分→高分(因为低客户集中度等是好的)
        contribution = round(w * s, 4)
        details[f] = {
            "weight": w,
            "direction": adj,
            "raw_score": scores.get(f, 0.5),
            "adjusted_score": round(s, 4),
            "contribution": contribution,
        }
        weighted_sum += contribution
    return details, round(weighted_sum, 4)


def map_score_to_position(weighted_score):
    """加权总分 → 匹配仓位 (与layered_risk_control §5一致)"""
    if weighted_score >= 0.70:
        return RISK_CFG["pos_high"], "25% (高分满仓)"
    elif weighted_score >= 0.55:
        return RISK_CFG["pos_medium"], "12% (中等仓位)"
    elif weighted_score >= RISK_CFG["base_score_min"]:
        return RISK_CFG["pos_low"], "3% (轻仓试探)"
    else:
        return RISK_CFG["pos_zero"], "0% (禁止开仓)"


def apply_risk_control(scenario_id, scenario_data, weighted_score, base_position):
    """
    应用多层风控逻辑 (layered_risk_control.py §2~§5 逻辑内联复制)
    返回: (final_allowed, final_position, risk_logs, stack_reduction_plan)
    """
    triggers = scenario_data["risk_triggers"]
    pe_mult = scenario_data["pe_impact_multiplier"]
    logs = []
    reduction_steps = []

    # 原始仓位
    pos = base_position
    original_pos = pos

    # ── 条件1: 因子基础分检查 (base_score_min ≥ 0.4) ──
    condition1_met = weighted_score >= RISK_CFG["base_score_min"]
    logs.append((
        "entry_cond_1",
        f"加权总分 {weighted_score:.2f} ≥ {RISK_CFG['base_score_min']}",
        "✅ 通过" if condition1_met else "❌ 不满足"
    ))

    # ── 条件2: 趋势分检查 (trend_score_min ≥ 0.35) ──
    # 用pe_forward_growth + revenue_yoy + gross_margin_trend 模拟趋势分
    fs = scenario_data["factor_scores"]
    trend_raw = (fs.get("pe_forward_growth", 0.5) * 0.4 +
                 fs.get("revenue_yoy", 0.5) * 0.3 +
                 fs.get("gross_margin_trend", 0.5) * 0.3)
    condition2_met = trend_raw >= RISK_CFG["trend_score_min"]
    logs.append((
        "entry_cond_2",
        f"趋势综合分 {trend_raw:.2f} ≥ {RISK_CFG['trend_score_min']}",
        "✅ 通过" if condition2_met else "❌ 不满足"
    ))

    # ── 条件3: 动态风控系数 (dynamic_coeff_min ≥ 0.35) ──
    # 用raw_material_index + overseas_capacity + asic_diversification 模拟
    dynamic_raw = (fs.get("raw_material_index", 0.5) * 0.3 +
                   fs.get("overseas_capacity", 0.5) * 0.3 +
                   fs.get("asic_diversification", 0.5) * 0.4)
    condition3_met = dynamic_raw >= RISK_CFG["dynamic_coeff_min"]
    logs.append((
        "entry_cond_3",
        f"动态系数分 {dynamic_raw:.2f} ≥ {RISK_CFG['dynamic_coeff_min']}",
        "✅ 通过" if condition3_met else "❌ 不满足"
    ))

    # ── 准入判断: 3条件必须全部满足，否则禁止开仓 ──
    all_conditions_met = condition1_met and condition2_met and condition3_met
    if not all_conditions_met:
        logs.append(("entry_final", "3项准入条件未全部满足", "❌ 禁止开仓"))
        return False, 0.0, logs, [{"layer": "准入条件", "trigger": "3项准入条件未满足", "reduction": "100%", "remaining_position": "0%"}]

    # ── PE×1.3 风险检查 ──
    pe_normal = RISK_CFG["pe_normal"]
    pe_risk_threshold = pe_normal * RISK_CFG["pe_risk_multiplier"]
    pe_implied = pe_normal * pe_mult
    if pe_implied > pe_risk_threshold:
        pe_reduction = 0.20  # PE超限减仓20%
        pos *= (1 - pe_reduction)
        reduction_steps.append({
            "layer": "PE×1.3 超限",
            "trigger": f"隐含PE {pe_implied:.0f}x > 警戒线 {pe_risk_threshold:.0f}x (57×1.3)",
            "reduction": f"{pe_reduction*100:.0f}%",
            "remaining_position": f"{pos:.2%}"
        })
        logs.append(("pe_risk", f"PE={pe_implied:.0f}x > {pe_risk_threshold:.0f}x警戒线, 减仓{pe_reduction*100:.0f}%",
                     f"⚡ 触发" if pe_implied > pe_risk_threshold else "✅ 正常"))
    else:
        logs.append(("pe_risk", f"PE={pe_implied:.0f}x ≤ 警戒线{pe_risk_threshold:.0f}x", "✅ 正常"))

    # ── 静态硬约束: 单票12%上限 ──
    if pos > RISK_CFG["single_stock_max_pos"]:
        old_pos = pos
        pos = RISK_CFG["single_stock_max_pos"]
        reduction_steps.append({
            "layer": "单票上限12%",
            "trigger": f"仓位{old_pos:.2%} > 12%",
            "reduction": f"{(old_pos - pos)/old_pos*100:.0f}%",
            "remaining_position": f"{pos:.2%}"
        })
        logs.append(("static_stock", f"单票上限12%, 从{old_pos:.2%}→{pos:.2%}", "✅ 已适配"))

    # ── 静态硬约束: 行业30%上限 (假设同行业仅此一票) ──
    if pos > RISK_CFG["single_industry_max_pos"]:
        pos = RISK_CFG["single_industry_max_pos"]
        reduction_steps.append({
            "layer": "行业上限30%",
            "trigger": f"仓位{pos:.2%} > 30%",
            "reduction": "已适配",
            "remaining_position": f"{pos:.2%}"
        })

    # ── 静态硬约束: 总仓75%上限 ──
    if pos > RISK_CFG["account_total_max_pos"]:
        pos = RISK_CFG["account_total_max_pos"]
        reduction_steps.append({
            "layer": "总仓上限75%",
            "trigger": f"仓位{pos:.2%} > 75%",
            "reduction": "已适配",
            "remaining_position": f"{pos:.2%}"
        })

    # ── 流动性风险叠加 (triggers) ──
    if triggers.get("liquidity_risk", False):
        liq_reduction = 0.15
        pos *= (1 - liq_reduction)
        reduction_steps.append({
            "layer": "流动性风险",
            "trigger": "场景触发流动性枯竭假设",
            "reduction": f"{liq_reduction*100:.0f}%",
            "remaining_position": f"{pos:.2%}"
        })
        logs.append(("liquidity", f"流动性风险减仓{liq_reduction*100:.0f}%", "⚡ 触发"))

    # ── 因子基础分风险叠加 ──
    if triggers.get("factor_base_below_min", False) and weighted_score < 0.5:
        base_reduction = 0.15
        pos *= (1 - base_reduction)
        reduction_steps.append({
            "layer": "因子基础分薄弱",
            "trigger": f"加权总分{weighted_score:.2f} < 0.5",
            "reduction": f"{base_reduction*100:.0f}%",
            "remaining_position": f"{pos:.2%}"
        })
        logs.append(("factor_base", f"因子基础分薄弱减仓{base_reduction*100:.0f}%", "⚡ 触发"))

    # ── 趋势风险叠加 ──
    if triggers.get("trend_below_min", False) and trend_raw < 0.5:
        trend_reduction = 0.15
        pos *= (1 - trend_reduction)
        reduction_steps.append({
            "layer": "趋势偏弱",
            "trigger": f"趋势综合分{trend_raw:.2f} < 0.5",
            "reduction": f"{trend_reduction*100:.0f}%",
            "remaining_position": f"{pos:.2%}"
        })
        logs.append(("trend_risk", f"趋势偏弱减仓{trend_reduction*100:.0f}%", "⚡ 触发"))

    # ── 最终仓位映射 ──
    final_pos = round(pos, 4)
    if final_pos >= RISK_CFG["pos_high"]:
        mapped_pos = RISK_CFG["pos_high"]
        pos_label = "25% (高分满仓)"
    elif final_pos >= RISK_CFG["pos_medium"]:
        mapped_pos = RISK_CFG["pos_medium"]
        pos_label = "12% (中等仓位)"
    elif final_pos >= RISK_CFG["pos_low"]:
        mapped_pos = RISK_CFG["pos_low"]
        pos_label = "3% (轻仓试探)"
    else:
        mapped_pos = RISK_CFG["pos_zero"]
        pos_label = "0% (禁止开仓)"

    reduction_steps.append({
        "layer": "仓位等级映射",
        "trigger": f"计算值{final_pos:.2%}",
        "reduction": f"→ {pos_label}",
        "remaining_position": f"{mapped_pos:.0%}"
    })

    final_allowed = mapped_pos > 0
    logs.append(("final",
                 f"原始{original_pos:.0%} → 风控后{mapped_pos:.0%} ({pos_label})",
                 "✅ 允许开仓" if final_allowed else "❌ 禁止开仓"))

    return final_allowed, mapped_pos, logs, reduction_steps


def generate_bull_bear_logic(scenario_id, scenario_data, weighted_score, final_allowed, final_position):
    """生成多空逻辑判断列表"""
    logic = []
    fs = scenario_data["factor_scores"]

    # 筛选最优/最差因子
    sorted_factors = sorted(fs.items(), key=lambda x: x[1])
    worst = sorted_factors[:3]
    best = sorted_factors[-3:]

    sc_type = scenario_data["type"]

    if sc_type == "bull" and final_allowed and weighted_score >= 0.6:
        logic.append(("趋势判断", "🟢 强烈看多", f"加权总分{weighted_score:.2f}, 多因子共振上行"))
        for name, score in reversed(best):
            logic.append(("核心驱动", f"↑ {name}", f"得分{score:.2f}: {FACTOR_DESC.get(name,'')}"))
        for name, score in worst:
            if score < 0.4:
                logic.append(("风险关注", f"⚠ {name}", f"得分{score:.2f}, 后续需改善"))
    elif sc_type == "bull" and final_allowed:
        logic.append(("趋势判断", "🟡 谨慎看多", f"加权总分{weighted_score:.2f}, 需观察持续性"))
        for name, score in reversed(best):
            logic.append(("潜在驱动", f"↑ {name}", f"得分{score:.2f}"))
    elif sc_type == "bear" and not final_allowed:
        logic.append(("趋势判断", "🔴 强烈看空", f"加权总分{weighted_score:.2f}, 风控禁止开仓"))
        for name, score in worst:
            logic.append(("核心风险", f"↓ {name}", f"得分{score:.2f}: {FACTOR_DESC.get(name,'')}"))
    elif sc_type == "bear" and final_allowed:
        logic.append(("趋势判断", "🟠 偏空防御", f"加权总分{weighted_score:.2f}, 轻仓或观望"))
        for name, score in worst:
            logic.append(("风险提示", f"↓ {name}", f"得分{score:.2f}"))
    else:
        logic.append(("趋势判断", "⚪ 中性/观望", f"加权总分{weighted_score:.2f}, 条件不明确"))

    # 综合判断
    logic.append(("综合建议", f"目标仓位: {final_position:.0%}",
                  "执行" if final_allowed else "放弃开仓"))
    return logic


def run_scenario(scenario_id):
    """运行单个压力测试场景"""
    sc = SCENARIOS[scenario_id]

    # 1. 计算因子评分
    details, weighted_score = compute_factor_scores(sc)

    # 2. 映射初始仓位
    base_pos, pos_label = map_score_to_position(weighted_score)

    # 3. 应用风控
    final_allowed, final_pos, risk_logs, reduction_steps = apply_risk_control(
        scenario_id, sc, weighted_score, base_pos
    )

    # 4. 多空逻辑
    bull_bear = generate_bull_bear_logic(
        scenario_id, sc, weighted_score, final_allowed, final_pos
    )

    # 5. 场景汇总
    result = {
        "scenario_id": scenario_id,
        "scenario_name": sc["name"],
        "scenario_type": sc["type"],
        "description": sc["description"],
        "pe_impact_multiplier": sc["pe_impact_multiplier"],
        "factor_scores_detail": details,
        "weighted_total_score": weighted_score,
        "base_position_label": pos_label,
        "base_position_pct": base_pos,
        "risk_control_allowed": final_allowed,
        "final_position_pct": final_pos,
        "entry_conditions": risk_logs[:4],  # 3 entry + 1 final
        "risk_logs": risk_logs[4:],
        "multi_risk_stacked_reduction": reduction_steps,
        "bull_bear_logic": bull_bear,
    }
    return result


def print_table(results):
    """打印所有场景结果表格"""
    sep = "=" * 140
    print("\n" + sep)
    print(f"{'300476 胜宏科技 — 极端冲突场景压力测试报告':^138}")
    ts = f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
    print(f"{ts:^138}")
    print(sep)

    # ── 总览表 ──
    header = f"{'场景':<6} {'类型':<6} {'场景名称':<38} {'总分':<8} {'初始仓位':<10} {'风控通过':<10} {'最终仓位':<10} {'判定':<12}"
    print("\n📊 场景总览")
    print("-" * 140)
    print(header)
    print("-" * 140)
    for rid in sorted(results.keys()):
        r = results[rid]
        verdict = "🟢 做多" if r["risk_control_allowed"] and r["scenario_type"] == "bull" else \
                  "🔴 看空" if not r["risk_control_allowed"] else \
                  "🟠 防御"
        print(f"{rid:<6} {r['scenario_type']:<6} {r['scenario_name']:<38} "
              f"{r['weighted_total_score']:<8.4f} "
              f"{r['base_position_label']:<10} "
              f"{'✅' if r['risk_control_allowed'] else '❌':<10} "
              f"{r['final_position_pct']:.0%}       "
              f"{verdict:<12}")
    print("-" * 140)

    # ── 详细结果 ──
    for rid in sorted(results.keys()):
        r = results[rid]
        print(f"\n{'─' * 140}")
        print(f"📋 {rid} | {r['scenario_name']} | {'🟢 BULL' if r['scenario_type']=='bull' else '🔴 BEAR'}")
        print(f"   描述: {r['description']}")
        print(f"   PE乘数: {r['pe_impact_multiplier']}x (隐含PE: {57*r['pe_impact_multiplier']:.0f}x)")
        print(f"   {'─' * 60}")

        # 因子评分表
        print(f"\n   📈 因子评分明细 (权重×调整分):")
        print(f"   {'因子名称':<24} {'权重':<8} {'方向':<6} {'原始分':<8} {'调整分':<10} {'贡献':<8}")
        print(f"   {'─' * 64}")
        for f in FACTORS:
            d = r["factor_scores_detail"][f]
            arrow = "↑" if d["direction"] == 1 else "↓"
            print(f"   {f:<24} {d['weight']:<8.2f} {arrow:<6} {d['raw_score']:<8.2f} "
                  f"{d['adjusted_score']:<10.4f} {d['contribution']:<8.4f}")
        print(f"   {'─' * 64}")
        print(f"   {'加权总分':>53} {r['weighted_total_score']:<8.4f}")
        print(f"   {'初始匹配仓位':>53} {r['base_position_label']:<20}")

        # 准入条件
        print(f"\n   🚦 准入条件 (三项必须全部满足):")
        for ec in r["entry_conditions"]:
            print(f"     {ec[0]}: {ec[1]:<55} {ec[2]}")

        # 多风险叠加减仓
        print(f"\n   📉 多风险叠加减仓计划:")
        print(f"   {'#':<4} {'风险层':<22} {'触发条件':<40} {'减仓比例':<12} {'剩余仓位':<12}")
        print(f"   {'─' * 88}")
        for i, step in enumerate(r["multi_risk_stacked_reduction"], 1):
            reduction = step.get("reduction", "-")
            remaining = step.get("remaining_position", "-")
            print(f"   {i:<4} {step['layer']:<22} {step['trigger']:<40} {reduction:<12} {remaining:<12}")

        # 多空逻辑
        print(f"\n   🎯 多空逻辑判断:")
        for item in r["bull_bear_logic"]:
            print(f"     [{item[0]:<12}] {item[1]:<30} | {item[2]}")

        print(f"\n   🔒 最终判定: {'✅ 允许开仓' if r['risk_control_allowed'] else '❌ 禁止开仓'}, "
              f"仓位: {r['final_position_pct']:.0%}")

    print(f"\n{sep}")
    print(f"{'报告结束':^138}")
    print(f"{sep}")


def export_results(results, today_str):
    """导出完整报告JSON和快照"""
    report = {
        "report_type": "极端冲突场景压力测试",
        "stock": "300476 胜宏科技",
        "generated_at": datetime.now().isoformat(),
        "report_date": today_str,
        "factor_weight_source": "pcb_factor_init.py (13因子IC权重)",
        "risk_control_source": "layered_risk_control.py §2~§5 (内联复制)",
        "total_scenarios": len(results),
        "scenarios": results,
        "summary": {
            "bullish_count": sum(1 for r in results.values() if r["scenario_type"] == "bull"),
            "bearish_count": sum(1 for r in results.values() if r["scenario_type"] == "bear"),
            "allowed_count": sum(1 for r in results.values() if r["risk_control_allowed"]),
            "blocked_count": sum(1 for r in results.values() if not r["risk_control_allowed"]),
            "avg_weighted_score": round(
                sum(r["weighted_total_score"] for r in results.values()) / len(results), 4
            ),
        }
    }

    # 报告
    report_path = REPORT_DIR / f"simulation_stress_{today_str}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 报告已保存: {report_path}")

    # 快照
    snapshot = {
        "snapshot_type": "stress_scenario_snapshot",
        "stock": "300476 胜宏科技",
        "generated_at": datetime.now().isoformat(),
        "snapshot_date": today_str,
        "factor_ic_weights": FACTOR_WEIGHTS,
        "factor_directions": FACTOR_DIR,
        "mapped_5cat_weights": PCB_5CAT_MAP,
        "scenario_results": {
            rid: {
                "weighted_total_score": r["weighted_total_score"],
                "final_position_pct": r["final_position_pct"],
                "risk_allowed": r["risk_control_allowed"],
                "scenario_type": r["scenario_type"],
            }
            for rid, r in results.items()
        }
    }
    snap_path = SNAP_DIR / f"stress_scenario_{today_str}.json"
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"✅ 快照已保存: {snap_path}")

    return report_path, snap_path


# ====================================================================
# §3 主入口
# ====================================================================
def main():
    today_str = date.today().isoformat()
    print(f"\n🚀 300476 胜宏科技 — 极端冲突场景压力测试开始 [{today_str}]")
    print(f"   因子基础: {len(FACTORS)}个PCB赛道因子, IC权重合计={sum(FACTOR_WEIGHTS.values()):.2f}")
    print(f"   场景数量: {len(SCENARIOS)}个\n")

    results = {}
    for sid in sorted(SCENARIOS.keys()):
        print(f"▶ 运行 {sid}: {SCENARIOS[sid]['name']} ... ", end="", flush=True)
        results[sid] = run_scenario(sid)
        print(f"总分={results[sid]['weighted_total_score']:.4f}, "
              f"风控={'✅' if results[sid]['risk_control_allowed'] else '❌'}, "
              f"仓位={results[sid]['final_position_pct']:.0%}")

    # 打印表格
    print_table(results)

    # 导出
    report_path, snap_path = export_results(results, today_str)

    print(f"\n{'=' * 60}")
    print(f"  测试完成!")
    print(f"  报告: {report_path}")
    print(f"  快照: {snap_path}")
    print(f"{'=' * 60}")

    return results


if __name__ == "__main__":
    main()
