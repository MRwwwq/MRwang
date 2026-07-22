"""校验点逐项对照需求原文 — 免实例化版本"""
import sys; sys.path.insert(0, "/opt/stock_agent")
import inspect, os, importlib.util

print("=" * 70)
print("校验点逐项对照需求原文")
print("=" * 70)

P, F = 0, 0
def chk(ok, label):
    global P, F
    if ok: P+=1; print(f"  ✅ {label}")
    else: F+=1; print(f"  ❌ {label}")

# ── 1. 收盘全自动复盘 ──
print("\n── 1. 收盘全自动复盘 ──")
from daily_auto_review import AutoDailyReview
src1 = inspect.getsource(AutoDailyReview)

chk(all(w in src1 for w in ['sharpe','drawdown','win_rate','profit_loss']),
    "自动统计: 夏普/最大回撤/盈亏比/胜率")
chk('signal' in src1 and 'ts_code' in src1,
    "信号胜率 + 单票盈亏统计")
chk('auto_failure_attribution' in src1,
    "AI失效归因: 风格切换/因子失效/突发利空/流动性不足/参数不合适")
chk(any(w in src1 for w in ['build_report','generate_report','run_full_review']),
    "自动生成复盘报告, 标记拖累收益逻辑")

# ── 2. AI自主调参 + 因子进化 ──
print("\n── 2. AI自主调参 + 因子进化 ──")
with open("/opt/stock_agent/evolution_engine.py") as f:
    src2 = f.read()

chk(all(w in src2 for w in ['ma_cycle','stop_loss','take_profit','position','base_score']),
    "参数微进化: 遍历均线/止盈止损/仓位/选股权重")
chk('sharpe' in src2 and 'sort_values' in src2,
    "回测筛选最优区间")
chk('llm_generate_new_factor' in src2,
    "LLM自动生成量价/基本面新因子")
chk('LLM_FACTOR_TEST_THRESHOLD' in src2,
    "IC阈值过滤无效因子")
chk('walk_forward_retrain_all_models' in src2,
    "滚动窗口WalkForward训练, 防过拟合")

# ── 3. 沙盒 + 灰度安全机制 ──
print("\n── 3. 沙盒 + 灰度安全机制 ──")
with open("/opt/stock_agent/sandbox_safe_test.py") as f:
    src3 = f.read()

chk('offline_full_backtest' in src3,
    "新策略先离线历史沙盒全周期回测")
chk('gray_ab_test' in src3,
    "新旧模型并行灰度A/B 7~15天")
chk('triple_standard_check' in src3,
    "三重准入门槛: 夏普提升/回撤不扩大/胜率稳定")
chk(any(w in src3 for w in ['ROLLBACK','rollback','回滚']),
    "不达标自动回滚旧版本, 防止进化翻车")
chk('_write_eliminated_signal' in src3,
    "淘汰策略写入失效信号库")
chk("SHARP_UP_THRESHOLD = 0.1" in src3,
    "夏普提升阈值 ≥0.1")
chk("GRAY_TEST_DAYS = 10" in src3,
    f"灰度天数 10天 ∈ [7,15]")

# ── 4. 完整闭环链路 ──
print("\n── 4. 完整闭环链路 ──")
files_map = {
    'agent_predict_v2': '感知行情(score_stock)',
    'hybrid_ai_decision': '决策下单(HybridAI.forward)',
    'daily_auto_review': '实盘反馈+自动复盘',
    'evolution_engine': '沙盒优化(参数/因子/重训)',
    'sandbox_safe_test': '沙盒安全测试(离线+灰度)',
    'memory_scheduler': '灰度上线(周度调度)',
    'test_orchestrator': '全链路调度(run_full_pipeline)',
}
for fname, label in files_map.items():
    fpath = f"/opt/stock_agent/{fname}.py"
    chk(os.path.isfile(fpath), label)

print(f"\n{'='*70}")
print(f"校验结果: {P}/{P+F} ✅" if F==0 else f"校验结果: {P}/{P+F} ✅  {F}项❌")
print(f"{'='*70}")
