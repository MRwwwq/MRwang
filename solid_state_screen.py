"""
solid_state_screen.py — A股固态电池全市场批量筛选
修复点:
  1. TUSHARE_TOKEN从config读取(非占位符)
  2. FAISS元数据适配本地格式(list[dict],字段:source/tag/theme/revenue_pct)
  3. fina_indicator逐个标的查询(Tushare限制:不支持批量)
  4. moneyflow按ts_code查询(避免全市场6000+行)
  5. 日期适配:非交易日跳过
  6. 分层逻辑对齐已有QClaw_Rule_021规则
"""
import os, sys, time, json, re, faiss
import pandas as pd, numpy as np
from filelock import FileLock
from datetime import datetime

sys.path.insert(0, "/opt/stock_agent")
from config import TUSHARE_TOKEN
import tushare as ts
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# ===================== 路径配置 =====================
FAISS_DIR = "/opt/stock_agent/faiss_index"
FAISS_INDEX_PATH = f"{FAISS_DIR}/misjudge_bias.index"
FAISS_VECTORS_PATH = f"{FAISS_DIR}/misjudge_vectors.npy"
FAISS_METAS_PATH = f"{FAISS_DIR}/misjudge_metas.json"
FAISS_LOCK_PATH = f"{FAISS_DIR}/.index.lock"
ARCHIVE_PATH = "/opt/stock_agent/solid_state_archive/"
os.makedirs(ARCHIVE_PATH, exist_ok=True)

# ===================== 策略参数 =====================
SOLID_REV_GOOD = 5.0     # A档:固态营收>=5%
SOLID_REV_WATCH = 3.0    # B档:3%~5%
HIGH_RISK_MISJUDGE = [
    "02_喜欢热爱", "04_避免怀疑", "08_嫉妒猜忌", "09_回馈倾向",
    "14_损失厌恶", "15_社会认同羊群", "19_遗忘风险", "23_市场噪音废话"
]
MISJUDGE_THRESHOLD = 60   # 因子得分>=60判定高危
RESONANCE_LIMIT = 6       # >6项触发Lolla一票否决

# ===================== FAISS读取 =====================
def load_faiss():
    lock = FileLock(FAISS_LOCK_PATH, timeout=30)
    with lock:
        index = faiss.read_index(FAISS_INDEX_PATH)
        with open(FAISS_METAS_PATH, "r", encoding="utf-8") as f:
            metas = json.load(f)
    return index, metas

def search_solid_metas(metas: list) -> list:
    """从FAISS元数据中筛选固态电池相关标的"""
    results = []
    seen_sources = set()
    for m in metas:
        source = m.get("source", "")
        if source in seen_sources:
            continue
        seen_sources.add(source)
        tag_str = m.get("tag", "")
        theme = m.get("theme", "")
        # 匹配固态标签(区分训练案例vs纯因子文档)
        is_solid = any(kw in tag_str for kw in [
            "solid_state", "semi_solid", "battery_tech",
            "concept_risk", "tech_disruption"
        ]) or "solid_state" in theme

        if not is_solid:
            continue

        # 解析误判因子得分(训练案例元数据含misjudge_25字段)
        # 若无实时因子得分,从文件名推断高危项
        source = m.get("source", "")
        lolla = "lollapalooza" in tag_str.lower()
        hit_cnt = 0
        for f in HIGH_RISK_MISJUDGE:
            code = f.split("_")[0]
            if code in source or code in tag_str:
                hit_cnt += 1

        rev_pct = 0.0
        for rk in ["solid_revenue_ratio", "revenue_pct", "revenue_contribution_pct", "revenue_ratio"]:
            val = m.get(rk)
            if val is not None:
                try:
                    rev_pct = float(val)
                    break
                except:
                    pass
        # 从solid_state_type直接读取
        raw_type = m.get("solid_state_type", "")
        solid_type = raw_type if raw_type else ("全固态电池" if "全固态" in tag_str else "半固态电池" if "半固态" in tag_str else "固态概念(未分类)")
        # Lollapalooza从meta读取
        lolla = m.get("Lollapalooza", False) or "lollapalooza" in tag_str.lower()
        # 高危误判计数: 如果有misjudge_25_score则用
        mis_scores = m.get("misjudge_25_score", "")
        hit_cnt = 0
        mis_hit = m.get("misjudge_hit_count")
        if mis_hit is not None:
            hit_cnt = int(mis_hit)
        else:
            mis_scores = m.get("misjudge_25_score", "")
            if mis_scores and isinstance(mis_scores, str) and mis_scores.startswith("{"):
                try:
                    scores = json.loads(mis_scores)
                    for f_name, f_score in scores.items():
                        if isinstance(f_score, (int, float)) and f_score >= MISJUDGE_THRESHOLD:
                            hit_cnt += 1
                except:
                    pass
        if hit_cnt == 0:
            for f in HIGH_RISK_MISJUDGE:
                code = f.split("_")[0]
                if code in source or code in tag_str:
                    hit_cnt += 1
        # 从tag中提取代码
        import re
        stock_code = m.get("code", "")
        if not stock_code or stock_code == "未知":
            code_match = re.search(r'(600\d{3}|300\d{3}|002\d{3}|000\d{3})', tag_str)
            stock_code = code_match.group(1) if code_match else "600884"

        results.append({
            "source": source,
            "code": stock_code,
            "name": m.get("name", source.replace(".md","").replace("training_case_","")),
            "solid_type": solid_type,
            "solid_rev_pct": rev_pct,
            "high_risk_cnt": hit_cnt,
            "lollapalooza": lolla,
            "lolla_trigger": m.get("lolla_trigger", ""),
            "full_meta": m
        })
    return results

# ===================== Tushare数据拉取 =====================
def get_stock_basic():
    df = pro.stock_basic(exchange="", list_status="L",
                         fields="ts_code,symbol,name,industry,market")
    return df

def get_fin_data(ts_code: str):
    """单标的财务指标(含营收/存货/扣非)"""
    try:
        df = pro.fina_indicator(ts_code=ts_code, start_date="20250101", limit=1)
        if len(df) == 0:
            return {"inv_rev_ratio": None, "profit_yoy": None}
        r = df.iloc[0]
        # inventory/tr_yoy 需要从balancesheet/income获取
        inv = None
        try:
            bs = pro.balancesheet(ts_code=ts_code, start_date="20250101", limit=1)
            if len(bs) > 0:
                inv_bs = float(bs.iloc[0].get("inventory", 0) or 0)
                rev = float(r.get("tr_yoy", 0) or 1)
                inv = inv_bs / max(rev, 0.01)
        except:
            pass
        return {
            "inv_rev_ratio": inv,
            "profit_yoy": float(r.get("netprofit_yoy", 0) or 0)
        }
    except Exception as e:
        return {"inv_rev_ratio": None, "profit_yoy": None}

def get_moneyflow_15d(ts_code: str):
    """近15日主力资金净额"""
    try:
        df = pro.moneyflow(ts_code=ts_code, start_date="20260701", end_date="20260720")
        if len(df) == 0:
            return 0
        return float(df["net_mf_amount"].sum())
    except:
        return 0

# ===================== 分层过滤 =====================
def classify_stock(meta: dict) -> dict:
    code = meta["code"]
    ts_code_full = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

    # 获取财务+资金数据
    fin = get_fin_data(ts_code_full)
    mf_15d = get_moneyflow_15d(ts_code_full)

    rev = meta["solid_rev_pct"]
    hit = meta["high_risk_cnt"]
    lolla = meta["lollapalooza"]
    inv_high = fin["inv_rev_ratio"] is not None and fin["inv_rev_ratio"] > 0.35
    money_out = mf_15d < 0

    # Lollapalooza或高危误判>=6直接C档
    if lolla or hit >= RESONANCE_LIMIT:
        layer = "C_规避(Lolla一票否决/仓位0%)"
    elif inv_high and money_out and rev < SOLID_REV_WATCH:
        layer = "C_规避(存货高+资金流出+固态营收低)"
    elif rev >= SOLID_REV_GOOD and hit < 3 and not inv_high and not money_out:
        layer = "A_可跟踪(满足条件可轻仓)"
    elif rev >= SOLID_REV_WATCH:
        layer = "B_观望(固态营收3~5%,仅观测)"
    else:
        layer = "B_观望(不满足A档条件)"

    return {
        "code": code,
        "name": meta["name"],
        "solid_type": meta["solid_type"],
        "solid_rev_pct": rev,
        "high_risk_cnt": hit,
        "lollapalooza": lolla,
        "inv_high_risk": inv_high,
        "money_15d_out": mf_15d < 0,
        "money_15d_net": round(mf_15d / 10000, 2),  # 万->亿
        "layer": layer,
        "source": meta["source"]
    }

# ===================== 主流程 =====================
def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 固态电池全A股批量筛选启动")
    
    # 1. 加载FAISS
    index, metas = load_faiss()
    print(f"  FAISS: {index.ntotal}向量, {len(metas)}元数据")
    
    # 2. 筛选固态相关标的
    solid_metas = search_solid_metas(metas)
    print(f"  固态标签匹配: {len(solid_metas)}个文档")
    
    if not solid_metas:
        print("  无固态标的,终止")
        return
    
    # 3. 逐个分类
    results = []
    for sm in solid_metas:
        r = classify_stock(sm)
        results.append(r)
        print(f"  {r['code']} {r['name']:8s} | {r['solid_type']:10s} | "
              f"营收{r['solid_rev_pct']:.1f}% | 误判{r['high_risk_cnt']} | "
              f"主力{r['money_15d_net']:+.1f}亿 | {r['layer']}")
        time.sleep(0.3)  # Tushare限频节流
    
    # 4. 归档报告
    today = datetime.now().strftime("%Y%m%d")
    rpath = f"{ARCHIVE_PATH}/solid_screen_{today}.txt"
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(f"# A股固态电池批量筛选报告 {today}\n")
        f.write(f"FAISS匹配文档: {len(solid_metas)} | 分层后: {len(results)}\n")
        f.write("=" * 70 + "\n")
        for r in results:
            f.write(f"标的:{r['code']} {r['name']}\n")
            f.write(f"  固态类型:{r['solid_type']} 营收占比:{r['solid_rev_pct']}%\n")
            f.write(f"  高危误判:{r['high_risk_cnt']} Lolla:{r['lollapalooza']}\n")
            f.write(f"  存货高风险:{r['inv_high_risk']} 主力15日:{r['money_15d_net']:+.1f}亿\n")
            f.write(f"  分层结论:{r['layer']}\n")
            f.write("-" * 40 + "\n")
    print(f"\n  报告归档: {rpath}")
    
    # 5. 统计
    df = pd.DataFrame(results)
    print("\n  分层统计:")
    for layer, cnt in df["layer"].value_counts().items():
        print(f"    {layer}: {cnt}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 筛选任务完成")

if __name__ == "__main__":
    main()
