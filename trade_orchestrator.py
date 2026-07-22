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
        self.log["step2"] = result
        return result

    def step3_direction(self, main_line: str, driver_type: str,
                        driver_detail: str, candidate_stocks: list) -> dict:
        """Step3: 定方向"""
        logging.info(f"\n--- Step3: 定方向 ---")
        active_style = self.ctx.get("active_style", "D")
        result = run_module03(main_line, driver_type, driver_detail,
                              candidate_stocks, active_style)
        self.ctx["step3"] = result
        self.ctx["selected_filtered"] = result["selected_filtered"]
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

        result = run_module04(active_style, style_name, total_cap,
                              per_stock_max, stop_loss, sentiment_label, selected)
        self.ctx["step4"] = result
        self.ctx["tradeable_pool"] = result["tradeable_pool"]
        self.log["step4"] = result
        return result

    # ====================== 三层风控流水线 (v2.0) ======================

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

        result = run_layer1(signal_output)
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
        self.step1_style(trade_period, capital_type, total_pct, per_stock_pct, stop_loss)
        self.step2_sentiment(up_count, down_count, highest_board, seal_rate, blow_rate, has_massacre)
        self.step3_direction(main_line, driver_type, driver_detail, candidate_stocks)
        self.step4_strategy()

        # 三层风控流水线
        self.step5_layer0(tech_data=tech_data, fund_data=fund_data,
                          sent_data=sent_data, ind_data=ind_data, macro_data=macro_data)
        self.step6_layer1()
        decision = self.step7_layer2()

        l1_result = self.ctx.get("layer1_result", {})

        return {
            "trade_date": self.trade_date,
            "pipeline_complete": True,
            "pipeline_version": "2.0 三层风控",
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
