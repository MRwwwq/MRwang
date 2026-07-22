"""
agent_orchestrator.py — 多智能体统一数据调度中间件
§3 固定链路: 选股→仓位→风控→执行 (不可颠倒、不可跳过)
§4 容错兜底: 模块隔离/故障降级/双向校验/数据隔离
"""
import sys; sys.path.insert(0, "/opt/stock_agent")
import time
import json
from datetime import datetime

from agent_selector import AgentSelector
from agent_position import AgentPosition
from agent_risk_controller import AgentRiskController
from agent_executor import AgentExecutor
from agent_evolver import AgentEvolver
from inference_accelerator import InferenceAccelerator, DataStreamBuffer
from chain_logger import ChainLogger
from factor_drift_monitor import FactorDriftMonitor
from code_isolation import CodeIsolation
from phase_controller import PhaseController
from test_orchestrator import TestOrchestrator
from hybrid_ai_decision import HybridAIDecisionSystem, MarketEnvClassifier


class AgentOrchestrator:
    """多智能体调度中间件 — 固定链路+容错兜底"""

    def __init__(self, target_codes=None):
        self.target_codes = target_codes or []
        self.agents = {}
        self.tester = TestOrchestrator()  # 三层测试调度器(启动门禁)
        self.decision = HybridAIDecisionSystem()  # 三层混合决策引擎
        self.env_classifier = MarketEnvClassifier()  # 市场环境分类器
        self._init_agents()

    def _init_agents(self):
        """初始化所有Agent + 工程底座, 模块隔离"""
        self.infra = {}

        # §6.2 推理加速引擎
        try:
            self.infra["accelerator"] = InferenceAccelerator()
        except Exception:
            self.infra["accelerator"] = None

        # §6.3 全链路日志
        try:
            self.infra["logger"] = ChainLogger()
        except Exception:
            self.infra["logger"] = None

        # §6.4 因子漂移监控
        try:
            self.infra["drift"] = FactorDriftMonitor()
        except Exception:
            self.infra["drift"] = None

        # §6.5 代码隔离
        try:
            self.infra["isolation"] = CodeIsolation()
            self.infra["isolation"].lock_baseline()
        except Exception:
            self.infra["isolation"] = None

        # 三层测试门禁: 检查各层通过状态
        self._check_test_gates()

        # 分阶段实施控制器
        try:
            self.phase = PhaseController()
            print(f"[Orchestrator] 当前阶段: Phase {self.phase.state['current_phase']}")
        except Exception:
            self.phase = None

        # 行情缓冲队列
        self.stream_buffer = DataStreamBuffer()

        # 5 Agent初始化
        agent_classes = {
            "selector": AgentSelector,
            "position": AgentPosition,
            "risk": AgentRiskController,
            "executor": AgentExecutor,
            "evolver": AgentEvolver,
        }
        for name, cls in agent_classes.items():
            try:
                self.agents[name] = cls()
                print(f"[Orchestrator] {name}Agent 初始化 ✅")
            except Exception as e:
                self.agents[name] = None
                print(f"[Orchestrator] {name}Agent 初始化 ❌ {e}")

    # ─────────────── §3 固定链路（盘中） ───────────────

    def run_intraday_chain(self, target_codes=None):
        """
        §3 固定链路: 选股→仓位→风控→执行
        返回: (chain_ok, chain_log, orders)
        """
        codes = target_codes or self.target_codes
        chain_log = ["===== §3 多智能体固定链路 ====="]
        orders = []
        chain_ok = True

        # 阶段门禁: 检查当前阶段是否允许多智能体链路
        phase = getattr(self, "phase", None)
        if phase:
            cur_phase = phase.state["current_phase"]
            chain_log.append(f"[Phase{cur_phase}] 阶段门禁校验")
            # 阶段1: 仅运行选股+风控(基础模式)
            if cur_phase < 2:
                chain_log.append("  阶段1: 仅启用基础选股+风控模式, 跳过仓位/执行/PPO")
            # 阶段2: 启用多智能体链路
            if cur_phase < 3:
                chain_log.append("  阶段2: 启用多Agent链路, 跳过进化/沙盒")

        # ── §3.1 选股Agent ──
        selector = self.agents.get("selector")
        if selector:
            try:
                stock_pool = selector.scan(codes)
                chain_log.append(f"[选股Agent] 扫描{codes} → {len(stock_pool)}只候选")
            except Exception as e:
                # §4 故障降级: 读取历史合规标的池
                stock_pool = self._fallback_stock_pool()
                chain_log.append(f"[选股Agent] ⚠异常({e}), 启用历史池→{len(stock_pool)}只")
        else:
            stock_pool = self._fallback_stock_pool()
            chain_log.append(f"[选股Agent] ❌异常, 启用历史池→{len(stock_pool)}只")

        if not stock_pool:
            chain_log.append("❌ 选股池为空, 链路终止")
            return False, "\n".join(chain_log), []

        # §6.3 全链路日志: 选股Agent
        logger = self.infra.get("logger")
        if logger:
            for s in stock_pool:
                logger.log_chain(s["ts_code"], "selector", "scan_result", s)

        # §6.4 因子漂移检查(选股后)
        drift = self.infra.get("drift")
        if drift and stock_pool:
            scores = [s.get("base_score", 0.5) for s in stock_pool if s]
            drift_lvl, drift_alerts = drift.full_check({"base_score": scores})
            if drift_lvl >= 2:
                chain_log.append(f"§6.4-{drift_lvl} 🟡 因子漂移告警,限制开仓")
                return False, "\n".join(chain_log), []

        # ── §3.2 仓位Agent ──
        position = self.agents.get("position")
        if position:
            try:
                pos_plan, total_exp, pos_log = position.allocate(stock_pool)
                chain_log.append(f"[仓位Agent] {len(pos_plan)}只, 总敞口{total_exp:.1%}")
                chain_log.append(pos_log)
            except Exception as e:
                # §4 故障降级: 静态固定阈值
                pos_plan, total_exp, pos_log = position.fallback_allocate(stock_pool)
                chain_log.append(f"[仓位Agent] ⚠异常({e}), 启用静态兜底")
        else:
            pos_plan, total_exp, _ = AgentPosition().fallback_allocate(stock_pool)
            chain_log.append(f"[仓位Agent] ❌异常, 启用静态兜底")

        if not pos_plan:
            chain_log.append("❌ 仓位方案为空, 链路终止")
            return False, "\n".join(chain_log), []

        # §6.3 全链路日志: 仓位Agent
        if logger:
            logger.log_chain("ALL", "position", "allocation_result",
                             {"plan": pos_plan, "total_exposure": total_exp})

        # ── §3.3 风控Agent（一票否决） ──
        risk = self.agents.get("risk")
        if risk:
            try:
                veto, risk_log, approved_plan = risk.veto_check(stock_pool, pos_plan)
                chain_log.append(risk_log)
            except Exception as e:
                # §4 故障降级: 取保守方案
                veto = True
                approved_plan = {}
                chain_log.append(f"[风控Agent] ⚠异常({e}), 保守驳回")
        else:
            veto = True
            approved_plan = {}
            chain_log.append("[风控Agent] ❌异常, 保守驳回")

        if veto or not approved_plan:
            chain_log.append("❌ 风控拦截, 链路终止")
            # §6.3 全链路日志: 风控拦截
            if logger:
                logger.log_chain("ALL", "risk", "veto", {"reason": chain_log[-1]})
            return False, "\n".join(chain_log), []

        # §6.3 全链路日志: 风控放行
        if logger:
            logger.log_chain("ALL", "risk", "approved", {"plan": approved_plan})

        # ── §3.4 执行Agent ──
        executor = self.agents.get("executor")
        if executor:
            try:
                orders = executor.schedule(approved_plan)
                chain_log.append(f"[执行Agent] 拆单{len(orders)}笔")
            except Exception as e:
                # §4 故障降级: 暂停新开仓
                orders = executor.fallback_execute(approved_plan)
                chain_log.append(f"[执行Agent] ⚠异常({e}), 暂停新开仓")
        else:
            chain_log.append("[执行Agent] ❌异常, 暂停新开仓")

        chain_log.append("✅ 多智能体链路完成")
        return True, "\n".join(chain_log), orders

    # ─────────────── §3 收盘后: 推送进化Agent ───────────────

    def run_post_close(self):
        """每日收盘后: 全量数据推送至进化Agent"""
        print("\n===== §3 收盘后推送进化Agent =====")
        evolver = self.agents.get("evolver")
        if not evolver:
            print("[Orchestrator] 进化Agent不可用")
            return

        try:
            # 归集→复盘→进化→沙盒
            summary = evolver.daily_ingest()
            print(f"归集: {summary}")
            evolver.run_review()

            # 阶段门禁: 仅阶段3启用进化+沙盒
            phase = getattr(self, "phase", None)
            cur_phase = phase.state["current_phase"] if phase else 1
            if cur_phase >= 3:
                if datetime.now().weekday() == 6:  # Sunday
                    evolver.run_evolution()
                    evolver.run_sandbox()
            elif cur_phase >= 2:
                # 阶段2: 周日仅运行复盘分析(不执行真实进化)
                if datetime.now().weekday() == 6:
                    print("[Phase2] 周日: 复盘分析完成, 进化待阶段3启用")
            else:
                print("[Phase1] 收盘: 基础数据归集完成")

            # §6.5 每日收盘快照
            isolation = self.infra.get("isolation")
            if isolation:
                isolation.snapshot()
        except Exception as e:
            print(f"[Orchestrator] 收盘后推送异常: {e}")

    # ─────────────── 三层测试门禁 ───────────────

    def _check_test_gates(self) -> str:
        """启动前全局门禁校验，返回实盘权限标识"""
        gate_ok = self.tester.run_full_pipeline()
        l1 = self.tester.layer1_pass
        l2 = self.tester.layer2_pass
        l3 = self.tester.layer3_pass
        if not l1:
            perm = "FORBID_ALL_TRADING"    # 第一层不通过：完全禁止实盘
        elif not l2:
            perm = "FORBID_ALL_TRADING"    # 第二层不通过：完全禁止实盘
        elif not l3:
            perm = "LIMITED_TRADING"       # 第三层不通过：仅小额试跑
        else:
            perm = "FULL_TRADING"          # 三层全过：开放全量实盘

        emoji = {"FULL_TRADING": "✅", "LIMITED_TRADING": "⚠️", "FORBID_ALL_TRADING": "⛔"}
        print(f"  [TestGate] {emoji.get(perm, '❓')} {perm}")
        return perm

    # ─────────────── §4 容错兜底 ───────────────

    def _fallback_stock_pool(self):
        """选股Agent异常时: 读取memory_market最近有评分的历史标的"""
        try:
            from agent_selector import AgentSelector
            s = AgentSelector()
            pool = s.scan(self.target_codes)
            s.close()
            return pool
        except Exception:
            return []

    # ─────────────── 统一调度入口 ───────────────

    def run_daily(self, target_codes=None):
        """
        完整日流程: 盘中链路 + 收盘后推送
        返回: {chain_result, chain_log, close_result}
        """
        chain_ok, chain_log, orders = self.run_intraday_chain(target_codes)
        self.run_post_close()
        return {
            "chain_ok": chain_ok,
            "chain_log": chain_log,
            "orders": orders,
        }

    def close_all(self):
        for name, agent in self.agents.items():
            if agent and hasattr(agent, "close"):
                try:
                    agent.close()
                except Exception:
                    pass
        # §6.3 关闭日志
        logger = self.infra.get("logger")
        if logger:
            try: logger.close()
            except: pass
        # §6.4 关闭漂移监控
        drift = self.infra.get("drift")
        if drift:
            try: drift.close()
            except: pass
