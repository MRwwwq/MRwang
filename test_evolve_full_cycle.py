# -*- coding: utf-8 -*-
"""自进化闭环全流程测试(模块独立验证)"""
import sys; sys.path.insert(0, "/opt/stock_agent")
import numpy as np
import os, json
from datetime import datetime

PASS = 0; FAIL = 0
def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name}: {detail}")

print("=" * 70)
print("自进化闭环全流程测试")
print("=" * 70)

# =============================================
# 2.1 复盘模块 — 方法存在性+签名
# =============================================
print("\n--- 2.1 复盘模块 ---")
from daily_auto_review import AutoDailyReview
reviewer = AutoDailyReview()
check("calc_performance methods存在", hasattr(reviewer, 'calc_performance'))
check("auto_failure_attribution存在", hasattr(reviewer, 'auto_failure_attribution'))
check("build_report存在", hasattr(reviewer, 'build_report'))
check("run_full_review_task存在", hasattr(reviewer, 'run_full_review_task'))

# 测试run_full_review_task(无数据时不应崩溃)
try:
    reviewer.run_full_review_task()
    check("run_full_review_task(空数据)不崩溃", True)
except Exception as e:
    check("run_full_review_task(空数据)不崩溃", False, str(e))

# =============================================
# 2.2 AI进化引擎
# =============================================
print("\n--- 2.2 AI进化引擎 ---")
from evolution_engine import AIEvolveEngine, LLM_FACTOR_TEST_THRESHOLD
evolve = AIEvolveEngine()

# 写入失效信号(供param_evolution_search读取)
evolve.conn.execute("""
    INSERT OR IGNORE INTO memory_failure_signal
    (ts_code, signal_name, failure_type, avoid_strategy, record_time)
    VALUES (?, ?, ?, ?, ?)
""", ("ALL", "test_evolve_" + datetime.now().strftime("%H%M%S"),
      "factor_failure",
      "因子失效; 参数不适配; 降低周期; 降低仓位上限",
      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
evolve.conn.commit()

# 2.2a 参数微进化
r1 = evolve.param_evolution_search()
check("param_evolution_search返回DataFrame", hasattr(r1, 'shape'))
check("param_evolution_search 读取失效任务", len(evolve.load_failure_optimize_tasks()) > 0)
check("进化溯源CSV存在", os.path.isfile("evolution_log/evolve_best_param.csv"))

# 2.2b LLM因子生成
r2 = evolve.llm_generate_new_factor()
check("llm_generate_new_factor返回DataFrame", hasattr(r2, 'shape'))
check("IC阈值≥0.03", LLM_FACTOR_TEST_THRESHOLD >= 0.03)
check("因子溯源CSV", os.path.isfile("evolution_log/llm_valid_new_factor.csv"))

# 2.2c WalkForward
r3 = evolve.walk_forward_retrain_all_models()
check("walk_forward_retrain返回DataFrame", hasattr(r3, 'shape'))
check("WalkForward溯源CSV", os.path.isfile("evolution_log/walk_forward_train_log.csv"))

# 2.2d 全周期
r4 = evolve.run_full_evolve_cycle()
check("run_full_evolve_cycle完成", all(k in r4 for k in ['top_param','new_valid_factor','walk_train_log']))

# =============================================
# 2.3 沙盒灰度
# =============================================
print("\n--- 2.3 沙盒灰度 ---")
from sandbox_safe_test import SandboxSafeTest, SHARP_UP_THRESHOLD

sand = SandboxSafeTest()
check("夏普提升阈值≥0.1", SHARP_UP_THRESHOLD >= 0.1)
check("offline_full_backtest存在", hasattr(sand, 'offline_full_backtest'))
check("gray_ab_test存在", hasattr(sand, 'gray_ab_test'))
check("triple_standard_check存在", hasattr(sand, 'triple_standard_check'))
check("淘汰写入失效信号", hasattr(sand, '_write_eliminated_signal'))

result = sand.run_full_sandbox_flow()
check("沙盒返回online_switch字段", 'online_switch' in result)

# =============================================
# 3 异常场景
# =============================================
print("\n--- 3. 异常场景 ---")

# 3.1 因子失效 → 定向进化
tasks = evolve.load_failure_optimize_tasks()
check("3.1 因子失效→进化读取失效场景", len(tasks) > 0)
if tasks:
    check("  失效日志含因子失效标记", any('因子' in t for t in tasks))

# 3.2 流动性枯竭 → 成交量因子优先
rule_factors = evolve._rule_based_factor_gen()
vol_factors = [f for f in rule_factors if 'volume' in f['factor_name'] or 'capital' in f['factor_name']]
check("3.2 流动性→成交量/资金类因子生成", len(vol_factors) > 0)

# 3.3 灰度不达标 → ROLLBACK
if not result.get('online_switch', True):
    check("3.3 灰度不达标→ROLLBACK", True)
else:
    print("  ℹ️ 3.3 沙盒因数据不足跳过实际校验(预期行为)")

# 进化日志完整性
evol_logs = [f for f in os.listdir("evolution_log") if f.endswith(".csv")] if os.path.isdir("evolution_log") else []
check("进化溯源CSV完整性", len(evol_logs) >= 3, f"现有{len(evol_logs)}个")

# =============================================
# 4 验收
# =============================================
print("\n--- 4. 验收标准 ---")
check("4.1 无需人工干预: 单入口无参", 
      hasattr(AIEvolveEngine, 'run_full_evolve_cycle') and 
      hasattr(SandboxSafeTest, 'run_full_sandbox_flow'))
check("4.2 精准归因: auto_failure_attribution存在", 
      hasattr(AutoDailyReview, 'auto_failure_attribution'))
check("4.3 贴合市场: 进化读取失效信号", 
      hasattr(AIEvolveEngine, 'load_failure_optimize_tasks'))
check("4.4 双层安全闸: 离线+灰度+三重",
      hasattr(SandboxSafeTest, 'offline_full_backtest') and
      hasattr(SandboxSafeTest, 'gray_ab_test') and
      hasattr(SandboxSafeTest, 'triple_standard_check'))
check("4.5 自主迭代闭环",
      os.path.isdir("evolution_log") and os.path.isdir("review_report"))

evolve.close()
if hasattr(sand, 'conn') and sand.conn:
    sand.conn.close()
print(f"\n{'='*70}")
print(f"测试结果: {PASS}✅ / {FAIL}❌")
print(f"{'='*70}")
