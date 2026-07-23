#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trading Agent Orchestrator — 交易智能体全流程编排 v2.0

正式定义并注册 Module01~05 + Layer0~Layer2 到交易系统。
执行顺序固定不可逆，盘中参数变更需从 Step0 重新运行。

新流水线:
  Step0 → Step1(定风格) → Step2(定情绪) → Step3(定方向) → Step4(定策略)
  → Step5(Layer0采集) → Step6(Layer1特征) → Step7(Layer2决策) → Module05(离场)
"""

import logging
import json
from datetime import datetime
from psy_hit_manager import (
    psy_hit_codes,
    add_psy_code,
    remove_psy_code,
    get_psy_hit_count,
    clear_all_psy_codes,
)

from module01_style import run_module01
from module02_sentiment import run_module02
from module03_direction import run_module03
from module04_strategy import run_module04
from module05_exit import check_exit, polling_cycle, snapshot_exit_check

# 新三层风控（v2.0）
from layer0_collector import Layer0Collector
from layer1_feature import run_layer1
from layer2_decision import run_layer2

# 盘后校准（v2.1）
from post_market_calibration import run_calibration_pipeline, validate_completeness

# 误判自愈权重衰减（v2.2）
from self_heal_weight_decay import SelfHealWeightDecayUnit, run_self_heal_daily

# 四层联动风控（v3.0）
from four_layer_pipeline import FourLayerPipeline, run_four_layer_pipeline
from layer0_macro import L0MacroHedgeChecker
from layer3_reverse import Rule015ReverseChecker
from rule021_dual_branch import Rule021DualBranchChecker, classify_stock_type, determine_risk_tier
from dynamic_weight_mapping import MisjudgmentScoreCalculator, SignalStrengthScorer
# 共振熔断进化（v3.1）
from resonance_evolution import ResonanceEvolutionAgent, run_resonance_evolution, query_evolution_history

# 旧版layer1_risk保留兼容引用
from layer1_risk import judge_risk_level as old_judge_risk_level

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Orch] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"/opt/stock_agent/logs/trade_orch_{datetime.now().strftime('%Y%m%d')}.log")
    ]
)

# ====================== 模块注册表 (v2.0) ======================

MODULE_REGISTRY = {
    "psy_hit_manager": {
        "name": "心理误判编码管理器",
        "file": "psy_hit_manager.py",
        "defined": True,
        "version": "1.0.0",
    },
    "Module01": {
        "name": "定风格 — 盘前底层定位",
        "file": "module01_style.py",
        "defined": True,
        "entry": "run_module01",
        "version": "1.0.0",
    },
    "Module02": {
        "name": "定情绪 — 市场情绪阶段判定",
        "file": "module02_sentiment.py",
        "defined": True,
        "entry": "run_module02",
        "version": "1.0.0",
    },
    "Module03": {
        "name": "定方向 — 主线板块与选股分层",
        "file": "module03_direction.py",
        "defined": True,
        "entry": "run_module03",
        "version": "1.0.0",
    },
    "Module04": {
        "name": "定策略 — 可交易标的池生成",
        "file": "module04_strategy.py",
        "defined": True,
        "entry": "run_module04",
        "version": "1.0.0",
    },
    "Module05": {
        "name": "机械离场规则 — 盘中持续轮询",
        "file": "module05_exit.py",
        "defined": True,
        "entry": "check_exit / polling_cycle",
        "version": "1.0.0",
    },
    "Layer0": {
        "name": "数据采集层 — 原始输入汇聚与信号标准化",
        "file": "layer0_collector.py",
        "defined": True,
        "entry": "Layer0Collector.collect_all",
        "version": "2.0.0",
    },
    "Layer1": {
        "name": "特征校验层 — 关键词提取/向量检索/Rescore/五维共振",
        "file": "layer1_feature.py",
        "defined": True,
        "entry": "run_layer1",
        "version": "2.0.0",
    },
    "Layer2": {
        "name": "风控决策层 — RED/YELLOW/GREEN分级输出",
        "file": "layer2_decision.py",
        "defined": True,
        "entry": "run_layer2",
        "version": "2.0.0",
    },
    "Step8_CALIB": {
        "name": "盘后校准 — 10类标签+智能体自修正",
        "file": "post_market_calibration.py",
        "defined": True,
        "entry": "run_calibration_pipeline",
        "version": "2.1.0",
    },
    "StepS_HEAL": {
        "name": "误判自愈衰减 — 乐观误判权重自主消解",
        "file": "self_heal_weight_decay.py",
        "defined": True,
        "entry": "SelfHealWeightDecayUnit / run_self_heal_daily",
        "version": "2.2.0",
    },
    # === 四层联动风控 v3.0 ===
    "Layer0_MACRO": {
        "name": "宏观对冲校验层 — 顶层前置修正系数",
        "file": "layer0_macro.py",
        "defined": True,
        "entry": "L0MacroHedgeChecker.check",
        "version": "3.0.0",
    },
    "Layer1_R021": {
        "name": "Rule021基础打分 — 双分支五维+阶梯+雷区",
        "file": "rule021_dual_branch.py",
        "defined": True,
        "entry": "Rule021DualBranchChecker.check",
        "version": "3.0.0",
    },
    "Layer2_DWM": {
        "name": "全量误判动态加权 — 衰减/冲突/自愈/加权",
        "file": "dynamic_weight_mapping.py",
        "defined": True,
        "entry": "MisjudgmentScoreCalculator.calculate",
        "version": "3.0.0",
    },
    "Layer3_R015": {
        "name": "双重共振反转校验 — 基本面强支撑兜底反转",
        "file": "layer3_reverse.py",
        "defined": True,
        "entry": "Rule015ReverseChecker.check",
        "version": "3.0.0",
    },
    "FourLayerPipeline": {
        "name": "四层联动全链路编排器 — L0→L1→L2→L3→阈值",
        "file": "four_layer_pipeline.py",
        "defined": True,
        "entry": "run_four_layer_pipeline",
        "version": "3.0.0",
    },
    # === 共振熔断进化 v3.1 ===
    "StepX_EVOLVE": {
        "name": "共振熔断进化 — Lollapalooza+RED自动迭代参数",
        "file": "resonance_evolution.py",
        "defined": True,
        "entry": "ResonanceEvolutionAgent.run",
        "version": "3.1.0",
    },
}


# ====================== 全流程执行 (v2.0) ======================

class DailyPipeline:
    """每日交易流水线：持有一个交易日完整的上下文 (v2.0 三层风控)"""

    def __init__(self, trade_date: str = None):
        self.trade_date = trade_date or datetime.now().strftime("%Y%m%d")
        self.log = {}
        self.ctx = {}

    def step0_init(self) -> dict:
        """Step0: 系统初始化"""
        logging.info(f"\n{'='*60}")
        logging.info(f"📅 {self.trade_date} 交易流水线启动 (v2.0 三层风控)")
        logging.info(f"{'='*60}")

        clear_all_psy_codes()
        self.ctx["psy_hit_codes"] = psy_hit_codes.copy()
        self.ctx["modules_loaded"] = list(MODULE_REGISTRY.keys())

        result = {
            "step": "Step0_初始化",
            "psy_hit_codes_cleared": True,
            "modules_loaded": len(MODULE_REGISTRY),
            "module_list": list(MODULE_REGISTRY.keys()),
            "pipeline_version": "2.0 三层风控",
        }
        self.log["step0"] = result
        logging.info(f"✅ Step0: 初始化完成——psy_hit_codes已清空, {len(MODULE_REGISTRY)}个模块已注册")
        return result

    def step_self_heal(self) -> dict:
        """
        StepS: 误判自愈衰减扫描。

        执行时机: 每日开盘前(Step0之后, Step1之前)。
        作用: 对所有活跃自愈样本执行到期权重衰减，
              自动将衰减后的权重同步到dynamic_signal_mapping表，
              使评分引擎自动使用低权重。
        """
        logging.info(f"\n--- StepS: 误判自愈衰减扫描 ---")
        try:
            result = run_self_heal_daily(self.trade_date)
            self.ctx["self_heal"] = result
            self.log["step_self_heal"] = {
                "active_before": result["summary"]["active_samples"],
                "decayed": result["decay_result"]["decayed"],
                "new_completed": result["decay_result"]["completed"],
            }
            status_icon = "🏥"
            if result["decay_result"]["completed"] > 0:
                status_icon = "🏁"
            logging.info(f"  {status_icon} 自愈衰减: "
                         f"活跃{result['summary']['active_samples']}条, "
                         f"衰减{result['decay_result']['decayed']}次, "
                         f"新完成{result['decay_result']['completed']}条")
            return result
        except Exception as e:
            logging.warning(f"  ⚠️ 自愈衰减扫描失败: {e}")
            result = {"skipped": True, "error": str(e)}
            self.ctx["self_heal"] = result
            self.log["step_self_heal"] = {"error": str(e)}
            return result

    def _build_stock_context(self, step3_result: dict) -> dict:
        """
        从Module03结果构建标的上下文，用于Rule021双分支分类。

        step3_result格式:
            {"main_line": ..., "selected_filtered": {...}, "selected_raw": {...}, ...}
        """
        selected_filtered = step3_result.get("selected_filtered", {})
        selected_raw = step3_result.get("selected_raw", {})
        stocks_raw = step3_result.get("stocks_raw", [])
        if isinstance(stocks_raw, int):
            stocks_raw = []

        pool = []
        for layer in ["core", "fill", "latent"]:
            pool.extend(selected_filtered.get(layer, []))
        # 兜底：selected_raw
        if not pool:
            for layer in ["core", "fill", "latent"]:
                pool.extend(selected_raw.get(layer, []))
        # 兜底：stocks_raw
        if not pool and stocks_raw:
            pool = stocks_raw

        if not pool:
            return None

        # 取第一个标的作为上下文
        first = pool[0] if isinstance(pool, list) else pool
        return {
            "stock_code": first.get("code", ""),
            "stock_name": first.get("name", ""),
            "sector": first.get("sector", "") or step3_result.get("main_line", ""),
            "business_desc": first.get("reason", ""),
            "stock_data": {
                "commodity_price_percentile": first.get("commodity_pct", 50),
                "capacity_stability": first.get("capacity", "full"),
                "cost_position": first.get("cost", "mid"),
                "ore_grade": first.get("ore_grade", "mid"),
                "debt_ratio": first.get("debt_ratio", 50),
                "pe_percentile": first.get("pe_pct", 50),
                "production_cost": first.get("production_cost", 50),
                "policy_score": first.get("policy_score", 50),
                "board_heat": first.get("board_heat", 50),
                "concept_purity": first.get("purity", 50),
                "fund_inflow": first.get("fund_inflow", 0),
                "chip_concentration": first.get("chip_conc", 50),
                "remaining_catalysts": first.get("remaining_catalysts", 50),
            },
        }

    def step1_style(self, trade_period: str, capital_type: str,
                    total_pct: int, per_stock_pct: int, stop_loss: float) -> dict:
        """Step1: 定风格"""
        logging.info(f"\n--- Step1: 定风格 ---")
        result = run_module01(trade_period, capital_type, total_pct, per_stock_pct, stop_loss)
        self.ctx["step1"] = result
        self.ctx["active_style"] = result["active_style"]
        self.ctx["style_name"] = result["style_name"]
        self.ctx["style_total_cap"] = result["position_cap"]["total"]
        self.ctx["style_per_stock"] = result["position_cap"]["per_stock"]
        self.ctx["style_stop_loss"] = result["position_cap"]["stop_loss"]
        self.log["step1"] = result
        return result

    def step2_sentiment(self, up_count: int, down_count: int,
                        highest_board: int, seal_rate: float,
                        blow_rate: float, has_massacre: bool) -> dict:
        """Step2: 定情绪"""
        logging.info(f"\n--- Step2: 定情绪 ---")
        style_cap = self.ctx.get("style_total_cap", 25)
        result = run_module02(up_count, down_count, highest_board,
                              seal_rate, blow_rate, has_massacre, style_cap)
        self.ctx["step2"] = result
        self.ctx["sentiment_label"] = result["sentiment_label"]
        self.ctx["final_total_cap"] = result["final_total_cap"]
        self.ctx["signal_520_weight"] = result.get("signal_520_weight", 1.0)  # ← 保存520权重
        self.log["step2"] = result
        return result

    def step3_direction(self, main_line: str, driver_type: str,
                        driver_detail: str, candidate_stocks: list) -> dict:
        """Step3: 定方向"""
        logging.info(f"\n--- Step3: 定方向 ---")
        active_style = self.ctx.get("active_style", "D")
        signal_520_weight = self.ctx.get("signal_520_weight", 1.0)
        result = run_module03(main_line, driver_type, driver_detail,
                              candidate_stocks, active_style,
                              signal_520_weight=signal_520_weight)
        self.ctx["step3"] = result
        self.ctx["selected_filtered"] = result["selected_filtered"]
        self.ctx["signal_520_count"] = result.get("signal_520_count", {})
        self.log["step3"] = result
        return result

    def step4_strategy(self) -> dict:
        """Step4: 定策略"""
        logging.info(f"\n--- Step4: 定策略 ---")
        active_style = self.ctx.get("active_style", "D")
        style_name = self.ctx.get("style_name", "兜底混合")
        total_cap = self.ctx.get("final_total_cap", self.ctx.get("style_total_cap", 25))
        per_stock_max = self.ctx.get("style_per_stock", 10)
        stop_loss = self.ctx.get("style_stop_loss", 2.0)
        sentiment_label = self.ctx.get("sentiment_label", "recovery")
        selected = self.ctx.get("selected_filtered", {"core": [], "fill": [], "latent": []})
        main_line = self.ctx.get("step3", {}).get("main_line", "")
        signal_520_weight = self.ctx.get("signal_520_weight", 1.0)

        result = run_module04(active_style, style_name, total_cap,
                              per_stock_max, stop_loss, sentiment_label, selected,
                              main_line=main_line, signal_520_weight=signal_520_weight)
        self.ctx["step4"] = result
        self.ctx["tradeable_pool"] = result["tradeable_pool"]
        self.log["step4"] = result
        return result

    # ====================== 520复盘输出 ======================

    def step_review_520(self) -> dict:
        """StepR: 520交易信号复盘输出（固定5项统计）。"""
        logging.info(f"\n--- StepR: 520复盘 ---")
        from module_review_520 import build_review

        # 从M03读取520筛选统计
        m03 = self.ctx.get("step3", {})
        s520_count = m03.get("signal_520_count", {})

        total_candidates_c5 = m03.get("stocks_raw", 0)  # M03原始候选数
        if isinstance(total_candidates_c5, int) and total_candidates_c5 == 0:
            # 改用selected_raw + eliminated求和
            selected_raw = m03.get("selected_raw", {})
            eliminated = m03.get("eliminated", [])
            total_candidates_c5 = (
                sum(len(v) for v in selected_raw.values()) + len(eliminated)
            )

        gold_cross_valid = s520_count.get("passed", 0)
        ma20_down_filtered = s520_count.get("failed_ma20_down", 0)
        sentiment_m02_downgraded = 0
        if self.ctx.get("signal_520_weight", 1.0) < 1.0:
            sentiment_m02_downgraded = gold_cross_valid  # 全部被情绪降级

        # 从M04读取芒格拦截
        m04 = self.ctx.get("step4", {})
        lolla_blocked = m04.get("lolla_blocked_count", 0)

        # 从M04读取最终交易池
        tradeable_pool = m04.get("tradeable_pool", [])
        final_pool_size = len(tradeable_pool)

        sentiment_label = self.ctx.get("sentiment_label", "")
        signal_520_weight = self.ctx.get("signal_520_weight", 1.0)

        report = build_review(
            total_candidates=total_candidates_c5,
            gold_cross_count=gold_cross_valid,
            ma20_down_filtered=ma20_down_filtered,
            sentiment_downgraded=sentiment_m02_downgraded,
            lolla_blocked=lolla_blocked,
            final_pool_size=final_pool_size,
            sentiment_label=sentiment_label,
            signal_520_weight=signal_520_weight,
            market_trend=m03.get("signal_520_weight_label", ""),
        )

        # 龙回头数量
        dragon_count = m04.get("dragon_return_count", 0)
        if dragon_count > 0:
            logging.info(f"  🐉 龙回头识别: {dragon_count}只")

        self.ctx["step_review_520"] = report
        self.log["step_review_520"] = report
        return report

    def step5_layer0(self,
                     tech_data: dict = None,
                     fund_data: dict = None,
                     sent_data: dict = None,
                     ind_data: dict = None,
                     macro_data: dict = None) -> dict:
        """
        Step5: Layer0 数据采集层

        采集 Module01~04 输出 + 外部市场数据 + psy_hit_codes
        → 标准化五类信号 → 送入 Layer1
        """
        logging.info(f"\n--- Step5: Layer0 数据采集层 ---")
        collector = Layer0Collector()

        # 从ctx获取上游模块结果
        m01 = self.ctx.get("step1", {})
        m02 = self.ctx.get("step2", {})
        m03 = self.ctx.get("step3", {})
        m04 = self.ctx.get("step4", {})

        signal_output = collector.collect_all(
            module01_result=m01,
            module02_result=m02,
            module03_result=m03,
            module04_result=m04,
            psy_codes=psy_hit_codes.copy(),
            tech_data=tech_data,
            fund_data=fund_data,
            sent_data=sent_data,
            ind_data=ind_data,
            macro_data=macro_data,
        )
        self.ctx["step5"] = signal_output
        self.ctx["signal_output"] = signal_output
        self.log["step5_layer0"] = {
            "total_signals": signal_output["total_signals"],
            "signals_by_type": {k: len(v) for k, v in signal_output.items()
                                if isinstance(v, list) and k != "signals_raw"},
        }
        logging.info(f"✅ Step5 Layer0完成: {signal_output['total_signals']}条信号采集")
        return signal_output

    def step6_layer1(self) -> dict:
        """
        Step6: Layer1 特征校验层

        关键词提取 → 向量检索 → Rescore → Lollapalooza → Rule_021 → 三层联动
        → 特征综合打分 + 心理误判触发总数 + 五维信号共振校验结果
        → 送入 Layer2
        """
        logging.info(f"\n--- Step6: Layer1 特征校验层 ---")
        signal_output = self.ctx.get("signal_output", {})

        # 从Module03提取标的上下文用于Rule021双分支分类
        m03 = self.ctx.get("step3", {})
        stock_context = self._build_stock_context(m03)

        result = run_layer1(signal_output, stock_context)
        self.ctx["step6"] = result
        self.ctx["layer1_result"] = result
        self.log["step6_layer1"] = {
            "composite_score": result["composite_score"],
            "active_features": result["active_features"],
            "psy_count": result["psy_count"],
            "lolla_direct_red": result["lolla_direct_red"],
            "resonance": result["rule_021"]["resonance_direction"],
            "linkage": result["three_layer_linkage"]["linkage_status"],
        }
        logging.info(f"✅ Step6 Layer1完成: score={result['composite_score']:.3f}, "
                     f"psy={result['psy_count']}条, "
                     f"共振={result['rule_021']['resonance_direction']}")
        return result

    def step7_layer2(self) -> dict:
        """
        Step7: Layer2 风控决策层

        基于Layer1校验结果 → RED/YELLOW/GREEN分级 → 同步下发Module04/05
        """
        logging.info(f"\n--- Step7: Layer2 风控决策层 ---")
        layer1_result = self.ctx.get("layer1_result", {})

        decision = run_layer2(layer1_result)
        self.ctx["step7"] = decision
        self.ctx["risk_level"] = decision["level"]         # RED/YELLOW/GREEN
        self.ctx["risk_decision"] = decision
        self.ctx["new_open_allowed"] = decision["rule"]["new_open_allowed"]
        self.ctx["total_cap_factor"] = decision["rule"]["total_cap_factor"]
        self.log["step7_layer2"] = {
            "level": decision["level"],
            "reason": decision["reason"],
            "module04": decision["outputs"]["module04"],
            "module05": decision["outputs"]["module05"],
        }

        # 计算修正后总仓
        base_total = self.ctx.get("final_total_cap", self.ctx.get("style_total_cap", 25))
        adjusted_total = int(base_total * decision["rule"]["total_cap_factor"])

        market_open = {
            "risk_level": decision["level"],
            "label": decision["label"],
            "reason": decision["reason"],
            "base_total_cap": base_total,
            "adjusted_total_cap": adjusted_total,
            "factor": decision["rule"]["total_cap_factor"],
            "new_open_allowed": decision["rule"]["new_open_allowed"],
            "action": decision["market_open_decision"],
            "module04_outputs": decision["outputs"]["module04"],
            "module05_outputs": decision["outputs"]["module05"],
        }
        self.ctx["market_open_decision"] = market_open
        self.log["step7_market_open"] = market_open
        logging.info(f"  → 开仓决策: {market_open['action']}")
        logging.info(f"✅ Step7 Layer2完成: {decision['label']} | "
                     f"新开仓={'允许' if decision['rule']['new_open_allowed'] else '禁止'}")
        return market_open

    # ====================== 四层联动风控 (v3.0) ======================

    def stepS_macro_layer(self,
                          commodity_data: dict = None,
                          monetary_data: dict = None,
                          reserve_data: dict = None) -> dict:
        logging.info(f"\n--- StepS: L0 宏观对冲校验 ---")
        checker = L0MacroHedgeChecker()
        result = checker.check(
            commodity_data=commodity_data,
            monetary_data=monetary_data,
            reserve_data=reserve_data,
        )
        self.ctx["macro_coefficient"] = result["macro_coefficient"]
        self.ctx["macro_result"] = result
        self.log["stepS_macro"] = {
            "verdict": result["macro_verdict"],
            "coefficient": result["macro_coefficient"],
        }
        logging.info(f"  L0宏观: {result['macro_label']} 系数={result['macro_coefficient']}")
        return result

    def stepT_rule021(self, deduction_count: int = 0) -> dict:
        logging.info(f"\n--- StepT: L1 Rule021 基础打分 ---")
        ctx = self.ctx.get("stock_context", {})
        macro_coeff = self.ctx.get("macro_coefficient", 1.0)

        l1 = Rule021DualBranchChecker()
        l1_result = l1.check(
            stock_code=ctx.get("stock_code", ""),
            stock_name=ctx.get("stock_name", ""),
            sector=ctx.get("sector", ""),
            business_desc=ctx.get("business_desc", ""),
            stock_data=ctx.get("stock_data", {}),
            deduction_count=deduction_count,
        )
        base_score = l1_result["final_risk_score"]
        macro_adjusted = round(base_score * macro_coeff, 1)

        self.ctx["l1_result"] = l1_result
        self.ctx["l1_macro_adjusted"] = macro_adjusted
        self.ctx["stock_type"] = l1_result.get("branch", "concept")
        self.log["stepT_rule021"] = {
            "branch": l1_result.get("branch_label", ""),
            "base_score": base_score,
            "macro_adjusted": macro_adjusted,
            "high_risk_count": l1_result.get("high_risk_count", 0),
        }
        logging.info(f"  L1 Rule021: base={base_score:.1f}->adj={macro_adjusted:.1f}")
        return l1_result

    def stepU_weighted(self, factor_values: dict = None) -> dict:
        logging.info(f"\n--- StepU: L2 全量误判动态加权 ---")
        macro_adjusted = self.ctx.get("l1_macro_adjusted", 0)
        l2_total = 0
        l2_result = {}

        if factor_values:
            try:
                scorer = MisjudgmentScoreCalculator()
                l2_result = scorer.calculate(factor_values=factor_values)
                l2_total = l2_result.get("total_score", 0)
            except Exception as e:
                logging.warning(f"  L2计算异常: {e}")

        fused_score = round(macro_adjusted * 0.7 + l2_total * 0.3, 1)
        self.ctx["l2_fused_score"] = fused_score
        self.ctx["l2_result"] = l2_result
        self.log["stepU_weighted"] = {
            "l2_score": l2_total,
            "fused_score": fused_score,
        }
        logging.info(f"  L2加权: L2={l2_total:.1f} 融合={fused_score:.1f}")
        return {"l2_score": l2_total, "fused_score": fused_score}

    def stepV_reverse(self, stock_data: dict = None) -> dict:
        logging.info(f"\n--- StepV: L3 双重共振反转 ---")
        stock_type = self.ctx.get("stock_type", "concept")
        fused_score = self.ctx.get("l2_fused_score", 0)
        l1_result = self.ctx.get("l1_result", {})
        current_tier = l1_result.get("risk_tier", "GREEN")

        checker = Rule015ReverseChecker()
        l3_result = checker.check(
            stock_type=stock_type,
            stock_data=stock_data or {},
            final_risk_score=fused_score,
            risk_tier=current_tier,
        )
        adjusted_score = l3_result["adjusted_score"]
        adjusted_tier = l3_result["adjusted_tier"]

        self.ctx["l3_result"] = l3_result
        self.ctx["l3_adjusted_score"] = adjusted_score
        self.ctx["l3_adjusted_tier"] = adjusted_tier
        self.log["stepV_reverse"] = {
            "resonance": l3_result["resonance_level"],
            "score_before": fused_score,
            "score_after": adjusted_score,
            "tier_before": current_tier,
            "tier_after": adjusted_tier,
        }
        logging.info(f"  L3反转: {l3_result['resonance_level']} "
                     f"{fused_score:.1f}->{adjusted_score:.1f} "
                     f"{current_tier}->{adjusted_tier}")
        return l3_result

    def stepW_threshold(self) -> dict:
        logging.info(f"\n--- StepW: 赛道阈值判定 ---\n")

        stock_type = self.ctx.get("stock_type", "concept")
        l3_result = self.ctx.get("l3_result", {})

        if l3_result.get("tier_downgrade_applied"):
            tier = l3_result["adjusted_tier"]
            score = l3_result["adjusted_score"]
        else:
            score = self.ctx.get("l3_adjusted_score",
                                 self.ctx.get("l2_fused_score", 0))
            tier_info = determine_risk_tier(stock_type, score)
            tier = tier_info["tier"]

        RISK_ACTION = {
            "RED": "拦截禁止新开仓;已有持仓启动强制减仓/止损",
            "YELLOW": "开启重点监控,下调仓位权重,不强制减仓",
            "GREEN": "放开约束,正常执行预设交易策略",
        }
        action = RISK_ACTION.get(tier, "未知等级")

        final = {"risk_tier": tier, "risk_action": action,
                 "final_score": score, "stock_type": stock_type}
        self.ctx["v3_risk_tier"] = tier
        self.ctx["v3_risk_action"] = action
        self.log["stepW_threshold"] = final
        icon = {"RED": "RED", "YELLOW": "YELLOW", "GREEN": "GREEN"}.get(tier, "?")
        logging.info(f"  最终判定: {icon} {tier} | {action}")
        return final

    def run_four_layer_v3(self,
                          stock_code="", stock_name="", sector="",
                          business_desc="",
                          commodity_data=None, monetary_data=None,
                          reserve_data=None,
                          stock_data=None, deduction_count=0,
                          factor_values=None) -> dict:
        """一键执行四层联动风控 v3.0。"""
        self.ctx["stock_context"] = {
            "stock_code": stock_code, "stock_name": stock_name,
            "sector": sector, "business_desc": business_desc,
            "stock_data": stock_data or {},
        }
        self.stepS_macro_layer(
            commodity_data=commodity_data,
            monetary_data=monetary_data,
            reserve_data=reserve_data,
        )
        self.stepT_rule021(deduction_count=deduction_count)
        self.stepU_weighted(factor_values=factor_values)
        self.stepV_reverse(stock_data=stock_data)
        return self.stepW_threshold()

    # ====================== Step8: 盘后校准 (v2.1) ======================

    def step8_post_calibration(self, records: list[dict] = None) -> dict:
        """
        Step8: 盘后校准流水线。

        收盘后执行: 归集当日全部标的 → 自动匹配10类标签
        → 完整性校验 → 推送智能体离线迭代

        参数:
            records: [{ts_code, ai_pred, ai_risk_tip, real_change_pct,
                       close_price, support_resistance, real_trade_action,
                       short_attribution}]

        返回: 校准结果字典
        """
        logging.info(f"\n--- Step8: 盘后校准 ---")
        if not records:
            logging.info("  ⏭️  无校准记录, 跳过")
            return {"skipped": True}

        result = run_calibration_pipeline(records)
        self.ctx["step8"] = result
        self.ctx["calibration_result"] = result
        self.log["step8_calibration"] = {
            "total": result["import_result"]["total"],
            "tagged": result["import_result"]["tagged"],
            "status": result["completeness"]["status"],
            "iterated": result["iteration_result"].get("iterated", False),
        }
        return result

    # ====================== 兼容旧版接口 ======================

    def step5_legacy(self, lolla_triggered: bool = False, lolla_high_count: int = 0) -> dict:
        """
        [兼容] 旧版 Step5 Layer1 风控 (保留引用)
        新流水线请使用 step5_layer0 → step6_layer1 → step7_layer2
        """
        logging.info(f"\n--- Step5: [旧版兼容] Layer1 风控 ---")
        psy_count = get_psy_hit_count()
        result = old_judge_risk_level(psy_count, lolla_triggered, lolla_high_count)
        self.ctx["step5_legacy"] = result
        self.log["step5_legacy"] = result
        return result

    # ====================== 全流程一键执行 ======================

    def run_full_pipeline(
        self,
        # Step1 参数
        trade_period: str, capital_type: str,
        total_pct: int, per_stock_pct: int, stop_loss: float,
        # Step2 参数
        up_count: int, down_count: int,
        highest_board: int, seal_rate: float, blow_rate: float, has_massacre: bool,
        # Step3 参数
        main_line: str, driver_type: str, driver_detail: str, candidate_stocks: list,
        # Layer0 外部市场数据 (可选)
        tech_data: dict = None,
        fund_data: dict = None,
        sent_data: dict = None,
        ind_data: dict = None,
        macro_data: dict = None,
        # 调度模式
        schedule_mode: str = "C",  # A=全流水线, B=盘中调整, C=单模块(默认)
    ) -> dict:
        """
        一键运行全流程 (Step0~7):
          Module01 → Module02 → Module03 → Module04
          → Layer0(采集) → Layer1(特征) → Layer2(决策)

        返回:
            {
                "trade_date",
                "pipeline_complete",
                "modules_executed": [step0, step1, ..., step7],
                "risk_level": RED/YELLOW/GREEN,
                "market_open_decision": {...},
                "layer1_composite_score": float,
                "final_psy_hit_count": int,
                "psy_hit_codes": [...],
            }
        """
        self.step0_init()
        self.step_self_heal()  # 开盘前衰减扫描

        if schedule_mode in ("A", "B"):
            # 模式A/B: 全链重跑 (含M00/M01)
            self.step1_style(trade_period, capital_type, total_pct, per_stock_pct, stop_loss)
        else:
            # 模式C: 复用M00/M01缓存, 仅从M02开始
            # M01结果仍需要 (由外部传入或缓存)
            self.step1_style(trade_period, capital_type, total_pct, per_stock_pct, stop_loss)

        # M02~M05 始终执行
        self.step2_sentiment(up_count, down_count, highest_board, seal_rate, blow_rate, has_massacre)
        self.step3_direction(main_line, driver_type, driver_detail, candidate_stocks)
        self.step4_strategy()

        # 520复盘 (新增)
        self.step_review_520()

        # 三层风控流水线
        self.step5_layer0(tech_data=tech_data, fund_data=fund_data,
                          sent_data=sent_data, ind_data=ind_data, macro_data=macro_data)
        self.step6_layer1()
        decision = self.step7_layer2()

        l1_result = self.ctx.get("layer1_result", {})

        return {
            "trade_date": self.trade_date,
            "pipeline_complete": True,
            "pipeline_version": "2.0 三层风控 + 520全嵌入",
            "schedule_mode": schedule_mode,
            "modules_executed": list(self.log.keys()),
            "risk_level": decision["risk_level"],
            "risk_label": decision["label"],
            "risk_reason": decision["reason"],
            "market_open_decision": decision,
            "layer1_composite_score": l1_result.get("composite_score", 0),
            "layer1_active_features": l1_result.get("active_features", []),
            "layer1_psy_count": l1_result.get("psy_count", 0),
            "final_psy_hit_count": get_psy_hit_count(),
            "psy_hit_codes": psy_hit_codes.copy(),
            "signal_520_weight": self.ctx.get("signal_520_weight", 1.0),
            "signal_520_review": self.ctx.get("step_review_520", {}),
            "signal_520_count": self.ctx.get("signal_520_count", {}),
        }


# ====================== Layer2 放行门禁 (硬约束) ======================

def check_layer2_gate(decision: dict = None, ctx: dict = None) -> dict:
    """
    🔒 Layer2 放行门禁 — 强制约束

    所有标的开仓指令，必须完整走完三层风控过滤。
    未经Layer2放行，禁止产生任何买入委托。

    参数:
        decision: step7_layer2 输出 (直接从pipeline获取)
        ctx: DailyPipeline.ctx (备选, 从上下文读取)

    返回:
        {
            "gate_passed": bool,       # True=放行, False=拦截
            "level": str,              # RED/YELLOW/GREEN
            "new_open_allowed": bool,  # 是否允许新开仓
            "action": str,             # 拦截消息或放行消息
            "total_cap_factor": float, # 仓位乘数
            "reason": str,             # 门禁判定理由
        }
    """
    # 从多方读取决策
    level = None
    new_open = None
    factor = None
    reason = ""

    if decision:
        level = decision.get("risk_level") or decision.get("level")
        new_open = decision.get("new_open_allowed")
        factor = decision.get("factor") or (decision.get("rule", {}).get("total_cap_factor"))
        reason = decision.get("reason", "")
    elif ctx:
        level = ctx.get("risk_level")
        new_open = ctx.get("new_open_allowed")
        factor = ctx.get("total_cap_factor")
        decision_obj = ctx.get("risk_decision", {})
        reason = decision_obj.get("reason", "")
    else:
        # 没有任何决策记录 → 硬拦截
        return {
            "gate_passed": False,
            "level": "UNKNOWN",
            "new_open_allowed": False,
            "action": "🚫 LAYER2门禁: 无风控决策记录, 拦截全部买入委托",
            "total_cap_factor": 0.0,
            "reason": "缺失三层风控流水线输出",
        }

    # 门禁判定 (硬逻辑)
    if level == "RED" or new_open is False:
        return {
            "gate_passed": False,
            "level": level or "RED",
            "new_open_allowed": False,
            "action": f"🚫 LAYER2门禁: {level}等级 — 拦截新开仓, 禁止任何买入委托",
            "total_cap_factor": factor or 0.0,
            "reason": reason,
        }

    if level == "YELLOW":
        return {
            "gate_passed": True,
            "level": "YELLOW",
            "new_open_allowed": True,
            "action": f"🟡 LAYER2门禁: YELLOW预警 — 允许新开仓, 但限仓{factor or 0.5:.0%}, 禁止加仓+禁止高弹性",
            "total_cap_factor": factor or 0.5,
            "reason": reason,
        }

    # GREEN 或未知 → 正常放行
    return {
        "gate_passed": True,
        "level": level or "GREEN",
        "new_open_allowed": True,
        "action": "🟢 LAYER2门禁: GREEN合规 — 正常放行, 执行预设交易策略",
        "total_cap_factor": factor or 1.0,
        "reason": reason,
    }


# ====================== 模块状态查询 ======================

def module_status() -> dict:
    """查询所有模块的定义状态"""
    total = len(MODULE_REGISTRY)
    defined = sum(1 for m in MODULE_REGISTRY.values() if m["defined"])
    return {
        "total_modules": total,
        "defined": defined,
        "undefined": total - defined,
        "modules": MODULE_REGISTRY,
    }


# ====================== 快速全链路测试 ======================

def run_quick_test(trade_date: str = "20260722") -> dict:
    """
    快速全链路测试 (预设参数).
    返回完整pipeline结果.
    """
    pipe = DailyPipeline(trade_date)
    result = pipe.run_full_pipeline(
        trade_period="短线3-5天", capital_type="量化轮动",
        total_pct=40, per_stock_pct=15, stop_loss=2.5,
        up_count=2800, down_count=1200, highest_board=3,
        seal_rate=55, blow_rate=22, has_massacre=False,
        main_line="固态电池", driver_type="业绩",
        driver_detail="2026H1预增262~334%",
        candidate_stocks=[
            {"code": "600884", "name": "杉杉股份", "role": "核心龙头",
             "reason": "负极材料龙头,业绩大增", "volume_ratio": 1.6},
        ],
        tech_data={
            "ma_status": "bullish", "volume_ratio": 1.6,
            "kdj_signal": "金叉", "macd_signal": "金叉",
        },
        fund_data={
            "pe_ttm": 18.5, "pb": 1.2, "roe": 12.5, "profit_growth": 35.0,
        },
        sent_data={
            "news_sentiment": 72, "guba_sentiment": 65, "research_sentiment": 80,
        },
        ind_data={
            "rsi": 62, "boll_position": 0.65,
        },
        macro_data={
            "shibor_1w": 1.8, "market_sentiment": 55, "industry_flow": "inflow",
        },
    )
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status = module_status()
        print(f"\n📋 交易智能体模块状态 (v2.0 三层风控)")
        print(f"{'='*50}")
        print(f"  总模块: {status['total_modules']}")
        print(f"  已定义: {status['defined']}")
        print(f"  未定义: {status['undefined']}")
        print()
        for name, info in status["modules"].items():
            icon = "✅" if info["defined"] else "❌"
            print(f"  {icon} {name:20s} | {info['name']:30s} | {info['file']}")
        print(f"{'='*50}")

    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        print(f"\n{'='*60}")
        print(f"  🔬 全链路测试 (v2.0 三层风控流水线)")
        print(f"{'='*60}")
        result = run_quick_test("20260722")
        print(f"\n{'='*60}")
        print(f"  ✅ 全链路测试完成")
        print(f"{'='*60}")
        print(f"  Risk Level:     {result['risk_level']}")
        print(f"  Risk Label:     {result['risk_label']}")
        print(f"  Risk Reason:    {result['risk_reason']}")
        print(f"  L1 Score:       {result['layer1_composite_score']:.4f}")
        print(f"  L1 Features:    {result['layer1_active_features']}")
        print(f"  L1 Psy Count:   {result['layer1_psy_count']}")
        print(f"  psy_hit_total:  {result['final_psy_hit_count']}条")
        print(f"  Action:         {result['market_open_decision']['action'][:60]}")
        print(f"  Module04:       {result['market_open_decision']['module04_outputs']}")
        print(f"  Module05:       {result['market_open_decision']['module05_outputs']}")
        print(f"{'='*60}")

    elif len(sys.argv) > 1 and sys.argv[1] == "legacy-test":
        # 旧版测试兼容性
        pipe = DailyPipeline("20260722")
        result = pipe.run_full_pipeline(
            trade_period="短线3-5天", capital_type="量化轮动",
            total_pct=40, per_stock_pct=15, stop_loss=2.5,
            up_count=2800, down_count=1200, highest_board=3,
            seal_rate=55, blow_rate=22, has_massacre=False,
            main_line="固态电池", driver_type="业绩",
            driver_detail="2026H1预增262~334%",
            candidate_stocks=[
                {"code": "600884", "name": "杉杉股份", "role": "核心龙头",
                 "reason": "负极材料龙头,业绩大增", "volume_ratio": 1.6},
            ],
        )
        print(f"\n✅ 旧版兼容测试完成")
        print(f"  Level: {result['risk_level']}")
        print(f"  psy_hit: {result['final_psy_hit_count']}条")
