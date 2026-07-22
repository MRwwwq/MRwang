#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finbert_chinese.py — 中文金融BERT情感引擎 v1.0
================================================================
功能：
  1. 优先加载本地 FinBERT 中文金融模型（若已下载）
  2. 若模型不存在 → 自动降级到 FinSentiment（词典85%+SnowNLP15%）
  3. 统一输出: {label, score, confidence, model_used}

模型下载（建议在服务器单独运行，需几分钟）:
  python3 finbert_chinese.py --download

输出标签：
  FINBERT_LABELS: {0: "中性", 1: "利好", 2: "利空"}
  阈值: >0.55 利好, <0.45 利空, 其余中性

依赖：
  transformers>=4.30, torch, numpy
"""

import os, sys, json, re
from typing import Dict, Optional
from dataclasses import dataclass

import numpy as np

# 模型路径
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finbert_chinese")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@dataclass
class SentimentResult:
    """统一情感输出"""
    label: str        # "利好" | "利空" | "中性"
    score: float      # 0~1
    confidence: float # 0~1
    model_used: str   # "finbert" | "finsentiment"
    detail: dict = None


class ChineseFinBERT:
    """
    中文金融BERT情感分析引擎
    自动降级: 模型不存在→FinSentiment
    模型来源: hw2942/bert-base-chinese-finetuning-financial-news-sentiment-v2
    """

    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.label_map = {0: "中性", 1: "利好", 2: "利空"}
        self.model_loaded = False
        self._fallback = None

        # 尝试加载模型
        if self._check_model_exists():
            try:
                self._load_model()
            except Exception as e:
                print(f"  ⚠ FinBERT加载失败({e}), 降级到FinSentiment")

        # 总是初始化Fallback
        if not self.model_loaded:
            self._init_fallback()

    def _check_model_exists(self) -> bool:
        """检查模型文件是否存在"""
        required = ["config.json", "vocab.txt", "pytorch_model.bin"]
        if not os.path.isdir(MODEL_DIR):
            return False
        files = os.listdir(MODEL_DIR)
        # RoBERTa格式兼容
        alt_required = ["config.json", "vocab.json", "merges.txt"]
        has_bert = all(f in files for f in required)
        has_roberta = all(f in files for f in alt_required) and \
                      ("pytorch_model.bin" in files or any(f.endswith(".bin") or f.endswith(".safetensors") for f in files))
        return has_bert or has_roberta

    def _load_model(self):
        """加载本地模型文件"""
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, trust_remote_code=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR, trust_remote_code=True)
        self.model.eval()
        # 探测label数量
        if hasattr(self.model.config, "id2label"):
            self.label_map = self.model.config.id2label
        self.model_loaded = True
        print(f"  ✅ FinBERT中文模型已加载 ({sum(os.path.getsize(os.path.join(MODEL_DIR,f))/1024/1024 for f in os.listdir(MODEL_DIR) if os.path.isfile(os.path.join(MODEL_DIR,f))):.0f}MB)")

    def _init_fallback(self):
        """初始化FinSentiment降级引擎"""
        from fin_sentiment import FinSentiment
        self._fallback = FinSentiment()
        self.model_loaded = False

    def analyze(self, text: str, max_length: int = 256) -> SentimentResult:
        """
        情感分析主入口

        :param text: 输入文本
        :param max_length: BERT最大序列长度
        :return: SentimentResult
        """
        if not text or len(text.strip()) < 5:
            return SentimentResult(label="中性", score=0.5, confidence=0.0,
                                   model_used="none")

        if self.model_loaded and self.model is not None:
            return self._analyze_bert(text, max_length)
        else:
            return self._analyze_fallback(text)

    def _analyze_bert(self, text: str, max_length: int) -> SentimentResult:
        """使用FinBERT分析"""
        import torch
        try:
            inputs = self.tokenizer(
                text, return_tensors="pt",
                truncation=True, max_length=max_length,
                padding=True
            )
            with torch.no_grad():
                outputs = self.model(**inputs)
                probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
                scores = probs[0].cpu().numpy()

            pred_label = int(np.argmax(scores))
            confidence = float(np.max(scores))

            # 映射到中文标签
            if isinstance(self.label_map, dict):
                label_str = self.label_map.get(pred_label, f"label_{pred_label}")
            else:
                label_str = ["中性", "利好", "利空"][pred_label] if pred_label < 3 else "中性"

            # 标准化score到0~1（利好1.0 利空0.0 中性0.5）
            if pred_label == 1:  # 利好
                norm_score = 0.5 + confidence * 0.5
            elif pred_label == 2:  # 利空
                norm_score = 0.5 - confidence * 0.5
            else:  # 中性
                norm_score = 0.5

            return SentimentResult(
                label=label_str,
                score=round(norm_score, 3),
                confidence=round(confidence, 3),
                model_used="finbert",
                detail={"probs": {self.label_map.get(i, str(i)): float(scores[i]) for i in range(len(scores))}},
            )
        except Exception as e:
            # BERT失败→降级
            return self._analyze_fallback(text)

    def _analyze_fallback(self, text: str) -> SentimentResult:
        """使用FinSentiment降级"""
        if self._fallback is None:
            self._init_fallback()
        result = self._fallback.analyze(text)
        return SentimentResult(
            label=result["label"],
            score=result["score"],
            confidence=result.get("confidence", 0.5),
            model_used="finsentiment",
            detail={"pos_words": result.get("pos_words", []),
                    "neg_words": result.get("neg_words", [])},
        )

    def batch_analyze(self, texts: list, max_length: int = 256) -> list:
        """批量分析"""
        return [self.analyze(t, max_length) for t in texts]


# ═══════════════════════════════════════════════
#  龙虎榜资金流向情感解析
# ═══════════════════════════════════════════════

def analyze_money_flow_sentiment(net_amount: float) -> SentimentResult:
    """
    根据资金净流向判定情感

    :param net_amount: 净流入金额(万元)，正值=净流入
    :return: SentimentResult
    """
    if net_amount > 5000:
        return SentimentResult("利好", 0.85, 0.8, "rule_money_flow",
                               detail={"net_amount_wan": net_amount, "type": "主力大幅流入"})
    elif net_amount > 1000:
        return SentimentResult("利好", 0.65, 0.5, "rule_money_flow",
                               detail={"net_amount_wan": net_amount, "type": "主力小幅流入"})
    elif net_amount < -5000:
        return SentimentResult("利空", 0.15, 0.8, "rule_money_flow",
                               detail={"net_amount_wan": net_amount, "type": "主力大幅流出"})
    elif net_amount < -1000:
        return SentimentResult("利空", 0.35, 0.5, "rule_money_flow",
                               detail={"net_amount_wan": net_amount, "type": "主力小幅流出"})
    else:
        return SentimentResult("中性", 0.50, 0.3, "rule_money_flow",
                               detail={"net_amount_wan": net_amount, "type": "资金平衡"})


def analyze_top_inst_sentiment(buy_amount: float, sell_amount: float) -> SentimentResult:
    """
    龙虎榜机构席位买卖分析

    :param buy_amount: 买入金额(万元)
    :param sell_amount: 卖出金额(万元)
    :return: SentimentResult
    """
    net = buy_amount - sell_amount
    total = buy_amount + sell_amount
    if total == 0:
        return SentimentResult("中性", 0.5, 0.0, "rule_top_inst",
                               detail={"ratio": 0})

    ratio = net / total  # -1 ~ 1
    if ratio > 0.3:
        return SentimentResult("利好", 0.75, 0.7, "rule_top_inst",
                               detail={"net": net, "ratio": round(ratio, 3)})
    elif ratio < -0.3:
        return SentimentResult("利空", 0.25, 0.7, "rule_top_inst",
                               detail={"net": net, "ratio": round(ratio, 3)})
    else:
        return SentimentResult("中性", 0.5, 0.4, "rule_top_inst",
                               detail={"net": net, "ratio": round(ratio, 3)})


# ═══════════════════════════════════════════════
#  模型下载脚本
# ═══════════════════════════════════════════════

def download_model():
    """
    下载中文金融BERT模型到本地
    支持多源降级: hf-mirror → HuggingFace
    """
    os.makedirs(MODEL_DIR, exist_ok=True)
    model_id = "hw2942/bert-base-chinese-finetuning-financial-news-sentiment-v2"

    strategies = [
        ("hf-mirror.com (推荐)", f"https://hf-mirror.com/{model_id}/resolve/main"),
        ("HuggingFace", f"https://huggingface.co/{model_id}/resolve/main"),
    ]

    files_to_download = [
        "config.json",
        "vocab.txt",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "pytorch_model.bin",
    ]

    import urllib.request
    from urllib.error import URLError

    for source_name, base_url in strategies:
        print(f"\n📥 尝试从 {source_name} 下载...")
        success = True
        for fname in files_to_download:
            url = f"{base_url}/{fname}"
            local_path = os.path.join(MODEL_DIR, fname)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
                print(f"  ✅ {fname} 已存在 ({(os.path.getsize(local_path)/1024/1024):.1f}MB), 跳过")
                continue
            try:
                print(f"  ⏳ {fname}...", end=" ", flush=True)
                urllib.request.urlretrieve(url, local_path)
                size = os.path.getsize(local_path)
                print(f"✅ {size/1024/1024:.1f}MB" if size > 1000 else f"✅ {size}B")
            except URLError as e:
                print(f"❌ {e}")
                success = False
                break
            except Exception as e:
                print(f"❌ {e}")
                success = False
                break
        if success:
            print(f"\n✅ 模型已保存至 {MODEL_DIR}")
            return True

    print(f"\n❌ 所有下载源均失败。")
    print(f"   手动下载后将文件放入 {MODEL_DIR}/")
    print(f"   需要: config.json, vocab.txt, tokenizer_config.json, pytorch_model.bin")
    return False


# ═══════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    if "--download" in sys.argv:
        download_model()
    else:
        sa = ChineseFinBERT()
        if sa.model_loaded:
            print(f"✅ 当前引擎: FinBERT (中文金融)")
        else:
            print(f"ℹ️  当前引擎: FinSentiment (降级)")

        test_cases = [
            "山东黄金净利润同比增长56%，业绩大幅超预期",
            "公司发布减持公告，控股股东计划减持5%股份",
            "山东黄金子公司售东海证券股权产生公允价值变动损失",
            "公司召开股东大会，董事会换届选举完成",
            "今天加仓了，期望明天涨停",
            "金价上涨提振业绩，未来增量可期",
        ]
        for t in test_cases:
            r = sa.analyze(t)
            model_tag = "🤖" if r.model_used == "finbert" else "📖"
            print(f"  {model_tag} {r.label:<4} {r.score:.3f} ({r.model_used}) | {t[:40]}")
