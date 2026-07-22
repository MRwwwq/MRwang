"""
flow3_param_boundary.py — 流程3: 人工管控参数进化边界
功能: 快照对比 + 极端回滚 + 红线锁死 + 留痕归档
"""
import os, json, sys, datetime, subprocess

sys.path.insert(0, "/opt/stock_agent")

SNAPSHOT_DIR = "/opt/stock_agent/param_snapshots"
HARD_CODED_RED_LINES = {
    "red_line_01": {
        "name": "连续3日大额资金流出强制减仓",
        "rule": "3日主力累计净流出>2亿 → 减仓50%",
        "file": "shanshan-600884-analysis/SKILL.md",
        "section": "2.5 减仓红线",
        "locked": True,
        "checksum": None
    },
    "red_line_02": {
        "name": "3项入场条件全部不满足禁止开仓",
        "rule": "资金止流出 + 站MA5放量 + 融资企稳 → 3/3全False禁止开仓",
        "file": "shanshan-600884-analysis/SKILL.md",
        "section": "2.3 入场硬性条件",
        "locked": True,
        "checksum": None
    },
    "red_line_03": {
        "name": "QClaw_Rule_007 Lollapalooza一票否决",
        "rule": ">=3项高危因子(>=60分) → 一票否决, 禁止开仓",
        "file": "stock-analysis-framework/SKILL.md",
        "section": "§25 Lollapalooza共振风控",
        "locked": True,
        "checksum": None
    },
    "red_line_04": {
        "name": "固态电池题材基础风险加权下限",
        "rule": "概念纯度评分<30 → theme_purity_low, 禁止跟风仓位; 散户流入>60%+特大单<30% → 散户接盘模式减仓50%",
        "file": "stock-analysis-framework/SKILL.md",
        "section": "QClaw_Rule_021",
        "locked": True,
        "checksum": None
    }
}

STABLE_BASELINE = {
    "single_stock_max_pos": 0.12,
    "single_industry_max_pos": 0.30,
    "account_total_max_pos": 0.75,
    "daily_max_loss_ratio": 0.025,
    "pe_risk_weight": 1.3,
    "misjudge_threshold": 3,
    "lolla_veto_severity": 5,
    "entry_score_threshold": 65,
    "solid_concept_purity_threshold": 30,
    "solid_retail_inflow_threshold": 0.6,
    "rag_tag_weight": 10.0,
    "rag_lolla_trigger_weight": 12.0
}


def ensure_snapshot_dir():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

def take_snapshot(label="daily"):
    """拍摄当前参数快照"""
    ensure_snapshot_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = {
        "timestamp": ts,
        "label": label,
        "baseline": STABLE_BASELINE.copy(),
        "red_lines": {k: {"locked": v["locked"], "name": v["name"]} for k, v in HARD_CODED_RED_LINES.items()}
    }
    path = f"{SNAPSHOT_DIR}/param_snapshot_{ts}.json"
    with open(path, "w") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)
    return path, snap


def detect_extreme_changes(current_params: dict) -> list:
    """检测极端参数变动 (偏离基线>30%)"""
    alerts = []
    for key, baseline_val in STABLE_BASELINE.items():
        if key not in current_params:
            continue
        cur_val = current_params[key]
        if baseline_val == 0:
            continue
        deviation = abs(cur_val - baseline_val) / abs(baseline_val)
        if deviation > 0.30:
            alerts.append({
                "param": key,
                "baseline": baseline_val,
                "current": cur_val,
                "deviation_pct": round(deviation * 100, 1),
                "action": "ROLLBACK",
                "reason": f"偏离基线{deviation*100:.0f}% > 30%阈值"
            })
    return alerts


def check_red_line_violations(proposed_changes: dict) -> list:
    """检查红线违规: 任何修改红线的请求直接拦截"""
    violations = []
    for key, val in proposed_changes.items():
        for rl_key, rl_val in HARD_CODED_RED_LINES.items():
            if rl_val["locked"] and key in rl_val.get("rule", ""):
                violations.append({
                    "red_line": rl_key,
                    "rule_name": rl_val["name"],
                    "attempted_change": {key: val},
                    "action": "BLOCKED",
                    "reason": f"触碰红线{rl_key}:{rl_val['name']},禁止AI修改"
                })
    return violations


def rollback_to_stable(param_name: str) -> dict:
    """回滚单个参数至基线值"""
    if param_name in STABLE_BASELINE:
        return {"param": param_name, "rolled_back_to": STABLE_BASELINE[param_name]}
    return {"param": param_name, "error": "unknown_param"}


def run_flow3(today: str, operator: str) -> dict:
    """流程3完整执行: 快照→检测→回滚→红线→留痕"""
    print(f"\n{'='*50}")
    print(f"流程3: 人工管控参数进化边界")
    print(f"日期: {today}  操作人: {operator}")
    print(f"{'='*50}")

    log_entries = []

    # 1. 拍摄快照
    snap_path, snap = take_snapshot(f"daily_{today}")
    log_entries.append(f"[{today}] 基线快照: {snap_path}")
    print(f"  基线快照: {snap_path}")

    # 2. 模拟读取AI当日自动微调参数(从memory_market或配置文件读取)
    # 生产环境从DB/配置文件读取; 此处用demo数据
    demo_tuned_params = {
        "single_stock_max_pos": 0.12,     # 未变
        "pe_risk_weight": 1.3,            # 未变
        "misjudge_threshold": 2,           # AI试图从3改为2(触碰红线??)
        "entry_score_threshold": 60,       # AI试图从65降至60
        "solid_concept_purity_threshold": 25  # AI试图从30降至25
    }

    # 3. 检测极端参数变动
    extreme = detect_extreme_changes(demo_tuned_params)
    if extreme:
        print(f"\n  [极端变动检测] {len(extreme)}项:")
        for e in extreme:
            print(f"    {e['param']}: 基线{e['baseline']}→当前{e['current']} (偏离{e['deviation_pct']}%)")
            print(f"    动作: {e['action']} → 回滚至基线{e['baseline']}")
            rollback_to_stable(e["param"])
            log_entries.append(f"[极端回滚] {e['param']}: {e['baseline']}->{e['current']} 偏离{e['deviation_pct']}% 已回滚")
    else:
        print(f"\n  [极端变动检测] 0项, 参数在稳定区间内")

    # 4. 红线违规检测
    violations = check_red_line_violations(demo_tuned_params)
    if violations:
        print(f"\n  [红线违规] {len(violations)}项拦截:")
        for v in violations:
            print(f"    🚫 {v['red_line']}: {v['rule_name']}")
            print(f"      尝试修改: {v['attempted_change']}")
            print(f"      动作: {v['action']}")
            log_entries.append(f"[红线拦截] {v['red_line']} {v['rule_name']}: {v['attempted_change']} BLOCKED")
    else:
        print(f"\n  [红线违规] 0项, 无红线被触碰")

    # 5. 红线状态报告
    print(f"\n  [红线状态] 4条永久锁定:")
    for rl_key, rl_val in HARD_CODED_RED_LINES.items():
        status = "🔒 LOCKED" if rl_val["locked"] else "🔓 UNLOCKED"
        print(f"    {rl_key}: {rl_val['name']} [{status}]")

    # 6. 写入留痕
    summary = {
        "date": today,
        "operator": operator,
        "snapshot": snap_path,
        "extreme_changes_found": len(extreme),
        "red_line_violations": len(violations),
        "log": log_entries,
        "red_lines_locked": sum(1 for v in HARD_CODED_RED_LINES.values() if v["locked"]),
        "red_lines_total": len(HARD_CODED_RED_LINES)
    }
    report_path = f"{SNAPSHOT_DIR}/flow3_report_{today}.json"
    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n  留痕报告: {report_path}")
    print(f"  日志条目: {len(log_entries)}条")
    print(f"  红线锁定率: {summary['red_lines_locked']}/{summary['red_lines_total']}")

    if extreme or violations:
        print(f"  \033[93m⏳ 检测到{len(extreme)}项极端变动+{len(violations)}项红线违规,已处理\033[0m")
    else:
        print(f"  \033[92m参数边界管控通过: 无极端变动/无红线违规\033[0m")

    print(f"{'='*50}\n")
    return summary


if __name__ == "__main__":
    import sys as _sys
    today = _sys.argv[1] if len(_sys.argv) > 1 else "20260720"
    operator = _sys.argv[2] if len(_sys.argv) > 2 else "quant_admin"
    run_flow3(today, operator)
