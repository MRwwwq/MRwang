# memory_vector.py
import faiss
import numpy as np
import os
from config_memory import VECTOR_INDEX_PATH, TOP_K_SIMILAR
from memory_db import TradingMemoryDB


class VectorMemorySearch:
    def __init__(self):
        self.db = TradingMemoryDB()
        self.dim = None          # 特征向量维度自动识别
        self.index = None
        self._load_or_build_index()

    def _load_or_build_index(self):
        # 存在索引直接加载，不存在重建
        if os.path.exists(VECTOR_INDEX_PATH):
            self.index = faiss.read_index(VECTOR_INDEX_PATH)
            self.dim = self.index.d
        else:
            self._rebuild_index()

    def _rebuild_index(self):
        samples = self.db.get_all_samples()
        if not samples:
            self.dim = 10
            self.index = faiss.IndexFlatL2(self.dim)
            return
        vecs = np.array([s["feature"] for s in samples], dtype=np.float32)
        self.dim = vecs.shape[1]
        self.index = faiss.IndexFlatL2(self.dim)
        self.index.add(vecs)
        faiss.write_index(self.index, VECTOR_INDEX_PATH)

    # 新增一条向量到索引
    def add_vector(self, feat: np.ndarray):
        feat = feat.reshape(1, -1).astype(np.float32)
        self.index.add(feat)
        faiss.write_index(self.index, VECTOR_INDEX_PATH)

    # 检索相似历史样本下标
    def search_similar(self, current_feat: np.ndarray):
        current_feat = current_feat.reshape(1, -1).astype(np.float32)
        k = min(TOP_K_SIMILAR, self.index.ntotal)
        if k <= 0 or self.index.ntotal <= 0:
            return []
        dists, idx_list = self.index.search(current_feat, k)
        all_samples = self.db.get_all_samples()
        result = []
        seen = set()
        for idx in idx_list[0]:
            if idx < 0 or idx >= len(all_samples):
                continue
            key = all_samples[idx]["stock_code"] + str(all_samples[idx]["profit_rate"])
            if key not in seen:
                seen.add(key)
                result.append(all_samples[idx])
        return result
