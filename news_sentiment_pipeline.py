#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
news_sentiment_pipeline.py — 新闻/股吧/研报情感分析流水线 v2.0
================================================================
三种模式：
  (默认) 新闻:   ak.stock_news_em → 正文抓取(可选) → FinSentiment
  --guba    股吧:   guba.eastmoney.com 页面嵌入JSON解析 → FinSentiment
  --research 研报:  ak.stock_research_report_em → 标题情感+评级综合 → FinSentiment
  --all     全模式: 以上三种顺序执行

用法：
  python3 news_sentiment_pipeline.py 600547
  python3 news_sentiment_pipeline.py 600547 --full       # 新闻含正文
  python3 news_sentiment_pipeline.py 600547 --guba        # 股吧
  python3 news_sentiment_pipeline.py 600547 --research    # 机构研报
  python3 news_sentiment_pipeline.py 600547 --all         # 全模式

依赖：
  pip install akshare beautifulsoup4 lxml requests pandas
"""

import sys, os, time, random, json
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

# 引入情感分析引擎
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fin_sentiment import FinSentiment

# ── 请求头(东方财富反爬) ──
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.eastmoney.com/"
}
REQUEST_DELAY = (2, 4)  # 随机延时范围(秒)


def fetch_news_list(symbol: str) -> list:
    """
    获取个股最新新闻列表
    
    :param symbol: 股票代码(如 600547)
    :return: [{"title":str, "content":str, "source":str, "url":str, "time":str}, ...]
    """
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=symbol)
        if df is None or len(df) == 0:
            return []
        
        news_list = []
        for _, row in df.iterrows():
            news_list.append({
                "title": row.get("新闻标题", ""),
                "summary": row.get("新闻内容", ""),
                "source": row.get("文章来源", ""),
                "url": row.get("新闻链接", ""),
                "time": str(row.get("发布时间", "")),
            })
        return news_list
    except ImportError:
        print("❌ 请安装 akshare: pip install akshare")
        return []
    except Exception as e:
        print(f"❌ 新闻列表获取失败: {e}")
        return []


def fetch_article_content(url: str) -> str:
    """
    抓取新闻正文(东方财富格式)
    
    :param url: 新闻链接
    :return: 正文文本(失败返回空字符串)
    """
    time.sleep(random.uniform(*REQUEST_DELAY))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # 东方财富正文div: contentbox > contentwrap > mainleft
        for cls in ["contentbox", "contentwrap", "mainleft"]:
            div = soup.find("div", class_=cls)
            if div:
                text = div.get_text(strip=True, separator=" ")
                clean = " ".join(text.split())
                # 过滤广告尾缀
                for noise in ["郑重声明", "文章来源", "责任编辑", "分享到", "扫一扫", "东方财富", "风险自担"]:
                    idx = clean.find(noise)
                    if idx > 50:
                        clean = clean[:idx]
                if len(clean) > 30:
                    return clean
        return ""
    except Exception:
        return ""


def run_pipeline(symbol: str, fetch_content: bool = False, max_articles: int = 5) -> dict:
    """
    全流程执行
    
    :param symbol: 股票代码
    :param fetch_content: 是否抓取正文(慢)
    :param max_articles: 最大分析条数
    :return: 汇总结果dict
    """
    sa = FinSentiment()
    
    # 1. 获取新闻列表
    news_list = fetch_news_list(symbol)
    if not news_list:
        return {"symbol": symbol, "total": 0, "error": "无新闻数据"}
    
    articles = news_list[:max_articles]
    
    # 2. 逐条分析
    results = []
    for art in articles:
        # 正文(可选)或摘要
        text = ""
        if fetch_content:
            text = fetch_article_content(art["url"])
        if not text:
            text = art["summary"]
        
        # 情感分析(标题+内容)
        title_sent = sa.analyze(art["title"])
        text_sent = sa.analyze(text) if text else title_sent
        
        results.append({
            "title": art["title"][:60],
            "source": art["source"],
            "time": art["time"],
            "title_label": title_sent["label"],
            "title_score": title_sent["score"],
            "content_snippet": text[:120] if text else "",
            "content_label": text_sent["label"],
            "content_score": text_sent["score"],
            "keywords": list(set(title_sent["pos_words"] + title_sent["neg_words"] +
                               text_sent["pos_words"] + text_sent["neg_words"])),
        })
    
    # 3. 汇总统计
    labels = [r["content_label"] for r in results]
    bullish = labels.count("利好")
    bearish = labels.count("利空")
    neutral = labels.count("中性")
    
    return {
        "symbol": symbol,
        "total": len(results),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "sentiment_score": round((bullish * 1.0 + neutral * 0.5) / max(1, len(results)), 3),
        "articles": results,
        "fetched_content": fetch_content,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ═══════════════════════════════════════════════
#  股吧情感分析（集成guba_scraper）
# ═══════════════════════════════════════════════

def run_guba_pipeline(symbol: str, pages: int = 3, since_date: str = "",
                      fetch_detail: bool = False, max_analyze: int = 50) -> dict:
    """
    股吧帖子情感分析流水线

    :param symbol: 股票代码
    :param pages: 抓取页数
    :param since_date: 起始日期
    :param fetch_detail: 是否抓取正文
    :param max_analyze: 最大分析条数
    :return: 汇总结果dict
    """
    from guba_scraper import run as run_guba

    result = run_guba(
        code=symbol,
        max_pages=pages,
        since_date=since_date,
        fetch_detail=fetch_detail,
        analyze=True,
        max_analyze=max_analyze,
    )
    return result


# ═══════════════════════════════════════════════
#  机构研报情感分析（ak.stock_research_report_em）
# ═══════════════════════════════════════════════

def fetch_research_reports(symbol: str) -> list:
    """
    获取个股机构研报+盈利预测

    :param symbol: 股票代码
    :return: [{"title":str, "org":str, "rating":str, "date":str,
               "eps_2026":float, "pe_2026":float,
               "eps_2027":float, "pe_2027":float,
               "eps_2028":float, "pe_2028":float,
               "url":str}, ...]
    """
    try:
        import akshare as ak
        df = ak.stock_research_report_em(symbol=symbol)
        if df is None or len(df) == 0:
            return []
        reports = []
        for _, row in df.iterrows():
            r = {
                "title": row.get("报告名称", ""),
                "org": row.get("机构", ""),
                "rating": row.get("东财评级", ""),
                "date": str(row.get("日期", "")),
                "eps_2026": row.get("2026-盈利预测-收益", None),
                "pe_2026": row.get("2026-盈利预测-市盈率", None),
                "eps_2027": row.get("2027-盈利预测-收益", None),
                "pe_2027": row.get("2027-盈利预测-市盈率", None),
                "eps_2028": row.get("2028-盈利预测-收益", None),
                "pe_2028": row.get("2028-盈利预测-市盈率", None),
                "url": row.get("报告PDF链接", ""),
            }
            reports.append(r)
        return reports
    except ImportError:
        print("❌ 请安装 akshare: pip install akshare")
        return []
    except Exception as e:
        print(f"❌ 研报获取失败: {e}")
        return []


def run_research_pipeline(symbol: str) -> dict:
    """
    机构研报情感分析流水线

    :param symbol: 股票代码
    :return: 汇总结果dict
    """
    sa = FinSentiment()
    reports = fetch_research_reports(symbol)
    if not reports:
        return {"symbol": symbol, "total": 0, "error": "无研报数据"}

    # 评级映射分数
    rating_map = {"买入": 1.0, "增持": 0.7, "推荐": 0.7, "谨慎推荐": 0.5,
                  "中性": 0.0, "谨慎增持": 0.5, "减持": -0.5, "卖出": -1.0}

    # 逐条分析
    results = []
    avg_eps_list = []
    for r in reports:
        # 标题情感
        sent = sa.analyze(r["title"])
        rating_score = rating_map.get(r["rating"], 0.0)
        # 综合情绪: 标题情感60% + 评级40%
        combined = round(sent["score"] * 0.6 + (rating_score * 0.5 + 0.5) * 0.4, 3)
        if combined > 0.6:
            label = "利好"
        elif combined < 0.4:
            label = "利空"
        else:
            label = "中性"

        eps_vals = [v for v in [r["eps_2026"], r["eps_2027"], r["eps_2028"]] if v is not None]
        if eps_vals:
            avg_eps_list.extend(eps_vals)

        results.append({
            "title": r["title"][:60],
            "org": r["org"],
            "rating": r["rating"],
            "date": r["date"],
            "sentiment_label": label,
            "sentiment_score": combined,
            "eps_2026": r["eps_2026"],
            "pe_2026": r["pe_2026"],
            "eps_2027": r["eps_2027"],
            "pe_2027": r["pe_2027"],
        })

    # 汇总
    labels = [r["sentiment_label"] for r in results]
    bullish = labels.count("利好")
    bearish = labels.count("利空")
    neutral = labels.count("中性")
    avg_score = round(sum(r["sentiment_score"] for r in results) / max(1, len(results)), 3)

    # 评级分布
    rating_counts = {}
    for r in reports:
        rating_counts[r["rating"]] = rating_counts.get(r["rating"], 0) + 1

    return {
        "symbol": symbol,
        "total": len(results),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "avg_score": avg_score,
        "sentiment": "积极" if avg_score > 0.55 else ("消极" if avg_score < 0.45 else "中性"),
        "rating_distribution": rating_counts,
        "avg_eps": round(sum(avg_eps_list) / max(1, len(avg_eps_list)), 3) if avg_eps_list else None,
        "reports": results,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="新闻/股吧/研报情感分析流水线 v2.0")
    parser.add_argument("symbol", type=str, help="股票代码(如 600547)")
    parser.add_argument("--full", action="store_true", help="新闻: 抓取正文(较慢)")
    parser.add_argument("--max", type=int, default=5, help="新闻: 最多分析条数")
    parser.add_argument("--guba", action="store_true", help="股吧情感分析模式")
    parser.add_argument("--pages", type=int, default=3, help="股吧: 抓取页数")
    parser.add_argument("--since", type=str, default="", help="股吧: 起始日期 YYYY-MM-DD")
    parser.add_argument("--detail", action="store_true", help="股吧: 抓取正文(慢)")
    parser.add_argument("--research", action="store_true", help="机构研报情感分析模式")
    parser.add_argument("--all", action="store_true", help="全模式: 新闻+股吧+研报")
    args = parser.parse_args()

    if args.all:
        print(f"\n{'='*60}")
        print(f"  🔄 全模式: {args.symbol}")
        print(f"{'='*60}")
        result_news = run_pipeline(args.symbol, fetch_content=args.full, max_articles=args.max)
        result_guba = run_guba_pipeline(args.symbol, pages=args.pages,
                                        since_date=args.since, fetch_detail=args.detail,
                                        max_analyze=args.max)
        result_research = run_research_pipeline(args.symbol)
        result_all = {
            "symbol": args.symbol,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "news": result_news,
            "guba": result_guba,
            "research": result_research,
        }
        output = json.dumps(result_all, ensure_ascii=False, indent=2, default=str)
        print(output)
    elif args.research:
        result = run_research_pipeline(args.symbol)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.guba:
        result = run_guba_pipeline(
            symbol=args.symbol, pages=args.pages,
            since_date=args.since, fetch_detail=args.detail,
            max_analyze=args.max,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        result = run_pipeline(args.symbol, fetch_content=args.full, max_articles=args.max)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
