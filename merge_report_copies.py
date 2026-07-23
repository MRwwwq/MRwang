#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_report_copies.py — 副本自动合并脚本
============================================
功能: 当同一份研报产生多份MD副本时(本地目录+飞书同步目录),
      按"内容完整度优先"规则自动合并去重, 保留最完整版本

触发时机:
  手动: python3 merge_report_copies.py
  定时: cron 每日 17:45 (run_daily.sh step 6a)

合并规则:
  1. 同名文件: 比较字节数, 保留较大者
  2. 内容差异: 按行 diff, 合并双方独有行(含数字/核心数据)
  3. 归档: 合并后旧版本移入 _merged_backup 子目录

依赖:
  /www/wwwroot/stocks/reports/   — 源文件目录
  /www/wwwroot/stocks/reports/飞书同步摘要/ — 飞书副本

使用:
  python3 merge_report_copies.py                              # 全量合并
  python3 merge_report_copies.py --repo ./reports              # 指定源目录
  python3 merge_report_copies.py --dry-run                     # 仅预览, 不执行
"""

import os
import sys
import re
import json
import shutil
import hashlib
import argparse
import logging
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# ═══════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════

REPORT_DIR = Path("/www/wwwroot/stocks/reports")
FEISHU_SYNC_DIR = REPORT_DIR / "飞书同步摘要"
BACKUP_DIR = REPORT_DIR / "_merged_backup"
LOG_FILE = REPORT_DIR / "report_read_log.log"

# 文件命名模式: {base_name}_摘要.md
SUFFIX = "_摘要.md"

# 核心关键词 (用于判断哪份更完整)
CORE_KEYWORDS = [
    r"\d{4,}",           # 数字(数据)
    r"同比|环比|增长",
    r"营收|净利|产能|市占",
    r"元|亿|万|%",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8", mode="a"),
    ],
)
logger = logging.getLogger("MergeReports")


# ═══════════════════════════════════════════════
#  扫描副本对
# ═══════════════════════════════════════════════

def scan_duplicates(report_dir: Path, feishu_dir: Path) -> list:
    """
    扫描同名副本对

    :return: [{"base_name": str, "local": Path|None, "feishu": Path|None}, ...]
    """
    local_files = {}
    feishu_files = {}

    # 扫描本地目录(跳过飞书同步子目录和备份目录)
    for root, _, files in os.walk(str(report_dir)):
        if "_merged_backup" in root or "飞书同步摘要" in root:
            continue
        for f in files:
            if f.endswith(SUFFIX):
                base = f[: -len(SUFFIX)]
                local_files[base] = Path(root) / f

    # 扫描飞书同步目录
    if feishu_dir.exists():
        for f in os.listdir(str(feishu_dir)):
            if f.endswith(SUFFIX):
                base = f[: -len(SUFFIX)]
                feishu_files[base] = feishu_dir / f

    # 找重叠(同名)
    all_bases = set(local_files.keys()) | set(feishu_files.keys())
    pairs = []
    for base in sorted(all_bases):
        pairs.append({
            "base_name": base,
            "local": local_files.get(base),
            "feishu": feishu_files.get(base),
        })

    return pairs


# ═══════════════════════════════════════════════
#  内容完整度评分
# ═══════════════════════════════════════════════

def score_completeness(file_path: Path) -> dict:
    """评估单份文件的完整度"""
    result = {"size": 0, "lines": 0, "data_points": 0, "keywords": 0, "overall": 0}

    if not file_path or not file_path.exists():
        return result

    text = file_path.read_text(encoding="utf-8")
    result["size"] = file_path.stat().st_size
    result["lines"] = text.count("\n") + 1

    # 数据点: 数字出现的次数
    result["data_points"] = len(re.findall(r"\b\d+(?:\.\d+)?(?:%|亿|万|元|美元)?", text))

    # 核心关键词命中
    result["keywords"] = sum(1 for kw in CORE_KEYWORDS if re.search(kw, text))

    # 综合评分: 字节数权重0.3 + 数据点权重0.4 + 关键词权重0.3
    size_score = min(result["size"] / 5000, 1.0) * 100
    dp_score = min(result["data_points"] / 20, 1.0) * 100
    kw_score = min(result["keywords"] / 5, 1.0) * 100
    result["overall"] = round(size_score * 0.3 + dp_score * 0.4 + kw_score * 0.3, 1)

    return result


# ═══════════════════════════════════════════════
#  合并策略
# ═══════════════════════════════════════════════

def merge_two_md(path_a: Path, path_b: Path, output_path: Path) -> str:
    """
    智能合并两份MD: 取完整度高者为主, 补充缺失内容

    策略:
      1. 按完整度评分排序
      2. 以高分版本为主体
      3. 从低分版本提取: (a) 主体中缺失的含数据行 (b) 核心关键词行
      4. 写入合并后文件

    :return: "keep_a" / "keep_b" / "merged"
    """
    score_a = score_completeness(path_a)
    score_b = score_completeness(path_b)

    # 如果一个文件无效, 直接保留有效文件
    if score_a["size"] == 0 and score_b["size"] > 0:
        if path_b.resolve() != output_path.resolve():
            shutil.copy2(str(path_b), str(output_path))
        return "keep_b"
    if score_b["size"] == 0 and score_a["size"] > 0:
        if path_a.resolve() != output_path.resolve():
            shutil.copy2(str(path_a), str(output_path))
        return "keep_a"
    if score_a["size"] == 0 and score_b["size"] == 0:
        return "both_empty"

    # 以高分版本为主
    primary = path_a if score_a["overall"] >= score_b["overall"] else path_b
    secondary = path_b if primary == path_a else path_a
    primary_name = "A" if primary == path_a else "B"

    primary_text = primary.read_text(encoding="utf-8")
    secondary_text = secondary.read_text(encoding="utf-8")

    # 提取二级版本的独特行(含数据的)
    primary_lines = set(primary_text.split("\n"))
    new_lines = []
    for line in secondary_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in primary_lines:
            continue
        # 保留含数字或核心关键词的独有行
        if re.search(r"\d", stripped) or any(re.search(kw, stripped) for kw in CORE_KEYWORDS[:5]):
            new_lines.append(stripped)

    if not new_lines:
        # 无新增内容, 直接保留主版本
        if primary.resolve() != output_path.resolve():
            shutil.copy2(str(primary), str(output_path))
        return f"keep_{primary_name}"

    # 合并: 在主版本末尾追加补充行
    merged = primary_text.rstrip() + "\n\n## 补充数据(自动合并)\n"
    for line in new_lines:
        merged += f"- {line}\n"
    merged += f"\n*自动合并于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n"

    output_path.write_text(merged, encoding="utf-8")
    return f"merged_from_{primary_name}"


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="研报副本自动合并")
    parser.add_argument("--repo", default=str(REPORT_DIR), help="报告根目录")
    parser.add_argument("--dry-run", action="store_true", help="仅预览, 不执行合并")
    args = parser.parse_args()

    report_dir = Path(args.repo)
    feishu_dir = report_dir / "飞书同步摘要"
    backup_dir = report_dir / "_merged_backup"

    logger.info("=" * 60)
    logger.info(f"📑 研报副本自动合并 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        logger.info("🔍 DRY RUN 模式 — 仅预览, 不执行")
    logger.info("=" * 60)

    pairs = scan_duplicates(report_dir, feishu_dir)
    if not pairs:
        logger.info("未找到同名副本, 无需合并")
        return

    # 只处理同时存在 local 和 feishu 的副本对
    mergable = [p for p in pairs if p["local"] and p["feishu"]]
    logger.info(f"扫描完成: 共{len(pairs)}个基准名, {len(mergable)}对需合并")

    merged_count = 0
    for p in mergable:
        local_path = p["local"]
        feishu_path = p["feishu"]

        score_local = score_completeness(local_path)
        score_feishu = score_completeness(feishu_path)

        logger.info(f"\n  [{p['base_name']}]")
        logger.info(f"    本地: {local_path.name} ({score_local['overall']}分/{score_local['size']}B)")
        logger.info(f"    飞书: {feishu_path.name} ({score_feishu['overall']}分/{score_feishu['size']}B)")

        if args.dry_run:
            # 仅打印差异
            if score_local["overall"] >= score_feishu["overall"]:
                logger.info(f"    → 将保留本地版本")
            else:
                logger.info(f"    → 将保留飞书版本")
            continue

        # 执行合并: 输出到本地路径
        backup_dir.mkdir(parents=True, exist_ok=True)

        # 备份旧版
        if local_path.exists():
            backup_path = backup_dir / f"{p['base_name']}_备份_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            shutil.copy2(str(local_path), str(backup_path))

        # 合并
        result = merge_two_md(local_path, feishu_path, local_path)

        # 同步更新飞书副本(仅当路径不同)
        if local_path.resolve() != feishu_path.resolve():
            shutil.copy2(str(local_path), str(feishu_path))

        logger.info(f"    → 合并结果: {result} | 已同步至飞书副本")
        merged_count += 1

    logger.info(f"\n{'=' * 60}")
    logger.info(f"合并完成: {merged_count}对 | 备份目录: {backup_dir}")
    logger.info(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
