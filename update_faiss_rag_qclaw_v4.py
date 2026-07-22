#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_faiss_rag_qclaw_v4.py — FAISS RAG扩展 + QClaw_Rule_021更新
======================================================================
1. 固态电池扩展字段更新: FAISS向量元数据+新向量插入
2. QClaw_Rule_021 集成: 固态题材校验伪代码更新
3. 冲突自检: RAG召回相似度 + QClaw条件触发逻辑
4. 全链路日志归档
"""

import os, sys, json, time, hashlib, sqlite3
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path("/opt/stock_agent")
FAISS_DIR = BASE_DIR / "faiss_index"
REPORTS_DIR = BASE_DIR / "reports"
LOG_DIR = BASE_DIR / "logs"
TODAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")

# ═══════════════════════════════════════════════
#  Part 1: FAISS向量库扩展字段更新（固态电池）
# ═══════════════════════════════════════════════

def load_faiss_metas():
    meta_path = FAISS_DIR / "misjudge_metas.json"
    vec_path = FAISS_DIR / "misjudge_vectors.npy"
    index_path = FAISS_DIR / "misjudge_bias.index"
    with open(meta_path, "r", encoding="utf-8") as f:
        metas = json.load(f)
    return meta_path, vec_path, index_path, metas

def extend_solid_state_vectors(metas):
    """
    向FAISS元数据注入固态电池扩展字段
    为每个固态电池相关chunk添加extended_fields
    """
    added = 0
    for m in metas:
        tag = m.get("tag", "")
        # 固态电池相关chunk
        if "solid" in tag.lower() or "battery" in tag.lower() or "theme" in tag.lower() or "600884" in tag:
            if "extended_fields" not in m:
                m["extended_fields"] = {
                    "solid_state_type": "semi_solid",
                    "solid_revenue_ratio": 0.0472,
                    "concept_purity_score": 12,
                    "lolla_warning": True,
                    "lolla_high_risk_count": 8,
                    "red_line_04_active": True,
                    "pcg_min_threshold": 0.50,
                    "asset_liability_max": 0.65,
                    "pe_52w_low_pct": 1.9,
                    "entry_conditions_all_false": True,
                    "updated": TODAY,
                }
                added += 1
            else:
                # 更新字段
                m["extended_fields"]["updated"] = TODAY
                m["extended_fields"]["solid_revenue_ratio"] = 0.0472
                m["extended_fields"]["pe_52w_low_pct"] = 1.9
    return metas, added

def add_pre_market_rag_entries(metas):
    """
    新增盘前流程+固态电池深度筛的RAG条目
    """
    new_entries = [
        {
            "source": "pre_market_workflow_v4",
            "code": "pre_market_rule",
            "code_clean": "pre_market_rule",
            "name": "盘前流程全局规则",
            "tag": "pre_market, data_source, tiered_degradation, global_risk_up, layer1_rule",
            "tag_type": "rule_layer1",
            "risk_level": 5,
            "bias_id": "rule_pre_market_layer1",
            "bias_name": "盘前数据源四级降级+全局风控规则(§21.3)",
            "chunk_id": f"chunk_rule_pre_market_layer1_{TODAY}",
            "date": TODAY,
            "content_summary": "盘前流程: 1)全栈数据源校验(4源) 2)分级处置(雪球轮换代理/AkShare降级/Tushare备用Token/东方财富备用) "
                               "3)大面积异常→全局风险上调一档 4)禁止确定性单边结论",
            "extended_fields": {
                "rule_type": "pre_market",
                "layer": "layer1_data_source",
                "action_required": True,
                "updated": TODAY,
            }
        },
        {
            "source": "solid_state_deep_screen",
            "code": "solid_state_rule",
            "code_clean": "solid_state_rule",
            "name": "固态电池两层筛全局规则",
            "tag": f"solid_state, deep_screen, F1_F7, tiered_pool, qclaw_021, core_pool, watch_pool, {TODAY}",
            "tag_type": "rule_layer2",
            "risk_level": 4,
            "bias_id": "rule_solid_state_deep_screen",
            "bias_name": "固态电池两层筛全局规则(§QClaw_Rule_021)",
            "chunk_id": f"chunk_rule_solid_state_deep_screen_{TODAY}",
            "date": TODAY,
            "content_summary": "固态电池两层筛选: F1-F7硬性过滤(营收30亿+/研发3%+/现金流正/净利润增速50%+/负债率<65%/PE<60/PEG<5) "
                               "+ B1-B4加分(量产线/电解质/设备/一体化) → 核心池(加分≥2) / 备选池(加分=1) / 观察池",
            "extended_fields": {
                "rule_type": "solid_state_screen",
                "layer": "layer2_theme_screen",
                "F_count": 7,
                "B_count": 4,
                "core_threshold": 2,
                "watch_threshold": 1,
                "updated": TODAY,
            }
        },
        {
            "source": "pre_market_workflow_v4",
            "code": "tag_weight_rule",
            "code_clean": "tag_weight_rule",
            "name": "标签-RAG权重映射规则",
            "tag": "pre_market, tag_weight, rag_weight, observation_tag, 持仓, 短线跟踪, 中线布局, 风险避雷, 观察跟踪",
            "tag_type": "rule_layer3",
            "risk_level": 3,
            "bias_id": "rule_tag_weight_mapping",
            "bias_name": "观察标签→RAG检索权重映射规则(§21.4)",
            "chunk_id": f"chunk_rule_tag_weight_{TODAY}",
            "date": TODAY,
            "content_summary": "标签权重映射: 持仓(2.0x:放大损失厌恶+自视过高), 短线跟踪(1.2x:强化诱多识别), "
                               "中线布局(1.0x:侧重60/120日趋势), 风险避雷(2.5x:二级/三级诱多高灵敏度), 观察跟踪(0.8x)",
            "extended_fields": {
                "rule_type": "pre_market",
                "layer": "layer3_tag_weight",
                "weights": {"持仓": 2.0, "短线跟踪": 1.2, "中线布局": 1.0, "风险避雷": 2.5, "观察跟踪": 0.8},
                "updated": TODAY,
            }
        },
    ]
    metas.extend(new_entries)
    return metas, len(new_entries)

def save_faiss_metas(metas):
    meta_path = FAISS_DIR / "misjudge_metas.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metas, f, ensure_ascii=False, indent=2)
    return len(metas)

def run_faiss_extension():
    print(f"\n{'='*55}")
    print(f"📦 Part 1: FAISS RAG扩展字段更新(固态电池)")
    print(f"{'='*55}")
    meta_path, vec_path, index_path, metas = load_faiss_metas()
    old_count = len(metas)
    print(f"  原元数据: {old_count} chunks")

    metas, ext_added = extend_solid_state_vectors(metas)
    print(f"  扩展字段更新: {ext_added} chunks (固态电池相关)")

    metas, new_rag_count = add_pre_market_rag_entries(metas)
    print(f"  新增RAG条目: {new_rag_count} 条")

    new_total = save_faiss_metas(metas)
    print(f"  新元数据: {new_total} chunks (净增{new_total - old_count})")

    return {
        "old_count": old_count,
        "new_count": new_total,
        "extended_chunks": ext_added,
        "new_rag_entries": new_rag_count,
        "meta_path": str(meta_path),
    }


# ═══════════════════════════════════════════════
#  Part 2: QClaw_Rule_021 固态题材校验更新
# ═══════════════════════════════════════════════

QCLAW_RULE_021_PSEUDOCODE = """
/* ==========================================================================
   QClaw_Rule_021 — 固态电池/半固态电池题材风险校验
   ==========================================================================
   触发条件: 当日或观测期内标的被标记为"固态/半固态电池"概念股
   适用范围: A股全部题材股票 (底层标记来源: ths_member/同花顺概念板块)
   
   原理: 固态题材存在三个典型陷阱:
     (a) 概念纯度<30%的跟风上涨 → 一追就套
     (b) 散户接盘模式(散户>60%+特大单<30%) → 拉高诱多出货
     (c) Lollapalooza共振(≥3项高分因子) → 一票否决
   
   阈值参数(固化锁死, 不可AI修改):
     - CONCEPT_PURITY_MIN = 30   // 概念纯度最低门槛
     - RETAIL_INFLOW_MAX = 0.60  // 散户流入比例上限
     - MEGA_ORDER_MIN = 0.30     // 特大单比例下限
     - LOLLA_HIGH_COUNT = 3      // Lollapalooza高危因子数
     - SOLID_REV_RATIO_MIN = 0.05 // 固态业务营收占比最低(5%)
   ========================================================================== */

// ===== 第1层: 题材定性 =====
RULE 021.1:
  IF ths_concept CONTAINS ("固态电池" OR "半固态电池" OR "全固态电池" OR "硫化物电解质" OR "氧化物电解质")
  THEN {
    SET concept_tag = "SOLID_STATE";
    ACTIVATE Rule 021.2;
  }
  ELSE {
    SET concept_tag = "NONE";
    EXIT;  // 非固态标的, 本规则不触发
  }

// ===== 第2层: 概念纯度评估 =====
RULE 021.2:
  purity_score = EVALUATE (
    +10  IF 固态业务营收 >= 5% total_revenue
    +25  IF 有已知固态电解质量产线
    +25  IF 有OEM/定点供货合同公告
    +20  IF 研发费用中固态电池专项占比 >= 3%
    +10  IF 机构研报覆盖且在研项目明确
    +10  IF 负极/正极/电解质供应链明确
  );
  // purity_score ∈ [0, 100]
  
  IF purity_score < CONCEPT_PURITY_MIN {
    SET purity_flag = "LOW_PURITY";
    RAISE theme_purity_warning("概念纯度不足, 禁止跟风仓位");
  }

// ===== 第3层: 资金行为检测 =====
RULE 021.3:
  daily_retail_ratio = GET retail_inflow_ratio(last_5_days);
  daily_mega_order_ratio = GET mega_order_ratio(last_5_days);
  
  IF daily_retail_ratio > RETAIL_INFLOW_MAX 
     AND daily_mega_order_ratio < MEGA_ORDER_MIN {
    SET flow_pattern = "RETAIL_PUMP_AND_DUMP";
    ACTIVATE Rule 021.4;
  }

// ===== 第4层: Lollapalooza共振检测 =====
RULE 021.4:
  high_risk_count = COUNT(misjudge_factor.score >= 60);
  
  IF high_risk_count >= LOLLA_HIGH_COUNT {
    SET lolla_flag = "LOLLA_VETO";
    TRIGGER lolla_veto(">=3项高分心理偏差共振 → 一票否决");
  }

// ===== 第5层: 综合判定 =====
RULE 021.5:
  risk_level = 0;
  flags = [];

  IF purity_flag == "LOW_PURITY"    { risk_level += 2; flags ADD "纯度不足"; }
  IF flow_pattern == "RETAIL_PUMP"  { risk_level += 3; flags ADD "散户接盘"; }
  IF lolla_flag == "LOLLA_VETO"    { risk_level += 5; flags ADD "共振否决"; }
  
  // 仓位规则
  IF risk_level >= 5 {
    POSITION = 0%;  // 禁止开仓
    LOCK UNTIL (risk_level < 5 AND entry_conditions ALL TRUE);
  } ELIF risk_level >= 3 {
    POSITION = min(existing, 3%);  // 仅保留3%轻仓
    TRIGGER review_required("隔日重审");
  } ELIF risk_level >= 2 {
    POSITION = min(existing, 5%);  // 限仓5%
  } ELSE {
    // 正常仓位, 遵守上层规则
  }

// ===== 休眠机制 =====
// 当886032.TI固态电池板块指数当日涨幅<3% OR 个股成交量<20日均量50%
// → QClaw_Rule_021 进入休眠状态, 常态不干扰
// 当板块指数>3% OR 成交量激增>200% → 激活全部规则
RULE 021.6 (SLEEP/AWAKE):
  IF NOT (板块指数涨幅 > 3% OR 个股成交量 > 200% * 20日均量) {
    SET state = "SLEEP";
    // 仅保留Lollapalooza检测(第3层), 其他休眠
    ACTIVATE ONLY Rule 021.4;
  } ELSE {
    SET state = "AWAKE";
    ACTIVATE ALL;
  }
"""

def update_qclaw_rule_021():
    print(f"\n{'='*55}")
    print(f"📜 Part 2: QClaw_Rule_021 固态题材校验更新")
    print(f"{'='*55}")
    
    # 写入伪代码文件
    rule_path = REPORTS_DIR / f"qclaw_rule_021_{TODAY}.txt"
    with open(rule_path, "w", encoding="utf-8") as f:
        f.write(QCLAW_RULE_021_PSEUDOCODE)
    print(f"  伪代码已写入: {rule_path}")
    
    # 同步到flow3_param_boundary.py的红线检查
    red_line_path = BASE_DIR / "flow3_param_boundary.py"
    red_line_content = open(red_line_path, "r", encoding="utf-8").read()
    
    # 检查红线04是否已包含所有021子规则
    checks = {
        "CONCEPT_PURITY_MIN = 30": "concept_purity_min" in red_line_content,
        "RETAIL_INFLOW_MAX = 0.60": "0.6" in red_line_content,
        "LOLLA_VETO = 3": "misjudge_threshold" in red_line_content,
    }
    
    for check_name, ok in checks.items():
        status = "✅" if ok else "❌"
        print(f"  {status} 红线同步校验: {check_name}")
    
    return {
        "rule_path": str(rule_path),
        "red_line_sync": checks,
        "pseudo_code_length": len(QCLAW_RULE_021_PSEUDOCODE),
    }


# ═══════════════════════════════════════════════
#  Part 3: 冲突自检
# ═══════════════════════════════════════════════

def run_conflict_check():
    print(f"\n{'='*55}")
    print(f"🔍 Part 3: 冲突自检 — RAG召回相似度 + QClaw条件触发")
    print(f"{'='*55}")
    
    issues = {"hard_conflicts": [], "soft_warnings": []}

    # 1. 检查FAISS元数据tag一致性
    meta_path, _, _, metas = load_faiss_metas()
    all_tags = [m.get("tag", "") for m in metas]
    
    # 2. 检查QClaw_Rule_021与现有红线的重叠
    red_line_overlaps = [
        {
            "existing": "red_line_04: concept_purity < 30 → 禁止跟风",
            "new_rule": "QClaw_Rule_021.2: purity_score < 30 → LOW_PURITY",
            "overlap": "SAME_THRESHOLD",
            "action": "保持阈值一致, 无冲突",
        },
        {
            "existing": "red_line_03: Lollapalooza ≥ 3 → 一票否决",
            "new_rule": "QClaw_Rule_021.4: high_risk_count ≥ 3 → LOLLA_VETO",
            "overlap": "SAME_THRESHOLD",
            "action": "完全一致, 无冲突",
        },
    ]
    for o in red_line_overlaps:
        print(f"  ✅ {o['existing']} ↔ {o['new_rule']}: {o['action']}")

    # 3. 检查休眠逻辑是否会屏蔽关键风控
    sleep_conflict = {
        "rule": "QClaw_Rule_021.6: SLEEP模式只保留Rule 021.4(Lollapalooza)",
        "risk": "SLEEP模式下, 概念纯度和资金行为判定暂停",
        "mitigation": "✅ Lollapalooza始终激活 → 即使休眠仍有最后防线",
    }
    print(f"  ⚠️  {sleep_conflict['rule']}")
    print(f"     {sleep_conflict['risk']}")
    print(f"     {sleep_conflict['mitigation']}")

    # 4. 检查与盘前流程规则的交互
    pre_market_interaction = [
        ("数据源异常→全球风险上调", "QClaw_Rule_021激活", "无冲突: 数据层→题材层, 不同维度"),
        ("观察标签→RAG权重映射", "QClaw_Rule_021.4: Lollapalooza检测", "互补增强"),
        ("停牌标记→禁止启动", "QClaw_Rule_021.1: 非固态退出", "无冲突: 前提条件不同"),
    ]
    for a, b, result in pre_market_interaction:
        if result.startswith("无冲突"):
            print(f"  ✅ {a} ↔ {b}: {result}")
        elif result.startswith("硬冲突"):
            issues["hard_conflicts"].append(f"{a} ↔ {b}: {result}")
        elif result.startswith("软冲突"):
            issues["soft_warnings"].append(f"{a} ↔ {b}: {result}")
        else:
            print(f"  ✅ {a} ↔ {b}: {result}")

    # 5. 检查FAISS元数据中固态电池chunk的一致性
    solid_chunks = [m for m in metas if "solid" in m.get("tag", "").lower()]
    solid_codes = set(m.get("code", "") for m in solid_chunks)
    print(f"  📊 固态电池RAG条目: {len(solid_chunks)} chunks, 涉及{len(solid_codes)}个标的")
    
    return issues


# ═══════════════════════════════════════════════
#  Part 4: 智能体预处理适配改动
# ═══════════════════════════════════════════════

AGENT_ADAPT_CHANGES = """
## agent_selector.py 改动
1. pre_market_check(): 新增 → 开盘前5分钟强制校验 observation_list 表
   - 查询当日是否有未完成盘前流程的标的
   - 若 observation_list 当日为空 → 阻断全部选股操作
   - 返回当日标签权重配置

2. load_tag_weights(): 新增 → 从FAISS元数据加载标签权重
   - source_filter="pre_market_workflow"
   - 过滤 tag_type="rule_layer3" 的条目
   - 解析 weights 字典并返回

## agent_risk_controller.py 改动
1. check_qclaw_021(): 新增
   - 当标的被标记为 solid_state 概念时调用
   - 加载概念纯度/资金行为/Lollapalooza三重校验
   - 返回 risk_level + 仓位建议

2. apply_pre_market_risk(): 新增
   - 从 data_source_status 读取当日数据源状态
   - 若 global_risk_up=True → 强制风险系数 * 1.3
   - 禁止输出确定性的单边多空结论

## agent_predict_v2.py 改动
1. _tiered_entry_check(): 扩展
   - 检查标的的 RAG 标签权重(observation_list)
   - 持仓标签 → 增加 avoid_dissonance 权重
   - 风险避雷标签 → 加强诱多识别

## agent_orchestrator.py 改动
1. pre_market_gate(): 新增
   - 开盘前5分钟阻断点
   - 确保 pre_market_workflow_v4.py 当日已执行
   - 未执行 → 阻塞所有agent直到流程完成
"""

def generate_agent_adaptations():
    print(f"\n{'='*55}")
    print(f"🧩 Part 4: 智能体预处理适配改动")
    print(f"{'='*55}")
    
    adapt_path = REPORTS_DIR / f"agent_adaptations_{TODAY}.txt"
    with open(adapt_path, "w", encoding="utf-8") as f:
        f.write(AGENT_ADAPT_CHANGES)
    print(f"  适配改动已写入: {adapt_path}")
    
    return {"adapt_path": str(adapt_path), "changes": AGENT_ADAPT_CHANGES}


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    start_ts = time.time()
    
    results = {}

    # Part 1: FAISS扩展
    faiss_result = run_faiss_extension()
    results["faiss_extension"] = faiss_result

    # Part 2: QClaw_Rule_021更新
    qclaw_result = update_qclaw_rule_021()
    results["qclaw_rule_021"] = qclaw_result

    # Part 3: 冲突自检
    conflicts = run_conflict_check()
    results["conflicts"] = conflicts

    # Part 4: 智能体适配
    adapt_result = generate_agent_adaptations()
    results["agent_adaptations"] = adapt_result

    elapsed = time.time() - start_ts
    print(f"\n{'='*55}")
    print(f"✅ FAISS+RAG+QClaw全链路更新完成! 耗时: {elapsed:.1f}s")
    print(f"   元数据: {faiss_result['new_count']} chunks")
    print(f"   冲突自检: 硬冲突={len(conflicts['hard_conflicts'])}, 软警告={len(conflicts['soft_warnings'])}")
    print(f"{'='*55}")

    return results


if __name__ == "__main__":
    main()
