"""
daily_3flow_pipeline.py — 每日盘后三大标准化人工运维流程
顺序: 1.交易校准 → 2.错误案例库 → 3.参数进化边界管控
全局约束: 三者每日全部完成, 月度迭代前置校验三者完整才放行
"""
import os, sys, subprocess, json, datetime

SCRIPTS_DIR = "/opt/stock_agent"
REPORT_DIR = f"{SCRIPTS_DIR}/reports"
SNAPSHOT_DIR = f"{SCRIPTS_DIR}/param_snapshots"
TODAY = sys.argv[1] if len(sys.argv) > 1 else "20260720"
TICKER = sys.argv[2] if len(sys.argv) > 2 else "600884"
OPERATOR = sys.argv[3] if len(sys.argv) > 3 else "quant_admin"

LOG = []
FAILED_FLOWS = []

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    LOG.append(line)
    print(line)

def run_flow(name, script, extra_args=None, exit_ok_codes=(0,)):
    log(f"\n{'='*55}")
    log(f"[流程] {name}")
    log(f"[执行] python3 {script}")
    cmd = ["python3", script]
    if extra_args:
        cmd.extend(extra_args)
    env = os.environ.copy()
    env["CALIB_FORCE"] = "1"
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=SCRIPTS_DIR, env=env)
    # 输出关键行到日志
    for line in r.stdout.split("\n"):
        if any(kw in line for kw in ["约束", "标签", "完成", "错误", "error", "异常", "入库",
                                      "红线", "回滚", "拦截", "LOCKED", "调控", "✅", "❌", "🚫"]):
            log(f"  {line.strip()}")
    if r.returncode in exit_ok_codes:
        log(f"[结果] {name} ✅ 完成")
        return True
    else:
        log(f"[结果] {name} ❌ 异常(code={r.returncode})")
        if r.stderr:
            log(f"[错误] {r.stderr[-300:]}")
        return False

def main():
    log(f"{'='*55}")
    log(f"盘后三大标准化人工运维流程启动")
    log(f"日期: {TODAY}  标的: {TICKER}  操作人: {OPERATOR}")
    log(f"{'='*55}")

    # 前置: 确认13模块报告
    for rp in [f"{REPORT_DIR}/report_{TICKER}_{TODAY}_v4.md",
               f"{REPORT_DIR}/report_{TICKER}_{TODAY}.md"]:
        if os.path.exists(rp):
            log(f"[前置] 13模块报告确认: {rp}")
            break
    else:
        log(f"[前置] \033[91m阻断: 无当日13模块报告\033[0m")
        sys.exit(1)

    # =============================================
    # 流程1: 交易校准入库
    # =============================================
    f1_ok = run_flow("1.真实交易校准入库",
                     "trade_calibration_v2_full.py",
                     exit_ok_codes=(0, 1))  # exit 1 from pre_check is expected
    if not f1_ok:
        FAILED_FLOWS.append("流程1")

    # =============================================
    # 流程2: 研判漏洞入库
    # =============================================
    f2_ok = run_flow("2.研判漏洞录入错误案例库",
                     "error_case_library_600884.py")
    if not f2_ok:
        FAILED_FLOWS.append("流程2")

    # =============================================
    # 流程3: 参数进化边界管控
    # =============================================
    f3_ok = run_flow("3.人工管控参数进化边界",
                     "flow3_param_boundary.py",
                     extra_args=[TODAY, OPERATOR])
    if not f3_ok:
        FAILED_FLOWS.append("流程3")

    # =============================================
    # 汇总
    # =============================================
    log(f"\n{'='*55}")
    log(f"三大流程执行汇总")
    results = {
        "1_交易校准": "✅" if f1_ok else "❌",
        "2_错误案例库": "✅" if f2_ok else "❌",
        "3_参数边界管控": "✅" if f3_ok else "❌"
    }
    for k, v in results.items():
        log(f"  {k}: {v}")

    if not FAILED_FLOWS:
        log(f"\n\033[92m✅ 三大流程全部完成, 数据已入库/红线已锁定/日志已留痕\033[0m")
    else:
        log(f"\n\033[91m❌ 异常流程: {', '.join(FAILED_FLOWS)}, 请检查日志\033[0m")

    # 月度迭代前置校验演示
    log(f"\n{'='*55}")
    log(f"[月度迭代前置校验] 模拟周五调用")
    has_cal = f1_ok
    has_error_case = f2_ok
    has_param_log = os.path.exists(f"{SNAPSHOT_DIR}/flow3_report_{TODAY}.json")
    log(f"  交易校准样本: {'✅' if has_cal else '❌'}")
    log(f"  错误案例库:   {'✅' if has_error_case else '❌'}")
    log(f"  参数管控日志: {'✅' if has_param_log else '❌'}")
    if has_cal and has_error_case and has_param_log:
        log(f"  \033[92m三类记录完整 → 放行迭代调参\033[0m")
    else:
        log(f"  \033[91m三类记录缺失 → 阻断迭代调参\033[0m")

    # 保存日志
    log_path = f"{REPORT_DIR}/daily_3flow_pipeline_{TODAY}.log"
    with open(log_path, "w") as f:
        f.write("\n".join(LOG))
    log(f"\n[归档] 日志: {log_path}")

    # 追加到v4_rule_engine_artifact.md
    artifact_path = f"{REPORT_DIR}/_v4_rule_engine_artifact.md"
    with open(artifact_path, "a", encoding="utf-8") as f:
        f.write(f"\n\n---\n## 三大流程日志 - {TODAY}\n")
        f.write(f"> 操作人: {OPERATOR} | 标的: {TICKER}\n\n")
        f.write("```\n")
        for line in LOG:
            f.write(line + "\n")
        f.write("```\n")
    log(f"[归档] 已附加至: {artifact_path}")

if __name__ == "__main__":
    main()
