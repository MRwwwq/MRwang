"""
phase_controller.py — 分阶段实施路线控制器
全程阶梯式落地、禁止跳阶段开发、禁止高阶模块裸上线
每阶段稳定验收后再进入下一阶段
"""
import json
import os
from datetime import datetime, timedelta

STATE_PATH = "/opt/stock_agent/phase_state.json"
REPORT_DIR = "/opt/stock_agent/phase_reports"

# 阶段定义 (不可修改)
PHASES = {
    1: {
        "name": "基础智能化底座",
        "duration": "1~2周",
        "modules": [
            "stock_daily_basic / stock_money_flow 数据自动采集",
            "XGBoost多因子选股 agent_predict_v2",
            "静态硬风控 layered_risk_control (§2)",
            "全自动收盘复盘 daily_auto_review",
            "基本面因子预测 stock_predict 批量入库",
        ],
        "gates": {
            "data_complete": False,      # 数据采集连续7日无中断
            "factor_stable": False,      # 因子打分波动<15%
            "risk_blocking": False,      # 风控拦截正常(拦截率>0)
            "review_running": False,     # 复盘每日生成
        },
        "upgrade_target": 2,
    },
    2: {
        "name": "自适应智能决策",
        "duration": "1个月",
        "modules": [
            "LSTM时序预测 trend_capture_model",
            "PPO强化学习仓位 agent_position",
            "市场状态识别体系 (牛熊/震荡/高波动)",
            "动态资金分配与自适应调仓",
            "多智能体协同 agent_orchestrator",
        ],
        "gates": {
            "lstm_converged": False,     # LSTM验证损失<0.01
            "ppo_stable": False,         # PPO回报稳定>0
            "market_state": False,        # 状态识别准确率>70%
            "multi_agent": False,         # 5Agent链路通过
        },
        "upgrade_target": 3,
    },
    3: {
        "name": "全自动自主进化闭环",
        "duration": "2~3个月",
        "modules": [
            "AI参数微进化 evolution_engine",
            "LLM因子生成+IC检验",
            "沙盒+灰度A/B安全测试 sandbox_safe_test",
            "五大智能体协同完整闭环",
            "全自进化闭环端到端",
        ],
        "gates": {
            "evolution_ok": False,       # 进化不降低夏普
            "sandbox_pass": False,       # 沙盒三重标准达标
            "llm_factor_valid": False,   # LLM因子IC>0.03
            "full_loop": False,          # 端到端闭环验证
        },
        "upgrade_target": None,
    },
}


class PhaseController:
    """分阶段实施控制器 — 全程阶梯式落地"""

    def __init__(self, state_path=STATE_PATH):
        self.state_path = state_path
        self.state = self._load()
        os.makedirs(REPORT_DIR, exist_ok=True)

    def _load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {
                "current_phase": 1,
                "phase_start": datetime.now().strftime("%Y-%m-%d"),
                "gates": {"1": {}, "2": {}, "3": {}},
                "history": [],
                "blockers": [],
            }

    def _save(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ── 准入/准出检测 ──

    def phase_gates(self, phase):
        """返回当前阶段的所有门禁状态"""
        return PHASES[phase]["gates"].copy()

    def check_entry(self, phase):
        """检查是否可进入phase（上一阶段门禁必须全部通过）"""
        if phase == 1:
            return True, "阶段1为起始阶段"
        prev = phase - 1
        gates = self.state["gates"].get(str(prev), {})
        required = PHASES[prev]["gates"]
        missing = [k for k in required if not gates.get(k, False)]
        if missing:
            return False, f"阶段{prev}门禁未通过: {', '.join(missing)}"
        return True, f"阶段{prev}门禁全部通过，允许进入阶段{phase}"

    def check_exit(self, phase):
        """检查当前阶段是否可退出(所有门禁通过)"""
        gates = self.state["gates"].get(str(phase), {})
        required = PHASES[phase]["gates"]
        missing = [k for k in required if not gates.get(k, False)]
        if missing:
            return False, f"阶段{phase}门禁未通过: {', '.join(missing)}"
        return True, f"阶段{phase}门禁全部通过，允许升级"

    def mark_gate(self, phase, gate_name, passed=True, detail=""):
        """标记单个门禁状态"""
        phase_str = str(phase)
        self.state["gates"].setdefault(phase_str, {})
        self.state["gates"][phase_str][gate_name] = passed
        self._save()
        status = "✅" if passed else "❌"
        log = f"[Phase{phase}] {status} {gate_name}: {detail}"
        print(log)
        # 写入阶段报告
        report_path = f"{REPORT_DIR}/phase{phase}_gate_{gate_name}.log"
        with open(report_path, "a") as f:
            f.write(f"{datetime.now()} {log}\n")
        return passed

    # ── 升级/降级 ──

    def upgrade(self):
        """尝试升级到下一阶段"""
        cur = self.state["current_phase"]
        exit_ok, exit_msg = self.check_exit(cur)
        if not exit_ok:
            return False, exit_msg, cur

        targets = PHASES[cur].get("upgrade_target")
        if targets is None:
            return False, "已是最终阶段", cur

        entry_ok, entry_msg = self.check_entry(targets)
        if not entry_ok:
            return False, entry_msg, cur

        # 执行升级
        self.state["history"].append({
            "from": cur,
            "to": targets,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.state["current_phase"] = targets
        self.state["phase_start"] = datetime.now().strftime("%Y-%m-%d")
        self._save()
        msg = f"✅ 阶段{cur}→{targets} 升级成功 ({PHASES[targets]['name']})"
        print(msg)
        return True, msg, targets

    def rollback_phase(self):
        """降级到上一阶段(当本阶段门禁长期未通过)"""
        cur = self.state["current_phase"]
        if cur <= 1:
            return False, "已是基础阶段，无法降级", cur
        prev = cur - 1
        self.state["history"].append({
            "from": cur,
            "to": prev,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "manual_rollback",
        })
        self.state["current_phase"] = prev
        self._save()
        return True, f"↩️ 阶段{cur}→{prev} 降级", prev

    # ── 模块可用性校验(供Orchestrator调用) ──

    def is_module_allowed(self, module_name):
        """
        检查某模块在当前阶段是否可启用
        禁止高阶模块裸上线
        """
        cur = self.state["current_phase"]

        # 阶段1允许的模块
        p1_modules = [
            "stock_data_collector", "agent_predict_v2",
            "static_hard_risk_control", "layered_risk_control",
            "daily_auto_review", "factor_weekly_iterate",
            "chain_logger", "factor_drift_monitor", "code_isolation",
            "inference_accelerator",
        ]
        if cur >= 1 and module_name in p1_modules:
            return True

        # 阶段2新增
        p2_modules = [
            "trend_capture_model", "ppo_trade_agent",
            "agent_selector", "agent_position", "agent_risk_controller",
            "agent_executor", "agent_orchestrator",
        ]
        if cur >= 2 and module_name in p2_modules:
            return True

        # 阶段3新增
        p3_modules = [
            "evolution_engine", "sandbox_safe_test",
            "agent_evolver", "full_integrated_pipeline",
        ]
        if cur >= 3 and module_name in p3_modules:
            return True

        return False

    # ── 每日状态报告 ──

    def daily_report(self):
        """输出当前阶段状态摘要"""
        cur = self.state["current_phase"]
        p = PHASES[cur]
        gates = self.state["gates"].get(str(cur), {})

        lines = [
            f"===== Phase {cur}: {p['name']} =====",
            f"开始时间: {self.state['phase_start']}",
            f"预计时长: {p['duration']}",
            f"门禁进度: {sum(1 for v in gates.values() if v)}/{len(p['gates'])}",
        ]
        for g_name, g_required in p["gates"].items():
            status = gates.get(g_name, False)
            lines.append(f"  {'✅' if status else '❌'} {g_name}")

        # 检查是否可升级
        exit_ok, exit_msg = self.check_exit(cur)
        lines.append(f"升级状态: {'✅ 可升级' if exit_ok else '⏳ 继续积累'}")
        if not exit_ok:
            lines.append(f"  {exit_msg}")

        report = "\n".join(lines)
        # 写文件
        fname = f"{REPORT_DIR}/phase_status_{datetime.now().strftime('%Y%m%d')}.md"
        with open(fname, "w") as f:
            f.write(report)
        return report

    def list_blockers(self):
        """列出当前阶段阻碍升级的阻塞项"""
        cur = self.state["current_phase"]
        gates = self.state["gates"].get(str(cur), {})
        required = PHASES[cur]["gates"]
        blockers = [k for k in required if not gates.get(k, False)]
        return blockers
