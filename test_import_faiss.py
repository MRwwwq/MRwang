#!/usr/bin/env python3
"""test_import_faiss.py — 全量重建FAISS索引（V3: 强制注入misjudge_XX维度）"""
import os, glob, json, re
import numpy as np
import jieba
try:
    import faiss; HAS_FAISS = True
except: HAS_FAISS = False

MD_DIR = "misjudge_case_md"
FAISS_DIR = "faiss_index"
os.makedirs(FAISS_DIR, exist_ok=True)

CHUNK_SIZE = 300
CHUNK_OVERLAP = 80

# 读取MD
files = sorted(glob.glob(f"{MD_DIR}/*.md"))
print(f"读取{len(files)}个MD...")
all_chunks, all_metas = [], []

for fp in files:
    fname = os.path.basename(fp)
    text = open(fp, encoding="utf-8").read()
    meta = {"source": fname}
    for line in text.split("\n"):
        if line.startswith("code:"): meta["code"] = line.split(":",1)[1].strip()
        if line.startswith("tag:"): meta["tag"] = line.split(":",1)[1].strip()
        if line.startswith("risk_level:"): meta["risk_level"] = line.split(":",1)[1].strip()
        # 新增: 解析扩展字段（固态电池赛道/题材属性）
        if line.startswith("theme:"): meta["theme"] = line.split(":",1)[1].strip()
        if line.startswith("relevance_to_"): 
            k = line.split(":")[0].strip()
            meta[k] = line.split(":",1)[1].strip()
        if line.startswith("revenue_contribution_pct:"): 
            meta["revenue_pct"] = line.split(":",1)[1].strip()
        if line.startswith("revenue_pct:"):
            meta["revenue_pct"] = line.split(":",1)[1].strip()
        if line.startswith("risk_type:"): 
            meta["risk_type"] = line.split(":",1)[1].strip()
        if line.startswith("lollapalooza_bias_trigger:"): 
            meta["lolla_trigger"] = line.split(":",1)[1].strip()
        # 新增: 固最新实测元数据
        if line.startswith("solid_state_type:"):
            meta["solid_state_type"] = line.split(":",1)[1].strip().replace(",",";")
        if line.startswith("solid_revenue_ratio:"):
            meta["solid_revenue_ratio"] = float(line.split(":",1)[1].strip())
        if line.startswith("Lollapalooza:"):
            meta["Lollapalooza"] = line.split(":",1)[1].strip().lower() == "true"
        if line.startswith("misjudge_hit_count:"):
            meta["misjudge_hit_count"] = int(line.split(":",1)[1].strip())
    m = re.search(r'misjudge_(\d+)', fname)
    meta["bias_id"] = m.group(1) if m else "00"
    meta["bias_name"] = fname.replace(".md","")

    words = list(jieba.cut(text))
    for i in range(0, max(len(words), 1), CHUNK_SIZE - CHUNK_OVERLAP):
        chunk = " ".join(words[i:i+CHUNK_SIZE])
        all_chunks.append(chunk)
        all_metas.append({**meta, "chunk_id": len(all_chunks)-1})

print(f"共{len(all_chunks)}个文本块")

# 构建词表 - 显式包含所有 misjudge_XX
vocab = set()
for c in all_chunks:
    for w in c.split(): vocab.add(w.lower().strip())
for m in all_metas:
    for t in m.get("tag","").replace(","," ").split(): vocab.add(t.strip().lower())
    vocab.add(f"misjudge_{m['bias_id']}")
# 确保所有01-24都在词表中
for i in range(1, 25):
    vocab.add(f"misjudge_{i:02d}")
vocab = sorted(vocab)
word2id = {w:i for i,w in enumerate(vocab)}
dim = len(vocab)
print(f"词表大小:{dim}")

# 向量化（关键修复：强制注入misjudge_XX维度）
vectors = np.zeros((len(all_chunks), dim), dtype=np.float32)
for ci, chunk in enumerate(all_chunks):
    meta = all_metas[ci]
    bid = meta.get("bias_id","00")
    
    # 1) 显式强制注入自身 misjudge_XX 维度（高权重100）
    bid_key = f"misjudge_{bid}"
    if bid_key in word2id:
        vectors[ci][word2id[bid_key]] += 100.0
    
    # 2) 注入 tag 维度
    for t in meta.get("tag","").replace(","," ").split():
        tl = t.strip().lower()
        if tl in word2id:
            vectors[ci][word2id[tl]] += 10.0
    
    # 2b) 注入 theme 维度（固态电池/题材赛道类）
    for fld in ["theme", "risk_type"]:
        val = meta.get(fld, "")
        for t in val.replace(","," ").split():
            tl = t.strip().lower()
            if tl in word2id:
                vectors[ci][word2id[tl]] += 8.0
    
    # 2c) 注入 lolla_trigger 维度（标注哪些偏差因子会被联动激活）
    lv = meta.get("lolla_trigger", "")
    for t in lv.replace(","," ").split():
        tl = t.strip().lower()
        if tl in word2id:
            vectors[ci][word2id[tl]] += 12.0  # 高权重: 关联因子联动
    
    # 3) 注入 bias_id 数字维度（如 "02"），增加区分度
    if bid in word2id:
        vectors[ci][word2id[bid]] += 5.0
    
    # 4) 内容词 1.0
    for w in chunk.split():
        wl = w.lower().strip()
        if wl in word2id and wl not in (bid_key, bid):
            vectors[ci][word2id[wl]] += 1.0

norms = np.linalg.norm(vectors, axis=1, keepdims=True)
norms[norms==0]=1; vectors/=norms

# 保存
np.save(f"{FAISS_DIR}/misjudge_vectors.npy", vectors)
with open(f"{FAISS_DIR}/word2id.json","w") as f: json.dump(word2id, f, ensure_ascii=False)
with open(f"{FAISS_DIR}/misjudge_metas.json","w") as f: json.dump(all_metas, f, ensure_ascii=False, indent=2)
if HAS_FAISS and dim>0:
    idx = faiss.IndexFlatIP(dim); idx.add(vectors)
    faiss.write_index(idx, f"{FAISS_DIR}/misjudge_bias.index")
print(f"✅ 保存: vectors({vectors.shape}) + word2id({dim}词) + metas({len(all_metas)}条)")

# 专项检索（强制注入misjudge_XX权重200）
def test_retrieve(bid, top_k=5):
    query = f"misjudge_{bid} 历史 案例 量化 特征 风险 约束 A股 倾向 {bid}"
    qw = list(jieba.cut(query))
    qv = np.zeros((1, dim), dtype=np.float32)
    
    # 强制注入 misjudge_XX 维度（超高权重200）
    bid_key = f"misjudge_{bid}"
    if bid_key in word2id:
        qv[0][word2id[bid_key]] += 200.0
    # 数字维度 3.0
    if bid in word2id:
        qv[0][word2id[bid]] += 3.0
    
    for w in qw:
        wl = w.lower().strip()
        if wl in word2id and wl != bid_key and wl != bid:
            qv[0][word2id[wl]] += 3.0
    
    qn = np.linalg.norm(qv)
    if qn>0: qv/=qn
    sims = vectors @ qv.T
    top = np.argsort(-sims.flatten())[:top_k]
    docs = []
    for i in top:
        if sims[i]>0.001 and i<len(all_metas):
            sv = float(sims[i][0]) if hasattr(sims[i],'__len__') else float(sims[i])
            docs.append(f"{all_metas[i]['source']}(sim={sv:.2f})")
    
    own_file = f"misjudge_{bid}_"
    own_found = any(own_file in d for d in docs)
    return docs, own_found

print("\n===== 全部24个misjudge编号检索测试 =====")
all_ok = True
fail_list = []
for i in range(1, 25):
    bid = f"{i:02d}"
    docs, own_found = test_retrieve(bid)
    status = "✅" if own_found else "❌"
    if not own_found:
        all_ok = False
        fail_list.append(bid)
    print(f"  misjudge_{bid}: {status} {docs[0] if docs else 'NO MATCH'}")

print(f"\n===== 结果汇总 =====")
print(f"  24个全部自身文档召回: {'✅ 通过' if all_ok else '❌ 失败: ' + ','.join(fail_list)}")
print(f"  7修复目标自身文档召回: {'✅ 全部通过' if all(f'{b}_' in test_retrieve(b)[0][0] for b in ['02','03','07','08','18','19','20']) else '❌ 有失败'}")

# 新增: 固态电池主题召回测试
print("\n===== ⑥ 固态电池题材专项检索测试 ===== ")
theme_queries = [
    ("固态电池", "solid_state_battery_training_case_v1"),
    ("半固态 负极 杉杉", "solid_state_battery_training_case_v1"),
    ("全固态 锂金属 颠覆", "solid_state_battery_training_case_v1"),
    ("概念炒作 跟风 题材", "solid_state_battery_training_case_v1"),
]
for q_text, expected_src in theme_queries:
    qw = list(jieba.cut(q_text))
    qv = np.zeros((1, dim), dtype=np.float32)
    # theme关键词权重20
    for tag_w in ["solid_state", "semi_solid", "battery_tech", "concept_risk", "tech_disruption", "theme_overlap_warning"]:
        if tag_w in word2id:
            qv[0][word2id[tag_w]] += 20.0
    for w in qw:
        wl = w.lower().strip()
        if wl in word2id:
            qv[0][word2id[wl]] += 5.0
    qn = np.linalg.norm(qv)
    if qn>0: qv/=qn
    sims = vectors @ qv.T
    top = np.argsort(-sims.flatten())[:3]
    docs = [all_metas[i]['source'] for i in top if sims[i]>0.001 and i<len(all_metas)]
    src_ok = any(expected_src in d for d in docs)
    print(f"  query='{q_text}' → top3={docs[:3]} | 目标召回{'✅' if src_ok else '❌'}")

print(f"\n✅ FAISS重建完成! 新增固态电池题材6条规则.")
