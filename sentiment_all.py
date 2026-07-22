#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sentiment_all.py — 另类舆情全层情感分析 v1.0
================================================================
覆盖5大数据源:
  1. 公告   (stock_notice_report / major_news)
  2. 新闻   (stock_news_em + 正文)
  3. 股吧   (guba_scraper 页面嵌入JSON)
  4. 研报   (stock_research_report_em)
  5. 龙虎榜 (top_list + top_inst 资金流向)

用法:
  python3 sentiment_all.py 600547                      # 全部5项
  python3 sentiment_all.py 600547 --quick              # 仅公告+新闻+研报(轻量)
  python3 sentiment_all.py 600547 --download-finbert   # 下载FinBERT模型
"""

import sys, os, json, time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from finbert_chinese import ChineseFinBERT, SentimentResult, analyze_money_flow_sentiment, analyze_top_inst_sentiment


# ═══════════════════════════════════════════════
#  1. 公告情感分析
# ═══════════════════════════════════════════════

def analyze_notices(symbol: str, sa: ChineseFinBERT, max_items: int = 20) -> dict:
    """分析全市场公告(按标的过滤) + major_news"""
    results = []
    try:
        import akshare as ak
        # 公告
        df = ak.stock_notice_report()
        if df is not None and len(df) > 0:
            mask = df['代码'] == symbol
            notices = df[mask].head(max_items)
            for _, row in notices.iterrows():
                title = row.get("公告标题", "")
                if not title:
                    continue
                sent = sa.analyze(title)
                results.append({
                    "type": "公告",
                    "title": title[:80],
                    "date": str(row.get("公告日期", "")),
                    "label": sent.label,
                    "score": sent.score,
                    "model": sent.model_used,
                })
        # major_news
        try:
            import tushare as ts
            pro = ts.pro_api()
            df_news = pro.major_news(ts_code=symbol, limit=10)
            if df_news is not None and len(df_news) > 0:
                for _, row in df_news.iterrows():
                    sent = sa.analyze(str(row.get("title", "")))
                    results.append({
                        "type": "公告",
                        "title": str(row.get("title", ""))[:80],
                        "date": str(row.get("pub_time", "")),
                        "label": sent.label,
                        "score": sent.score,
                        "model": sent.model_used,
                    })
        except Exception:
            pass
    except Exception as e:
        print(f"  ⚠ 公告分析异常: {e}")

    return _summarize("公告", results)


# ═══════════════════════════════════════════════
#  2. 新闻情感分析
# ═══════════════════════════════════════════════

def analyze_news(symbol: str, sa: ChineseFinBERT, max_items: int = 10) -> dict:
    """分析东方财富新闻"""
    from news_sentiment_pipeline import fetch_news_list, fetch_article_content

    news_list = fetch_news_list(symbol)
    if not news_list:
        return {"source": "新闻", "total": 0, "sentiment": "无数据"}

    results = []
    for art in news_list[:max_items]:
        text = art["title"]
        if art.get("summary"):
            text += " " + art["summary"]
        sent = sa.analyze(text)
        results.append({
            "type": "新闻",
            "title": art["title"][:60],
            "source": art.get("source", ""),
            "label": sent.label,
            "score": sent.score,
            "model": sent.model_used,
        })

    return _summarize("新闻", results)


# ═══════════════════════════════════════════════
#  3. 股吧情感分析
# ═══════════════════════════════════════════════

def analyze_guba(symbol: str, sa: ChineseFinBERT, max_items: int = 30) -> dict:
    """分析股吧帖子"""
    from guba_scraper import fetch_post_list, parse_post_item, filter_by_date

    results = []
    posts = fetch_post_list(symbol, 1)
    for raw in posts[:max_items]:
        p = parse_post_item(raw)
        sent = sa.analyze(p["title"])
        results.append({
            "type": "股吧",
            "title": p["title"][:60],
            "author": p["author"],
            "reads": p["read_cnt"],
            "label": sent.label,
            "score": sent.score,
            "model": sent.model_used,
        })

    return _summarize("股吧", results)


# ═══════════════════════════════════════════════
#  4. 研报情感分析
# ═══════════════════════════════════════════════

def analyze_research(symbol: str, sa: ChineseFinBERT, max_items: int = 30) -> dict:
    """分析机构研报"""
    from news_sentiment_pipeline import fetch_research_reports

    reports = fetch_research_reports(symbol)
    if not reports:
        return {"source": "研报", "total": 0, "sentiment": "无数据"}

    results = []
    rating_map = {"买入": 1.0, "增持": 0.8, "推荐": 0.7, "谨慎推荐": 0.5,
                  "中性": 0.3, "减持": 0.1, "卖出": 0.0}

    for r in reports[:max_items]:
        sent = sa.analyze(r["title"])
        rating_score = rating_map.get(r["rating"], 0.5)
        combined = sent.score * 0.5 + rating_score * 0.35 + 0.15  # bias toward stability
        label = "利好" if combined > 0.55 else ("利空" if combined < 0.45 else "中性")
        results.append({
            "type": "研报",
            "title": r["title"][:60],
            "org": r["org"],
            "rating": r["rating"],
            "label": label,
            "score": round(combined, 3),
            "model": "hybrid",
        })

    return _summarize("研报", results)


# ═══════════════════════════════════════════════
#  5. 龙虎榜资金流向
# ═══════════════════════════════════════════════

def analyze_money_flow(symbol: str) -> dict:
    """资金流向情感分析"""
    results = []
    try:
        import tushare as ts
        pro = ts.pro_api()
        # 日线资金流
        end_date = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=20)).strftime("%Y%m%d")
        df = pro.money_flow(ts_code=symbol, start_date=start, end_date=end_date)
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                net = row.get("net_amount", 0) or 0
                sent = analyze_money_flow_sentiment(float(net))
                results.append({
                    "type": "资金流向",
                    "date": str(row.get("trade_date", "")),
                    "net_amount_wan": float(net),
                    "label": sent.label,
                    "score": sent.score,
                    "model": sent.model_used,
                })

        # 龙虎榜
        df_top = pro.top_list(trade_date=end_date)
        if df_top is not None and len(df_top) > 0:
            mask = df_top['ts_code'] == symbol
            top_stocks = df_top[mask]
            for _, row in top_stocks.iterrows():
                buy = float(row.get("buy_amount", 0) or 0)
                sell = float(row.get("sell_amount", 0) or 0)
                sent = analyze_top_inst_sentiment(buy, sell)
                results.append({
                    "type": "龙虎榜",
                    "date": str(row.get("trade_date", "")),
                    "buy_wan": buy,
                    "sell_wan": sell,
                    "reason": str(row.get("reason", "")),
                    "label": sent.label, 
                    "score": sent.score,
                    "model": sent.model_used,
                })
    except ImportError:
        pass
    except Exception as e:
        print(f"  ⚠ 资金流向分析异常: {e}")

    return _summarize("资金流", results)


# ═══════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════

def _summarize(source: str, results: list) -> dict:
    """汇总统计"""
    if not results:
        return {"source": source, "total": 0, "sentiment": "无数据"}

    labels = [r["label"] for r in results]
    bullish = labels.count("利好")
    bearish = labels.count("利空")
    neutral = labels.count("中性")
    avg_score = round(sum(r["score"] for r in results) / len(results), 3)

    return {
        "source": source,
        "total": len(results),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "avg_score": avg_score,
        "sentiment": "积极" if avg_score > 0.55 else ("消极" if avg_score < 0.45 else "中性"),
        "details": results,
    }


def print_summary(all_results: list):
    """打印汇总表格"""
    print(f"\n{'='*60}")
    print(f"  另类舆情全层情感分析")
    print(f"{'='*60}")
    print(f" {'数据源':<10} {'总量':<6} {'利好':<6} {'利空':<6} {'中性':<6} {'均分':<8} {'情绪':<8}")
    print(f" {'-'*10} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*8}")
    total_bullish = 0
    total_bearish = 0
    total_neutral = 0
    for r in all_results:
        if r["total"] > 0:
            print(f" {r['source']:<10} {r['total']:<6} {r['bullish']:<6} {r['bearish']:<6} {r['neutral']:<6} {r['avg_score']:<8.3f} {r['sentiment']:<8}")
            total_bullish += r["bullish"]
            total_bearish += r["bearish"]
            total_neutral += r["neutral"]
        else:
            print(f" {r['source']:<10} {'--':<6} {'--':<6} {'--':<6} {'--':<6} {'--':<8} {'--':<8}")
    grand_total = max(1, total_bullish + total_bearish + total_neutral)
    print(f" {'─'*10} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*8} {'─'*8}")
    print(f" {'合计':<10} {grand_total:<6} {total_bullish:<6} {total_bearish:<6} {total_neutral:<6} {((total_bullish+total_neutral*0.5)/grand_total):<8.3f} ", end="")
    if total_bullish > total_bearish * 2:
        print("🟢 积极")
    elif total_bearish > total_bullish * 2:
        print("🔴 消极")
    else:
        print("🟡 中性")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="另类舆情全层情感分析")
    parser.add_argument("symbol", type=str, help="股票代码")
    parser.add_argument("--quick", action="store_true", help="轻量模式(仅公告+新闻+研报)")
    parser.add_argument("--download-finbert", action="store_true", help="下载FinBERT模型")
    args = parser.parse_args()

    if args.download_finbert:
        from finbert_chinese import download_model
        download_model()
        sys.exit(0)

    sa = ChineseFinBERT()
    print(f"  引擎: {'FinBERT' if sa.model_loaded else 'FinSentiment(降级)'}")
    print(f"  标的: {args.symbol} | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    results = []

    # 1. 公告
    t0 = time.time()
    r = analyze_notices(args.symbol, sa)
    r["elapsed"] = round(time.time() - t0, 1)
    results.append(r)
    print(f"  📋 公告: {r['total']}条 | {r.get('sentiment', '--')} | {r.get('elapsed', 0)}s")

    # 2. 新闻
    t0 = time.time()
    r = analyze_news(args.symbol, sa)
    r["elapsed"] = round(time.time() - t0, 1)
    results.append(r)
    print(f"  📰 新闻: {r['total']}条 | {r.get('sentiment', '--')} | {r.get('elapsed', 0)}s")

    if not args.quick:
        # 3. 股吧
        t0 = time.time()
        r = analyze_guba(args.symbol, sa)
        r["elapsed"] = round(time.time() - t0, 1)
        results.append(r)
        print(f"  💬 股吧: {r['total']}条 | {r.get('sentiment', '--')} | {r.get('elapsed', 0)}s")

    # 4. 研报
    t0 = time.time()
    r = analyze_research(args.symbol, sa)
    r["elapsed"] = round(time.time() - t0, 1)
    results.append(r)
    print(f"  📄 研报: {r['total']}条 | {r.get('sentiment', '--')} | {r.get('elapsed', 0)}s")

    if not args.quick:
        # 5. 资金流向
        t0 = time.time()
        r = analyze_money_flow(args.symbol)
        r["elapsed"] = round(time.time() - t0, 1)
        results.append(r)
        print(f"  💰 资金: {r['total']}条 | {r.get('sentiment', '--')} | {r.get('elapsed', 0)}s")

    # 汇总
    print_summary(results)

    # JSON output
    print(json.dumps({"symbol": args.symbol, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "engine": "finbert" if sa.model_loaded else "finsentiment",
                       "sources": results},
                      ensure_ascii=False, indent=2, default=str))
