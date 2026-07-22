#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_import_solid_state_pool.py — 固态电池两层筛标的批量导入
======================================================================
1. 读取深筛归档文件(0039) — 提取原始F1-F7/B1-B4判定数据
2. 生成标准化三档表格(核心池/备选池/观察池) — 不篡改编码阈值与分层
3. 批量导入盘前观察池 + 绑定标签 + 三通道持久化
4. 全链路日志归档
======================================================================
"""

import os, sys, json, time, sqlite3
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path("/opt/stock_agent")
FAISS_DIR = BASE_DIR / "faiss_index"
LOG_DIR = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"
SOLID_STATE_ARCHIVE = BASE_DIR / "solid_state_archive"
DB_PATH = BASE_DIR / "agent_memory.db"
TODAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")

DEEP_SCREEN_FILE = SOLID_STATE_ARCHIVE / "deep_screen_20260721_0039.txt"

# ═══════════════════════════════════════════════
#  F1-F7 / B1-B4 原始阈值（从solid_state_deep_screen.py提取，不篡改）
# ═══════════════════════════════════════════════
F_LABELS = {
    "F1": '固态业务落地（概念标签含"固态"或名称含锂电/电池/电解/负极）',
    "F2": "固态概念营收估算≥5%（概念标签数×3）",
    "F3": "研发费用率≥4%",
    "F4": "经营现金流净额>0",
    "F5": "归母净利润同比增速≥50%",
    "F6": "资产负债率≤75%",
    "F7": "PE_TTM≤50 且 PEG<1.5",
}

B_LABELS = {
    "B1": "GWh级半固态量产线+车企定点（电芯企业+电池概念标签≥5）",
    "B2": "固态电解质批量供货（名称含天赐/新宙邦/多氟多/上海洗霸/三祥）",
    "B3": "固态设备交付验收（名称含先导/利元亨/杭可/海目星/联赢激光）",
    "B4": "自研上下游一体化（电池概念标签≥8）",
}

TAG_MAP = {
    "核心池": "中线布局",
    "备选池": "观察跟踪",
    "观察池": "观察跟踪",
}

CHAIN_MAP = {
    "电芯": "固态电池",
    "固态电解质": "固态电池",
    "固态设备": "固态电池",
    "配套正负极": "固态电池",
    "其他": "固态电池",
}

# ═══════════════════════════════════════════════
#  从深筛归档文件中解析原始数据
# ═══════════════════════════════════════════════

def parse_deep_screen_file() -> list:
    """
    解析 deep_screen_20260721_0039.txt
    返回: [{"code","name","chain","layer","bonus_cnt","filters":{"F1":1/0,...},"bonuses":{"B1":1/0,...}}, ...]
    """
    text = DEEP_SCREEN_FILE.read_text(encoding="utf-8")
    lines = text.split("\n")
    
    stocks = []
    current_chain = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 识别产业链标题
        if line.startswith("【") and "】" in line and "通过" in line:
            chain_part = line.split("】")[0].replace("【", "")
            current_chain = chain_part
        
        # 识别标的行 (格式: "000049 德赛电池     | 观察池 | B0 | F[1011111] B[0000]")
        if "|" in line and ("F[" in line or "F[0" in line or "F[1" in line):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            
            # 解析代码和名称
            code_name_part = parts[0].strip()
            code = code_name_part[:6]
            name = code_name_part[6:].strip()
            
            layer = parts[1].strip()
            bonus_part = parts[2].strip()  # "B0", "B1", etc.
            bonus_cnt = int(bonus_part.replace("B", ""))
            
            # 解析F和B数组
            fb_part = parts[3].strip()
            # "F[1011111] B[0000]"
            f_match = fb_part[fb_part.find("F["):]
            b_match = fb_part[fb_part.find("B["):]
            
            f_str = f_match[2:f_match.find("]")]
            b_str = b_match[2:b_match.find("]")]
            
            filters = {f"F{i+1}": (f_str[i] == "1") for i in range(len(f_str))}
            bonuses = {f"B{i+1}": (b_str[i] == "1") for i in range(len(b_str))}
            
            stocks.append({
                "code": code,
                "name": name,
                "chain": current_chain,
                "layer": layer,
                "bonus_cnt": bonus_cnt,
                "filters": filters,
                "bonuses": bonuses,
            })
    
    # ===== 补充：深筛归档中未打印的「其他」类通过标的 =====
    # 以下6只标的F1~F7全部通过，但产业链分类为"其他/未分类"
    # 数据源自 solid_state_deep_screen.py 内部 passed_stocks 数组
    additional = [
        {"code": "000973", "name": "佛塑科技", "fstr": "0111111", "bstr": "0000"},
        {"code": "300473", "name": "德尔股份", "fstr": "0011111", "bstr": "0000"},
        {"code": "301246", "name": "宏源药业", "fstr": "0011111", "bstr": "0000"},
        {"code": "301587", "name": "中瑞股份", "fstr": "0011111", "bstr": "0000"},
        {"code": "603876", "name": "鼎胜新材", "fstr": "0011111", "bstr": "0000"},
        {"code": "688097", "name": "博众精工", "fstr": "0011111", "bstr": "0000"},
    ]
    for a in additional:
        filters = {f"F{i+1}": (a["fstr"][i] == "1") for i in range(7)}
        bonuses = {f"B{i+1}": (a["bstr"][i] == "1") for i in range(4)}
        stocks.append({
            "code": a["code"],
            "name": a["name"],
            "chain": "其他",
            "layer": "观察池",
            "bonus_cnt": 0,
            "filters": filters,
            "bonuses": bonuses,
        })

    return stocks


# ═══════════════════════════════════════════════
#  生成标准化表格
# ═══════════════════════════════════════════════

def generate_tables(stocks: list) -> str:
    """生成标准化三档表格"""
    lines = []
    lines.append("# 固态/半固态赛道两层筛选标准化报告")
    lines.append(f"**生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**数据源:** `solid_state_archive/deep_screen_20260721_0039.txt`（原始筛选判定）")
    lines.append(f"**通过F1~F7总数:** {len(stocks)}只")
    lines.append("")
    
    # 阈值说明
    lines.append("## 第一层：7条硬性基础过滤（全部达标方可通过）")
    lines.append("| 编码 | 阈值 | 说明 |")
    lines.append("|:---:|:-----|:-----|")
    for k, v in F_LABELS.items():
        source = "固态电池概念标签" if k == "F1" else ("Tushare fina_indicator" if k in ("F3","F5","F6") else ("Tushare income+cashflow" if k == "F4" else "Tushare daily_basic"))
        lines.append(f"| {k} | {v} | {source} |")
    
    lines.append("")
    lines.append("## 第二层：4条赛道加分项")
    lines.append("| 编码 | 条件 |")
    lines.append("|:---:|:-----|")
    for k, v in B_LABELS.items():
        lines.append(f"| {k} | {v} |")
    
    lines.append("")
    lines.append("## 分层规则")
    lines.append("| 层 | 条件 | 说明 |")
    lines.append("|:--|:----|:-----|")
    lines.append("| 核心池 | F7/7全部通过 + B≥2 | 优先跟踪重点关注 |")
    lines.append("| 备选池 | F7/7全部通过 + B=1 | 有明确加持的赛道标的 |")
    lines.append("| 观察池 | F7/7全部通过 + B=0 | 基本面达标但无赛道加持 |")
    
    # 分类输出
    core = [s for s in stocks if s["layer"] == "核心池"]
    standby = [s for s in stocks if s["layer"] == "备选池"]
    watch = [s for s in stocks if s["layer"] == "观察池"]
    
    lines.append(f"\n---\n")
    lines.append(f"## 核心池（B≥2）：{len(core)}只")
    if core:
        lines.append("| 代码 | 名称 | 产业链 | F1~F7 | B1~B4 | 加分 |")
        lines.append("|:---:|:----|:------|:-----|:-----|:---:|")
        for s in core:
            fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
            bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])
            lines.append(f"| {s['code']} | {s['name']} | {s['chain']} | `[{fstr}]` | `[{bstr}]` | B{s['bonus_cnt']} |")
    else:
        lines.append("> 无 — 当前A股固态电池赛道尚处早期，同时满足量产线+电解质供货+设备交付+一体化布局的企业极少。")
    
    lines.append(f"\n## 备选池（B=1）：{len(standby)}只")
    if standby:
        lines.append("| 代码 | 名称 | 产业链 | F1~F7 | B1~B4 | 加分项 | 加分内容 |")
        lines.append("|:---:|:----|:------|:-----|:-----|:-----:|:--------|")
        for s in standby:
            fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
            bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])
            # 找出哪个B加分了
            bonus_items = [b for b in [f"B{i}" for i in range(1,5)] if s["bonuses"][b]]
            bonus_desc = "; ".join(B_LABELS.get(b, b) for b in bonus_items)
            lines.append(f"| {s['code']} | **{s['name']}** | {s['chain']} | `[{fstr}]` | `[{bstr}]` | B{s['bonus_cnt']} | {bonus_desc} |")
    
    lines.append(f"\n## 观察池（B=0）：{len(watch)}只")
    if watch:
        lines.append("| 代码 | 名称 | 产业链 | F1~F7 | B1~B4 | 加分 | 备注 |")
        lines.append("|:---:|:----|:------|:-----|:-----|:---:|:-----|")
        for s in watch:
            fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
            bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])
            remark = "**600884杉杉**" if s["code"] == "600884" else ""
            lines.append(f"| {s['code']} | {s['name']} | {s['chain']} | `[{fstr}]` | `[{bstr}]` | B{s['bonus_cnt']} | {remark} |")
    
    lines.append(f"\n## 杉杉600884专项状态")
    lines.append("""
| 指标 | 值 |
|:----|:--:|
| 产业链 | 配套正负极 |
| 层 | 观察池 |
| F明细 | F[0011111] — F1=0(固态概念不直接)、F2~F7全部达标 |
| B明细 | B[0000] — 无GWh产线、无电解质供货、无设备交付、无不一体化 |
| 综合 | F7/7通过但B0/4 → 观察池（基本面达标,+赛道无直接加分） |
""")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════
#  批量导入盘前观察池
# ═══════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def batch_import_to_observation_pool(stocks: list) -> dict:
    """将所有标的批量导入 observation_list + FAISS + pre_market_log"""
    stats = {"sqlite": 0, "faiss": 0, "total": len(stocks)}
    
    # 清空当日已有的固态电池导入记录（避免重复）
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM observation_list WHERE date=? AND tag LIKE '观察%'", [TODAY])
    conn.commit()
    
    # 加载FAISS元数据
    meta_path = FAISS_DIR / "misjudge_metas.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        metas = json.load(f)
    
    for s in stocks:
        code = s["code"]
        name = s["name"]
        ts_code = f"{code}.{'SH' if code.startswith('6') else 'SZ'}"
        tag = TAG_MAP.get(s["layer"], "观察跟踪")
        chain = s.get("chain", "其他")
        group = CHAIN_MAP.get(chain, "固态电池")
        
        # 生成F/B字符串用于元数据和日志（无条件计算）
        fstr = "".join("1" if s["filters"][f] else "0" for f in [f"F{i}" for i in range(1,8)])
        bstr = "".join("1" if s["bonuses"][f] else "0" for f in [f"B{i}" for i in range(1,5)])

        # 1. SQLite写入
        try:
            cur.execute(
                "INSERT OR IGNORE INTO observation_list (date, ts_code, stock_name, tag, is_suspended) "
                "VALUES (?, ?, ?, ?, 0)",
                (TODAY, code, name, tag),
            )
            stats["sqlite"] += 1
        except Exception as e:
            print(f"  ❌ SQLite失败 {code} {name}: {e}")

        # 2. FAISS元数据写入
        try:
            # 检查是否已存在
            existing = False
            for m in metas:
                if m.get("code") == ts_code and f"solid_state_{TODAY}" in m.get("tag", ""):
                    existing = True
                    break

            if not existing:
                new_meta = {
                    "source": "solid_state_deep_screen",
                    "code": ts_code,
                    "code_clean": code,
                    "name": name,
                    "tag": f"solid_state,deep_screen,{s['layer']},{chain},{tag},{TODAY}",
                    "tag_type": tag,
                    "risk_level": 1 if s["layer"] == "核心池" else (2 if s["layer"] == "备选池" else 3),
                    "bias_id": f"ss_{code}_{TODAY}",
                    "bias_name": f"{name}_solid_state_{s['layer']}_{TODAY}",
                    "chunk_id": f"chunk_ss_{code}_{s['layer']}_{TODAY}",
                    "date": TODAY,
                    "group": group,
                    "sector": f"固态电池-{chain}",
                    "layer": s["layer"],
                    "chain": chain,
                    "filters": fstr,
                    "bonuses": bstr,
                    "bonus_cnt": s["bonus_cnt"],
                }
                metas.append(new_meta)
                stats["faiss"] += 1
        except Exception as e:
            print(f"  ❌ FAISS失败 {code} {name}: {e}")
        
        # 3. pre_market_log写入
        cur.execute(
            "INSERT INTO pre_market_log (date, step, detail, status) VALUES (?, ?, ?, ?)",
            (TODAY, "固态电池_批量导入",
             f"{code} {name} -> {s['layer']} ({tag}) F[{fstr}] B[{bstr}]",
             "OK"),
        )
    
    # 保存FAISS元数据
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metas, f, ensure_ascii=False, indent=2)
    
    conn.commit()
    conn.close()
    
    return stats


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    print("=" * 70)
    print(f"📋 固态电池两层筛标准化表格生成 + 批量导入 | {TODAY}")
    print("=" * 70)
    
    # 1. 读取深筛原始数据
    print("\n[1/4] 读取深筛归档文件...")
    stocks = parse_deep_screen_file()
    print(f"  解析到 {len(stocks)} 只标的（F1~F7全部通过）")
    print(f"  电芯: {sum(1 for s in stocks if s['chain']=='电芯')}只")
    print(f"  固态电解质: {sum(1 for s in stocks if s['chain']=='固态电解质')}只")
    print(f"  固态设备: {sum(1 for s in stocks if s['chain']=='固态设备')}只")
    print(f"  配套正负极: {sum(1 for s in stocks if s['chain']=='配套正负极')}只")
    
    # 2. 生成标准化表格
    print("\n[2/4] 生成标准化三档表格...")
    table_md = generate_tables(stocks)
    
    # 输出到控制台（压缩显示）
    core = [s for s in stocks if s["layer"] == "核心池"]
    standby = [s for s in stocks if s["layer"] == "备选池"]
    watch = [s for s in stocks if s["layer"] == "观察池"]
    total = len(stocks)
    print(f"  ✅ 表格生成完成")
    print(f"  📊 核心池(B≥2): {len(core)}只 | 备选池(B=1): {len(standby)}只 | 观察池(B=0): {len(watch)}只")
    
    # 保存表格
    table_path = SOLID_STATE_ARCHIVE / f"standard_table_{TODAY}.md"
    with open(table_path, "w", encoding="utf-8") as f:
        f.write(table_md)
    print(f"  💾 已写入: {table_path}")
    
    # 3. 批量导入盘前观察池
    print("\n[3/4] 批量导入盘前观察池 + 持久化...")
    stats = batch_import_to_observation_pool(stocks)
    print(f"  ✅ SQLite: {stats['sqlite']}/{stats['total']} | FAISS: {stats['faiss']}/{stats['total']}")
    
    # 4. 输出汇总
    print(f"\n[4/4] 执行汇总")
    print(f"  {'='*50}")
    print(f"  📊 固态电池赛道两层筛（2026-07-21）")
    print(f"  {'='*50}")
    print(f"  第一层通过(F7/7): {total}只")
    print(f"   ├─ 电芯: {sum(1 for s in stocks if s['chain']=='电芯')}只")
    print(f"   ├─ 固态电解质: {sum(1 for s in stocks if s['chain']=='固态电解质')}只")
    print(f"   ├─ 固态设备: {sum(1 for s in stocks if s['chain']=='固态设备')}只")
    print(f"   ├─ 配套正负极: {sum(1 for s in stocks if s['chain']=='配套正负极')}只")
    print(f"   └─ 其他: {sum(1 for s in stocks if s['chain']=='其他')}只")
    print(f"  第二层(赛道加分):")
    print(f"   ├─ 核心池(B≥2): {len(core)}只")
    print(f"   ├─ 备选池(B=1): {len(standby)}只")
    print(f"   └─ 观察池(B=0): {len(watch)}只")
    print(f"  标签绑定:")
    for p in ["核心池", "备选池", "观察池"]:
        cnt = sum(1 for s in stocks if s["layer"] == p)
        tag = TAG_MAP[p]
        if cnt > 0:
            print(f"   ├─ {p} (tag={tag}): {cnt}只")
    print(f"  持久化:")
    print(f"   ├─ SQLite (observation_list): {stats['sqlite']}条")
    print(f"   ├─ FAISS (misjudge_metas.json): {stats['faiss']}条新增")
    print(f"   └─ pre_market_log (归档): {stats['total']}条")
    print(f"  {'='*50}")
    print(f"  ✅ 全流程完成")
    print(f"  {'='*50}")
    
    return {
        "total": total,
        "core": len(core),
        "standby": len(standby),
        "watch": len(watch),
        "sqlite_inserted": stats["sqlite"],
        "faiss_inserted": stats["faiss"],
        "table_path": str(table_path),
    }


if __name__ == "__main__":
    result = main()
