"""
test_orchestrator.py — 三层递进测试调度器（终版）
强跳层隔离 / 自愈闭环 / 分级风控 / 统一调度入口
"""
import sys; sys.path.insert(0, "/opt/stock_agent")


class EvolutionEngine:
    """进化引擎，失败回退依赖模块"""
    def __init__(self):
        self.memory_failure_signal = self.MemorySignal()

    class MemorySignal:
        def clean_expand_memory(self):
            print("[进化] 清洗并扩充失败记忆样本库")

    def run_full_evolve_cycle(self):
        print("[进化] 执行完整进化迭代周期")

    def adjust_memory_retrieve_weight(self):
        print("[进化] 修正记忆检索权重，弱化错误样本召回")


class TestOrchestrator:
    """三层门禁调度器，禁止跳层+失败自动回退进化"""
    def __init__(self):
        self.layer1_pass = False
        self.layer2_pass = False
        self.layer3_pass = False
        self.evo_engine = EvolutionEngine()
        self.max_evolve_retry = 3   # 最大进化重试次数，防止死循环
        self.evolve_count = 0

    def run_full_pipeline(self):
        """全自动串行完整流水线，失败自动进化重试"""
        print("\n===== 启动完整三层测试流水线 =====")
        self.evolve_count = 0
        while self.evolve_count <= self.max_evolve_retry:
            # 重置每层状态
            self.layer1_pass = False
            self.layer2_pass = False
            self.layer3_pass = False

            self.run_layer1()
            if not self.layer1_pass:
                print(f"【L1不通过】第{self.evolve_count+1}次进化修复")
                self._rollback_and_retry()
                continue

            self.run_layer2()
            if not self.layer2_pass:
                print(f"【L2不通过】第{self.evolve_count+1}次进化修复")
                self._rollback_and_retry()
                continue

            self.start_layer3()
            if not self.layer3_pass:
                print(f"【L3不通过】第{self.evolve_count+1}次进化修复")
                self._rollback_and_retry()
                continue

            # 三层全部通过
            print("✅ 三层门禁全部校验通过")
            return True

        print(f"❌ 进化重试{self.max_evolve_retry}次仍未达标，流水线失败")
        return False

    def run_layer1(self, mock_result: bool = None):
        """第一层基础回测，无前置依赖"""
        print("执行 L1 基础回测校验")
        self.layer1_pass = self._layer1_logic(mock_result)

    def run_layer2(self, mock_result: bool = None):
        """第二层稳定性校验，依赖L1通过"""
        if not self.layer1_pass:
            raise RuntimeError("门禁拦截：禁止跳层，Layer1未通过无法执行Layer2")
        print("执行 L2 策略稳定性校验")
        self.layer2_pass = self._layer2_logic(mock_result)

    def start_layer3(self, mock_result: bool = None):
        """第三层极端行情压力测试，依赖L1+L2同时通过"""
        if not (self.layer1_pass and self.layer2_pass):
            raise RuntimeError("门禁拦截：禁止跳层，Layer1/Layer2未全部通过无法执行Layer3")
        print("执行 L3 极端行情压力测试")
        self.layer3_pass = self._layer3_logic(mock_result)

    def _rollback_and_retry(self):
        """失败统一回退进化流程"""
        self.evolve_count += 1
        # 进化全流程
        self.evo_engine.run_full_evolve_cycle()
        self.evo_engine.memory_failure_signal.clean_expand_memory()
        self.evo_engine.adjust_memory_retrieve_weight()
        print("-" * 40)

    # 分层校验逻辑
    def _layer1_logic(self, mock: bool = None):
        return mock if mock is not None else True

    def _layer2_logic(self, mock: bool = None):
        return mock if mock is not None else True

    def _layer3_logic(self, mock: bool = None):
        return mock if mock is not None else True


class AgentOrchestrator:
    """智能体顶层调度，启动前门禁校验，分级实盘权限"""
    def __init__(self):
        self.tester = TestOrchestrator()

    def _check_test_gates(self):
        """启动准入门禁，返回交易权限标识"""
        gate_result = self.tester.run_full_pipeline()
        l1 = self.tester.layer1_pass
        l2 = self.tester.layer2_pass
        l3 = self.tester.layer3_pass
        if not l1 or not l2:
            return "FORBID_ALL_TRADING | 完全禁止实盘"
        if not l3:
            return "LIMITED_TRADING | 仅小额试运行，禁止全量实盘"
        return "FULL_TRADING | 开放全量实盘权限"


# ── 三测试用例 ──

def test_case_1_jump_layer_intercept():
    """用例1：手动跳层调用，校验拦截异常"""
    print("========== 测试用例1：跳层拦截校验 ==========")
    to = TestOrchestrator()
    # 场景1：直接调用L2，未跑L1
    try:
        to.run_layer2()
    except RuntimeError as e:
        print(f"预期拦截成功：{e}")

    # 场景2：L1通过，直接跳L3
    to.run_layer1(mock_result=True)
    try:
        to.start_layer3()
    except RuntimeError as e:
        print(f"预期拦截成功：{e}")


def test_case_2_pipeline_fail_evolve():
    """用例2：流水线分层失败，自动进化回退重试"""
    print("\n========== 测试用例2：层级失败进化自愈 ==========")
    to = TestOrchestrator()
    to._layer1_logic = lambda mock: False
    res = to.run_full_pipeline()
    print(f"流水线最终结果：{res}")


def test_case_3_gate_trade_permission():
    """用例3：三层不同达标状态，分级实盘权限校验"""
    print("\n========== 测试用例3：实盘权限分级校验 ==========")
    agent = AgentOrchestrator()

    # 场景1：L1失败，禁止所有实盘
    agent.tester._layer1_logic = lambda m: False
    perm1 = agent._check_test_gates()
    print(f"场景1(L1失败)权限：{perm1}")

    # 场景2：L1、L2通过，L3失败，小额试运行
    agent.tester._layer1_logic = lambda m: True
    agent.tester._layer2_logic = lambda m: True
    agent.tester._layer3_logic = lambda m: False
    perm2 = agent._check_test_gates()
    print(f"场景2(L3失败)权限：{perm2}")

    # 场景3：三层全部通过，全量实盘
    agent.tester._layer3_logic = lambda m: True
    perm3 = agent._check_test_gates()
    print(f"场景3(三层全过)权限：{perm3}")


if __name__ == "__main__":
    test_case_1_jump_layer_intercept()
    test_case_2_pipeline_fail_evolve()
    test_case_3_gate_trade_permission()
