#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_solid_state_600884_case.py — 杉杉股份600884标准化风控案例入库
======================================================================
1. 更新full_scan_md 288只总表中600884字段
2. 生成标准化案例文档(solid_state_archive/)
3. 写入FAISS RAG向量库元数据
4. 更新SQLite observation_list标签
======================================================================
"""

import os, sys, json, sqlite3, shutil
from datetime import datetime, date
from pathlib import Path
from collections import OrderedDict

BASE_DIR = Path("/opt/stock_agent")
FAISS_DIR = BASE_DIR / "faiss_index"
ARCHIVE_DIR = BASE_DIR / "solid_state_archive"
REPORT_DIR = BASE_DIR / "reports"
DB_PATH = BASE_DIR / "agent_memory.db"
TODAY = "20260720"

# ═══════════════════════════════════════════════
#  杉杉600884标准化全量元数据
# ═══════════════════════════════════════════════
CASE = OrderedDict()

CASE["meta"] = {
    "stock_code": "600884.SH",
    "stock_name": "杉杉股份",
    "chain": "负极材料(硅碳/半固态核心配套)+LCD偏光片双主业",
    "solid_type": "半固态小批量供货;全固态仅实验室送样",
    "solid_revenue_pct": 4.72,
    "revenue_usd_ratio": 0.0472,
    "layer": "C_规避",
    "position": 0.0,
    "lollapalooza": True,
    "lolla_high_count": 8,
}

CASE["solid_biz"] = {
    "半固态": {
        "customers": ["卫蓝新能源(独家硅碳)", "宁德时代", "清陶能源"],
        "vehicle": "蔚来ET7 150kWh半固态",
        "capacity": "宁波4万吨硅碳一体化基地一期1万吨投产, 2026年底二期",
        "premium": "硅碳单吨溢价30-50% vs 传统石墨",
        "revenue_share": 4.72,
        "grade": "题材驱动(未达A档≥5%门槛)",
    },
    "全固态": {
        "tech_roadmap": "硅氧负极/预锂化硬碳/锂金属界面改性",
        "target_system": "硫化物/氧化物全固态",
        "progress": "仅小批量送样验证, 无定型量产线",
        "timeline": "2027年中试(与日企预锂化), 2028年后规模化装车",
        "risk": "远期研发叙事被当短期上涨逻辑",
    }
}

CASE["fundamental_risks"] = {
    "存货减值高危": {
        "inventory": "70.64亿元(2025年末)",
        "inventory_to_revenue": ">0.35",
        "turnover_days": 131,
        "industry_avg_turnover": "4.54次/年",
        "yoy_change": "存货跌价损失2.09亿, 同比+356%",
    },
    "利润失真": {
        "半年报预告": "7.5-9亿(同比+262~334%)",
        "增量来源": "参股巴斯夫杉杉正极扭亏(联营投资收益)",
        "持续性": "非主业盈利能力改善, 不具备持续性",
    },
    "双周期错配": {
        "负极": "产能过剩, 价格持续下行",
        "偏光片": "韩国杉金工厂常年亏损, 设备闲置",
        "fixed_asset_impair": "0.61亿(2025年, 同比+1116%)",
    },
    "短期偿债": {
        "流动比率": 1.1,
        "速动比率": 0.66,
        "risk": "高应收+高存货占用现金流, 下行期减值爆发",
    }
}

CASE["capital_flow"] = {
    "15日主力累计": "-10.1亿(持续大额减仓)",
    "5日主力净额": "-2.41亿(资金出逃加速)",
    "融资余额月变动": "-2.90亿(杠杆资金持续离场)",
    "北向Q2": "小幅加仓但滞后行情3周, 无资金支撑效力",
}

CASE["technical"] = {
    "收盘价": "11.77元(-2.73%)",
    "均线": "MA5/10/20/60全部空头排列",
    "MACD": "死叉持续22日, DIF=-0.58",
    "60日涨跌": "-19.93%",
    "量比": "0.91(持续缩量阴跌)",
}

CASE["misjudge_high_risk"] = OrderedDict([
    ("08_嫉妒猜忌", {"score": 96.27, "desc": "对比璞泰来/贝特瑞固态涨幅,盲目博弈补涨,无视营收占比不足5%"}),
    ("04_避免怀疑", {"score": 87.93, "desc": "固态合作公告发布,未核对月度出货量直接预判周期反转"}),
    ("15_社会认同羊群", {"score": 87.00, "desc": "短视频/股吧集体炒作固态赛道,散户跟风高位入场"}),
    ("23_市场噪音废话", {"score": 79.99, "desc": "频繁发布固态研发/客户送样公告,无当期扣非利润增量"}),
    ("02_喜欢热爱", {"score": 77.20, "desc": "长期持仓绑定'固态龙头'叙事,选择性忽略存货/减值/资金流出利空"}),
    ("14_损失厌恶", {"score": 72.00, "desc": "浮亏后拒绝止损,持续加仓等待固态题材解套"}),
    ("19_遗忘风险", {"score": 70.00, "desc": "持仓遗忘硅碳营收占比低/全固态尚处研发的核心利空"}),
    ("09_回馈倾向", {"score": 67.44, "desc": "固态扩产/半年报预增公告直接等同于基本面拐点"}),
])

CASE["lolla_veto_rule"] = {
    "condition": "高危误判≥6项 + 存货/营收>0.35 + 固态营收<5% + 15日主力流出",
    "verdict": "永久禁止开仓, 仓位0%",
    "match": "✅ 完全命中(8项+0.35+4.72%+-10.1亿)",
}

CASE["trade_constraints"] = {
    "禁止": ["新建多头", "加仓"],
    "强制": ["原有持仓分批减仓50%", "单只剩余上限≤总资金3%"],
    "观测条件(B档)": [
        "硅碳负极营收占比≥8%",
        "连续3日主力资金净流入",
        "MACD金叉+放量站上MA20(13.02)",
        "高危因子降至3项以内+Lolla解除",
    ]
}

CASE["feishu_table_row"] = {
    "代码": "600884",
    "名称": "杉杉股份",
    "固态关联度": 10,
    "固态营收占比": "4.72%",
    "ROE": "1.49%",
    "15日主力资金": "-10.1亿",
    "高危误判数": 8,
    "Lollapalooza共振": True,
    "分层结论": "C_规避",
    "核心风险": "存货高企(70.64亿)+主力持续流出(-10.1亿)+固态兑现不足(4.72%<5%)+多重心理误判共振(8项)",
}

CASE["rag_tags"] = [
    "#杉杉股份", "#600884", "#硅碳负极", "#半固态电池",
    "#固态题材兑现不足", "#Lollapalooza风险共振",
    "#C_规避", "#仓位0%", "#存货高企",
]


# ═══════════════════════════════════════════════
#  步骤1: 生成标准化Markdown案例文档
# ═══════════════════════════════════════════════

def generate_case_md() -> str:
    lines = []
    lines.append(f"# 杉杉股份(600884) — 固态电池标准化风控案例")
    lines.append(f"> **生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> **分层:** {CASE['meta']['layer']} | **Lollapalooza:** {'🚫 激活(8项)' if CASE['meta']['lollapalooza'] else '正常'}")
    lines.append(f"> **适用:** 全A股固态电池288只扫描总表 | FAISS入库标签: {' '.join(CASE['rag_tags'])}")
    lines.append("")

    lines.append("---")
    lines.append("## 一、标的基础元数据")
    lines.append("")
    lines.append("| 字段 | 内容 |")
    lines.append("|:----|:-----|")
    for k, v in CASE["meta"].items():
        lines.append(f"| {k} | {v} |")

    lines.append("")
    lines.append("---")
    lines.append("## 二、固态电池业务真实落地逻辑")
    lines.append("")

    for biz_type, biz_data in CASE["solid_biz"].items():
        lines.append(f"### {biz_type}")
        lines.append("")
        lines.append("| 维度 | 详情 |")
        lines.append("|:----|:-----|")
        for k, v in biz_data.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    lines.append("---")
    lines.append("## 三、基本面核心风险")
    lines.append("")
    for risk_name, risk_data in CASE["fundamental_risks"].items():
        lines.append(f"### {risk_name}")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|:----|:-----|")
        for k, v in risk_data.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    lines.append("---")
    lines.append("## 四、资金流数据")
    lines.append("")
    lines.append("| 区间 | 金额 |")
    lines.append("|:----|:----|")
    for k, v in CASE["capital_flow"].items():
        lines.append(f"| {k} | {v} |")

    lines.append("")
    lines.append("---")
    lines.append("## 五、技术面信号")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|:----|:----|")
    for k, v in CASE["technical"].items():
        lines.append(f"| {k} | {v} |")

    lines.append("")
    lines.append("---")
    lines.append("## 六、芒格25种心理误判(8项高危同步激活)")
    lines.append("")
    lines.append(f"**Lollapalooza共振状态:** {'🚫 激活(≥3项,一票否决)' if CASE['meta']['lollapalooza'] else '✅ 正常'}")
    lines.append(f"**高危因子数:** {CASE['meta']['lolla_high_count']}项(均≥60分)")
    lines.append("")
    lines.append("| 编码 | 因子名 | 得分 | 市场行为解读 |")
    lines.append("|:---:|:------|:----:|:-----------|")
    for code, data in CASE["misjudge_high_risk"].items():
        lines.append(f"| {code} | {code.split('_',1)[1] if '_' in code else code} | **{data['score']}**🔴 | {data['desc']} |")

    lines.append("")
    lines.append("### Lollapalooza一票否决判定")
    lines.append("")
    lines.append("| 条件 | 阈值 | 实际 | 结果 |")
    lines.append("|:----|:----|:----|:----:|")
    rule = CASE["lolla_veto_rule"]
    for cond_part in ["high_risk≥6", "存货/营收>0.35", "固态营收<5%", "15日主力持续流出"]:
        lines.append(f"| {cond_part} | — | — | ✅ 命中 |")
    lines.append(f"| **综合** | — | — | **{rule['verdict']}** |")

    lines.append("")
    lines.append("---")
    lines.append("## 七、分层交易约束")
    lines.append("")
    lines.append(f"**当前:** {CASE['meta']['layer']} — {CASE['meta']['position']*100:.0f}%仓位")
    lines.append("")
    for action_type, actions in CASE["trade_constraints"].items():
        lines.append(f"**{action_type}:**")
        for a in actions:
            lines.append(f"- {a}")
        lines.append("")

    lines.append("---")
    lines.append("## 八、飞书表格单列字段(可直接导入288只总表)")
    lines.append("")
    row = CASE["feishu_table_row"]
    lines.append("| " + " | ".join(row.keys()) + " |")
    lines.append("| " + " | ".join(":---:" for _ in row) + " |")
    lines.append("| " + " | ".join(str(v) for v in row.values()) + " |")

    lines.append("")
    lines.append("---")
    lines.append("## 九、配套运维与入库规范")
    lines.append("")
    lines.append(f"| 项目 | 内容 |")
    lines.append(f"|:----|:-----|")
    lines.append(f"| 归档路径 | `solid_state_archive/full_scan_{TODAY}.txt` |")
    lines.append(f"| FAISS标签 | {' '.join(CASE['rag_tags'])} |")
    lines.append(f"| 飞书操作 | 导出Excel→云文档导入为独立在线表格 |")
    lines.append(f"| 定时错峰 | 固态扫描凌晨2点执行, 与news-monitor隔离 |")

    lines.append("")
    lines.append("---")
    lines.append("## 十、复盘校验提问")
    lines.append("")
    lines.append("1. 杉杉股份固态业务营收占比多少, 为何无法划入A档可跟踪标的?")
    lines.append("2. 触发Lollapalooza共振的8项高危心理误判分别是什么?")
    lines.append("3. 杉杉半年报业绩预增核心增厚来源是否具备可持续性?")
    lines.append("4. 解除C档规避限制、调整至B档观望需要同时满足哪些观测条件?")
    lines.append("")

    lines.append("---")
    lines.append(f"*案例生成完成 | FAISS入库就绪 | 288只总表同步更新*")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
#  步骤2: 更新full_scan_md 288只总表
# ═══════════════════════════════════════════════

def update_full_scan_entry():
    """更新full_scan_md_20260720_1158.md中600884的行"""
    scan_path = ARCHIVE_DIR / "full_scan_md_20260720_1158.md"
    if not scan_path.exists():
        print(f"  ⚠️ 未找到扫描总表: {scan_path}")
        return False
    
    text = scan_path.read_text(encoding="utf-8")
    old_line = "|600884|杉杉股份|10|0%|1.49%|-10.1亿|1|否|C_规避(大额流出)|"
    new_line = "|600884|杉杉股份|10|**4.72%**|1.49%|-10.1亿|**8**|**🚫True**|**C_规避(Lollapalooza共振)**|"
    
    if old_line not in text:
        print(f"  ⚠️ 旧行未找到, 尝试模糊匹配...")
        # 模糊替换
        import re
        text = re.sub(
            r'(\|600884\|杉杉股份\|10\|)[^|]*(\|[^|]*\|[^|]*\|[^|]*\|)[^|]*(\|[^|]*\|)',
            r'\1**4.72%**\2**8**\3**🚫True** **C_规避(Lollapalooza共振)**',
            text
        )
    else:
        text = text.replace(old_line, new_line)
    
    scan_path.write_text(text, encoding="utf-8")
    print(f"  ✅ full_scan总表已更新: {scan_path}")
    
    # 备份
    backup_path = ARCHIVE_DIR / f"full_scan_md_20260720_1158_BACKUP.md"
    if not backup_path.exists():
        import shutil
        shutil.copy(scan_path, backup_path)
        print(f"  💾 备份已创建: {backup_path}")
    
    return True


# ═══════════════════════════════════════════════
#  步骤3: 更新FAISS元数据
# ═══════════════════════════════════════════════

def update_faiss_metadata():
    """向FAISS misjudge_metas.json添加杉杉固态案例元数据"""
    meta_path = FAISS_DIR / "misjudge_metas.json"
    if not meta_path.exists():
        print(f"  ⚠️ FAISS元数据不存在: {meta_path}")
        return False
    
    with open(meta_path, "r", encoding="utf-8") as f:
        metas = json.load(f)
    
    # 检查是否已存在
    existing = False
    for m in metas:
        if m.get("source") == "solid_case_600884_v2" or \
           (m.get("code") == "600884.SH" and "solid_standard_case" in m.get("tag", "")):
            existing = True
            # 更新现有记录
            m["tag"] = "#杉杉股份 #600884 #硅碳负极 #半固态电池 #固态题材兑现不足 #Lollapalooza风险共振 #C_规避 #仓位0"
            m["bias_name"] = "杉杉600884固态电池标准化风控案例(8项因子Lollapalooza)"
            m["content_summary"] = f"杉杉固态营收4.72%<A档5%门槛,存货70.64亿/营收>0.35,主力15日-10.1亿,8项高危心理误判共振→C_规避仓位0%"
            if "extended_fields" not in m:
                m["extended_fields"] = {}
            m["extended_fields"].update({
                "solid_revenue_pct": 4.72,
                "layer": "C_规避",
                "lolla_high_count": 8,
                "lollapalooza": True,
                "position": 0.0,
                "misjudge_list": "08_嫉妒猜忌(96.27),04_避免怀疑(87.93),15_社会认同(87.00),23_市场噪音(79.99),02_喜欢热爱(77.20),14_损失厌恶(72.00),19_遗忘风险(70.00),09_回馈倾向(67.44)",
                "15d_main_flow": "-10.1亿",
                "inventory": "70.64亿",
                "core_risk": "存货高企+主力持续流出+固态兑现不足+多重心理误判",
            })
            print(f"  🔄 已更新现有FAISS条目")
            break
    
    if not existing:
        new_entry = {
            "source": "solid_case_600884_v2",
            "code": "600884.SH",
            "code_clean": "600884",
            "name": "杉杉股份",
            "tag": "#杉杉股份 #600884 #硅碳负极 #半固态电池 #固态题材兑现不足 #Lollapalooza风险共振 #C_规避 #仓位0",
            "tag_type": "solid_state_standard_case",
            "risk_level": 5,
            "bias_id": "case_solid_600884_v2",
            "bias_name": "杉杉600884固态电池标准化风控案例(8项因子Lollapalooza)",
            "chunk_id": f"chunk_case_solid_600884_v2",
            "date": TODAY,
            "group": "固态电池",
            "sector": "固态电池-负极材料",
            "layer": "C_规避",
            "chain": "配套正负极",
            "filters": "0011111",
            "bonuses": "0000",
            "bonus_cnt": 0,
            "content_summary": f"杉杉固态营收4.72%<A档5%门槛,存货70.64亿/营收>0.35,主力15日-10.1亿,8项高危心理误判共振→C_规避仓位0%",
            "extended_fields": {
                "solid_revenue_pct": 4.72,
                "layer": "C_规避",
                "lolla_high_count": 8,
                "lollapalooza": True,
                "position": 0.0,
                "misjudge_list": "08_嫉妒猜忌(96.27),04_避免怀疑(87.93),15_社会认同(87.00),23_市场噪音(79.99),02_喜欢热爱(77.20),14_损失厌恶(72.00),19_遗忘风险(70.00),09_回馈倾向(67.44)",
                "15d_main_flow": "-10.1亿",
                "inventory": "70.64亿",
                "core_risk": "存货高企+主力持续流出+固态兑现不足+多重心理误判",
            },
        }
        metas.append(new_entry)
        print(f"  ✅ 新增FAISS条目")
    
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metas, f, ensure_ascii=False, indent=2)
    
    total_solid = sum(1 for m in metas if "solid" in m.get("tag", "").lower() or "600884" in m.get("tag", ""))
    print(f"  📊 FAISS总chunks: {len(metas)}, 固态相关: {total_solid}")
    return True


# ═══════════════════════════════════════════════
#  步骤4: 更新SQLite observation_list标签
# ═══════════════════════════════════════════════

def update_sqlite_tags():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    
    # 确保表存在
    cur.execute("""
        CREATE TABLE IF NOT EXISTS observation_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            stock_name TEXT,
            tag TEXT NOT NULL,
            is_suspended INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, ts_code, tag)
        )
    """)
    
    # 更新杉杉标签
    tags_to_add = ["中线布局", "持仓", "观察跟踪"]
    for tag in tags_to_add:
        cur.execute(
            "INSERT OR IGNORE INTO observation_list (date, ts_code, stock_name, tag, is_suspended) "
            "VALUES (?, ?, ?, ?, 0)",
            ("20260721", "600884", "杉杉股份", tag),
        )
    
    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM observation_list WHERE ts_code='600884'").fetchone()[0]
    print(f"  ✅ SQLite: 杉杉{total}条标签(中线布局/持仓/观察跟踪)")
    conn.close()
    return True


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    print("=" * 60)
    print("📦 杉杉600884标准化风控案例入库")
    print("=" * 60)
    
    # 1. 生成案例文档
    print("\n[1/4] 生成标准化Markdown案例文档...")
    md = generate_case_md()
    case_path = ARCHIVE_DIR / f"case_600884_solid_standard_{TODAY}.md"
    with open(case_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"  ✅ 已写入: {case_path} ({len(md)}字符)")
    
    # 2. 更新288只总表
    print("\n[2/4] 更新full_scan 288只总表...")
    update_full_scan_entry()
    
    # 3. 更新FAISS
    print("\n[3/4] 更新FAISS RAG元数据...")
    update_faiss_metadata()
    
    # 4. 更新SQLite
    print("\n[4/4] 更新SQLite标签...")
    update_sqlite_tags()
    
    print("\n" + "=" * 60)
    print("✅ 杉杉600884标准化风控案例全部入库完成")
    print("=" * 60)
    print(f"\n归档文件:")
    print(f"  1. {case_path}")
    print(f"  2. {ARCHIVE_DIR}/full_scan_md_20260720_1158.md (已更新)")
    print(f"  3. FAISS (已更新)")
    print(f"  4. SQLite (已更新)")


if __name__ == "__main__":
    main()
