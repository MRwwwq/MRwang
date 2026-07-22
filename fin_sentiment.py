#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fin_sentiment.py — 金融文本情感分析引擎 v1.0
================================================================
功能：
  1. 基于SnowNLP + 金融关键词词典的双层情感打分
  2. 支持新闻、公告、快评三种文本场景
  3. 输出: {label, score, pos_words, neg_words, confidence}
  4. 无需联网(本地词典+通用模型)

词典来源：
  - 正面关键词: 业绩增长/扭亏/中标/突破/回购/增持...
  - 负面关键词: 亏损/减持/违约/诉讼/套保浮亏/减值...
  - 修饰词: 超预期/大幅/略超/不及预期...

用法：
  from fin_sentiment import FinSentiment
  sa = FinSentiment()
  result = sa.analyze("山东黄金净利润同比增长56%")
  print(result)  # {label:'利好', score:0.87, ...}
"""

import re
import json
from typing import Dict, Optional


# ═══════════════════════════════════════════════
#  金融情感关键词词典 v1.0
# ═══════════════════════════════════════════════

FIN_POSITIVE = {
    # 业绩/财务
    "业绩增长": 0.6, "扭亏为盈": 0.8, "大幅增长": 0.7, "同比增长": 0.5,
    "环比增长": 0.5, "营收增长": 0.6, "净利润": 0.3, "利润大增": 0.7,
    "超预期": 0.8, "盈利": 0.4, "毛利率提升": 0.6, "净利率提升": 0.6,
    "现金流改善": 0.5, "经营现金流": 0.3, "预喜": 0.5, "大幅预增": 0.7,
    # 经营
    "中标": 0.6, "订单": 0.3, "签约": 0.3, "合同": 0.2,
    "突破": 0.5, "量产": 0.5, "投产": 0.4, "产能释放": 0.6,
    "扩产": 0.4, "市占率": 0.3,
    # 资本
    "回购": 0.6, "增持": 0.6, "分红": 0.5, "派息": 0.4,
    "股权激励": 0.5, "定增": 0.2,
    # 行业/政策
    "政策利好": 0.7, "扶持": 0.5, "补贴": 0.4, "关税减免": 0.5,
    "需求增长": 0.5, "景气": 0.4, "复苏": 0.5,
    # 黄金/贵金属专用
    "金价上涨": 0.6, "金价创新高": 0.7, "央行购金": 0.5,
    "降息": 0.5, "地缘风险": 0.3,
    # 技术/产品
    "AI": 0.3, "数字化": 0.3, "研发": 0.2, "专利": 0.3,
    "技术突破": 0.6, "新产品": 0.4,
    # 股吧/论坛专用
    "抄底": 0.5, "拉升": 0.5, "涨停": 0.6, "起飞": 0.5,
    "吃肉": 0.4, "加仓": 0.4, "反攻": 0.5, "反转": 0.5,
    "龙头": 0.4, "利好出尽": 0.2, "企稳": 0.3,
    # 黄金股吧专用
    "金价涨": 0.6, "大涨": 0.5, "看多": 0.4, "牛": 0.3,
}

FIN_NEGATIVE = {
    # 业绩/财务
    "亏损": -0.7, "下滑": -0.6, "下降": -0.5, "减少": -0.4,
    "净利润下降": -0.7, "营收下滑": -0.6, "毛利率下降": -0.6,
    "不及预期": -0.7, "低于预期": -0.6, "预亏": -0.7,
    "业绩变脸": -0.8, "由盈转亏": -0.8,
    # 风险
    "减值": -0.7, "坏账": -0.7, "商誉": -0.5, "亏损": -0.7,
    "违约": -0.8, "诉讼": -0.6, "仲裁": -0.5, "处罚": -0.7,
    "立案": -0.8, "调查": -0.6, "风险警示": -0.8, "ST": -0.9,
    # 资本
    "减持": -0.6, "解禁": -0.4, "质押": -0.3, "平仓": -0.8,
    "资金流出": -0.5, "套现": -0.6, "资金出逃": -0.6,
    # 经营
    "停产": -0.7, "停工": -0.6, "减产": -0.5, "裁员": -0.6,
    "价格战": -0.5, "产能过剩": -0.5, "库存积压": -0.5,
    # 负面
    "套保亏损": -0.8, "套期保值亏损": -0.8, "套期保值浮亏": -0.8,
    "套保浮亏": -0.8, "产生浮亏": -0.7, "浮亏": -0.6,
    "公允价值变动": -0.5, "公允价值变动损失": -0.7,
    "克金成本上涨": -0.5,
    "深部开采": -0.3, "外购冶炼": -0.2,
    # 负面事件
    "安全事故": -0.7, "环保处罚": -0.6, "违规": -0.6,
    "通报": -0.4, "批评": -0.4, "警示": -0.4,
    # 股吧/论坛专用
    "跌停": -0.7, "暴跌": -0.7, "崩盘": -0.7, "套牢": -0.5,
    "出货": -0.6, "割肉": -0.6, "止损": -0.4, "跳水": -0.5,
    "利空": -0.4, "空仓": -0.2, "跑路": -0.5, "清仓": -0.3,
    "退市": -0.8, "st": -0.7, "接盘": -0.4, "杀跌": -0.5,
    "观望": -0.2, "腰斩": -0.6, "阴跌": -0.4,
    # 黄金股吧专用
    "金价跌": -0.6, "看空": -0.4, "回落": -0.4,
}

# 修饰词(放大/缩小情感强度)
MODIFIERS = {
    "大幅": 1.5, "明显": 1.3, "显著": 1.4, "持续": 1.2,
    "略": 0.7, "小幅": 0.8, "微": 0.6,
    "超": 1.3, "创": 1.2, "历史": 1.3,
    "首次": 1.2, "再次": 1.1, "连续": 1.2,
}


class FinSentiment:
    """金融文本情感分析引擎"""
    
    def __init__(self):
        self.pos_dict = FIN_POSITIVE
        self.neg_dict = FIN_NEGATIVE
        self.modifiers = MODIFIERS
        # 预编译关键词(按长度降序,优先匹配长词)
        self.pos_words = sorted(self.pos_dict.keys(), key=len, reverse=True)
        self.neg_words = sorted(self.neg_dict.keys(), key=len, reverse=True)
    
    def _extract_keywords(self, text: str) -> tuple:
        """提取命中的正面/负面关键词及修饰词"""
        hit_pos = []
        hit_neg = []
        hit_mod = []
        text_lower = text.lower()
        
        for word in self.pos_words:
            if word in text_lower:
                hit_pos.append(word)
        
        for word in self.neg_words:
            if word in text_lower:
                hit_neg.append(word)
        
        for word in self.modifiers:
            pattern = word + r"(?:增长|提升|下降|下滑|亏损|盈利|超出|预期)"
            if re.search(pattern, text):
                hit_mod.append(word)
        
        return hit_pos, hit_neg, hit_mod
    
    def _snownlp_fallback(self, text: str) -> float:
        """SnowNLP辅助打分"""
        try:
            from snownlp import SnowNLP
            s = SnowNLP(text)
            return s.sentiments
        except:
            return 0.5
    
    def analyze(self, text: str, method: str = "hybrid") -> Dict:
        """
        情感分析主函数
        
        :param text: 输入文本(中文)
        :param method: hybrid(词典+SnowNLP) | dict_only | snownlp_only
        :return: {label, score, pos_words, neg_words, confidence, method}
        """
        if not text or len(text.strip()) < 5:
            return {"label": "中性", "score": 0.5, "confidence": 0,
                    "pos_words": [], "neg_words": [], "method": method}
        
        pos_hit, neg_hit, mod_hit = self._extract_keywords(text)
        
        # 词典得分
        dict_score = 0.5  # 中性基准
        for w in pos_hit:
            modifier = 1.0
            for m in mod_hit:
                if re.search(m + r".{0,4}" + w, text):
                    modifier = self.modifiers.get(m, 1.0)
                    break
            dict_score += self.pos_dict[w] * modifier * 0.15
        for w in neg_hit:
            modifier = 1.0
            for m in mod_hit:
                if re.search(m + r".{0,4}" + w, text):
                    modifier = self.modifiers.get(m, 1.0)
                    break
            dict_score += self.neg_dict[w] * modifier * 0.15
        
        dict_score = max(0.0, min(1.0, dict_score))
        
        # SnowNLP得分(辅助)
        sn_score = self._snownlp_fallback(text)
        
        # 混合
        if method == "hybrid":
            if len(pos_hit) + len(neg_hit) >= 1:
                # 有明确金融关键词时以词典为主(权重85%)
                final_score = dict_score * 0.85 + sn_score * 0.15
            else:
                # 无明确信号时以SnowNLP为参考
                final_score = dict_score * 0.4 + sn_score * 0.6
        elif method == "dict_only":
            final_score = dict_score
        else:
            final_score = sn_score
        
        # 判定标签
        if final_score > 0.6:
            label = "利好"
        elif final_score < 0.4:
            label = "利空"
        else:
            label = "中性"
        
        # 置信度: 基于命中的关键词数量和强度
        confidence = min(1.0, (len(pos_hit) + len(neg_hit)) * 0.15 + 0.3)
        
        return {
            "label": label,
            "score": round(final_score, 3),
            "pos_words": pos_hit[:5],
            "neg_words": neg_hit[:5],
            "confidence": round(confidence, 2),
            "method": method,
        }
    
    def batch_analyze(self, texts: list, method: str = "hybrid") -> list:
        """批量分析"""
        return [self.analyze(t, method) for t in texts]


# ═══════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    sa = FinSentiment()
    
    test_cases = [
        # 利好
        "山东黄金业绩大幅增长，净利润同比增长56%，金价持续创新高",
        "公司中标重大工程项目，订单金额超预期",
        "公司发布回购计划，控股股东增持股份",
        # 利空
        "山东黄金套期保值产生大额浮亏，利润不及预期",
        "公司发布减持公告，控股股东计划减持5%股份",
        "行业价格战加剧，公司毛利率持续下滑",
        # 中性
        "公司发布公告，董事会换届选举完成",
        "公司召开2025年度股东大会",
        "行业月度数据发布，总体平稳",
        # 真实山东黄金场景
        "山东黄金公告称2025年预增56%-66%，金价上行推动利润增加",
        "山东黄金子公司售东海证券股权产生公允价值变动损失",
        "伦敦金现货持续回调，山东黄金受影响",
    ]
    
    print(f"{'文本':<40} {'标签':<6} {'得分':<8} {'置信度':<6}")
    print("-" * 70)
    for t in test_cases:
        r = sa.analyze(t)
        print(f"{t[:38]:<40} {r['label']:<6} {r['score']:<8.3f} {r['confidence']:<6.2f}")
