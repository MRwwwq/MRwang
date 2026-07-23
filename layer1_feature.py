#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer1 特征校验层 — 核心运算层

时序: Layer0 数据采集之后, Layer2 风控决策之前
执行流水线:
  1. 关键词提取: 从五类市场信号提取16组交易特征, 绑定25类心理误判知识库
  2. 向量检索: 3000维 BOW 向量检索, 87组 chunk 向量索引库匹配特征
  3. Rescore 加权打分: 综合得分 = 向量cos相似度 * 0.6 + 关键词权重 * 0.4
  4. Lollapalooza 叠加效应校验: ≥23条 → RED直接
  5. Rule_021 五条件 AND 校验
  6. 三层联动架构校验: 技术+基本面+情绪三维协同性

层输出:
  - 特征综合打分
  - 心理误判触发总数
  - 五维信号共振校验结果
  向下输送至 Layer2 风控决策层
"""

import logging
import json
import os
import numpy as np
from typing import Optional
from psy_hit_manager import get_psy_hit_count, psy_hit_codes
from rule021_dual_branch import Rule021DualBranchChecker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [L1] %(message)s", datefmt="%H:%M:%S")

# ====================== 路径配置 ======================

FAISS_DIR = os.path.join(os.path.dirname(__file__), "faiss_index")

# ====================== 16组交易特征 ↔ 25类心理误判绑定 ======================

# 16组交易特征: key=特征ID, 包含中文名+英文名+绑定的misjudge编号
TRADE_FEATURES = {
    "F01": {"name": "放量拉升", "en": "volume_surge_up", "misjudge_ids": ["01", "13"]},
    "F02": {"name": "缩量阴跌", "en": "volume_shrink_down", "misjudge_ids": ["14", "19"]},
    "F03": {"name": "利好公告", "en": "positive_news", "misjudge_ids": ["02", "22"]},
    "F04": {"name": "利空公告", "en": "negative_news", "misjudge_ids": ["11", "14"]},
    "F05": {"name": "连板涨停", "en": "consecutive_limit_up", "misjudge_ids": ["08", "15"]},
    "F06": {"name": "散户流入", "en": "retail_inflow", "misjudge_ids": ["01", "15"]},
    "F07": {"name": "主力流出", "en": "institutional_outflow", "misjudge_ids": ["05", "11"]},
    "F08": {"name": "概念炒作", "en": "concept_speculation", "misjudge_ids": ["06", "23"]},
    "F09": {"name": "震荡横盘", "en": "range_oscillation", "misjudge_ids": ["04", "24"]},
    "F10": {"name": "反抽冲高", "en": "dead_cat_bounce", "misjudge_ids": ["12", "20"]},
    "F11": {"name": "破位新低", "en": "breakdown_new_low", "misjudge_ids": ["14", "17"]},
    "F12": {"name": "逆势走强", "en": "contrarian_strong", "misjudge_ids": ["08", "12"]},
    "F13": {"name": "机构买入", "en": "institutional_buy", "misjudge_ids": ["09", "22"]},
    "F14": {"name": "业绩披露", "en": "earnings_release", "misjudge_ids": ["10", "19"]},
    "F15": {"name": "政策利好", "en": "policy_tailwind", "misjudge_ids": ["02", "10"]},
    "F16": {"name": "市场暴跌", "en": "market_crash", "misjudge_ids": ["17", "18"]},
}

# 25类心理误判名称映射 (code_01~code_24 + Lollapalooza)
MISJUDGE_NAMES = {
    "01": "奖励与惩罚", "02": "喜欢/热爱", "03": "讨厌/憎恨",
    "04": "避免怀疑", "05": "避免不一致", "06": "好奇心",
    "07": "公平倾向", "08": "嫉妒猜忌", "09": "回馈倾向",
    "10": "简单联想", "11": "痛苦否认", "12": "自视过高",
    "13": "过度乐观", "14": "损失厌恶", "15": "社会认同羊群",
    "16": "对比偏差", "17": "压力影响", "18": "易得性误导",
    "19": "遗忘风险", "20": "化学情绪干扰", "21": "思维老化固化",
    "22": "权威盲从", "23": "市场噪音废话", "24": "虚假理由轻信",
}

# misjudge编号 → code_xx前缀
def misjudge_to_code(mid: str) -> str:
    return f"code_{int(mid):02d}_{MISJUDGE_NAMES.get(mid, 'unknown')}"


# ====================== BOW 向量检索引擎 ======================

class BOWVectorEngine:
    """
    3000维 BOW 向量检索引擎。

    依托 87 组 chunk 向量索引库 (兼容现有 FAISS 系统)。
    """

    def __init__(self):
        self.vectors: Optional[np.ndarray] = None
        self.word2id: dict = {}
        self.metas: list = []
        self.dim = 3000
        self.loaded = False
        self._load_index()

    def _load_index(self) -> None:
        """加载已有的 FAISS 向量索引 (兼容模式)"""
        try:
            vec_path = os.path.join(FAISS_DIR, "misjudge_vectors.npy")
            meta_path = os.path.join(FAISS_DIR, "misjudge_metas.json")
            w2id_path = os.path.join(FAISS_DIR, "word2id.json")

            if os.path.exists(vec_path) and os.path.exists(meta_path) and os.path.exists(w2id_path):
                self.vectors = np.load(vec_path).astype(np.float32)
                with open(meta_path) as f:
                    self.metas = json.load(f)
                with open(w2id_path) as f:
                    self.word2id = json.load(f)
                self.dim = self.vectors.shape[1]
                self.loaded = True
                logging.info(f"  ✅ FAISS索引加载成功: {len(self.metas)}chunks, dim={self.dim}")
            else:
                # 无现有索引, 创建伪BOW索引(测试/离线模式)
                logging.warning(f"  ⚠️ 未找到FAISS索引文件, 使用内置BOW精简索引(87chunks)")
                self._build_builtin_index()
        except Exception as e:
            logging.warning(f"  ⚠️ FAISS索引加载失败: {e}, 使用BOW精简索引")
            self._build_builtin_index()

    def _build_builtin_index(self) -> None:
        """构建内置BOW精简索引 (87 chunks, 3000维)"""
        np.random.seed(42)
        chunk_count = 87
        self.dim = 3000
        self.vectors = np.zeros((chunk_count, self.dim), dtype=np.float32)

        # 预定义87个chunk的基础词集
        base_terms = [
            "放量", "缩量", "拉升", "阴跌", "利好", "利空", "连板", "涨停",
            "散户", "主力", "概念", "炒作", "震荡", "横盘", "反抽", "破位",
            "逆势", "机构", "业绩", "政策", "暴跌", "抄底", "追高", "止损",
            "诱多", "出货", "建仓", "洗盘", "主升", "退潮", "冰点", "高潮",
            "金叉", "死叉", "背离", "共振", "风险", "预警", "拦截", "放行",
            "misjudge_01", "misjudge_02", "misjudge_03", "misjudge_04",
            "misjudge_05", "misjudge_06", "misjudge_07", "misjudge_08",
            "misjudge_09", "misjudge_10", "misjudge_11", "misjudge_12",
            "misjudge_13", "misjudge_14", "misjudge_15", "misjudge_16",
            "misjudge_17", "misjudge_18", "misjudge_19", "misjudge_20",
            "misjudge_21", "misjudge_22", "misjudge_23", "misjudge_24",
            "高估", "低估", "过热", "恐慌", "贪婪", "犹豫",
            "F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08",
            "F09", "F10", "F11", "F12", "F13", "F14", "F15", "F16",
            "多头", "空头", "风险偏好", "避险", "流动性",
            "技术", "基本面", "情绪", "指标", "宏观",
        ]

        self.word2id = {w: i for i, w in enumerate(base_terms)}
        self.word2id.update({f"word_{i}": i for i in range(len(base_terms), self.dim)})

        # 为每个chunk分配特征
        self.metas = []
        for i in range(chunk_count):
            for wid in range(i % 24 + 1, 25, 3):
                mkey = f"misjudge_{wid:02d}"
                if mkey in self.word2id:
                    self.vectors[i, self.word2id[mkey]] = np.random.uniform(0.5, 1.0)
            for fid in range(1, 17):
                fkey = f"F{fid:02d}"
                if fkey in self.word2id and np.random.random() > 0.7:
                    self.vectors[i, self.word2id[fkey]] = np.random.uniform(0.3, 0.8)
            self.metas.append({
                "source": f"chunk_{i:03d}.md",
                "misjudge_ids": [f"{x:02d}" for x in range(i % 24 + 1, 25, 3)],
                "feature_ids": [f"F{x:02d}" for x in range(1, 17) if np.random.random() > 0.7],
            })

        # 归一化
        norms = np.linalg.norm(self.vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self.vectors = self.vectors / norms

        self.loaded = True
        logging.info(f"  ✅ BOW内置索引构建完成: {len(self.metas)}chunks, dim={self.dim}")

    def build_query_vector(self, features: list[str], psy_codes: list[str]) -> np.ndarray:
        """
        根据激活的特征和psy_codes构建查询向量。

        参数:
            features: 激活的交易特征ID列表, 如 ["F01", "F05", "F06"]
            psy_codes: 当前psy_hit_codes

        返回: (1, dim) 归一化查询向量
        """
        qv = np.zeros((1, self.dim), dtype=np.float32)

        # 特征权重
        for fid in features:
            if fid in self.word2id:
                qv[0, self.word2id[fid]] += 5.0
            # 添加绑定的misjudge编码
            feat_info = TRADE_FEATURES.get(fid)
            if feat_info:
                for mid in feat_info["misjudge_ids"]:
                    mkey = f"misjudge_{mid}"
                    if mkey in self.word2id:
                        qv[0, self.word2id[mkey]] += 3.0

        # psy_codes 权重
        for code in psy_codes:
            for mid in range(1, 25):
                mkey = f"misjudge_{mid:02d}"
                if mkey in self.word2id:
                    qv[0, self.word2id[mkey]] += 2.0

        # 归一化
        qnorm = np.linalg.norm(qv)
        if qnorm > 0:
            qv /= qnorm
        return qv

    def search(self, qv: np.ndarray, top_k: int = 5) -> list[dict]:
        """向量检索"""
        if not self.loaded or self.vectors is None:
            return []
        sims = self.vectors @ qv.T
        top = np.argsort(-sims.flatten())[:top_k]
        results = []
        for i in top:
            if sims[i] > 0.01 and i < len(self.metas):
                sv = float(sims[i]) if not hasattr(sims[i], '__len__') else float(sims[i][0])
                results.append({
                    "chunk_index": int(i),
                    "similarity": round(sv, 4),
                    "source": self.metas[i].get("source", f"chunk_{i:03d}"),
                    "misjudge_ids": self.metas[i].get("misjudge_ids", []),
                })
        return results


# ====================== 关键词提取 ======================

class KeywordExtractor:
    """
    从五类市场信号提取16组交易特征。

    规则: 根据信号的方向/值/描述, 判定哪些交易特征被激活。
    """

    @staticmethod
    def extract(signal_output: dict) -> tuple[list[str], dict[str, float]]:
        """
        提取激活的交易特征和关键词权重。

        参数:
            signal_output: Layer0输出的五类信号字典

        返回:
            (active_features: list[str], keyword_weights: dict[feat, weight])
        """
        active_features = set()
        keyword_weights = {}

        # 1. 技术面信号 → 提取
        tech_signals = signal_output.get("technical", [])
        for s in tech_signals:
            name = s.get("name", "")
            value = s.get("value", 50)
            direction = s.get("direction", "neutral")

            if "放量" in name or "量比" in name:
                if value > 60:
                    active_features.add("F01")  # 放量拉升
                    _update_weight(keyword_weights, "F01", 0.3 + value / 200)
            if "均线" in name:
                if "多头" in direction or "bullish" in direction:
                    active_features.add("F01")
                    _update_weight(keyword_weights, "F01", 0.4)
                else:
                    active_features.add("F02")  # 缩量阴跌
                    _update_weight(keyword_weights, "F02", 0.4)

        # 2. 基本面信号 → 提取
        fund_signals = signal_output.get("fundamental", [])
        for s in fund_signals:
            name = s.get("name", "")
            value = s.get("value", 50)
            if "ROE" in name and value > 70:
                active_features.add("F13")  # 机构买入
                _update_weight(keyword_weights, "F13", 0.5)
            if "利润" in name:
                if value > 60:
                    active_features.add("F14")  # 业绩披露
                    _update_weight(keyword_weights, "F14", 0.5)

        # 3. 情绪面信号 → 提取
        sent_signals = signal_output.get("sentiment", [])
        for s in sent_signals:
            name = s.get("name", "")
            value = s.get("value", 50)
            if "舆情" in name and value > 60:
                active_features.add("F03")  # 利好
                _update_weight(keyword_weights, "F03", 0.4)
            elif "舆情" in name and value < 40:
                active_features.add("F04")  # 利空
                _update_weight(keyword_weights, "F04", 0.4)
            if "亏钱" in name or "核按钮" in name:
                active_features.add("F11")  # 破位
                _update_weight(keyword_weights, "F11", 0.6)
            if "心理误判" in name and value < 50:
                active_features.add("F04")
                _update_weight(keyword_weights, "F04", 0.5)

        # 4. 指标面信号 → 提取
        ind_signals = signal_output.get("indicator", [])
        for s in ind_signals:
            name = s.get("name", "")
            value = s.get("value", 50)
            if "RSI" in name:
                if value > 70:
                    active_features.add("F05")  # 连板/过热
                    _update_weight(keyword_weights, "F05", 0.3)
                elif value < 30:
                    active_features.add("F16")  # 市场暴跌
                    _update_weight(keyword_weights, "F16", 0.5)
            if "BOLL" in name:
                if "0.8" in str(value) or (value < 30 and "位置" not in name):
                    active_features.add("F10")  # 反抽冲高
                    _update_weight(keyword_weights, "F10", 0.3)

        # 5. 宏观面信号 → 提取
        macro_signals = signal_output.get("macro", [])
        for s in macro_signals:
            name = s.get("name", "")
            direction = s.get("direction", "neutral")
            value = s.get("value", 50)
            if "流动" in name and "bullish" in direction:
                active_features.add("F15")  # 政策利好
                _update_weight(keyword_weights, "F15", 0.3)
            elif "资金" in name and "outflow" in direction:
                active_features.add("F07")  # 主力流出
                _update_weight(keyword_weights, "F07", 0.5)
            if "风格匹配" in name and value < 40:
                active_features.add("F09")  # 震荡横盘(观望)
                _update_weight(keyword_weights, "F09", 0.4)

        return list(active_features), keyword_weights


def _update_weight(weights: dict, key: str, value: float) -> None:
    """更新关键词权重 (取最大值)"""
    if key in weights:
        weights[key] = max(weights[key], value)
    else:
        weights[key] = value


# ====================== 心理误判编码统计 ======================

def count_psy_by_category(active_features: list[str]) -> dict:
    """
    根据激活的交易特征, 统计对应的心理误判类别和数量。

    返回: {misjudge_id: count_matched_features}
    """
    category_count: dict[str, int] = {}
    for fid in active_features:
        feat = TRADE_FEATURES.get(fid)
        if feat:
            for mid in feat["misjudge_ids"]:
                category_count[mid] = category_count.get(mid, 0) + 1
    return category_count


# ====================== Rule_021 五条件 AND 校验 ======================

class Rule021Checker:
    """
    Rule_021 五条件 AND 校验。

    校验【技术 / 基本面 / 情绪 / 指标 / 宏观】五维信号是否共振
    全部五维同时利空 → 五维共振利空
    全部五维同时利多 → 五维共振利多
    """

    @staticmethod
    def check(signal_output: dict) -> dict:
        """
        五维共振校验。

        返回:
            {
                "resonance_direction": "bullish"|"bearish"|"mixed",
                "resonance_strength": 0~1,
                "dimension_details": [...],
                "pass_and_check": bool,  # 五维是否全部AND通过
            }
        """
        dimensions = []

        # 技术面
        tech = signal_output.get("technical", [])
        tech_score = sum(s.get("value", 50) for s in tech) / max(len(tech), 1)
        tech_dir = _majority_direction(tech)
        dimensions.append({
            "name": "技术面", "score": round(tech_score, 1), "direction": tech_dir,
            "signal_count": len(tech),
        })

        # 基本面
        fundamental = signal_output.get("fundamental", [])
        fund_score = sum(s.get("value", 50) for s in fundamental) / max(len(fundamental), 1)
        fund_dir = _majority_direction(fundamental)
        dimensions.append({
            "name": "基本面", "score": round(fund_score, 1), "direction": fund_dir,
            "signal_count": len(fundamental),
        })

        # 情绪面
        sentiment = signal_output.get("sentiment", [])
        sent_score = sum(s.get("value", 50) for s in sentiment) / max(len(sentiment), 1)
        sent_dir = _majority_direction(sentiment)
        dimensions.append({
            "name": "情绪面", "score": round(sent_score, 1), "direction": sent_dir,
            "signal_count": len(sentiment),
        })

        # 指标面
        indicator = signal_output.get("indicator", [])
        ind_score = sum(s.get("value", 50) for s in indicator) / max(len(indicator), 1)
        ind_dir = _majority_direction(indicator)
        dimensions.append({
            "name": "指标面", "score": round(ind_score, 1), "direction": ind_dir,
            "signal_count": len(indicator),
        })

        # 宏观面
        macro = signal_output.get("macro", [])
        macro_score = sum(s.get("value", 50) for s in macro) / max(len(macro), 1)
        macro_dir = _majority_direction(macro)
        dimensions.append({
            "name": "宏观面", "score": round(macro_score, 1), "direction": macro_dir,
            "signal_count": len(macro),
        })

        # 判定共振方向
        all_dirs = [d["direction"] for d in dimensions]
        all_bullish = all(d == "bullish" for d in all_dirs)
        all_bearish = all(d == "bearish" for d in all_dirs)
        any_bearish = any(d == "bearish" for d in all_dirs)

        if all_bullish:
            resonance_dir = "bullish"
            strength = 1.0
        elif all_bearish:
            resonance_dir = "bearish"
            strength = 1.0
        elif any_bearish:
            resonance_dir = "mixed_bearish"
            strength = sum(1 for d in all_dirs if d == "bearish") / 5
        else:
            resonance_dir = "mixed"
            strength = 0.5

        result = {
            "resonance_direction": resonance_dir,
            "resonance_strength": round(strength, 2),
            "pass_and_check": all_bullish or all_bearish,
            "all_bullish": all_bullish,
            "all_bearish": all_bearish,
            "dimensions": dimensions,
        }
        return result


def _majority_direction(signals: list[dict]) -> str:
    """判断信号列表的方向 (多数投票)"""
    bullish = sum(1 for s in signals if s.get("direction") == "bullish")
    bearish = sum(1 for s in signals if s.get("direction") == "bearish")
    if bullish > bearish and bullish > 0:
        return "bullish"
    elif bearish > bullish and bearish > 0:
        return "bearish"
    return "neutral"


# ====================== 三层联动校验 ======================

class ThreeLayerLinkageCheck:
    """
    三层联动架构校验。

    核验【技术面 + 基本面 + 情绪面】三维信号协同性:
      - 协同做多: 三维全部利多 → 强烈做多信号
      - 协同做空: 三维全部利空 → 强烈做空信号
      - 分歧: 三维方向不一致 → 降低信号可靠性
    """

    @staticmethod
    def check(signal_output: dict) -> dict:
        """
        三层联动校验。

        返回:
            {
                "linkage_status": "cooperative_bullish"|"cooperative_bearish"|"divergent",
                "details": {three_dimensions},
                "reliability": float,  # 信号可靠度 0~1
            }
        """
        # 提取三维核心信号
        tech_signals = signal_output.get("technical", [])
        fund_signals = signal_output.get("fundamental", [])
        sent_signals = signal_output.get("sentiment", [])

        tech_dir = _majority_direction(tech_signals)
        fund_dir = _majority_direction(fund_signals)
        sent_dir = _majority_direction(sent_signals)

        dirs = [tech_dir, fund_dir, sent_dir]
        tech_score = sum(s.get("value", 50) for s in tech_signals) / max(len(tech_signals), 1)

        all_bullish = all(d == "bullish" for d in dirs)
        all_bearish = all(d == "bearish" for d in dirs)
        any_neutral = any(d == "neutral" for d in dirs)

        if all_bullish:
            status = "cooperative_bullish"
            reliability = min(1.0, tech_score / 70)
        elif all_bearish:
            status = "cooperative_bearish"
            reliability = min(1.0, (100 - tech_score) / 70)
        elif any_neutral:
            # 包含中立 → 信号欠明确
            bullish_count = sum(1 for d in dirs if d == "bullish")
            bearish_count = sum(1 for d in dirs if d == "bearish")
            if bullish_count > bearish_count:
                status = "weak_bullish"
            elif bearish_count > bullish_count:
                status = "weak_bearish"
            else:
                status = "no_clear_signal"
            reliability = 0.4
        else:
            # 三维分歧
            status = "divergent"
            reliability = 0.3

        return {
            "linkage_status": status,
            "reliability": round(reliability, 2),
            "details": {
                "technical": tech_dir,
                "fundamental": fund_dir,
                "sentiment": sent_dir,
            },
        }


# ====================== Layer1 主引擎 ======================

class Layer1FeatureEngine:
    """
    Layer1 特征校验层主引擎。

    串行执行完整流水线:
      提取关键词 → 向量检索 → Rescore → Lolla检查 → Rule_021 → 三层联动
    """

    def __init__(self):
        self.vector_engine = BOWVectorEngine()
        self.keyword_extractor = KeywordExtractor()
        self.rule_021 = Rule021Checker()
        self.linkage = ThreeLayerLinkageCheck()
        self.dual_branch_checker = Rule021DualBranchChecker()

    def run(self, signal_output: dict,
            stock_context: dict = None) -> dict:
        """
        全流水线执行。

        参数:
            signal_output: Layer0 输出的五类信号字典

        返回:
            Layer1 全部校验结果, 向下送入 Layer2
        """
        logging.info("=" * 60)
        logging.info("Layer1 特征校验层 启动")

        # Step 1: 关键词提取
        logging.info("  [Step 1/6] 关键词提取 → 16组交易特征")
        active_features, keyword_weights = self.keyword_extractor.extract(signal_output)
        logging.info(f"    → 激活特征: {active_features}")
        logging.info(f"    → 关键词权重: {keyword_weights}")

        # Step 2: 向量检索
        logging.info("  [Step 2/6] 向量检索 (3000维 BOW, 87 chunks)")
        psy_count = get_psy_hit_count()
        psy_codes_list = psy_hit_codes.copy()
        qv = self.vector_engine.build_query_vector(active_features, psy_codes_list)
        search_results = self.vector_engine.search(qv, top_k=5)
        if search_results:
            for r in search_results:
                logging.info(f"    → chunk[{r['chunk_index']:03d}] {r['source']:30s} sim={r['similarity']:.4f}")
        else:
            logging.info("    → 无向量检索结果 (使用关键词权重兜底)")

        # Step 3: Rescore 加权打分
        logging.info("  [Step 3/6] Rescore 加权打分")
        vec_score = search_results[0]["similarity"] if search_results else 0
        kw_score = sum(keyword_weights.values()) / max(len(keyword_weights), 1)
        kw_score = min(kw_score, 1.0)
        composite_score = vec_score * 0.6 + kw_score * 0.4
        logging.info(f"    → vec_sim={vec_score:.4f} * 0.6 + kw_weight={kw_score:.4f} * 0.4")
        logging.info(f"    → composite_score={composite_score:.4f}")

        # Step 4: Lollapalooza 叠加效应校验
        logging.info("  [Step 4/6] Lollapalooza 叠加效应校验")
        psy_category_count = count_psy_by_category(active_features)
        total_psy_matched = len(psy_category_count)

        # 从psy_hit_codes统计
        total_psy_codes = psy_count
        for code in psy_codes_list:
            for mid in range(1, 25):
                if f"code_{mid:02d}" in code and mid not in psy_category_count:
                    total_psy_matched += 1

        lolla_direct_red = total_psy_codes >= 23
        logging.info(f"    → psy_hit_codes: {total_psy_codes}条 (≥23=直接RED: {lolla_direct_red})")
        logging.info(f"    → 特征绑定编码命中: {len(psy_category_count)}类")
        logging.info(f"    → 综合心理误判计数: {total_psy_matched}")

        # Step 5: Rule_021 五条件 AND 校验
        logging.info("  [Step 5/6] Rule_021 五条件 AND 校验")
        rule_021_result = self.rule_021.check(signal_output)
        logging.info(f"    → 共振方向: {rule_021_result['resonance_direction']}")
        logging.info(f"    → 共振强度: {rule_021_result['resonance_strength']}")
        logging.info(f"    → 五维AND: {rule_021_result['pass_and_check']}")
        for dim in rule_021_result["dimensions"]:
            arrow = "🟢" if dim["direction"] == "bullish" else ("🔴" if dim["direction"] == "bearish" else "🟡")
            logging.info(f"    {arrow} {dim['name']:6s}: score={dim['score']:<5.1f} {dim['direction']:10s} ({dim['signal_count']}信号)")

        # Step 5b: Rule021 双分支差异化打分 (v2.2)
        if stock_context:
            dual_branch_result = self.dual_branch_checker.check(
                stock_code=stock_context.get("stock_code", ""),
                stock_name=stock_context.get("stock_name", ""),
                sector=stock_context.get("sector", ""),
                business_desc=stock_context.get("business_desc", ""),
                signal_output=signal_output,
                stock_data=stock_context.get("stock_data"),
            )
            logging.info(dual_branch_result["score_table"])
        else:
            dual_branch_result = None

        # Step 6: 三层联动校验
        logging.info("  [Step 6/6] 三层联动架构校验 (技术+基本面+情绪)")
        linkage_result = self.linkage.check(signal_output)
        logging.info(f"    → 协同状态: {linkage_result['linkage_status']}")
        logging.info(f"    → 信号可靠度: {linkage_result['reliability']}")
        for dim_name, dim_dir in linkage_result["details"].items():
            arrow = "🟢" if dim_dir == "bullish" else ("🔴" if dim_dir == "bearish" else "🟡")
            logging.info(f"    {arrow} {dim_name}: {dim_dir}")

        # 构建完整输出
        result = {
            "composite_score": round(composite_score, 4),
            "active_features": active_features,
            "keyword_weights": keyword_weights,
            "vector_search_results": search_results,
            "vector_similarity_top": round(vec_score, 4),
            "keyword_weight_avg": round(kw_score, 4),
            "psy_count": total_psy_codes,
            "psy_category_matched": len(psy_category_count),
            "psy_category_detail": psy_category_count,
            "lolla_direct_red": lolla_direct_red,
            "rule_021": rule_021_result,
            "rule_021_dual_branch": dual_branch_result,  # v2.2 新增
            "three_layer_linkage": linkage_result,
            "composite_signal": self._determine_composite_signal(
                composite_score, rule_021_result, linkage_result, lolla_direct_red, total_psy_codes
            ),
        }

        # 输出汇总
        self._print_summary(result)
        logging.info("Layer1 特征校验层 完成")
        logging.info("=" * 60)
        return result

    def _determine_composite_signal(
        self,
        composite_score: float,
        rule_021: dict,
        linkage: dict,
        lolla_direct_red: bool,
        psy_count: int,
    ) -> dict:
        """判定综合信号方向"""
        if lolla_direct_red or psy_count >= 23:
            return {"direction": "bearish_strong", "level": "high_risk", "reason": "Lollapalooza≥23直接拦截"}
        if rule_021.get("all_bearish", False) and rule_021["resonance_strength"] >= 0.8:
            return {"direction": "bearish_strong", "level": "high_risk", "reason": "五维全利空共振"}
        if rule_021.get("all_bullish", False) and rule_021["resonance_strength"] >= 0.8:
            return {"direction": "bullish_strong", "level": "low_risk", "reason": "五维全利多共振"}
        if linkage.get("linkage_status") == "divergent":
            return {"direction": "uncertain", "level": "medium_risk", "reason": "三层联动分歧, 信号不可靠"}
        if composite_score > 0.6:
            return {"direction": "cautious_bullish", "level": "low_risk", "reason": "综合打分偏多"}
        if composite_score < 0.3:
            return {"direction": "cautious_bearish", "level": "medium_risk", "reason": "综合打分偏空"}
        return {"direction": "neutral", "level": "low_risk", "reason": "综合打分中性"}

    def _print_summary(self, result: dict) -> None:
        """打印执行汇总"""
        cr = result["composite_signal"]
        dual = result.get("rule_021_dual_branch")
        logging.info("-" * 40)
        logging.info(f"  Layer1 汇总:")
        logging.info(f"    综合得分: {result['composite_score']:.4f}")
        logging.info(f"    特征激活: {result['active_features']}")
        logging.info(f"    心理误判: {result['psy_count']}条 (特征绑定{result['psy_category_matched']}类)")
        if dual:
            branch_icon = "🪙" if dual["branch"] == "resource" else "💡"
            branch_label = "周期资源" if dual["branch"] == "resource" else "题材叙事"
            logging.info(f"    {branch_icon} Rule021双分支: {dual['branch_label']} | "
                         f"风险分{dual['final_risk_score']:.1f} | "
                         f"{'⚠️高危' if dual['is_high_risk'] else '🟢正常'}")
        logging.info(f"    综合信号: {cr['direction']} | {cr['level']} | {cr['reason']}")
        logging.info("-" * 40)


# ====================== 快捷入口 ======================

def run_layer1(signal_output: dict,
               stock_context: dict = None) -> dict:
    """
    Layer1 快捷执行入口 (v2.2 支持Rule021双分支)。

    参数:
        signal_output: Layer0 输出的五类信号字典
        stock_context: 标的上文数据 {
            "stock_code": "600547.SH",
            "stock_name": "山东黄金",
            "sector": "贵金属",
            "business_desc": "黄金采选",
            "stock_data": { commodity_price_percentile, ... }
        }

    返回:
        Layer1 全部校验结果 + Rule021双分支结果
    """
    engine = Layer1FeatureEngine()
    return engine.run(signal_output, stock_context)


# ====================== 测试 ======================

if __name__ == "__main__":
    from psy_hit_manager import clear_all_psy_codes
    clear_all_psy_codes()

    print("\n=== Layer1 特征校验层 测试 ===\n")

    # 模拟 Layer0 输出
    signal_output = {
        "technical": [
            {"name": "均线形态(bullish)", "value": 80, "direction": "bullish", "weight": 1.0},
            {"name": "量能(量比1.60)", "value": 64, "direction": "bullish", "weight": 1.0},
            {"name": "KDJ(金叉)", "value": 75, "direction": "bullish", "weight": 1.0},
            {"name": "MACD(金叉)", "value": 75, "direction": "bullish", "weight": 1.0},
        ],
        "fundamental": [
            {"name": "ROE(12.5%)", "value": 75, "direction": "bullish", "weight": 1.0},
            {"name": "利润增速(+35.0%)", "value": 85, "direction": "bullish", "weight": 1.0},
        ],
        "sentiment": [
            {"name": "综合舆情(72)", "value": 72, "direction": "bullish", "weight": 1.0},
            {"name": "心理误判累积(0条)", "value": 100, "direction": "bullish", "weight": 1.0},
        ],
        "indicator": [
            {"name": "RSI(62)", "value": 62, "direction": "neutral", "weight": 1.0},
            {"name": "BOLL位置(0.65)", "value": 70, "direction": "neutral", "weight": 1.0},
        ],
        "macro": [
            {"name": "流动性(SHIBOR 1W=1.80%)", "value": 70, "direction": "bullish", "weight": 1.0},
        ],
    }

    result = run_layer1(signal_output)

    print(f"\n结果:")
    print(f"  composite_score: {result['composite_score']}")
    print(f"  active_features: {result['active_features']}")
    print(f"  psy_count: {result['psy_count']}")
    print(f"  lolla_direct_red: {result['lolla_direct_red']}")
    print(f"  rule_021 resonance: {result['rule_021']['resonance_direction']}")
    print(f"  three_layer: {result['three_layer_linkage']['linkage_status']}")
    print(f"  composite_signal: {result['composite_signal']['direction']}")
