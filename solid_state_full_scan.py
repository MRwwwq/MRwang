"""
solid_state_full_scan.py — 全A股半固态/全固态双维度批量扫描
数据源: Tushare ths_member(固态电池概念886032.TI) + 基本面 + 资金流
分层: A可跟踪/B观望/C规避(含QClaw_Rule_021逻辑)
"""
import os, sys, time, json
import pandas as pd, numpy as np
from datetime import datetime

sys.path.insert(0, "/opt/stock_agent")
from config import TUSHARE_TOKEN
import tushare as ts
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

ARCHIVE_PATH = "/opt/stock_agent/solid_state_archive/"
os.makedirs(ARCHIVE_PATH, exist_ok=True)

# ===================== 参数 =====================
REV_THRESHOLD_GOOD = 5.0    # A: 固态营收>=5%
REV_THRESHOLD_WATCH = 3.0   # B: >=3%
MONEY_15D_OUT_THRESHOLD = 0 # 主力15日净额<0 -> 资金流出
HIGH_RISK_LIST = ["02_喜欢热爱","04_避免怀疑","08_嫉妒猜忌","09_回馈倾向",
                  "14_损失厌恶","15_社会认同羊群","19_遗忘风险","23_市场噪音废话"]

# 固态电池概念股列表缓存
SOLID_CONCEPT_CACHE = None

def get_solid_concept_stocks():
    global SOLID_CONCEPT_CACHE
    if SOLID_CONCEPT_CACHE is not None:
        return SOLID_CONCEPT_CACHE
    # 从Tushare获取固态电池概念成分股
    df = pro.ths_member(ts_code="886032.TI")
    # 提取代码(去掉.SZ/.SH后缀)
    df["code"] = df["con_code"].str.extract(r'(\d{6})')
    df["market"] = df["con_code"].str.extract(r'\.(SZ|SH)')
    # 过滤掉ST/退市
    df = df[~df["con_name"].str.contains("ST|退", na=False)]
    SOLID_CONCEPT_CACHE = df
    return df

def get_stock_name(code):
    """从stock_basic获取名称"""
    try:
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry")
        if len(df) > 0:
            return df.iloc[0]["name"], df.iloc[0].get("industry","")
    except:
        pass
    return code, ""

def get_business_keywords(code):
    """尝试从互动易/研报获取业务关键词"""
    try:
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        research = pro.ths_member(ts_code=ts_code)
        return research["con_name"].tolist() if len(research) > 0 else []
    except:
        return []

def get_fin_quick(ts_code):
    """快速获取营收/利润/存货数据"""
    try:
        ind = pro.fina_indicator(ts_code=ts_code, start_date="20250101", limit=1)
        if len(ind) == 0:
            return {}
        r = ind.iloc[0]
        return {
            "roe": float(r.get("roe", 0) or 0),
            "gross_margin": float(r.get("grossprofit_margin", 0) or 0),
            "debt_ratio": float(r.get("debt_to_assets", 0) or 0),
            "profit_yoy": float(r.get("netprofit_yoy", 0) or 0)
        }
    except:
        return {}

def get_moneyflow_15d(ts_code):
    """近15日主力净额(万元)"""
    try:
        df = pro.moneyflow(ts_code=ts_code, start_date="20260701", end_date="20260720")
        if len(df) == 0:
            return 0
        return float(df["net_mf_amount"].sum())
    except:
        return 0

def infer_solid_relevance(name, industry, keywords):
    """根据名称+行业+概念推断固态电池关联度(0~100)"""
    score = 0
    name_lower = name.lower()
    # 名称直接包含关键词
    for kw in ["固态", "半固态", "全固态", "锂电", "负极", "正极", "电解液",
               "隔膜", "硅碳", "电池", "新能源"]:
        if kw in name:
            score += 20
    # 行业匹配
    if "电气设备" in industry or "电力设备" in industry:
        score += 10
    if "化工" in industry:
        score += 5
    # 概念匹配
    for kw in keywords:
        if any(k in kw for k in ["固态", "电池", "锂电", "负极", "电解液", "隔膜"]):
            score += 5
    return min(score, 100)

def classify_stock(ts_code, code, name, industry, keywords):
    """单标的分类"""
    # 基础信息
    relevance = infer_solid_relevance(name, industry, keywords)
    fin = get_fin_quick(ts_code)
    mf_15d = get_moneyflow_15d(ts_code)
    money_out = mf_15d < MONEY_15D_OUT_THRESHOLD

    # 分层判定
    if relevance >= 60 and not money_out and fin.get("roe", 0) > 5:
        layer = "A_可跟踪(高关联+资金正向+盈利)"
    elif relevance >= 40:
        layer = "B_观望(中等关联,仅观测)"
    elif money_out and relevance < 20:
        layer = "C_规避(低关联+资金流出)"
    else:
        layer = "B_观望"

    return {
        "code": code,
        "name": name,
        "industry": industry,
        "relevance_score": relevance,
        "roe": fin.get("roe", "N/A"),
        "gross_margin": fin.get("gross_margin", "N/A"),
        "debt_ratio": fin.get("debt_ratio", "N/A"),
        "mf_15d_net": round(mf_15d / 10000, 2),  # 万元->亿
        "money_out": money_out,
        "layer": layer
    }

def main():
    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 全A股固态电池批量扫描启动")

    # 1. 获取概念成分股
    concept_df = get_solid_concept_stocks()
    print(f"  固态电池概念成分股: {len(concept_df)}只(已过滤ST)")

    # 2. 逐个扫描(全量288只)
    results = []
    batch = concept_df  # 全量,无截断
    total = len(batch)
    for idx, (_, row) in enumerate(batch.iterrows()):
        code = row["code"]
        name = row["con_name"]
        market = row["market"]
        ts_code = f"{code}.{market}"
        industry = ""

        # 获取行业
        try:
            basic = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry")
            if len(basic) > 0:
                name = basic.iloc[0]["name"]
                industry = basic.iloc[0].get("industry", "")
        except:
            pass

        # 获取概念关键词
        keywords = []
        try:
            cons = pro.ths_member(ts_code=ts_code)
            if len(cons) > 0:
                keywords = cons["con_name"].tolist()[:5]
        except:
            pass

        result = classify_stock(ts_code, code, name, industry, keywords)
        results.append(result)
        print(f"  [{idx+1}/{total}] {code} {name:8s} | 关联度{result['relevance_score']:2d} | "
              f"ROE{result['roe']} | 主力{result['mf_15d_net']:+.1f}亿 | {result['layer']}")
        time.sleep(0.15)  # Tushare限频

    # 3. 归档
    today = datetime.now().strftime("%Y%m%d")
    rpath = f"{ARCHIVE_PATH}/full_scan_{today}.txt"
    df = pd.DataFrame(results)
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(f"# 全A股固态电池批量扫描报告 {today}\n")
        f.write(f"扫描标的: {len(results)}只 | 扫描耗时: {time.time()-t0:.1f}s\n")
        f.write("=" * 80 + "\n")
        for _, r in df.sort_values("relevance_score", ascending=False).iterrows():
            f.write(f"{r['code']} {r['name']:8s} | 关联度{r['relevance_score']:2d} | "
                    f"ROE{r['roe']} | 毛利{r['gross_margin']} | 负债{r['debt_ratio']} | "
                    f"主力{r['mf_15d_net']:+.1f}亿 | {r['layer']}\n")
    print(f"\n  报告归档: {rpath}")

    # 4. 统计
    print("\n  分层统计:")
    for layer, cnt in df["layer"].value_counts().items():
        sub = df[df["layer"] == layer]
        top = sub.sort_values("relevance_score", ascending=False).head(3)
        codes = ",".join(f"{r['code']}({r['relevance_score']})" for _, r in top.iterrows())
        print(f"    {layer}: {cnt}只  (top: {codes})")
    print(f"  总耗时: {time.time()-t0:.1f}s")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 扫描完成")

if __name__ == "__main__":
    main()
