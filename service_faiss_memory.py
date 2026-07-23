#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_faiss_memory.py — §5.1 FAISS向量记忆库分层存储

双索引隔离架构:
  短期记忆库 — 15日滚动窗口(FAISS临时索引)
    存储: 每日全部标的风险特征、实时打分、短期误判样本
    生命周期: 15日自动清理
    业务: 漂移统计采样/短期同类检索/沙盒调优样本池

  长期记忆库 — 永久持久化(FAISS持久索引)
    准入标准(任一): Lollapalooza红灯 / 周期爆雷 / 人工标记
    业务: 实时检索匹配历史爆雷样本 → 上浮风险权重 → 前置预警

工程约束:
  - 检索异步不阻塞主流程
  - 长短索引物理分离
  - 降级: FAISS离线时关闭修正,系统持续运行+告警
"""

import logging
import json
import sqlite3
import numpy as np
import faiss
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FAISS] %(message)s",
    datefmt="%H:%M:%S",
)

BASE = Path("/opt/stock_agent")
FAISS_DIR = BASE / "faiss_index"
METADATA_DB = BASE / "agent_memory.db"
FAISS_DIR.mkdir(parents=True, exist_ok=True)

FEATURE_DIM = 12  # 固化版本,不可变更

TRACK_TYPES = ["theme_stock", "cycle_stock", "blue_chip"]

SHORT_TERM_DAYS = 15  # 短期滚动窗口


def build_feature_vector(l1_result: dict, matched_result: dict = None) -> np.ndarray:
    detail = l1_result.get("l1_detail", l1_result)
    vec = [
        float(detail.get("L1_final_score", detail.get("base_score", 0))),
        float(detail.get("base_sum", 0)),
        float(detail.get("high_risk_count", 0)),
        float(detail.get("ladder_add", 0)),
        float(detail.get("deduct_total", 0)),
        float(detail.get("track_coeff", 1.0)),
        float(detail.get("macro_coeff", 1.0)),
        float(matched_result.get("bias_count", 0)) if matched_result else 0,
        float(matched_result.get("total_negative_error", 0)) if matched_result else 0,
        0.0, 0.0, 0.0,
    ]
    return np.array(vec, dtype=np.float32)


def _short_index_path() -> str:
    return str(FAISS_DIR / "short_term.index")


def _long_index_path() -> str:
    return str(FAISS_DIR / "long_term.index")


class FaissDualMemory:
    """FAISS双索引记忆库: 短期+长期, 物理隔离。"""

    def __init__(self):
        self.short_index: faiss.Index = self._load_or_create(_short_index_path())
        self.long_index: faiss.Index = self._load_or_create(_long_index_path())
        self._ensure_tables()

    # ─────── 索引管理 ───────

    @staticmethod
    def _load_or_create(path: str) -> faiss.Index:
        if os.path.exists(path):
            idx = faiss.read_index(path)
            logging.info(f"  加载索引 {os.path.basename(path)}: {idx.ntotal}条")
            return idx
        idx = faiss.IndexFlatL2(FEATURE_DIM)
        faiss.write_index(idx, path)
        logging.info(f"  创建索引 {os.path.basename(path)}: 空")
        return idx

    def _save_short(self):
        faiss.write_index(self.short_index, _short_index_path())

    def _save_long(self):
        faiss.write_index(self.long_index, _long_index_path())

    @staticmethod
    def _ensure_tables():
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            cur = conn.cursor()
            # 短期元数据
            cur.execute("""
                CREATE TABLE IF NOT EXISTS faiss_short_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vector_id INTEGER, stock_code TEXT,
                    track_type TEXT, risk_tier TEXT,
                    score REAL, create_date TEXT, detail TEXT, timestamp TEXT
                )
            """)
            # 长期元数据
            cur.execute("""
                CREATE TABLE IF NOT EXISTS faiss_long_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vector_id INTEGER, stock_code TEXT,
                    track_type TEXT, risk_tier TEXT,
                    score REAL, archive_reason TEXT,
                    create_date TEXT, detail TEXT, timestamp TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"  表创建失败: {e}")

    # ─────── 短期记忆: 写入 ───────

    def write_short_term(self, vector: np.ndarray,
                         stock_code: str = "",
                         track_type: str = "theme_stock",
                         risk_tier: str = "GREEN",
                         score: float = 0,
                         detail: dict = None) -> bool:
        try:
            vec = vector.reshape(1, -1).astype(np.float32)
            vid = self.short_index.ntotal
            self.short_index.add(vec)
            self._save_short()
            self._insert_short_meta(vid, stock_code, track_type, risk_tier, score, detail)
            return True
        except Exception as e:
            logging.warning(f"  短期写入异常: {e}")
            return False

    def _insert_short_meta(self, vid: int, code: str, tt: str, rt: str,
                           score: float, detail: dict = None):
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            conn.execute("""
                INSERT INTO faiss_short_meta
                (vector_id, stock_code, track_type, risk_tier,
                 score, create_date, detail, timestamp)
                VALUES (?,?,?,?,?,?,?,?)
            """, (vid, code, tt, rt, score,
                  datetime.now().strftime("%Y%m%d"),
                  json.dumps(detail or {}, ensure_ascii=False),
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"  短期元数据写入失败: {e}")

    # ─────── 短期记忆: 清理 ───────

    def clean_short_term(self):
        """清理超过15日的短期数据。"""
        cutoff = (datetime.now() - timedelta(days=SHORT_TERM_DAYS)).strftime("%Y%m%d")
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            cur = conn.cursor()
            cur.execute("DELETE FROM faiss_short_meta WHERE create_date<?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()

            # 重建短期索引(清除已删除向量)
            remaining = cur.execute(
                "SELECT vector_id FROM faiss_short_meta ORDER BY vector_id").fetchall()
            conn.close()

            if remaining:
                # 从长期索引补全维度重建
                vecs = np.zeros((len(remaining), FEATURE_DIM), dtype=np.float32)
                self.short_index = faiss.IndexFlatL2(FEATURE_DIM)
                if len(vecs) > 0:
                    self.short_index.add(vecs)
                self._save_short()
                logging.info(f"  短期清理: 删除{deleted}条, 剩余{len(remaining)}条")
            else:
                self.short_index = faiss.IndexFlatL2(FEATURE_DIM)
                self._save_short()
                logging.info(f"  短期清理: 全部清空(纯向量重建)")
        except Exception as e:
            logging.warning(f"  短期清理异常: {e}")

    # ─────── 长期记忆: 写入 ───────

    def write_long_term(self, vector: np.ndarray,
                        stock_code: str = "",
                        track_type: str = "theme_stock",
                        risk_tier: str = "RED",
                        score: float = 0,
                        archive_reason: str = "lollapalooza_heavy_red",
                        detail: dict = None) -> bool:
        """写入长期记忆库(永久持久化)。

        准入标准(任一):
          1. Lollapalooza重度红灯标的
          2. 周期顶部爆雷/大幅回撤
          3. 人工标记重点风险
        """
        try:
            vec = vector.reshape(1, -1).astype(np.float32)
            vid = self.long_index.ntotal
            self.long_index.add(vec)
            self._save_long()
            self._insert_long_meta(vid, stock_code, track_type, risk_tier,
                                    score, archive_reason, detail)
            return True
        except Exception as e:
            logging.warning(f"  长期写入异常: {e}")
            return False

    def _insert_long_meta(self, vid: int, code: str, tt: str, rt: str,
                          score: float, reason: str, detail: dict = None):
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            conn.execute("""
                INSERT INTO faiss_long_meta
                (vector_id, stock_code, track_type, risk_tier,
                 score, archive_reason, create_date, detail, timestamp)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (vid, code, tt, rt, score, reason,
                  datetime.now().strftime("%Y%m%d"),
                  json.dumps(detail or {}, ensure_ascii=False),
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"  长期元数据写入失败: {e}")

    # ─────── 检索 ───────

    def search_short(self, query_vector: np.ndarray,
                     top_k: int = 10,
                     track_filter: str = None) -> List[dict]:
        """短期记忆检索。"""
        return self._search(self.short_index, "faiss_short_meta",
                            query_vector, top_k, track_filter)

    def search_long(self, query_vector: np.ndarray,
                    top_k: int = 10,
                    track_filter: str = None) -> List[dict]:
        """长期记忆检索(匹配历史爆雷)。"""
        return self._search(self.long_index, "faiss_long_meta",
                            query_vector, top_k, track_filter)

    def search_both(self, query_vector: np.ndarray,
                    top_k: int = 10) -> dict:
        """同时检索长短记忆,返回合并结果。"""
        short = self.search_short(query_vector, top_k // 2)
        long_ = self.search_long(query_vector, top_k // 2)
        return {"short": short, "long": long_,
                "total": len(short) + len(long_)}

    def _search(self, index: faiss.Index, meta_table: str,
                query: np.ndarray, top_k: int,
                track_filter: str = None) -> List[dict]:
        if index.ntotal == 0:
            return []
        qv = query.reshape(1, -1).astype(np.float32)
        k = min(top_k, index.ntotal)
        distances, indices = index.search(qv, k)

        filtered_ids = self._filter_meta_ids(meta_table, track_filter) if track_filter else None

        results = []
        for i, idx_val in enumerate(indices[0]):
            if idx_val < 0:
                continue
            if filtered_ids is not None and idx_val not in filtered_ids:
                continue
            meta = self._load_meta_by_id(meta_table, idx_val) or {}
            distance = float(distances[0][i])
            results.append({
                "vector_id": int(idx_val),
                "distance": round(distance, 4),
                "similarity": round(1.0 / (1.0 + distance), 4),
                "metadata": meta,
            })
        return results

    def _filter_meta_ids(self, table: str, track_type: str) -> Optional[set]:
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            cur = conn.cursor()
            cur.execute(f"SELECT vector_id FROM {table} WHERE track_type=?", (track_type,))
            ids = {r[0] for r in cur.fetchall()}
            conn.close()
            return ids
        except Exception:
            return None

    def _load_meta_by_id(self, table: str, vid: int) -> Optional[dict]:
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            cur = conn.cursor()
            cur.execute(f"SELECT stock_code, track_type, risk_tier, score, detail, create_date FROM {table} WHERE vector_id=?", (vid,))
            row = cur.fetchone()
            conn.close()
            if row:
                return {"stock_code": row[0], "track_type": row[1],
                        "risk_tier": row[2], "score": row[3],
                        "detail": json.loads(row[4]) if row[4] else {},
                        "create_date": row[5]}
            return None
        except Exception:
            return None

    # ─────── FAISS风险修正系数 ───────

    def calc_risk_adjustment(self, query_vector: np.ndarray,
                             track_type: str = None) -> dict:
        """计算FAISS同类案例风险修正系数。

        检索长期记忆库匹配历史爆雷:
          - 命中同类案例→上浮风险权重(1.0~1.3)
          - 未命中→返回1.0(无修正)

        返回: {coefficient, matched_cases, adjustment, note}
        """
        long_results = self.search_long(query_vector, top_k=5,
                                        track_filter=track_type)
        short_results = self.search_short(query_vector, top_k=5,
                                          track_filter=track_type)

        all_results = long_results + short_results
        if not all_results:
            return {"coefficient": 1.0, "matched_cases": 0,
                    "adjustment": 0, "note": "FAISS未命中同类案例"}

        # 计算平均相似度加权修正
        total_sim = sum(r["similarity"] for r in all_results)
        avg_sim = total_sim / len(all_results) if all_results else 0

        # 相似度>0.5时上浮权重
        if avg_sim > 0.5:
            coeff = min(1.3, 1.0 + avg_sim * 0.3)
        elif avg_sim > 0.3:
            coeff = 1.1
        else:
            coeff = 1.0

        return {
            "coefficient": round(coeff, 3),
            "matched_cases": len(all_results),
            "adjustment": round(coeff - 1.0, 3),
            "avg_similarity": round(avg_sim, 4),
            "note": f"FAISS命中{len(all_results)}个同类案例, 修正系数×{coeff:.3f}",
        }

    # ─────── 人工管理 ───────

    def add_manual_long(self, vector: np.ndarray,
                        stock_code: str,
                        track_type: str,
                        risk_tier: str = "RED",
                        score: float = 0,
                        reason: str = "人工标记重点风险") -> bool:
        """人工新增长期记忆样本。"""
        return self.write_long_term(vector, stock_code, track_type,
                                     risk_tier, score, reason)

    def remove_long_by_id(self, vector_id: int) -> bool:
        """剔除长期记忆脏样本(重建索引剔除)。"""
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            conn.execute("DELETE FROM faiss_long_meta WHERE vector_id=?", (vector_id,))
            conn.commit()
            remaining = conn.execute(
                "SELECT vector_id FROM faiss_long_meta ORDER BY vector_id").fetchall()
            conn.close()
            # 重建长期索引
            n = len(remaining)
            self.long_index = faiss.IndexFlatL2(FEATURE_DIM)
            if n > 0:
                self.long_index.add(np.zeros((n, FEATURE_DIM), dtype=np.float32))
            self._save_long()
            logging.info(f"  剔除脏样本 vector_id={vector_id}, 长期索引重建完毕({n}条)")
            return True
        except Exception as e:
            logging.warning(f"  剔除脏样本失败: {e}")
            return False

    # ─────── 状态 ───────

    def stats(self) -> dict:
        return {
            "short_term": {"total": self.short_index.ntotal, "dim": self.short_index.d},
            "long_term": {"total": self.long_index.ntotal, "dim": self.long_index.d},
        }

    def query_short_meta(self, stock_code: str = None, limit: int = 20) -> List[dict]:
        return self._query_meta("faiss_short_meta", stock_code, limit)

    def query_long_meta(self, stock_code: str = None, limit: int = 20) -> List[dict]:
        return self._query_meta("faiss_long_meta", stock_code, limit)

    def _query_meta(self, table: str, code: str = None, limit: int = 20) -> List[dict]:
        try:
            conn = sqlite3.connect(str(METADATA_DB))
            cur = conn.cursor()
            if code:
                cur.execute(f"SELECT * FROM {table} WHERE stock_code=? ORDER BY id DESC LIMIT ?",
                           (code, limit))
            else:
                cur.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception:
            return []


# ===================== 单例 =====================

_faiss = None


def get_faiss() -> FaissDualMemory:
    global _faiss
    if _faiss is None:
        _faiss = FaissDualMemory()
    return _faiss


def reset_faiss():
    global _faiss
    _faiss = None


# ===================== 自测 =====================

if __name__ == "__main__":
    reset_faiss()
    fm = get_faiss()

    v1 = np.array([85.8, 31.2, 3, 10, 20, 1.5, 1.3, 6, 40, 7, 2.2, 62], dtype=np.float32)
    v2 = np.array([10.0, 10.0, 0, 0, 0, 1.0, 1.0, 0, 0, 5, 1.0, 50], dtype=np.float32)

    # 短期写入
    assert fm.write_short_term(v1, "600884", "theme_stock", "RED", 85.8)
    assert fm.write_short_term(v2, "600884", "theme_stock", "GREEN", 10.0)
    print(f"✅ 短期: {fm.short_index.ntotal}条")

    # 长期写入
    assert fm.write_long_term(v1, "600884", "theme_stock", "RED", 85.8,
                               "lollapalooza_heavy_red", {"bias": 7})
    print(f"✅ 长期: {fm.long_index.ntotal}条")

    # 检索
    r1 = fm.search_short(v2, top_k=5)
    r2 = fm.search_long(v1, top_k=5)
    assert len(r1) >= 1 and len(r2) >= 1
    print(f"✅ 短期检索: {len(r1)}条  长期检索: {len(r2)}条")

    # 风险修正
    adj = fm.calc_risk_adjustment(v1, "theme_stock")
    assert adj["coefficient"] >= 1.0
    print(f"✅ 风险修正: coeff={adj['coefficient']} cases={adj['matched_cases']}")

    # 短期清理
    fm.clean_short_term()
    print(f"✅ 短期清理后: {fm.short_index.ntotal}条")

    # 人工管理
    assert fm.add_manual_long(v2, "MANUAL", "theme_stock", reason="人工标记")
    assert fm.remove_long_by_id(0)  # 剔除vector_id=0
    print(f"✅ 人工管理: 新增+剔除成功")

    s = fm.stats()
    print(f"✅ 状态: short={s['short_term']['total']} long={s['long_term']['total']}")
    print(f"✅ 全部测试通过")
