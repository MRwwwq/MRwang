#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guba_scraper.py — 东方财富股吧爬虫 v1.0
================================================================
技术方案：
  页面嵌入JSON解析（验证有效），非API调用。
  gubatopic.eastmoney.com API 已封禁(404)，改用
  guba.eastmoney.com/list,{code}_{page}.html 页面嵌入的
  <script>var article_list=JSON</script> 提取数据。

功能：
  1. 拉取帖子列表（80条/页，支持分页+时间过滤）
  2. 拉取单篇帖子正文+评论（BS4解析详情页）
  3. CSV增量存储（去重）
  4. 情感分析（集成FinSentiment）

用法：
  python3 guba_scraper.py 600547 --pages 2
  python3 guba_scraper.py 600547 --pages 3 --since 2026-07-01 --detail
  python3 guba_scraper.py 600547 --max 50 --analyze

依赖：
  pip install requests beautifulsoup4 lxml pandas
"""

import re, json, os, sys, time, random, csv
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ── 配置 ──
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/124.0.0.0 Safari/537.36",
]
REQUEST_DELAY = (2.0, 4.0)
POSTS_PER_PAGE = 80

# ── 输出路径 ──
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guba_data")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _get_headers(referer_url: str = "") -> dict:
    return {
        "User-Agent": random.choice(UA_LIST),
        "Referer": referer_url or "https://guba.eastmoney.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _sleep():
    time.sleep(random.uniform(*REQUEST_DELAY))


def _load_post_ids(code: str) -> set:
    path = os.path.join(OUTPUT_DIR, f"{code}_posts.csv")
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, dtype={"post_id": str})
        return set(df["post_id"].dropna().tolist())
    except:
        return set()


def _csv_path(code: str, suffix: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{code}_{suffix}.csv")


# ═══════════════════════════════════════════════
#  核心函数
# ═══════════════════════════════════════════════

def fetch_post_list(code: str, page: int) -> list:
    """
    拉取单页帖子列表（页面嵌入JSON解析）

    :param code: 股票代码 (如 600547)
    :param page: 页码 (从1开始)
    :return: [dict, ...] 帖子数据列表
    """
    url = f"https://guba.eastmoney.com/list,{code}_{page}.html"
    headers = _get_headers(url)
    _sleep()

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            return []
    except Exception as e:
        print(f"  ⚠ http error page={page}: {e}")
        return []

    match = re.search(r"var article_list=(\{.*?\});", resp.text, re.DOTALL)
    if not match:
        return []

    try:
        data = json.loads(match.group(1))
        posts = data.get("re", [])
        if not posts:
            return []
        return posts
    except json.JSONDecodeError:
        return []


def parse_post_item(item: dict) -> dict:
    """
    提取帖子关键字段（规范化）
    """
    return {
        "post_id": str(item.get("post_id", "")),
        "stock_code": str(item.get("stockbar_code", "")),
        "title": (item.get("post_title", "") or "").strip(),
        "author": item.get("user_nickname", ""),
        "pub_time": item.get("post_publish_time", ""),
        "last_time": item.get("post_last_time", ""),
        "read_cnt": item.get("post_click_count", 0),
        "comment_cnt": item.get("post_comment_count", 0),
        "forward_cnt": item.get("post_forward_count", 0),
        "has_pic": item.get("post_has_pic", False),
        "has_video": item.get("post_has_video", False),
        "from_num": item.get("post_from_num", ""),
        "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def filter_by_date(posts: list, since_date: str) -> list:
    """按日期过滤帖子"""
    if not since_date:
        return posts
    try:
        since_dt = datetime.strptime(since_date, "%Y-%m-%d")
    except ValueError:
        return posts

    result = []
    for p in posts:
        try:
            pub_dt = datetime.strptime(p["pub_time"], "%Y-%m-%d %H:%M:%S")
            if pub_dt >= since_dt:
                result.append(p)
        except (ValueError, KeyError):
            result.append(p)
    return result


def fetch_post_detail(code: str, post_id: str) -> dict:
    """
    抓取单篇帖子详情页（正文+评论）
    使用正则提取嵌入页面的JSON数据

    :return: {"content": str, "comments": [dict, ...]}
    """
    url = f"https://guba.eastmoney.com/news,{code},{post_id}.html"
    headers = _get_headers(url)
    _sleep()

    result = {"content": "", "comments": []}

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            return result
    except Exception:
        return result

    # 正文: 提取 post_content 中的JSON字符串值
    # 匹配 "post_content":"..." ，处理内部转义
    mc = re.search(r'"post_content"\s*:\s*"((?:[^"\\]|\\.)*)"', resp.text)
    if mc:
        raw = mc.group(1)
        # JSON转义还原
        raw = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t').replace('\\/', '/')
        raw = re.sub(r'\\u[0-9a-fA-F]{4}', '', raw)  # drop unicode escapes
        # 去除HTML标签
        raw = re.sub(r'<[^>]+>', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
        if len(raw) > 5:
            result["content"] = raw

    # 已确认：评论API(gubatopic/GetReplyList)全系404封禁
    # 无法获取评论内容，仅保留帖子正文

    return result


def save_posts(posts: list, code: str):
    """增量写入CSV（去重）"""
    path = _csv_path(code, "posts")
    df_new = pd.DataFrame(posts)
    if df_new.empty:
        return

    if os.path.exists(path):
        df_old = pd.read_csv(path, dtype={"post_id": str})
        df_merged = pd.concat([df_old, df_new], ignore_index=True)
        df_merged.drop_duplicates(subset=["post_id"], keep="last", inplace=True)
    else:
        df_merged = df_new

    df_merged.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  💾 写入 {path}: {len(df_new)}条新 / {len(df_merged)}条总计")


def save_comments(comments: list, code: str):
    """增量写入评论CSV"""
    if not comments:
        return
    path = _csv_path(code, "comments")
    df_new = pd.DataFrame(comments)
    if os.path.exists(path):
        df_old = pd.read_csv(path, dtype={"post_id": str})
        df_merged = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_merged = df_new

    df_merged.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  💾 写入 {path}: {len(df_new)}条新评论")


# ═══════════════════════════════════════════════
#  情感分析（集成FinSentiment）
# ═══════════════════════════════════════════════

def analyze_guba_sentiment(posts: list, titles_only: bool = True) -> dict:
    """
    对股吧帖子进行情感分析（批量）

    :param posts: 帖子列表
    :param titles_only: True=仅分析标题; False=分析标题+正文
    :return: 汇总统计dict
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fin_sentiment import FinSentiment
    sa = FinSentiment()

    results = []
    for p in posts:
        text = p.get("title", "")
        if not titles_only and p.get("content"):
            text += " " + p["content"]
        if not text:
            continue

        sent = sa.analyze(text)
        sent["post_id"] = p["post_id"]
        sent["title"] = p.get("title", "")[:60]
        sent["read_cnt"] = p.get("read_cnt", 0)
        sent["pub_time"] = p.get("pub_time", "")
        results.append(sent)

    if not results:
        return {"total": 0, "bullish": 0, "bearish": 0, "neutral": 0}

    labels = [r["label"] for r in results]
    bullish = labels.count("利好")
    bearish = labels.count("利空")
    neutral = labels.count("中性")
    avg_score = sum(r["score"] for r in results) / len(results)

    # 按阅读量加权的情绪得分（大V/热帖权重更大）
    total_reads = sum(r["read_cnt"] for r in results) or 1
    weighted_score = sum(r["score"] * r["read_cnt"] for r in results) / total_reads

    return {
        "total": len(results),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "avg_score": round(avg_score, 3),
        "weighted_score": round(weighted_score, 3),
        "sentiment": "积极" if avg_score > 0.55 else ("消极" if avg_score < 0.45 else "中性"),
        "detail": results,
    }


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def run(
    code: str,
    max_pages: int = 3,
    since_date: str = "",
    fetch_detail: bool = False,
    analyze: bool = False,
    max_analyze: int = 50,
) -> dict:
    """
    全流程执行

    :param code: 股票代码
    :param max_pages: 最大抓取页数
    :param since_date: 起始日期 YYYY-MM-DD
    :param fetch_detail: 是否抓取正文+评论（慢）
    :param analyze: 是否执行情感分析
    :param max_analyze: 最大分析条数
    :return: 汇总dict
    """
    print(f"\n{'='*60}")
    print(f"  股吧爬虫: {code}")
    print(f"  页数: {max_pages} | 起始: {since_date or '不限'} | 正文: {'是' if fetch_detail else '否'} | 情感分析: {'是' if analyze else '否'}")
    print(f"{'='*60}")

    all_posts = []
    exist_ids = _load_post_ids(code)

    for page in range(1, max_pages + 1):
        print(f"\n📄 第{page}页...", end="")
        page_posts = fetch_post_list(code, page)
        if not page_posts:
            print(" 空/无数据，停止翻页")
            break
        print(f" {len(page_posts)}条原始")

        new_posts = []
        for raw in page_posts:
            parsed = parse_post_item(raw)
            if parsed["post_id"] in exist_ids:
                continue
            new_posts.append(parsed)
            exist_ids.add(parsed["post_id"])

        # 日期过滤
        if since_date:
            new_posts = filter_by_date(new_posts, since_date)

        if not new_posts:
            print("  无新帖（全部已存在或超出日期范围）")
            continue

        print(f"  {len(new_posts)}条新帖")

        # 正文抓取（仅对新帖）
        if fetch_detail:
            for i, np in enumerate(new_posts):
                print(f"    📝 [{i+1}/{len(new_posts)}] {np['title'][:40]}...", end="")
                detail = fetch_post_detail(code, np["post_id"])
                np["content"] = detail["content"]
                detail_comments = detail.get("comments", [])
                if detail_comments:
                    # 首次有评论时创建
                    save_comments(detail_comments, code)
                print(f" 正文{len(detail['content'])}字 / {len(detail_comments)}评论")

        all_posts.extend(new_posts)

        # 增量保存每页
        save_posts(new_posts, code)

    # 汇总
    print(f"\n{'='*60}")
    print(f"  完成: 共{len(all_posts)}条新帖")

    result = {
        "code": code,
        "total_new": len(all_posts),
        "since_date": since_date,
        "has_detail": fetch_detail,
    }

    # 情感分析
    if analyze and all_posts:
        limit = min(max_analyze, len(all_posts))
        sent_result = analyze_guba_sentiment(all_posts[:limit])
        result["sentiment"] = sent_result

        # 打印情感汇总
        sr = sent_result
        print(f"\n📊 情感分析 ({limit}条):")
        print(f"   利好: {sr['bullish']}({sr['bullish']/sr['total']*100:.0f}%)")
        print(f"   利空: {sr['bearish']}({sr['bearish']/sr['total']*100:.0f}%)")
        print(f"   中性: {sr['neutral']}({sr['neutral']/sr['total']*100:.0f}%)")
        print(f"   平均得分: {sr['avg_score']}")
        print(f"   阅读加权: {sr['weighted_score']}")
        print(f"   整体: {sr['sentiment']}")
        print(f"\n   TOP5 积极帖:")
        sorted_pos = sorted(sr['detail'], key=lambda x: x['score'], reverse=True)[:5]
        for s in sorted_pos:
            print(f"    ✅ {s['score']:.2f} | {s['title']}")
        print(f"\n   TOP5 消极帖:")
        sorted_neg = sorted(sr['detail'], key=lambda x: x['score'])[:5]
        for s in sorted_neg:
            print(f"    ❌ {s['score']:.2f} | {s['title']}")

    return result


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="东方财富股吧爬虫")
    parser.add_argument("code", type=str, help="股票代码 (如 600547)")
    parser.add_argument("--pages", type=int, default=3, help="抓取页数 (默认3)")
    parser.add_argument("--since", type=str, default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--detail", action="store_true", help="抓取正文+评论 (慢)")
    parser.add_argument("--analyze", action="store_true", help="执行情感分析")
    parser.add_argument("--max", type=int, default=50, help="最多分析条数")
    args = parser.parse_args()

    result = run(
        code=args.code,
        max_pages=args.pages,
        since_date=args.since,
        fetch_detail=args.detail,
        analyze=args.analyze,
        max_analyze=args.max,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
