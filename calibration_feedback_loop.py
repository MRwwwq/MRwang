"""
calibration_feedback_loop.py — 校准数据驱动的智能体进化反馈环
读取 trade_calibration 历史校准数据 → 检测4类问题 → 输出调参建议
"""
import psycopg2, os, json, sys
sys.path.insert(0, '/opt/stock_agent')

DB_PARAMS = dict(host="127.0.0.1", port=5432, dbname="stock_data", user="stock_user", sslmode="require")
def _get_conn():
    with open(os.path.expanduser("~/.pgpass")) as f:
        DB_PARAMS["password"] = f.read().strip().split(":")[-1]
    return psycopg2.connect(**DB_PARAMS)

# ============================================================
# 读取校准数据
# ============================================================
def load_calibration(ticker: str = None, limit: int = 30):
    conn = _get_conn()
    cur = conn.cursor()
    where = "WHERE ticker=%s" if ticker else ""
    params = (ticker,) if ticker else ()
    cur.execute(f"""
        SELECT to_char(trade_date,'YYYYMMDD'), ticker, real_change_pct, close_price,
               error_label, misjudge_hit_count, solid_tech_tag, qclaw_rule_id, rag_match_score,
               ai_pred, ai_risk_tip, is_trapped
        FROM trade_calibration {where}
        ORDER BY trade_date DESC LIMIT %s
    """, params + (limit,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

# ============================================================
# 问题1: RAG向量检索匹配度偏低
# ============================================================
def detect_rag_low_match(records: list) -> list:
    issues = []
    for r in records:
        dt, tk, pct, price, tag, hit, solid, qclaw, rag, pred, tip, trapped = r
        rag_f = float(rag) if rag else 0.0
        if 0 < rag_f < 0.5:
            issues.append({
                "date": dt, "ticker": tk, "rag_score": rag_f,
                "problem": "RAG向量匹配度<0.5,FAISS召回精度不足",
                "action": "优化FAISS文本拼接:上调tag权重10→15,上调lolla_trigger权重12→18"
            })
    return issues

# ============================================================
# 问题2: 固态题材误判共振频繁
# ============================================================
def detect_solid_state_misjudge(records: list) -> dict:
    solid_entries = [r for r in records if r[6] != "无固态题材"]
    total = len(solid_entries)
    if total < 2:
        return {"issue": False, "msg": "固态题材样本不足,暂不调参"}
    resonance_hits = sum(1 for r in solid_entries if "共振" in (r[4] or ""))
    resonance_ratio = resonance_hits / total
    suggestion = {
        "total_samples": total,
        "resonance_count": resonance_hits,
        "resonance_ratio": f"{resonance_ratio:.0%}"
    }
    if resonance_ratio > 0.5:
        suggestion["problem"] = f"固态题材共振率{resonance_ratio:.0%}>50%,Rule021阈值偏松"
        suggestion["action"] = "上调Rule021固态题材风险加权:score_threshold 30→45,retail_inflow_threshold 0.6→0.5"
        suggestion["issue"] = True
    else:
        suggestion["msg"] = f"固态题材共振率{resonance_ratio:.0%}正常范围,暂不调参"
        suggestion["issue"] = False
    return suggestion

# ============================================================
# 问题3: 多误判共振风控失效
# ============================================================
def detect_lolla_veto_failure(records: list) -> list:
    issues = []
    for r in records:
        dt, tk, pct, price, tag, hit, solid, qclaw, rag, pred, tip, trapped = r
        hit_i = int(hit) if hit else 0
        tag_s = tag or ""
        # 误判数>=4但风险标签不是"共振强化"或"风控有效" → Rule007可能失效
        if hit_i >= 4 and "风控" not in tag_s and pct is not None and float(pct) <= -2:
            issues.append({
                "date": dt, "ticker": tk, "misjudge_count": hit_i,
                "error_label": tag_s, "real_pct": float(pct),
                "problem": f"芒格误判{hit_i}项但标签'{tag_s}'未标记风控有效,Rule007一票否决分值可能偏低",
                "action": "上调Rule007一票否决阈值:severity 5→7; misjudge_threshold 3→2"
            })
    return issues

# ============================================================
# 问题4: 入场条件频繁失效
# ============================================================
def detect_entry_failure(records: list) -> dict:
    entry_calls = [r for r in records if r[10] == "提示入场"]
    total = len(entry_calls)
    if total < 2:
        return {"issue": False, "msg": "入场信号样本不足,暂不调参"}
    trapped = sum(1 for r in entry_calls if r[11])
    trap_ratio = trapped / total
    suggestion = {
        "total_entry_signals": total,
        "trapped_count": trapped,
        "trap_ratio": f"{trap_ratio:.0%}"
    }
    if trap_ratio > 0.3:
        suggestion["problem"] = f"入场被套率{trap_ratio:.0%}>30%,入场条件偏松"
        suggestion["action"] = "收紧五维综合评分入场门槛:基线65→75;新增固态题材/QClaw触发时为否决因子"
        suggestion["issue"] = True
    else:
        suggestion["msg"] = f"入场被套率{trap_ratio:.0%}正常范围"
        suggestion["issue"] = False
    return suggestion

# ============================================================
# 主入口: 全量反馈分析
# ============================================================
def run_feedback_loop(ticker: str = "600884"):
    records = load_calibration(ticker, limit=30)
    print(f"========== 校准数据驱动进化反馈环 ==========")
    print(f"标的:{ticker} 加载校准记录:{len(records)}条\n")
    
    # 问题1
    rag_issues = detect_rag_low_match(records)
    if rag_issues:
        print(f"\033[93m[问题1] RAG向量匹配度偏低 ({len(rag_issues)}次)\033[0m")
        for i in rag_issues[:3]:
            print(f"  {i['date']} rag={i['rag_score']} → {i['action']}")
    else:
        print(f"\033[92m[问题1] RAG向量匹配度正常\033[0m")
    
    # 问题2
    solid = detect_solid_state_misjudge(records)
    print(f"\033[{'93m' if solid.get('issue') else '92m'}[问题2] 固态题材误判共振\033[0m")
    print(f"  {json.dumps(solid, ensure_ascii=False, indent=2)}")
    
    # 问题3
    lolla_issues = detect_lolla_veto_failure(records)
    if lolla_issues:
        print(f"\033[93m[问题3] 多误判共振风控失效 ({len(lolla_issues)}次)\033[0m")
        for i in lolla_issues[:3]:
            print(f"  {i['date']} 误判{i['misjudge_count']}项 pct={i['real_pct']}% → {i['action']}")
    else:
        print(f"\033[92m[问题3] 多误判共振风控有效\033[0m")
    
    # 问题4
    entry = detect_entry_failure(records)
    print(f"\033[{'93m' if entry.get('issue') else '92m'}[问题4] 入场条件失效\033[0m")
    print(f"  {json.dumps(entry, ensure_ascii=False, indent=2)}")
    
    # 汇总
    needs_tuning = bool(rag_issues) or solid.get("issue") or bool(lolla_issues) or entry.get("issue")
    print(f"\n{'='*50}")
    if needs_tuning:
        print(f"\033[93m⏳ 检测到调参需求,建议本周日21:00进化Agent迭代\033[0m")
    else:
        print(f"\033[92m✅ 4类问题均正常范围内,无需调参\033[0m")
    return needs_tuning

# ============================================================
# Rule007 一票否决分值固化（§25联动）
# ============================================================
# 当前阈值(固化不可修改,以下为文档参考)
#   misjudge_threshold = 3   (≥3项高危 → Lollapalooza)
#   severity_base = 5        (写入memory_failure_signal的warning_level)
#   veto_score = 100         (Lollapalooza固定分数)
# 
# 调参方向(进化Agent):
#   若detect_lolla_veto_failure命中 → misjudge_threshold 3→2
#   若平均风险分>40但Lolla未触发  → severity_base 5→7

if __name__ == "__main__":
    run_feedback_loop("600884")
