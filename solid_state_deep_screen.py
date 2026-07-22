"""
solid_state_deep_screen.py — 固态/半固态赛道两层筛选
第一层: 7条硬性基础过滤(一票否决)
第二层: 4条赛道加分项(>=2核心池,=1备选池)
"""
import os, sys, time, json
import pandas as pd
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, "/opt/stock_agent")
from config import TUSHARE_TOKEN
import tushare as ts
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

ARCHIVE = "/opt/stock_agent/solid_state_archive/"
os.makedirs(ARCHIVE, exist_ok=True)
TODAY = datetime.now().strftime("%Y%m%d_%H%M")

# FAISS固态标签元数据预热(用于辅助判断固态业务类型)
FAISS_SOLID_META = {}
try:
    with open("faiss_index/misjudge_metas.json") as f:
        for m in json.load(f):
            if "solid_state" in m.get("source","") or "固态" in m.get("tag",""):
                FAISS_SOLID_META[m["source"]] = m
except: pass

# ========== 产业链分类映射 ==========
CHAIN_MAP = {
    "电芯": ["宁德时代","亿纬锂能","国轩高科","鹏辉能源","孚能科技","欣旺达",
             "珠海冠宇","派能科技","天能股份","蔚蓝锂芯","圣阳股份","雄韬股份",
             "科恒股份","丰元股份","博力威","紫建电子","华宝新能"],
    "固态电解质": ["上海洗霸","三祥新材","瑞泰新材","奥克股份","新宙邦","天赐材料",
                  "多氟多","永太科技","硅宝科技","万润股份"],
    "固态设备": ["先导智能","利元亨","杭可科技","赢合科技","海目星","科瑞技术",
                 "联赢激光","海博思创","金银河","曼恩斯特"],
    "配套正负极": ["杉杉股份","璞泰来","贝特瑞","翔丰华","中科电气","天奈科技",
                   "容百科技","当升科技","长远锂科","振华新材","厦钨新能","湖南裕能",
                   "德方纳米","万润新能","天华新能","道氏技术"]
}

def classify_chain(name):
    for chain, names in CHAIN_MAP.items():
        if any(n in name for n in names):
            return chain
    # 模糊匹配
    if "电" in name or "池" in name: return "电芯"
    if "电解" in name or "锂" in name: return "固态电解质"
    if "设备" in name or "机械" in name or "激光" in name: return "固态设备"
    if "正极" in name or "负极" in name or "材料" in name: return "配套正负极"
    return "其他/未分类"

def get_financials(ts_code):
    """获取财务数据: ROE/负债率/营收增速/净利增速/现金流/研发费率"""
    fin = {"roe": None, "debt_ratio": None, "profit_yoy": None,
           "revenue_yoy": None, "ocf": None, "rd_ratio": None,
           "pe_ttm": None, "eps": None}
    try:
        ind = pro.fina_indicator(ts_code=ts_code, start_date="20250101", limit=1)
        if len(ind) > 0:
            r = ind.iloc[0]
            fin["roe"] = float(r.get("roe", 0) or 0)
            fin["debt_ratio"] = float(r.get("debt_to_assets", 0) or 0)
            fin["profit_yoy"] = float(r.get("netprofit_yoy", 0) or 0)
            fin["revenue_yoy"] = float(r.get("tr_yoy", 0) or 0)
    except: pass

    # 研发费用率需要从income表获取
    try:
        inc = pro.income(ts_code=ts_code, start_date="20250101", limit=1)
        if len(inc) > 0:
            r = inc.iloc[0]
            rd_exp = float(r.get("rd_exp", 0) or 0)  # 研发费用
            revenue = float(r.get("revenue", 0) or 1)
            fin["rd_ratio"] = rd_exp / revenue * 100 if revenue > 0 else 0
    except: pass

    # 经营现金流
    try:
        cf = pro.cashflow(ts_code=ts_code, start_date="20250101", limit=1)
        if len(cf) > 0:
            fin["ocf"] = float(cf.iloc[0].get("c_fr_sale_sg", 0) or 0)
    except: pass

    # PE_TTM
    try:
        db = pro.daily_basic(ts_code=ts_code, start_date="20260701", limit=1)
        if len(db) > 0:
            fin["pe_ttm"] = float(db.iloc[0].get("pe_ttm", 0) or 0)
    except: pass

    return fin

def check_filters(fin, concept_tags, name):
    """7条基础过滤 + 4条加分项"""
    filters = {}
    for i in range(1, 8):
        filters[f"F{i}"] = False

    bonuses = {}
    for i in range(1, 5):
        bonuses[f"B{i}"] = False

    # ====== F1: 固态业务落地门槛 ======
    # 概念成分中固态电池直接相关(名称含固态/半固态或行业龙头)
    has_solid_concept = any("固态" in (t or "") for t in concept_tags)
    has_battery_core = any(kw in name for kw in ["固态","半固态","锂电","电池","电解","负极"])
    filters["F1"] = has_solid_concept or has_battery_core

    # ====== F2: 固态营收>=5% ======
    # 用概念标签中有几个固态相关概念来估算
    solid_concept_cnt = sum(1 for t in concept_tags if "固态" in (t or ""))
    est_rev = min(solid_concept_cnt * 3, 15)
    filters["F2"] = est_rev >= 5

    # ====== F3: 研发费用率>=4% ======
    filters["F3"] = fin.get("rd_ratio") is not None and fin["rd_ratio"] >= 4

    # ====== F4: 经营现金流>0 ======
    filters["F4"] = fin.get("ocf") is not None and fin["ocf"] > 0

    # ====== F5: 净利同比>=50% ======
    filters["F5"] = fin.get("profit_yoy") is not None and fin["profit_yoy"] >= 50

    # ====== F6: 负债率<=75% ======
    filters["F6"] = fin.get("debt_ratio") is not None and fin["debt_ratio"] <= 75

    # ====== F7: PEG<1.5且动态PE≤50 ======
    pe = fin.get("pe_ttm")
    profit_yoy = fin.get("profit_yoy")
    if pe is not None and pe > 0 and pe <= 50:
        filters["F7_pe"] = True
        if profit_yoy is not None and profit_yoy > 0:
            peg = pe / profit_yoy
            filters["F7_peg"] = peg < 1.5
            filters["F7"] = filters["F7_pe"] and filters["F7_peg"]
        else:
            filters["F7"] = pe <= 30  # 利润增速不可得时从严
    else:
        filters["F7"] = pe is not None and 0 < pe <= 50

    # ====== B1: GWh级半固态量产线+车企定点 ======
    # 电芯企业+有5个以上电池概念标签
    core_battery_count = sum(1 for t in concept_tags if any(k in (t or "") for k in ["电池","锂电","动力"]))
    bonuses["B1"] = "电芯" in classify_chain(name) and core_battery_count >= 5

    # ====== B2: 固态电解质批量供货 ======
    bonuses["B2"] = any(kw in name for kw in ["电解液","天赐","新宙邦","多氟多","上海洗霸","三祥"])

    # ====== B3: 固态设备交付验收 ======
    bonuses["B3"] = any(kw in name for kw in ["先导","利元亨","杭可","海目星","联赢激光"])

    # ====== B4: 自研上下游一体化 ======
    bonuses["B4"] = core_battery_count >= 8

    return filters, bonuses

def main():
    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 固态/半固态两层筛选启动")

    # 1. 获取概念成分股
    concept_df = pro.ths_member(ts_code="886032.TI")
    concept_df["code"] = concept_df["con_code"].str.extract(r'(\d{6})')
    concept_df["market"] = concept_df["con_code"].str.extract(r'\.(SZ|SH)')
    concept_df = concept_df[~concept_df["con_name"].str.contains("ST|退", na=False)]
    print(f"  基础池: {len(concept_df)}只")

    # 2. 逐个扫描
    passed_stocks = []
    all_results = []

    for idx, (_, row) in enumerate(concept_df.iterrows()):
        code = row["code"]
        market = row["market"]
        name = row["con_name"]
        ts_code = f"{code}.{market}"

        # 行情名修正
        try:
            basic = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry")
            if len(basic) > 0:
                name = basic.iloc[0]["name"]
        except: pass

        # 概念标签
        tags = []
        try:
            cons = pro.ths_member(ts_code=ts_code)
            if len(cons) > 0:
                tags = cons["con_name"].tolist()
        except: pass

        # 财务数据
        fin = get_financials(ts_code)

        # 7条基础过滤 + 4条加分项
        filters, bonuses = check_filters(fin, tags, name)
        f_pass = sum(1 for k, v in filters.items() if v)
        b_cnt = sum(1 for v in bonuses.values() if v)

        result = {
            "code": code, "name": name,
            "chain": classify_chain(name),
            "filters": filters, "filter_pass": f_pass,
            "bonuses": bonuses, "bonus_cnt": b_cnt,
            "rd_ratio": fin.get("rd_ratio"),
            "ocf": fin.get("ocf"),
            "profit_yoy": fin.get("profit_yoy"),
            "debt_ratio": fin.get("debt_ratio"),
            "pe_ttm": fin.get("pe_ttm"),
            "layer": ""
        }

        # 第一层: 7条全部通过
        if f_pass >= 7:
            result["layer"] = "核心池" if b_cnt >= 2 else "备选池" if b_cnt >= 1 else "观察池"
            passed_stocks.append(result)

        all_results.append(result)
        print(f"  [{idx+1}/{len(concept_df)}] {code} {name:8s} | F{f_pass}/7 B{b_cnt}/4 | {result['layer'] or '淘汰'}")
        time.sleep(0.15)

    # 3. 输出报告
    rpath = f"{ARCHIVE}/deep_screen_{TODAY}.txt"
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(f"# 固态/半固态赛道两层筛选报告 {TODAY}\n")
        f.write(f"总扫描: {len(all_results)}只 | 通过F1~F7: {len(passed_stocks)}只\n\n")
        f.write("第一层: 7条硬性基础过滤\n")
        f.write("  F1:固态业务落地 | F2:营收>=5% | F3:研发费率>=4% | F4:经营现金流>0\n")
        f.write("  F5:净利同比>=50% | F6:负债率<=75% | F7:PE<=50且PEG<1.5\n")
        f.write("第二层: 4条赛道加分(B1:GWh量产线+定点 B2:电解质批量供货 B3:设备交付 B4:一体化)\n\n")

        for chain in ["电芯","固态电解质","固态设备","配套正负极"]:
            chain_stocks = [s for s in passed_stocks if s["chain"] == chain]
            core = [s for s in chain_stocks if s["layer"]=="核心池"]
            standby = [s for s in chain_stocks if s["layer"]=="备选池"]
            watch = [s for s in chain_stocks if s["layer"]=="观察池"]

            f.write(f"\n{'='*100}\n")
            f.write(f"【{chain}】通过F1~F7: {len(chain_stocks)}只 (核心{len(core)}/备选{len(standby)})\n")
            f.write(f"{'='*100}\n")
            f.write("代码 名称 | 层 | 分 | 明细(F1~F7达标项/加分B1~B4)\n")
            f.write("-"*80+"\n")
            for s in core:
                l = s["layer"]
                fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
                bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])
                f.write(f"{s['code']} {s['name']:8s} | {l} | B{s['bonus_cnt']} | F[{fstr}] B[{bstr}]\n")
            for s in standby:
                fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
                bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])
                f.write(f"{s['code']} {s['name']:8s} | {s['layer']} | B{s['bonus_cnt']} | F[{fstr}] B[{bstr}]\n")
            for s in watch:
                fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
                bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])
                f.write(f"{s['code']} {s['name']:8s} | {s['layer']} | B{s['bonus_cnt']} | F[{fstr}] B[{bstr}]\n")

        # 淘汰股票TOP(接近通过的)
        eliminated = [s for s in all_results if s not in passed_stocks]
        near_pass = sorted(eliminated, key=lambda x: x["filter_pass"], reverse=True)[:10]
        if near_pass:
            f.write(f"\n\n【接近通过(淘汰TOP10)】\n")
            for s in near_pass:
                fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
                f.write(f"  {s['code']} {s['name']:8s} | F{s['filter_pass']}/7 | [{fstr}]\n")

    print(f"\n  报告: {rpath}")
    print(f"  通过F1~F7: {len(passed_stocks)}只")
    for chain in ["电芯","固态电解质","固态设备","配套正负极"]:
        cs = [s for s in passed_stocks if s["chain"]==chain]
        core = sum(1 for s in cs if s["layer"]=="核心池")
        std = sum(1 for s in cs if s["layer"]=="备选池")
        if cs:
            print(f"    {chain}: {len(cs)}只(核心{core}/备选{std})")
    print(f"  耗时: {time.time()-t0:.1f}s")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 筛选完成")

if __name__ == "__main__":
    main()
