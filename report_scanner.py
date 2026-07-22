# -*- coding: utf-8 -*-
"""
report_scanner.py — 报告文件扫描解析模块 (v2)
=========================================
功能: 扫描报告目录 → 自动解析PDF/DOCX/TXT文本 → 推送智能体接口
用途: 将券商研报/行业报告/公司公告文本化后送入量化智能体

使用:
  python3 report_scanner.py                                # 扫描默认目录
  python3 report_scanner.py /path/to/report/folder          # 指定目录
  python3 report_scanner.py --file /path/to/single.pdf     # 单文件解析
"""

import os
import sys
import logging
import argparse
from pathlib import Path

# ====================== 1. 日志配置: 控制台+本地双输出 ======================

# 日志存放于报告根目录下
DEFAULT_LOG_DIR = "/www/wwwroot/stocks/reports"
LOG_SAVE_PATH = os.path.join(DEFAULT_LOG_DIR, "report_read_log.log")
os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)

log_format = "%(asctime)s | %(levelname)s | %(message)s"
logger = logging.getLogger("ReportAutoScan")
logger.setLevel(logging.INFO)

if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_SAVE_PATH, encoding="utf-8", mode="a")
    file_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(file_handler)

# ====================== 2. 基础配置 ======================

# 报告根目录 (与日志同目录)
DEFAULT_REPORT_DIR = DEFAULT_LOG_DIR

# 支持解析的文件后缀
ALLOW_SUFFIX = {".pdf", ".docx", ".txt"}


# ====================== 3. 文件扫描函数 ======================

def scan_target_folder(report_dir: str = None) -> list:
    """
    递归扫描指定目录下所有 PDF/DOCX/TXT 文件

    :param report_dir: 扫描目录, 默认 DEFAULT_REPORT_DIR
    :return: [{"file_name": str, "full_path": str, "suffix": str}, ...]
    """
    folder = report_dir or DEFAULT_REPORT_DIR
    all_file_list = []

    logger.info("======= 开始扫描本地报告目录 =======")
    logger.info(f"扫描目录路径: {folder}")

    if not os.path.exists(folder):
        logger.error(f"目录不存在! 路径校验失败: {folder}")
        os.makedirs(folder, exist_ok=True)
        return all_file_list

    for root_path, _, file_names in os.walk(folder):
        for file_name in sorted(file_names):
            file_suffix = os.path.splitext(file_name)[1].lower()
            if file_suffix in ALLOW_SUFFIX:
                full_file_path = os.path.join(root_path, file_name)
                all_file_list.append({
                    "file_name": file_name,
                    "full_path": full_file_path,
                    "suffix": file_suffix,
                })
                logger.info(f"已识别待解析文件: {full_file_path}")

    logger.info(f"目录扫描完成, 合计找到 {len(all_file_list)} 份可解析报告")
    return all_file_list


# ====================== 4. 文件文本解析函数 ======================

def parse_file_content(file_info: dict) -> str:
    """
    按后缀自动选择解析器(PDF/DOCX/TXT)提取文本

    :param file_info: {"file_name": str, "full_path": str, "suffix": str}
    :return: 提取的纯文本内容
    """
    path = file_info["full_path"]
    suffix = file_info["suffix"]
    text_content = ""

    logger.info(f"开始解析文件: {file_info['file_name']}")

    try:
        if suffix == ".pdf":
            pdf_doc = fitz.open(path)
            for page in pdf_doc:
                text_content += page.get_text()
            pdf_doc.close()

        elif suffix == ".docx":
            word_doc = docx.Document(path)
            text_content = "\n".join([p.text for p in word_doc.paragraphs])

        elif suffix == ".txt":
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text_content = f.read()
            except UnicodeDecodeError:
                with open(path, "r", encoding="gbk") as f:
                    text_content = f.read()

        logger.info(f"文件 {file_info['file_name']} 文本提取完成, 文本总长度: {len(text_content)}")
        logger.info(f"文本片段预览: {text_content[:300]}")
        return text_content

    except Exception as e:
        logger.error(f"文件 {file_info['file_name']} 解析失败, 错误详情: {str(e)}")
        return ""


# ====================== 5. 对接智能体交互 (带日志回执) ======================

def send_to_agent(raw_text: str, file_name: str):
    """
    将提取的报告文本送入量化智能体, 记录交互日志
    （占位函数, 根据实际智能体接口替换内部实现）
    """
    logger.info(f"【交互智能体】推送文件《{file_name}》结构化基本面信息")
    # ------------------------------------------------------------------
    # 此处填入智能体API调用代码
    # agent_result = agent_api.extract_fund_data(raw_text)
    # ------------------------------------------------------------------
    agent_result = "模拟智能体解析回执: 行业、催化、景气数据提取完成"
    logger.info(f"【交互智能体】返回回执: {agent_result}")
    return agent_result


# ====================== 6. 飞书群推送 ======================

_FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/9710028a-8263-458a-b75c-bcf13a0cc670"


def push_feishu_group(md_file_path: str, file_name: str):
    """读取MD文件, 推送前2000字摘要至飞书业务群"""
    try:
        import requests
        with open(md_file_path, "r", encoding="utf-8") as f:
            md_content = f.read()[:2000]
        payload = {
            "msg_type": "markdown",
            "content": {
                "title": f"新行业文档同步: {file_name}",
                "text": md_content,
            },
        }
        resp = requests.post(_FEISHU_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            logger.info(f"✅ 已将MD摘要推送至飞书业务群, 文件: {md_file_path}")
        else:
            logger.warning(f"⚠️ 飞书推送返回异常: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        logger.error(f"❌ 飞书推送失败: {e}")


# ====================== 主执行入口 ======================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="报告文件扫描解析工具")
    parser.add_argument("path", nargs="?", help="扫描目录或单文件路径")
    parser.add_argument("--file", "-f", help="解析单个文件")
    args = parser.parse_args()

    logger.info("========== 本地报告自动读取程序启动 ==========")

    # 单文件模式
    if args.file:
        filepath = args.file
        if not os.path.isfile(filepath):
            logger.error(f"文件不存在: {filepath}")
            sys.exit(1)
        suffix = os.path.splitext(filepath)[1].lower()
        file_info = {"file_name": os.path.basename(filepath), "full_path": filepath, "suffix": suffix}
        content = parse_file_content(file_info)
        if content:
            send_to_agent(content, file_info["file_name"])
        logger.info("========== 单文件解析流程结束 ==========")
        sys.exit(0)

    # 指定目录或默认目录模式
    folder = args.path or DEFAULT_REPORT_DIR

    if os.path.isfile(folder):
        # 传入的是文件路径
        suffix = os.path.splitext(folder)[1].lower()
        file_info = {"file_name": os.path.basename(folder), "full_path": folder, "suffix": suffix}
        content = parse_file_content(file_info)
        if content:
            send_to_agent(content, file_info["file_name"])
    else:
        # 目录模式: 扫描 → 解析 → 推送
        report_files = scan_target_folder(folder)

        if not report_files:
            logger.warning("目录内无PDF/DOCX/TXT报告文件, 流程终止")
        else:
            for file_item in report_files:
                content = parse_file_content(file_item)
                if content:
                    send_to_agent(content, file_item["file_name"])
                else:
                    logger.warning(f"文件 {file_item['file_name']} 无有效文本, 跳过推送智能体")

    logger.info("========== 全部报告读取解析流程结束 ==========\n")
