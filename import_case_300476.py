#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_case_300476.py — 胜宏科技300476标准化风控案例入库"""

import os, json, sqlite3, sys
from datetime import datetime
from pathlib import Path
from collections import OrderedDict

BASE_DIR = Path("/opt/stock_agent")
FAISS_DIR = BASE_DIR / "faiss_index"
ARCHIVE_DIR = BASE_DIR / "solid_state_archive"
DB_PATH = BASE_DIR / "agent_memory.db"
TODAY = "20260720"

CASE = OrderedDict()
CASE["meta"] = {
    "stock_code": "300476.SZ", "stock_name": "胜宏科技",
    "chain": "AI算力高阶PCB(英伟达Tier1供应链)",
    "core_revenue_pct": 43.2, "core_revenue_q1_2026": 52.0,
    "layer": "B_观望", "position": 0.0,
    "lollapalooza": False, "lolla_high_count": 3,
    "pe_ttm": 42.74, "debt_ratio": 41.35,
}

CASE["business"] = OrderedDict([
    ("AI算力PCB(核心)", {
        "market_share": "全球AI服务器PCB市占50-55%",
        "customers": "英伟达GB200/GB300/Rubin Tier1, UBB板份额70%; 谷歌TPU; 微软",
        "revenue_2025": "83.34亿(总营收43.2%), 毛利率43.5%",
        "revenue_2026q1": "单季算力52%, 交付GB300板15.2万台(超预期30%)",
        "order_backlog": "128亿, 覆盖2.3季度, 泰国+惠州扩产中",
        "profit_2025": "归母43.12亿(+273.52%), 经营现金流46.20亿",
    }),
    ("传统PCB(对冲)", {
        "智能终端": "营收占比19.1%",
        "汽车电子": "营收占比13.4%",
    }),
    ("题材属性", "算力实体业绩兑现标的, 营收>5%门槛, 订单/客户/产能/利润四维落地"),
])

CASE["risks"] = OrderedDict([
    ("客户集中", "英伟达占算力50%+, 前四大客户合计55%; 资本开支收缩/份额分流→业绩大跌"),
    ("存货减值", "2026Q1存货39.05亿(+67.6%); 高阶PCB迭代快, 旧型号跌价风险"),
    ("资本开支", "海内外多基地规划总投资180亿, 短期现金流弹性受限"),
    ("高估值杠杆", "流通市值2202亿, PE42.74, 融资余额203.47亿(占流通9.66%)"),
])

CASE["capital"] = OrderedDict([
    ("15日主力", "-32.75亿(短期机构集中兑现)"),
    ("5日主力", "-16.32亿(资金出逃加速)"),
    ("融资余额", "203.47亿高位震荡, 杠杆集中偿还"),
    ("盘面", "272→224元快速回撤(-17.6%), 高位放量分歧"),
])

CASE["technical"] = OrderedDict([
    ("收盘价", "224.11元(-7.20%)"),
    ("均线", "短期全部空头排列"),
    ("MACD", "绿柱持续扩张, 无金叉"),
    ("压力/支撑", "压力245/268元; 支撑213元"),
    ("60日涨跌", "-34.54%"),
])

CASE["misjudge"] = OrderedDict([
    ("02_喜欢热爱", {"s": 68.29, "d": "绑定英伟达成长叙事,忽略高估值/客户集中/资金出逃"}),
    ("04_避免怀疑", {"s": 100.0, "d": "订单超预期后线性外推,未算存货减值风险"}),
    ("08_嫉妒猜忌", {"s": 100.0, "d": "对比其他算力股涨幅,追高情绪"}),
    ("14_损失厌恶", {"s": 100.0, "d": "浮亏后拒绝止损"}),
    ("17_压力影响", {"s": 78.35, "d": "快速回撤导致恐慌"}),
    ("23_市场噪音", {"s": 96.14, "d": "算力炒作短视频/股吧影响"}),
])

CASE["lolla_compare"] = OrderedDict([
    ("标准§24规则", "≥3项→Lolla激活(6项→一票否决)"),
    ("标准判定结果", "⚠️ 6项≥60→Lollapalooza激活"),
    ("用户C档条件", "≥6项+营收<5%+存货/营收>35%+主力流出→C_规避"),
    ("营收判定", "AI算力43.2%>>5%→不触发营收<5%条件"),
    ("最终结论", "B_观望(营收达标+B档条件)"),
])

CASE["trade"] = OrderedDict([
    ("禁止", ["新建重仓多头"]),
    ("上限", "单只≤总资金4%, 逢反弹减仓, 禁止加仓"),
    ("A档条件", [
        "连续10日主力累计净流入",
        "英伟达/谷歌资本开支上调+新增大额长期包销订单",
        "存货同比增速回落至20%以内",
        "高危因子降至2项以内",
        "放量突破245元+周线MACD金叉",
    ]),
])

CASE["feishu_row"] = OrderedDict([
    ("代码","300476"),("名称","胜宏科技"),("算力关联度",42),
    ("AI算力营收","43.20%"),("ROE","35.56%"),("15日主力","-32.75亿"),
    ("高危误判数",3),("Lollapalooza",False),("分层","B_观望"),
    ("核心风险","客户集中+存货+资本开支+高估值杠杆+资金流出"),
])

CASE["rag_tags"] = "#胜宏科技 #300476 #AI算力PCB #英伟达供应链 #高阶HDI #业绩兑现算力标的"

def gen_md():
    L = [f"# 胜宏科技(300476) — AI算力PCB标准化风控案例\n> **时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | **分层:** {CASE['meta']['layer']}"]
    L.append("\n---\n## 一、基础元数据\n| 字段 | 内容 |\n|:----|:-----|")
    for k,v in CASE["meta"].items(): L.append(f"| {k} | {v} |")
    L.append("\n---\n## 二、业务结构\n")
    for biz, d in CASE["business"].items():
        L.append(f"### {biz}\n| 维度 | 详情 |\n|:----|:-----|")
        if isinstance(d,dict):
            for k,v in d.items(): L.append(f"| {k} | {v} |")
        else: L.append(f"| 属性 | {d} |")
    L.append("\n---\n## 三、底层风险\n| 风险 | 说明 |\n|:----|:------|")
    for k,v in CASE["risks"].items(): L.append(f"| {k} | {v} |")
    L.append("\n---\n## 四、资金流\n| 区间 | 金额 |\n|:----|:----|")
    for k,v in CASE["capital"].items(): L.append(f"| {k} | {v} |")
    L.append("\n---\n## 五、技术面\n| 指标 | 值 |\n|:----|:----|")
    for k,v in CASE["technical"].items(): L.append(f"| {k} | {v} |")
    L.append(f"\n---\n## 六、芒格心理误判\n标准引擎: **6项≥60→Lolla激活** | 用户判定: **3项高危→B档**\n| 因子 | 得分 | 解读 |\n|:----|:---:|:-----|")
    for c,d in CASE["misjudge"].items():
        m = "🔴" if d['s']>=60 else("🟡" if d['s']>=40 else"✅")
        L.append(f"| {c} | {d['s']}{m} | {d['d']} |")
    L.append("\n### Lollapalooza对比\n| 体系 | 结果 |\n|:----|:-----|")
    for k,v in CASE["lolla_compare"].items(): L.append(f"| {k} | {v} |")
    L.append("\n---\n## 七、交易约束\n")
    for a,items in CASE["trade"].items():
        if isinstance(items,list):
            for i in items: L.append(f"- {a}: {i}")
        else: L.append(f"- {a}: {items}")
    L.append("\n---\n## 八、飞书表格\n|"+"|".join(CASE["feishu_row"].keys())+"|")
    L.append("|"+"|".join(":---:" for _ in CASE["feishu_row"])+"|")
    L.append("|"+"|".join(str(v) for v in CASE["feishu_row"].values())+"|")
    L.append(f"\n---\n## 十、复盘提问\n1. 胜宏科技AI算力PCB全年营收占比多少,为何不属于纯题材概念股?\n2. 本标的仅3项高危心理误判,为什么没有触发Lollapalooza一票否决?\n3. 客户高度绑定英伟达会带来哪些持续性业绩风险?\n4. 从B_观望升级为A档需要满足哪五项观测条件?")
    return "\n".join(L)

def update_faiss():
    p = FAISS_DIR/"misjudge_metas.json"
    with open(p) as f: m = json.load(f)
    for x in m:
        if x.get("code")=="300476.SZ" and "case_pcb_300476_v1" in x.get("source",""):
            print("  FAISS已存在,跳过"); return
    m.append({
        "source":"case_pcb_300476_v1","code":"300476.SZ","code_clean":"300476",
        "name":"胜宏科技",
        "tag":"#胜宏科技 #300476 #AI算力PCB #英伟达供应链 #高阶HDI #业绩兑现算力标的 #B_观望",
        "tag_type":"pcb_ai_case","risk_level":3,
        "bias_id":"case_pcb_300476_v1",
        "bias_name":"胜宏科技AI算力PCB标准化风控案例(3项误判B档观望)",
        "chunk_id":"chunk_case_pcb_300476_v1","date":TODAY,
        "group":"AI算力","sector":"PCB-英伟达供应链","layer":"B_观望",
        "content_summary":"胜宏AI算力PCB营收43.2%>5%门槛,在手128亿,3项高危误判,资金-32.75亿→B_观望",
        "extended_fields":{
            "type":"ai_pcb","layer":"B_观望","lolla_count":3,
            "lollapalooza_standard":True,"lollapalooza_user":False,
            "position":0.0,"ai_revenue_pct":43.2,"debt_ratio":41.35,
            "client_concentration":"英伟达50%+前四55%","15d_main_flow":"-32.75亿"},
    })
    with open(p,"w") as f: json.dump(m,f,ensure_ascii=False,indent=2)
    print(f"  ✅ FAISS新增(总{len(m)}chunks)")

def update_pg():
    try:
        sys.path.insert(0,"/opt/stock_agent")
        import trade_calibration_pg_v3 as tg
        row = {"trade_date":TODAY,"ticker":"300476","real_change_pct":-7.20,
               "close_price":224.11,
               "support_resistance_status":"短期均线空头,MACD绿柱扩张,压力245支撑213",
               "real_trade_action":"空仓","ai_pred":"预判下跌","ai_risk_tip":"提示减仓",
               "is_trapped":False,"operator":"quant_admin",
               "misjudge_hit_count":3,"solid_tech_tag":"无固态题材",
               "qclaw_rule_id":"QClaw_Rule_007","rag_match_score":0.85}
        ok,msg = tg.data_validate(row)
        if ok:
            tag = tg.get_error_label(row["ai_pred"],row["real_change_pct"],row["ai_risk_tip"],
                                      row["is_trapped"],row["misjudge_hit_count"],row["solid_tech_tag"])
            tg.insert_calibration(row,tag)
            tg.logic_review(TODAY,["300476"])
            print(f"  ✅ PG完成 ({tag})")
    except Exception as e: print(f"  ⚠️ PG跳过: {e}")

def update_sqlite():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    for tag in ["中线布局"]:
        cur.execute("INSERT OR IGNORE INTO observation_list(date,ts_code,stock_name,tag) VALUES(?,?,?,?)",
                     ("20260721","300476","胜宏科技",tag))
    conn.commit(); conn.close()
    print("  ✅ SQLite已更新")

if __name__=="__main__":
    print("="*60+"\n📦 胜宏科技300476标准化风控案例入库\n"+"="*60)
    print("\n[1/4] 案例文档...")
    p = ARCHIVE_DIR/"case_300476_pcb_standard_20260720.md"
    with open(p,"w") as f: f.write(gen_md())
    print(f"  ✅ {p}")
    print("\n[2/4] FAISS..."); update_faiss()
    print("\n[3/4] PG..."); update_pg()
    print("\n[4/4] SQLite..."); update_sqlite()
    print(f"\n✅ 全部完成 | {CASE['meta']['layer']} | Lolla:{CASE['meta']['lollapalooza']}")
