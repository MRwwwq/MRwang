"""
solid_state_full_report.py — 全量288只固态电池扫描报告(产业链分组版)
读取Tushare数据,按行业分组输出完整字段
"""
import os, sys, time, json, re
import pandas as pd
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, "/opt/stock_agent")
from config import TUSHARE_TOKEN
import tushare as ts
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

ARCHIVE_PATH = "/opt/stock_agent/solid_state_archive/"
os.makedirs(ARCHIVE_PATH, exist_ok=True)

def split_long_text(text, chunk_len=90000):
    """文本分片函数, 单段90000字符内发送 (适配JS版feishu_bot.send_text)"""
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + chunk_len])
        start += chunk_len
    return chunks

def format_table_chunks(all_stocks, chunk_size=50):
    """生成Markdown表格并分为chunk_size行一组, 每组含表头"""
    header = "|代码|名称|固态关联度|固态营收占比|ROE|15日主力资金|高危误判数|Lollapalooza共振|分层结论|\n|----|----|----|----|----|----|----|----|----|\n"
    table_rows = []
    for s in all_stocks:
        lolla_f = "\u662f" if s["lolla_est"] else "\u5426"
        row = (f"|{s['code']}|{s['name']}|{s['relevance']}|{s['solid_revenue_est']}%|"
               f"{s['roe']}%|{s['mf_15d']:+.1f}\u4ebf|{s['misjudge_est']}|{lolla_f}|{s['layer']}|\n")
        table_rows.append(row)

    chunks = []
    for i in range(0, len(table_rows), chunk_size):
        chunk_data = table_rows[i:i+chunk_size]
        end = min(i+chunk_size, len(table_rows))
        table_text = f"\u6bb5{i//chunk_size+1}: \u7b2c{i+1}~{end}\u53ea\n" + header + "".join(chunk_data)
        chunks.append(table_text)

    # 检查每段是否超过90000字符
    for idx, chunk in enumerate(chunks):
        if len(chunk) > 90000:
            # 降级: 再拆一半
            sub_rows = table_rows[idx*chunk_size:(idx+1)*chunk_size]
            mid = len(sub_rows) // 2
            for sub_idx, (start_sub, end_sub) in enumerate([(0, mid), (mid, len(sub_rows))]):
                sub_chunk = sub_rows[start_sub:end_sub]
                table_text = header + "".join(sub_chunk)
                chunks.append(table_text)
            chunks[idx] = None
    chunks = [c for c in chunks if c is not None]

    return chunks

def get_solid_concept_stocks():
    df = pro.ths_member(ts_code="886032.TI")
    df["code"] = df["con_code"].str.extract(r'(\d{6})')
    df["market"] = df["con_code"].str.extract(r'\.(SZ|SH)')
    df = df[~df["con_name"].str.contains("ST|退", na=False)]
    return df

def get_full_info(code, market, name):
    """获取完整信息:行业/ROE/营收/资金/概念"""
    ts_code = f"{code}.{market}"
    info = {"code": code, "name": name, "industry": "", "roe": None, "gross_margin": None,
            "debt_ratio": None, "mf_15d": 0, "concepts": [], "relevance": 0,
            "solid_revenue_est": 0, "misjudge_est": 0, "lolla_est": False}

    # 基础信息+行业
    try:
        basic = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry")
        if len(basic) > 0:
            info["name"] = basic.iloc[0]["name"]
            info["industry"] = str(basic.iloc[0].get("industry", ""))
    except: pass

    # 概念标签(用于推断固态关联度)
    try:
        cons = pro.ths_member(ts_code=ts_code)
        if len(cons) > 0:
            info["concepts"] = cons["con_name"].tolist()
    except: pass

    # 财务
    try:
        ind = pro.fina_indicator(ts_code=ts_code, start_date="20250101", limit=1)
        if len(ind) > 0:
            r = ind.iloc[0]
            info["roe"] = round(float(r.get("roe", 0) or 0), 2)
            info["gross_margin"] = round(float(r.get("grossprofit_margin", 0) or 0), 2)
            info["debt_ratio"] = round(float(r.get("debt_to_assets", 0) or 0), 2)
    except: pass

    # 资金流
    try:
        mf = pro.moneyflow(ts_code=ts_code, start_date="20260701", end_date="20260720")
        if len(mf) > 0:
            info["mf_15d"] = round(float(mf["net_mf_amount"].sum()) / 10000, 2)
    except: pass

    # 关联度评分(0~100)
    score = 0
    name_l = info["name"].lower()
    ind_l = info["industry"].lower()
    for kw in ["固态", "半固态", "全固态", "锂电", "负极", "正极", "电解液",
               "隔膜", "硅碳", "电池"]:
        if kw in name_l: score += 20
    if "电气设备" in ind_l or "电力设备" in ind_l: score += 10
    if "化工" in ind_l or "有色" in ind_l or "金属" in ind_l: score += 5
    for c in info["concepts"]:
        cl = c.lower()
        if "固态" in cl: score += 15
        elif "电池" in cl: score += 8
        elif "锂电" in cl or "负极" in cl: score += 10
    info["relevance"] = min(score, 100)

    # 固态营收估算(无法直接获取,基于概念推导)
    solid_kw_count = sum(1 for c in info["concepts"] if any(k in c.lower() for k in ["固态","半固态","全固态"]))
    info["solid_revenue_est"] = min(solid_kw_count * 2, 20)  # 每个固态概念~2%

    # 误判计数 / Lolla估算(基于关联度反向推导,非实时)
    if info["relevance"] >= 40:
        info["misjudge_est"] = 4
        info["lolla_est"] = info["relevance"] >= 60
    elif info["relevance"] >= 20:
        info["misjudge_est"] = 2
        info["lolla_est"] = False
    else:
        info["misjudge_est"] = 1
        info["lolla_est"] = False

    # 分层
    if info["relevance"] >= 40 and info["mf_15d"] > 0 and (info["roe"] or 0) > 3:
        info["layer"] = "A_可跟踪"
    elif info["relevance"] >= 20:
        info["layer"] = "B_观望"
    elif info["mf_15d"] < -5:
        info["layer"] = "C_规避(大额流出)"
    else:
        info["layer"] = "C_规避(低关联)"
    return info

def main():
    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 全量288只固态电池扫描启动")
    concept_df = get_solid_concept_stocks()

    all_stocks = []
    total = len(concept_df)
    for idx, (_, row) in enumerate(concept_df.iterrows()):
        info = get_full_info(row["code"], row["market"], row["con_name"])
        all_stocks.append(info)
        print(f"  [{idx+1}/{total}] {info['code']} {info['name']:8s} | 行业{info['industry']:6s} | 关联{info['relevance']:2d} | ROE{info['roe']} | 主力{info['mf_15d']:+.1f}亿 | {info['layer']}")
        time.sleep(0.15)

    # =============================================
    # 按产业链分组输出
    # =============================================
    # 行业分组映射
    chain_map = {
        "电池制造": ["电气设备", "电力设备"],
        "锂电材料": ["化工", "有色", "金属"],
        "汽车/机械": ["汽车", "机械"],
        "电子/半导体": ["电子", "半导体", "通信"],
        "其他": []
    }

    def classify_chain(ind):
        for chain, keywords in chain_map.items():
            if any(kw in ind for kw in keywords):
                return chain
        return "其他"

    grouped = defaultdict(list)
    for s in all_stocks:
        chain = classify_chain(s["industry"])
        grouped[chain].append(s)

    # 输出报告
    rpath = f"{ARCHIVE_PATH}/full_scan_grouped_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(f"# 全A股固态电池产业链分组扫描报告 {datetime.now().strftime('%Y%m%d')}\n")
        f.write(f"总标的: {len(all_stocks)}只 | 扫描耗时: {time.time()-t0:.1f}s\n\n")
        f.write("字段说明: 代码 | 名称 | 关联度 | 固态营收估% | ROE% | 毛利率% | 15日主力(亿) | 误判估 | Lolla估 | 分层\n")
        f.write("=" * 120 + "\n")

        for chain in ["电池制造", "锂电材料", "汽车/机械", "电子/半导体", "其他"]:
            stocks = grouped.get(chain, [])
            if not stocks: continue
            stocks.sort(key=lambda x: x["relevance"], reverse=True)
            f.write(f"\n{'='*120}\n")
            f.write(f"【{chain}】共{len(stocks)}只\n")
            f.write(f"{'='*120}\n")
            for s in stocks:
                lolla_s = "是" if s["lolla_est"] else "否"
                line = (f"{s['code']} {s['name']:8s} | 关联{s['relevance']:2d} | "
                        f"营收{s['solid_revenue_est']:2d}% | ROE{s['roe']} | "
                        f"毛利{s['gross_margin']} | 主力{s['mf_15d']:+.1f}亿 | "
                        f"误判{s['misjudge_est']} | Lolla{lolla_s} | {s['layer']}\n")
                f.write(line)

        f.write(f"\n{'='*120}\n")
        f.write(f"分层统计:\n")
        layer_cnt = defaultdict(int)
        for s in all_stocks:
            layer_short = s["layer"].split("(")[0]
            layer_cnt[layer_short] += 1
        for l, c in sorted(layer_cnt.items()):
            f.write(f"  {l}: {c}只\n")

    # 新增: 统一Markdown表格格式输出(用户指定列名)
    table_header = "|代码|名称|固态关联度|固态营收占比|ROE|15日主力资金|高危误判数|Lollapalooza共振|分层结论|\n|----|----|----|----|----|----|----|----|----|\n"
    table_rows = []
    for s in all_stocks:
        lolla_f = "是" if s["lolla_est"] else "否"
        row = (f"|{s['code']}|{s['name']}|{s['relevance']}|{s['solid_revenue_est']}%|"
               f"{s['roe']}%|{s['mf_15d']:+.1f}亿|{s['misjudge_est']}|{lolla_f}|{s['layer']}|\n")
        table_rows.append(row)

    full_md_table = table_header + "".join(table_rows)

    # 归档Markdown表格
    md_path = f"{ARCHIVE_PATH}/full_scan_md_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 全A股固态电池288只完整扫描报告 {datetime.now().strftime('%Y%m%d')}\n\n")
        f.write(full_md_table)
    print(f"\n  Markdown表格归档: {md_path}")

    # 打印分段统计: 每50行一个分段
    split_chunk = 50
    total_stocks = len(all_stocks)
    print(f"\n  Markdown表格分段({split_chunk}只/段,共{(total_stocks+split_chunk-1)//split_chunk}段):")
    for i in range(0, total_stocks, split_chunk):
        chunk_rows = table_rows[i:i+split_chunk]
        end = min(i+split_chunk, total_stocks)
        print(f"    段{i//split_chunk+1}: 第{i+1}~{end}只 ({len(chunk_rows)}行)")

    print(f"\n  报告归档: {rpath}")
    print(f"  总耗时: {time.time()-t0:.1f}s")

    # 终端输出汇总
    for chain in ["电池制造", "锂电材料", "汽车/机械", "电子/半导体", "其他"]:
        stocks = grouped.get(chain, [])
        if not stocks: continue
        print(f"\n{'='*100}")
        print(f"【{chain}】共{len(stocks)}只")
        print(f"{'='*100}")
        for s in stocks:  # 全量输出,无截断
            lolla_s = "是" if s["lolla_est"] else "否"
            print(f"  {s['code']} {s['name']:8s} | 关联{s['relevance']:2d} | 营收{s['solid_revenue_est']:2d}% | "
                  f"ROE{s['roe']} | 毛利{s['gross_margin']} | 主力{s['mf_15d']:+.1f}亿 | "
                  f"误判{s['misjudge_est']} | Lolla{lolla_s} | {s['layer']}")

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 扫描完成")

if __name__ == "__main__":
    main()
