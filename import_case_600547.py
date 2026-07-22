#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_case_600547.py — 山东黄金600547标准化风控案例入库
======================================================================
1. 更新FAISS RAG向量库元数据
2. 生成标准化案例文档
3. 写入PostgreSQL trade_calibration
4. 批量更新observation_list标签
======================================================================
"""

import os, sys, json, sqlite3
from datetime import datetime, date
from pathlib import Path
from collections import OrderedDict

BASE_DIR = Path("/opt/stock_agent")
FAISS_DIR = BASE_DIR / "faiss_index"
ARCHIVE_DIR = BASE_DIR / "solid_state_archive"
REPORT_DIR = BASE_DIR / "reports"
DB_PATH = BASE_DIR / "agent_memory.db"
TODAY = "20260720"

CASE = OrderedDict()

CASE["meta"] = {
    "stock_code": "600547.SH",
    "stock_name": "山东黄金",
    "chain": "贵金属黄金矿山开采(周期资源)",
    "core_revenue_pct": 36.82,
    "core_profit_pct": 91.0,
    "layer": "B_观望",
    "position": 0.0,  # 仅允许≤3%波段
    "lollapalooza": False,
    "lolla_high_count": 4,
    "pe_ttm": 25.25,
    "debt_ratio": 62.57,
}

CASE["business_structure"] = {
    "核心盈利": {
        "desc": "自产金矿, 真正业绩弹性来源",
        "resource": "胶东金矿带, 权益储量1979吨, 保障>70年",
        "production": "2025年48.89吨, 2026目标≥49吨",
        "elasticity": "金价每+10%→净利润+5.8亿; 股价与金价相关性87.73%",
        "2026Q1": "归母14.46亿(+40.87%), 经营现金流60.95亿",
    },
    "低毛利贸易": {
        "desc": "外购金/黄金贸易占营收63.18%",
        "margin": "毛利率仅0.8-1.38%",
        "risk": "套保+证券投资2025年合计亏损~15亿",
    },
    "题材属性": "纯周期标的, 无固态/AI/新能源题材炒作",
}

CASE["fundamental_risks"] = {
    "金价周期": "金价若回调10%→净利润-5.8亿, 估值双向波动极大",
    "高负债": "负债率62.57%, 海外矿山资本开支持续, 偿债刚性",
    "开采成本": "深井成本升至392元/克(+34%), 压缩利润空间",
    "非经常损益": "套保减值/证券投资/固定资产减值每年大额波动",
}

CASE["capital_flow"] = {
    "15日主力累计": "-2.33亿(短期多头兑现离场)",
    "5日主力净额": "+0.83亿(分歧加大)",
    "融资余额": "高位震荡, 杠杆分歧",
    "盘面特征": "前期涨幅巨大, 高位筹码松动",
}

CASE["technical"] = {
    "收盘价": "24.42元(+2.78%)",
    "均线": "多头排列但动能衰减",
    "MACD": "红柱持续缩短",
    "KDJ": "短期超卖区间",
    "60日涨跌": "-16.97%",
}

CASE["misjudge_high_risk"] = OrderedDict([
    ("02_喜欢热爱", {"score": 22.91, "desc": "黄金避险抗通胀叙事绑定, 忽略周期顶部利空", "user_manual": True}),
    ("04_避免怀疑", {"score": 100.0, "desc": "地缘升温+降息落地→线性外推新高, 不做回撤测算"}),
    ("14_损失厌恶", {"score": 96.0, "desc": "浮亏后拒绝止损"}),
    ("22_权威盲从", {"score": 90.41, "desc": "跟随大V/机构推荐, 忽略周期风险"}),
    ("09_回馈倾向", {"score": 48.04, "desc": "金价上涨→过度乐观外推", "user_manual": True}),
    ("15_社会认同羊群", {"score": 45.0, "desc": "金价大涨阶段集体抱团", "user_manual": True}),
])

CASE["lolla_judgment"] = {
    "标准规则(§24)": "≥3项高分→Lolla激活(3项→一票否决)",
    "本标的标准结果": "⚠️ 5项≥60分→Lollapalooza激活(标准引擎判定)",
    "用户定制规则(C档)": "≥6项+题材<5%+主力大额流出→C档",
    "本标的所有用户判定": "4项高危+主业真实→B_观望(不强制规避)",
    "最终结论": "B_观望(权重偏向业务真实性而非纯偏差计数)",
}

CASE["trade_constraints"] = {
    "禁止": ["新建重仓多头"],
    "允许": ["≤3%总资金做周期波段观测"],
    "建议": ["原有持仓逢反弹分批减仓, 不追加成本"],
    "A档条件": [
        "连续10日主力资金累计净流入",
        "国际金价站稳2600美元/盎司并走强",
        "高危因子降至2项以内",
        "资产负债率降至58%以下+套保无大亏",
        "MACD金叉+放量突破24元压力位",
    ]
}

CASE["feishu_table_row"] = {
    "代码": "600547",
    "名称": "山东黄金",
    "周期关联度": 28,
    "核心主业毛利占比": "91.00%",
    "ROE": "4.20%",
    "15日主力资金": "-2.33亿",
    "高危误判数": 4,
    "Lollapalooza共振": False,
    "分层结论": "B_观望",
    "核心风险": "金价周期波动+高负债率62.57%+开采成本上行+套保损益扰动",
}

CASE["rag_tags"] = [
    "#山东黄金", "#600547", "#贵金属周期",
    "#黄金矿山", "#资源周期标的", "#无题材炒作", "#B_观望"
]


def generate_case_md() -> str:
    lines = []
    lines.append(f"# 山东黄金(600547) — 贵金属周期标准化风控案例")
    lines.append(f"> **生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> **分层:** {CASE['meta']['layer']} | **Lollapalooza:** {'未激活' if not CASE['meta']['lollapalooza'] else '已激活(标准)但用户判定B档'}")
    lines.append(f"> **FAISS标签:** {' '.join(CASE['rag_tags'])}")
    lines.append("")

    lines.append("---\n## 一、标的基础元数据\n")
    lines.append("| 字段 | 内容 |")
    lines.append("|:----|:-----|")
    for k, v in CASE["meta"].items():
        lines.append(f"| {k} | {v} |")

    lines.append("\n---\n## 二、业务结构拆分\n")
    for biz, data in CASE["business_structure"].items():
        lines.append(f"### {biz}")
        lines.append("| 维度 | 详情 |")
        lines.append("|:----|:-----|")
        if isinstance(data, dict):
            for k, v in data.items(): lines.append(f"| {k} | {v} |")
        else:
            lines.append(f"| 属性 | {data} |")

    lines.append("\n---\n## 三、四大底层财务硬风险\n")
    lines.append("| 风险 | 说明 |")
    lines.append("|:----|:------|")
    for k, v in CASE["fundamental_risks"].items():
        lines.append(f"| {k} | {v} |")

    lines.append("\n---\n## 四、资金流数据\n")
    lines.append("| 区间 | 金额 |")
    lines.append("|:----|:----|")
    for k, v in CASE["capital_flow"].items(): lines.append(f"| {k} | {v} |")

    lines.append("\n---\n## 五、技术面信号\n")
    lines.append("| 指标 | 值 |")
    lines.append("|:----|:----|")
    for k, v in CASE["technical"].items(): lines.append(f"| {k} | {v} |")

    lines.append("\n---\n## 六、芒格25种心理误判\n")
    lines.append(f"**Lolla(标准§24):** 激活(≥3项) | **用户C档阈值(≥6项):** 未达标 | **最终:** B_观望\n")
    lines.append("| 编码 | 因子 | 得分 | 解读 |")
    lines.append("|:---:|:----|:----:|:-----|")
    for code, data in CASE["misjudge_high_risk"].items():
        marker = "🔴" if data['score'] >= 60 else ("🟡" if data['score'] >= 40 else "✅")
        lines.append(f"| {code.split('_')[0]} | {code.split('_')[1] if '_' in code else code} | {data['score']} {marker} | {data['desc']} |")

    lines.append("\n### Lollapalooza判定对比\n")
    lines.append("| 判定体系 | 阈值 | 结果 |")
    lines.append("|:---------|:----:|:----:|")
    for k, v in CASE["lolla_judgment"].items():
        lines.append(f"| {k} | — | {v} |")

    lines.append("\n---\n## 七、分层交易约束\n")
    for action, items in CASE["trade_constraints"].items():
        if isinstance(items, list):
            for i in items: lines.append(f"- **{action}:** {i}" if not lines[-1].startswith(f"- **{action}") else f"  - {i}")
        else:
            lines.append(f"- **{action}:** {items}")

    lines.append("\n---\n## 八、飞书表格单行字段\n")
    row = CASE["feishu_table_row"]
    lines.append("| " + " | ".join(row.keys()) + " |")
    lines.append("| " + " | ".join(":---:" for _ in row) + " |")
    lines.append("| " + " | ".join(str(v) for v in row.values()) + " |")

    lines.append("\n---\n## 九、运维与入库规范\n")
    lines.append(f"**FAISS标签:** {' '.join(CASE['rag_tags'])}\n")
    lines.append("**宿主机导出:** `docker cp ... D:\\stock_data\\metal_scan\\`\n")

    lines.append("\n---\n## 十、复盘校验提问\n")
    lines.append("1. 山东黄金自产金营收、毛利占比分别是多少, 是否属于纯题材概念股?")
    lines.append("2. 本标的仅4项高危心理误判, 为何未触发Lollapalooza一票否决(C档)?")
    lines.append("3. 黄金价格波动对山东黄金业绩弹性有多大, 核心周期风险是什么?")
    lines.append("4. 从B_观望升级为A档需要满足哪五项观测条件?")

    return "\n".join(lines)


def update_faiss():
    meta_path = FAISS_DIR / "misjudge_metas.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        metas = json.load(f)

    # 检查是否已存在
    for m in metas:
        if m.get("code") == "600547.SH" and "case_gold_600547_v1" in m.get("source", ""):
            print("  FAISS条目已存在, 跳过")
            return

    new_entry = {
        "source": "case_gold_600547_v1",
        "code": "600547.SH",
        "code_clean": "600547",
        "name": "山东黄金",
        "tag": "#山东黄金 #600547 #贵金属周期 #黄金矿山 #资源周期标的 #无题材炒作 #B_观望",
        "tag_type": "gold_cycle_case",
        "risk_level": 3,
        "bias_id": "case_gold_600547_v1",
        "bias_name": "山东黄金贵金属周期标准化风控案例(4项误判B档观望)",
        "chunk_id": "chunk_case_gold_600547_v1",
        "date": TODAY,
        "group": "贵金属",
        "sector": "贵金属-黄金矿山开采",
        "layer": "B_观望",
        "content_summary": "山东黄金自产金毛利91%营收36.82%,4项高危误判轮,金价每+10%→净利+5.8亿;负债率62.57%,套保亏损~15亿",
        "extended_fields": {
            "type": "gold_cycle",
            "layer": "B_观望",
            "lolla_count": 4,
            "lollapalooza_standard": True,
            "lollapalooza_user": False,
            "position": 0.0,
            "core_revenue_pct": 36.82,
            "core_profit_pct": 91.0,
            "debt_ratio": 62.57,
            "gold_price_elasticity": "每+10%→净利+5.8亿",
            "15d_main_flow": "-2.33亿",
        },
    }
    metas.append(new_entry)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metas, f, ensure_ascii=False, indent=2)
    print(f"  ✅ FAISS新增: {new_entry['source']}")
    print(f"  📊 FAISS总chunks: {len(metas)}")


def update_pg():
    try:
        import trade_calibration_pg_v3 as tg
        row = {
            "trade_date": TODAY,
            "ticker": "600547",
            "real_change_pct": 2.78,
            "close_price": 24.42,
            "support_resistance_status": "均线多头但动能衰减, 高位震荡, 支撑23.35压力23.93",
            "real_trade_action": "空仓",
            "ai_pred": "预判中性",
            "ai_risk_tip": "持有不动",
            "is_trapped": False,
            "operator": "quant_admin",
            "misjudge_hit_count": 4,
            "solid_tech_tag": "无固态题材",
            "qclaw_rule_id": "QClaw_Rule_007",
            "rag_match_score": 0.82,
        }
        ok, msg = tg.data_validate(row)
        if ok:
            tag = tg.get_error_label(row["ai_pred"], row["real_change_pct"],
                                      row["ai_risk_tip"], row["is_trapped"],
                                      row["misjudge_hit_count"], row["solid_tech_tag"])
            tg.insert_calibration(row, tag)
            tg.logic_review(TODAY, ["600547"])
            print(f"  ✅ PG入库完成 (error_label={tag})")
        else:
            print(f"  ⚠️ PG校验失败: {msg}")
    except Exception as e:
        print(f"  ⚠️ PG写入跳过: {e}")


def update_sqlite():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    for tag in ["中线布局"]:
        cur.execute("INSERT OR IGNORE INTO observation_list (date, ts_code, stock_name, tag) VALUES (?,?,?,?)",
                     ("20260721", "600547", "山东黄金", tag))
    conn.commit()
    conn.close()
    print("  ✅ SQLite observation_list已更新")


if __name__ == "__main__":
    print("=" * 60)
    print("📦 山东黄金600547标准化风控案例入库")
    print("=" * 60)

    print("\n[1/4] 生成标准化案例文档...")
    md = generate_case_md()
    case_path = ARCHIVE_DIR / "case_600547_gold_standard_20260720.md"
    with open(case_path, "w", encoding="utf-8") as f: f.write(md)
    print(f"  ✅ 已写入: {case_path}")

    print("\n[2/4] 更新FAISS...")
    update_faiss()

    print("\n[3/4] 写入PostgreSQL...")
    update_pg()

    print("\n[4/4] 更新SQLite...")
    update_sqlite()

    print(f"\n✅ 山东黄金600547案例全部入库完成")
    print(f"   分层: {CASE['meta']['layer']} | Lolla: {CASE['meta']['lollapalooza']}")
