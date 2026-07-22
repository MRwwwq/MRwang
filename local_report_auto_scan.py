#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_report_auto_scan.py — 本地报告自动扫描解析推送系统 v3
================================================================
执行时机: 每日盘后17:30 (run_daily.sh step 5); 支持手动触发
监控目录: /www/wwwroot/stocks/reports/
解析格式: .pdf / .docx / .txt

核心链路:
  扫描→过滤(黑名单去重)→解析→文本提纯→Wiki上传→双副本MD→飞书推送→标记完成→数据回流

依赖文件:
  processed_record.json   — 已处理文件黑名单(防重复)
  report_read_log.log      — 双日志持久化

输出归档:
  本地MD摘要  (与源文件同目录)
  飞书同步MD  (/飞书同步摘要/)
  Wiki知识库  (API归档)
  飞书业务群  (Webhook推送)
"""

import os
import sys
import re
import json
import time
import random
import logging
import hashlib
import fcntl
import requests
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════
#  环境配置 (test / production)
# ═══════════════════════════════════════════════

ENV = os.environ.get("SCANNER_ENV", "test")  # 默认 test, 设置 SCANNER_ENV=production 启用
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_WIKI_SPACE_ID = os.environ.get("FEISHU_WIKI_SPACE_ID", "")  # 知识库空间ID

# ═══════════════════════════════════════════════
#  路径配置 (Linux适配)
# ═══════════════════════════════════════════════

MONITOR_DIR = Path("/www/wwwroot/stocks/reports")
PROCESSED_RECORD_FILE = MONITOR_DIR / "processed_record.json"
LOG_FILE = MONITOR_DIR / "report_read_log.log"
FEISHU_SYNC_DIR = MONITOR_DIR / "飞书同步摘要"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/9710028a-8263-458a-b75c-bcf13a0cc670"
HERMES_AGENT_URL = "http://127.0.0.1:8080/api/reflux"
STREAMLIT_DATA_FILE = MONITOR_DIR / "飞书同步摘要" / "streamlit_dashboard_data.json"

SUFFIX_ALLOW = {".pdf", ".docx", ".txt"}

# ═══════════════════════════════════════════════
#  归档保留策略
# ═══════════════════════════════════════════════

ARCHIVE_RETENTION_DAYS = 90  # 归档记录保留90天，超出自动清理


# ═══════════════════════════════════════════════
#  日志配置 (控制台+文件双持久化)
# ═══════════════════════════════════════════════

log_format = "%(asctime)s | %(levelname)s | %(message)s"
logger = logging.getLogger("LocalReportAutoScan")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter(log_format))
    logger.addHandler(ch)
    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8", mode="a")
    fh.setFormatter(logging.Formatter(log_format))
    logger.addHandler(fh)


# ═══════════════════════════════════════════════
#  一、前置初始化
# ═══════════════════════════════════════════════

def init_environment() -> dict:
    """
    校验并创建所需目录/文件
    :return: {"monitor_dir_ok": bool, "feishu_dir_ok": bool, "processed_count": int}
    """
    result = {"monitor_dir_ok": False, "feishu_dir_ok": False, "processed_count": 0}

    # 1. 监控目录
    if not MONITOR_DIR.exists():
        MONITOR_DIR.mkdir(parents=True)
        logger.warning(f"监控目录不存在, 已自动创建: {MONITOR_DIR}")
    result["monitor_dir_ok"] = True
    logger.info(f"监控目录: {MONITOR_DIR}")

    # 2. 飞书同步目录
    FEISHU_SYNC_DIR.mkdir(parents=True, exist_ok=True)
    result["feishu_dir_ok"] = True
    logger.info(f"飞书同步目录: {FEISHU_SYNC_DIR}")

    # 3. 已处理记录
    if PROCESSED_RECORD_FILE.exists():
        try:
            with open(PROCESSED_RECORD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = data if isinstance(data, list) else []
            result["processed_count"] = len(records)
            logger.info(f"已处理记录: {len(records)}条 (黑名单加载完成)")
        except:
            logger.warning("processed_record.json 解析失败, 重置为空")
            with open(PROCESSED_RECORD_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
            result["processed_count"] = 0
    else:
        with open(PROCESSED_RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        logger.info("processed_record.json 不存在, 已新建空文件")
        result["processed_count"] = 0

    return result


# ═══════════════════════════════════════════════
#  二、目录扫描 + 黑名单过滤
# ═══════════════════════════════════════════════

def load_processed_blacklist() -> set:
    """加载已处理文件黑名单"""
    if not PROCESSED_RECORD_FILE.exists():
        return set()
    try:
        with open(PROCESSED_RECORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        return set()
    except:
        return set()


def scan_new_files(blacklist: set = None) -> list:
    """
    递归扫描目录, 过滤黑名单, 返回新增文件列表

    :return: [{"path": str, "name": str, "suffix": str, "size_kb": float}, ...]
    """
    blacklist = blacklist or set()
    all_files = []
    new_files = []

    for root, _, filenames in os.walk(str(MONITOR_DIR)):
        # 跳过飞书同步目录(不处理自身的MD产物)
        if "飞书同步摘要" in root:
            continue
        for fname in filenames:
            suffix = os.path.splitext(fname)[1].lower()
            if suffix not in SUFFIX_ALLOW:
                continue
            full_path = os.path.abspath(os.path.join(root, fname))
            all_files.append({"path": full_path, "name": fname, "suffix": suffix,
                              "size_kb": round(os.path.getsize(full_path) / 1024, 1)})

    # 过滤黑名单
    processed_set = {os.path.abspath(p) for p in blacklist}
    for f in all_files:
        if f["path"] not in processed_set:
            new_files.append(f)

    logger.info(f"扫描完成: 共计{len(all_files)}份, 新增{len(new_files)}份待处理")
    for f in new_files:
        logger.info(f"  待处理: {f['name']} ({f['size_kb']}KB)")

    return new_files


# ═══════════════════════════════════════════════
#  三、文档解析 + 文本提纯
# ═══════════════════════════════════════════════

def parse_pdf(filepath: str) -> str:
    """PDF解析"""
    import fitz
    doc = fitz.open(filepath)
    text = "\n".join([page.get_text() for page in doc])
    doc.close()
    return text


def parse_docx(filepath: str) -> str:
    """DOCX解析"""
    import docx
    doc = docx.Document(filepath)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])


def parse_txt(filepath: str) -> str:
    """TXT解析(UTF-8→GBK回退)"""
    for enc in ["utf-8", "gbk"]:
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except:
            continue
    return ""


def purify_text(raw_text: str) -> str:
    """
    文本提纯 — 过滤券商主观话术, 保留客观产业数据

    过滤规则:
      1. 去掉页眉页脚(常见于PDF扫描件)
      2. 去掉券商联系方式/免责声明
      3. 去掉纯主观看多/看空短句
      4. 保留含数据的客观描述 (供需/订单/技术/政策/财报数字)
      5. 截断超长冗余, 保留完整逻辑链条 (前5000字符)
    """
    if not raw_text or len(raw_text.strip()) < 50:
        return ""

    text = raw_text

    # 过滤行级噪音
    noise_patterns = [
        r"(?:研究员|分析师|联系人|执业证书|资格编号)[：:\s][^\n]{0,50}",
        r"(?:免责声明|风险提示|本报告由|本公司|证券投资咨询)[^\n]{0,200}",
        r"(?:请阅读最后一页|重要声明|评级说明|投资评级)[^\n]{0,200}",
        r"(?:市场有风险|入市需谨慎|股市有风险)[^\n]{0,100}",
        r"(?:敬请参阅|报告日期|页眉页脚|\d{1,2}页?\s*/\s*\d{1,2}页?)",
        r"[\n\s]{3,}",  # 多余空行
    ]
    for p in noise_patterns:
        text = re.sub(p, "\n", text)

    # 过滤纯主观短句 (无数据的看多看空)
    subjective_patterns = [
        r"我们(?:看好|认为|预计|判断|建议|维持)[^，。\d]{2,20}(?:投资评级|关注|推荐)",
        r"(?:强烈|长期|重点|持续)(?:推荐|看好|关注)[^，。\d]{2,20}",
        r"(?:维持|给予)(?:买入|增持|持有|中性|减持)[^，。\d]{0,10}",
    ]
    for p in subjective_patterns:
        text = re.sub(p, "", text)

    # 保留含核心关键词的行 (产业数据, 供需, 技术, 政策)
    core_keywords = r"(?:同比|环比|增长|\d+[.%]|营收|净利|产能|产量|销量|订单|市占|渗透率|突破|量产|政策|补贴|关税|专利|研发|半导体|新能源|AI|算力|数据|模型|芯片|电池|光伏|风电|储能|电动|智能)"
    lines = text.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) < 5:
            continue
        # 保留含数字或核心关键词的行
        if re.search(r"\d", stripped) or re.search(core_keywords, stripped):
            kept.append(stripped)

    result = "\n".join(kept)

    # 截断超长冗余 (保留前5000字符)
    if len(result) > 5000:
        result = result[:5000] + "\n\n[---文本截断, 保留前5000字符---]"

    return result


def parse_and_purify(file_info: dict) -> dict:
    """
    对单文件执行: 解析 + 提纯 + 校验

    :return: {"ok": bool, "raw_text": str, "clean_text": str, "char_count": int, "error": str}
    """
    path = file_info["path"]
    suffix = file_info["suffix"]
    result = {"ok": False, "raw_text": "", "clean_text": "", "char_count": 0, "error": ""}

    logger.info(f"解析文件: {file_info['name']}")

    try:
        if suffix == ".pdf":
            raw = parse_pdf(path)
        elif suffix == ".docx":
            raw = parse_docx(path)
        elif suffix == ".txt":
            raw = parse_txt(path)
        else:
            result["error"] = f"不支持格式: {suffix}"
            logger.warning(f"  ⚠️ {result['error']}")
            return result
    except Exception as e:
        result["error"] = f"解析异常: {type(e).__name__}: {e}"
        logger.error(f"  ❌ {result['error']}")
        return result

    if not raw or len(raw.strip()) < 50:
        result["error"] = "空白文档/无效内容"
        logger.warning(f"  ⚠️ 空白文档({len(raw or '')}字符)")
        return result

    clean = purify_text(raw)
    result["raw_text"] = raw
    result["clean_text"] = clean
    result["char_count"] = len(clean)
    result["ok"] = len(clean) > 50

    logger.info(f"  ✅ 解析完成: 原始{len(raw)}字 → 提纯{len(clean)}字")
    logger.info(f"  核心片段: {clean[:200]}")
    return result


# ═══════════════════════════════════════════════
#  四、核心链路1: 飞书Wiki知识库上传 (Feishu Open API)
# ═══════════════════════════════════════════════

# 归档记录（内存 + 文件双持久）
ARCHIVE_RECORDS_FILE = MONITOR_DIR / "飞书同步摘要" / "archive_records.json"


def _load_archive_records() -> list:
    """加载归档记录"""
    if ARCHIVE_RECORDS_FILE.exists():
        try:
            return json.loads(ARCHIVE_RECORDS_FILE.read_text(encoding="utf-8"))
        except:
            return []
    return []


def _save_archive_record(record: dict):
    """写入归档记录"""
    records = _load_archive_records()
    records.append(record)
    ARCHIVE_RECORDS_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"  📚 归档记录已写入: {record.get('file_name', '')}")


def _get_feishu_token() -> Optional[str]:
    """获取飞书 tenant_access_token"""
    if ENV != "production":
        logger.info("  🔑 [test] 跳过token获取 (环境=test)")
        return "mock_token_test_env"
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        logger.warning("  ⚠️ FEISHU_APP_ID 或 FEISHU_APP_SECRET 未配置, 使用模拟上传")
        return None
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                token = data["tenant_access_token"]
                logger.info(f"  🔑 飞书token获取成功 (expires_in={data.get('expire', '?')}s)")
                return token
        logger.error(f"  ❌ 飞书token获取失败: {resp.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"  ❌ 飞书token请求异常: {e}")
        return None


def upload_to_wiki(file_name: str, file_path: str, clean_text: str) -> dict:
    """
    调用飞书Open API上传文档至知识库

    流程:
      1. 获取 tenant_access_token
      2. 创建 docx 文档
      3. 添加至知识库节点
      4. 写入归档记录

    test 环境: 打印日志, 模拟成功
    production 环境: 真实调用飞书API

    :return: {"ok": bool, "doc_id": str, "archive_url": str, "error": str}
    """
    result = {"ok": False, "doc_id": "", "archive_url": "", "error": ""}

    # ── test 环境: 模拟 ──
    if ENV != "production":
        simulated_id = hashlib.md5(file_path.encode()).hexdigest()[:12]
        result["ok"] = True
        result["doc_id"] = f"test_wiki_{simulated_id}"
        result["archive_url"] = f"/wiki/doc/{simulated_id}"
        logger.info(f"  📤 [test] 模拟Wiki上传: doc_id={result['doc_id']}")
        logger.info(f"  📤 [test] 文件={file_name}, 内容长度={len(clean_text)}字")
        logger.info(f"  📤 [test] 若需生产上传, 设置 SCANNER_ENV=production")
        return result

    # ── production 环境: 真实上传 ──
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        result["error"] = "FEISHU_APP_ID/SECRET 未配置"
        logger.error(f"  ❌ {result['error']}")
        return result

    token = _get_feishu_token()
    if not token:
        result["error"] = "飞书token获取失败"
        return result

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        # Step 1: 创建文档
        doc_title = f"【行业研报】{file_name.replace('.txt','').replace('.pdf','')}_{datetime.now().strftime('%Y%m%d')}"
        create_resp = requests.post(
            "https://open.feishu.cn/open-apis/docx/v1/documents",
            headers=headers,
            json={"title": doc_title},
            timeout=15,
        )
        if create_resp.status_code != 200:
            result["error"] = f"创建文档失败 HTTP {create_resp.status_code}"
            logger.error(f"  ❌ {result['error']}: {create_resp.text[:200]}")
            return result
        create_data = create_resp.json()
        if create_data.get("code") != 0:
            result["error"] = f"创建文档失败: {create_data.get('msg','')}"
            logger.error(f"  ❌ {result['error']}")
            return result

        doc_id = create_data["data"]["document"]["document_id"]
        logger.info(f"  📄 文档已创建: doc_id={doc_id}")

        # Step 2: 写入文档内容
        # 先清空默认内容块, 再写入提纯后的文本
        content_blocks = []
        for line in clean_text.split("\n"):
            if line.strip():
                content_blocks.append({
                    "block_type": 2,  # 文本块
                    "text": {"elements": [{"text_run": {"content": line[:200]}}]},
                })
        batch_resp = requests.post(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            headers=headers,
            json={"children": content_blocks[:50]},  # 最多50块/批
            timeout=15,
        )
        if batch_resp.status_code == 200:
            logger.info(f"  ✍️ 文档内容写入完成 ({len(content_blocks)}段)")
        else:
            logger.warning(f"  ⚠️ 文档内容写入返回: {batch_resp.status_code}")

        # Step 3: 添加至知识库节点
        wiki_url = ""
        if FEISHU_WIKI_SPACE_ID:
            node_resp = requests.post(
                f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{FEISHU_WIKI_SPACE_ID}/nodes",
                headers=headers,
                json={
                    "obj_type": "docx",
                    "parent_node_token": "",  # 根目录
                    "obj_token": doc_id,
                },
                timeout=15,
            )
            if node_resp.status_code == 200 and node_resp.json().get("code") == 0:
                node_data = node_resp.json()["data"]["node"]
                wiki_url = node_data.get("url", "")
                logger.info(f"  📚 已添加至知识库: {wiki_url}")
            else:
                logger.warning(f"  ⚠️ 知识库添加返回: {node_resp.status_code} {node_resp.text[:100]}")

        result["ok"] = True
        result["doc_id"] = doc_id
        result["archive_url"] = wiki_url or f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"

        # Step 4: 写入归档记录
        archive_record = {
            "file_name": file_name,
            "file_path": file_path,
            "doc_id": doc_id,
            "wiki_url": wiki_url,
            "upload_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content_length": len(clean_text),
            "env": ENV,
        }
        _save_archive_record(archive_record)
        logger.info(f"  ✅ 飞书Wiki上传全流程完成: doc_id={doc_id}")

    except Exception as e:
        result["error"] = f"飞书API异常: {type(e).__name__}: {e}"
        logger.error(f"  ❌ {result['error']}")

    return result


# ═══════════════════════════════════════════════
#  五、核心链路2: 双副本MD摘要生成
# ═══════════════════════════════════════════════

def generate_md_summary(file_info: dict, parse_result: dict, wiki_result: dict) -> dict:
    """
    生成MD摘要, 双路径存储

    :return: {"ok": bool, "local_path": str, "feishu_path": str}
    """
    md_content = f"""# 【{file_info['name']}】行业研究摘要

## 基础元数据
- 原始本地路径：{file_info['path']}
- 文档类型：{file_info['suffix']}
- 文件大小：{file_info['size_kb']}KB
- 自动处理时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Wiki知识库归档ID：{wiki_result.get('doc_id', 'N/A')}
- Wiki归档地址：{wiki_result.get('archive_url', 'N/A')}

## 客观核心基本面信息
{parse_result['clean_text']}

## 同步标识
【飞书自动同步】量化智能体Hermes Agent已收录，参与赛道因子迭代

---
*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | local_report_auto_scan v3*
"""

    # 副本A: 与源文件同目录
    src_dir = os.path.dirname(file_info["path"])
    base_name = os.path.splitext(file_info["name"])[0]
    local_md = os.path.join(src_dir, f"{base_name}_摘要.md")
    with open(local_md, "w", encoding="utf-8") as f:
        f.write(md_content)
    logger.info(f"  📝 本地MD: {local_md}")

    # 副本B: 飞书同步目录
    feishu_md = os.path.join(str(FEISHU_SYNC_DIR), f"{base_name}_摘要.md")
    with open(feishu_md, "w", encoding="utf-8") as f:
        f.write(md_content)
    logger.info(f"  📝 飞书同步MD: {feishu_md}")

    return {"ok": True, "local_path": local_md, "feishu_path": feishu_md}


# ═══════════════════════════════════════════════
#  六、核心链路3: 飞书推送
# ═══════════════════════════════════════════════

def push_to_feishu(file_name: str, clean_text: str, wiki_url: str, md_path: str) -> bool:
    """
    推送MD摘要至飞书业务群

    :return: True=成功
    """
    try:
        import requests
        title = f"新行业文档同步: {file_name}"
        summary = clean_text[:2000] if clean_text else "无有效内容"
        content = (
            f"**{title}**\n\n"
            f"{summary}\n\n"
            f"---\n"
            f"📁 本地路径: `{md_path}`\n"
            f"📎 Wiki归档: {wiki_url}\n"
            f"⏱ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
                "elements": [
                    {"tag": "markdown", "content": content},
                    {"tag": "hr"},
                    {"tag": "note", "elements": [{"tag": "plain_text",
                                                  "content": "自动扫描·不构成投资建议"}]},
                ],
            },
        }
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            logger.info(f"  📤 飞书推送成功: {file_name}")
            return True
        else:
            logger.warning(f"  ⚠️ 飞书推送返回异常: {resp.status_code} {resp.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"  ❌ 飞书推送失败: {e}")
        return False


# ═══════════════════════════════════════════════
#  六B、飞书推送2: 任务结束指标摘要+错误清单
# ═══════════════════════════════════════════════

def push_summary_to_feishu(summary: dict, elapsed: float = 0) -> bool:
    """
    任务结束后推送汇总指标+错误清单至飞书群

    :param summary: {"total": int, "success": int, "failed": int, "failures": list}
    :param elapsed: 总耗时(秒)
    :return: True=成功
    """
    try:
        total = summary.get("total", 0)
        success = summary.get("success", 0)
        failed = summary.get("failed", 0)
        failures = summary.get("failures", [])

        # 构建状态表情
        if failed == 0 and total > 0:
            header_emoji = "✅"
            template = "green"
        elif failed > 0 and success > 0:
            header_emoji = "⚠️"
            template = "yellow"
        elif failed == total > 0:
            header_emoji = "🚨"
            template = "red"
        else:
            header_emoji = "ℹ️"
            template = "blue"

        elapsed_str = f"{elapsed:.1f}s" if elapsed > 0 else "—"

        # 构建错误清单
        error_lines = ""
        if failures:
            for fl in failures:
                ename = fl.get("name", "?")
                err = fl.get("error", "")
                if not err:
                    failed_steps = [k for k, v in fl.get("steps", {}).items() if not v]
                    err = f"步骤失败: {failed_steps}" if failed_steps else "未知"
                error_lines += f"- ❌ **{ename}**: {err}\n"

        content = (
            f"**📊 报告自动扫描汇总**\n\n"
            f"| 指标 | 值 |\n"
            f"|:---|:---|\n"
            f"| 总计 | {total} |\n"
            f"| ✅ 成功 | {success} |\n"
            f"| ❌ 失败 | {failed} |\n"
            f"| ⏱ 耗时 | {elapsed_str} |\n"
            f"| 📁 产出目录 | `飞书同步摘要/` |\n"
        )

        elements = [
            {"tag": "markdown", "content": content},
            {"tag": "hr"},
        ]

        if error_lines:
            elements.append({
                "tag": "markdown",
                "content": f"**🔴 错误清单:**\n{error_lines}"
            })
            elements.append({"tag": "hr"})

        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')} · 自动扫描·不构成投资建议"}]
        })

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"{header_emoji} 报告扫描任务完成"},
                           "template": template},
                "elements": elements,
            },
        }
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            logger.info(f"  📊 飞书汇总推送成功: {total}条, 成功{success}, 失败{failed}")
            return True
        else:
            logger.warning(f"  ⚠️ 飞书汇总推送返回: {resp.status_code} {resp.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"  ❌ 飞书汇总推送异常: {e}")
        return False


def push_alert_to_feishu(alert_type: str, detail: str, traceback_str: str = ""):
    """
    超时/异常场景独立告警推送 (红色紧急卡片)

    :param alert_type: 告警类型 ('timeout', 'exception', 'system')
    :param detail:     问题描述
    :param traceback_str: 可选堆栈信息
    """
    try:
        type_labels = {
            "timeout": "⏰ 超时告警",
            "exception": "🚨 异常告警",
            "system": "⚙️ 系统告警",
        }
        header_title = type_labels.get(alert_type, f"⚠️ 告警({alert_type})")

        content = (
            f"**{header_title}**\n\n"
            f"**时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**环境:** {ENV}\n"
            f"**详情:** {detail}\n"
        )
        if traceback_str:
            content += f"\n**堆栈:**\n```\n{traceback_str[:1500]}\n```\n"

        elements = [
            {"tag": "markdown", "content": content},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text",
                     "content": "🆘 需人工介入检查日志·自动扫描·不构成投资建议"}
                ],
            },
        ]

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": header_title},
                           "template": "red"},
                "elements": elements,
            },
        }
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            logger.info(f"  🆘 飞书告警推送成功: {alert_type}")
        else:
            logger.warning(f"  ⚠️ 飞书告警推送返回: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        logger.error(f"  ❌ 飞书告警推送自身异常: {e}")


# ═══════════════════════════════════════════════
#  ＋文件锁上下文管理器
# ═══════════════════════════════════════════════

import contextlib

_LOCK_CACHE = {}


@contextlib.contextmanager
def file_lock(file_path: str, timeout: int = 10):
    """
    跨进程文件锁 (fcntl.flock), 防止并发写入脏数据

    :param file_path: 要锁定的文件路径
    :param timeout:    等待锁的超时秒数
    :raises TimeoutError: 超时未获取锁
    """
    lock_path = file_path + ".lock"
    lock_fd = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _LOCK_CACHE[file_path] = lock_fd
                break
            except (IOError, OSError):
                if time.time() > deadline:
                    raise TimeoutError(f"获取文件锁超时 ({timeout}s): {lock_path}")
                time.sleep(0.1)
        yield
    finally:
        if lock_fd is not None and file_path in _LOCK_CACHE:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            del _LOCK_CACHE[file_path]
            os.close(lock_fd)
            try:
                os.remove(lock_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════
#  ＋文件完整性校验
# ═══════════════════════════════════════════════

def verify_file_integrity(file_path: str, expected_markers: list = None) -> dict:
    """
    校验文件完整性: 存在性 → 非空 → 标记位 → 可选hash

    :param file_path:       文件路径
    :param expected_markers: 期望包含的文本标记列表
    :return: {"ok": bool, "size": int, "lines": int, "markers_ok": bool, "hash": str, "errors": list}
    """
    result = {"ok": False, "size": 0, "lines": 0, "markers_ok": False, "hash": "", "errors": []}

    if not os.path.exists(file_path):
        result["errors"].append("文件不存在")
        return result

    size = os.path.getsize(file_path)
    result["size"] = size
    if size == 0:
        result["errors"].append("文件为空(0字节)")
        return result

    try:
        with open(file_path, "rb") as f:
            content = f.read()
        text = content.decode("utf-8")
        result["lines"] = text.count("\n") + 1
        result["hash"] = hashlib.md5(content).hexdigest()

        if expected_markers:
            missing = [m for m in expected_markers if m not in text]
            if missing:
                result["errors"].append(f"缺失标记: {missing}")
            else:
                result["markers_ok"] = True
        else:
            result["markers_ok"] = True

        if not result["errors"]:
            result["ok"] = True
    except Exception as e:
        result["errors"].append(f"读取异常: {e}")

    return result

def cleanup_expired_archives():
    """
    定时清理过期归档记录
    规则: 删除 upload_time 超过 ARCHIVE_RETENTION_DAYS 的记录
    test 环境: 仅打印统计; production 环境: 执行全量清理
    """
    if not ARCHIVE_RECORDS_FILE.exists():
        logger.info("  归档文件不存在, 跳过清理")
        return 0

    records = _load_archive_records()
    if not records:
        logger.info("  归档记录为空, 跳过清理")
        return 0

    cutoff = datetime.now() - timedelta(days=ARCHIVE_RETENTION_DAYS)
    before = len(records)
    kept = []
    for r in records:
        ts = r.get("upload_time", "2000-01-01")[:10]
        try:
            r_date = datetime.strptime(ts, "%Y-%m-%d")
        except:
            r_date = datetime(2000, 1, 1)
        if r_date >= cutoff:
            kept.append(r)

    removed = before - len(kept)

    if removed > 0:
        ARCHIVE_RECORDS_FILE.write_text(
            json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"  归档清理: 移除{removed}条过期记录(>{ARCHIVE_RETENTION_DAYS}天), 剩余{len(kept)}条")
    else:
        logger.info(f"  归档检查: {len(kept)}条均在有效期内(≤{ARCHIVE_RETENTION_DAYS}天)")

    return removed


def mark_as_processed(file_path: str):
    """将文件路径写入 processed_record.json (仅全链路成功后)"""
    records = []
    if PROCESSED_RECORD_FILE.exists():
        try:
            with open(PROCESSED_RECORD_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                records = []
        except:
            records = []

    abs_path = os.path.abspath(file_path)
    if abs_path not in records:
        with file_lock(str(PROCESSED_RECORD_FILE)):
            records.append(abs_path)
            with open(PROCESSED_RECORD_FILE, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        logger.info(f"  ✅ 已标记完成: {os.path.basename(file_path)}")
    return True


# ═══════════════════════════════════════════════
#  八、数据回流 Hermes Agent
# ═══════════════════════════════════════════════

def reflux_to_hermes(file_name: str, clean_text: str, wiki_doc_id: str, md_path: str):
    """
    将提纯后的行业基本面数据推送至 Hermes Agent 8080

    同步回流:
      1. 产业结构化文本 → 补充错误案例库训练样本
      2. 赛道因子权重更新
    """
    try:
        import requests
        payload = {
            "source": "local_report_auto_scan",
            "file_name": file_name,
            "clean_text": clean_text[:3000],
            "wiki_doc_id": wiki_doc_id,
            "md_path": md_path,
            "processed_at": datetime.now().isoformat(),
        }
        resp = requests.post(HERMES_AGENT_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"  🔄 Hermes Agent回流成功: {file_name}")
        else:
            logger.warning(f"  ⚠️ Hermes回流返回{resp.status_code}")
    except requests.exceptions.ConnectionError:
        logger.warning(f"  ⚠️ Hermes Agent 8080未启动, 回流跳过")
    except Exception as e:
        logger.warning(f"  ⚠️ Hermes回流异常: {e}")


# ═══════════════════════════════════════════════
#  Streamlit 看板数据更新
# ═══════════════════════════════════════════════

def update_streamlit_dashboard(file_name: str, status: str, detail: str):
    """更新Streamlit看板的实时数据JSON"""
    records = []
    if STREAMLIT_DATA_FILE.exists():
        try:
            with open(STREAMLIT_DATA_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
        except:
            records = []

    records.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": date.today().strftime("%Y-%m-%d"),
        "file": file_name,
        "status": status,
        "detail": detail,
    })

    # 保留最近100条记录
    records = records[-100:]

    STREAMLIT_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STREAMLIT_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def run_full_pipeline(manual_trigger: bool = False):
    """
    全流程入口

    :param manual_trigger: True=手动触发, 日志标注
    """
    trigger_label = "手动触发" if manual_trigger else "定时调度(17:30)"
    logger.info("=" * 60)
    logger.info(f"📋 本地报告自动扫描解析 | {trigger_label} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    start_time = time.time()
    summary = {"total": 0, "success": 0, "failed": 0, "failures": []}

    try:
        # 1. 前置初始化
        init_result = init_environment()
        logger.info(f"初始化: 监控目录{'✅' if init_result['monitor_dir_ok'] else '❌'} "
                    f"飞书目录{'✅' if init_result['feishu_dir_ok'] else '❌'} "
                    f"已处理{init_result['processed_count']}条")

        # 2. 清理过期归档
        cleanup_expired_archives()

        # 3. 扫描+过滤
        blacklist = load_processed_blacklist()
        new_files = scan_new_files(blacklist)

        if not new_files:
            logger.info("无新增待处理文件, 流程结束")
            elapsed = time.time() - start_time
            # 无文件时也推送汇总(零任务)
            push_summary_to_feishu(summary, elapsed)
            logger.info("=" * 60 + "\n")
            return summary

        # 3-8: 逐文件处理
        summary = {"total": len(new_files), "success": 0, "failed": 0, "failures": []}

        for f in new_files:
            file_result = {"name": f["name"], "ok": False, "steps": {}}
            logger.info(f"\n--- 处理: {f['name']} ---")

            # 3. 解析+提纯
            parse_result = parse_and_purify(f)
            file_result["steps"]["parse"] = parse_result["ok"]
            if not parse_result["ok"]:
                file_result["error"] = parse_result.get("error", "解析失败")
                summary["failed"] += 1
                summary["failures"].append(file_result)
                update_streamlit_dashboard(f["name"], "❌", parse_result.get("error", ""))
                continue

            # 4. Wiki上传
            wiki_result = upload_to_wiki(f["name"], f["path"], parse_result["clean_text"])
            file_result["steps"]["wiki"] = wiki_result["ok"]
            if not wiki_result["ok"]:
                # 失败重试2次
                for retry in range(2):
                    logger.info(f"  🔄 Wiki重试({retry+1}/2)...")
                    time.sleep(2)
                    wiki_result = upload_to_wiki(f["name"], f["path"], parse_result["clean_text"])
                    if wiki_result["ok"]:
                        file_result["steps"]["wiki"] = True
                        break

            # 5. 双副本MD
            md_result = generate_md_summary(f, parse_result, wiki_result)
            file_result["steps"]["md"] = md_result["ok"]

            # 5b. 完整性校验
            for md_path in [md_result.get("local_path", ""), md_result.get("feishu_path", "")]:
                if md_path:
                    v = verify_file_integrity(md_path, expected_markers=["行业研究摘要", "飞书自动同步"])
                    if not v["ok"]:
                        logger.warning(f"  ⚠️ MD完整性校验失败: {md_path} → {v['errors']}")
                    else:
                        logger.info(f"  🔍 MD完整性校验通过: {v['size']}B/{v['lines']}行 md5={v['hash'][:12]}")

            # 6. 飞书推送 (单文件详情)
            wiki_url = wiki_result.get("archive_url", "")
            feishu_ok = push_to_feishu(f["name"], parse_result["clean_text"], wiki_url, md_result.get("feishu_path", ""))
            file_result["steps"]["feishu"] = feishu_ok

            # 7. 数据回流
            reflux_to_hermes(f["name"], parse_result["clean_text"],
                             wiki_result.get("doc_id", ""), md_result.get("feishu_path", ""))

            # 8. 完成标记 (仅全链路成功)
            all_ok = all(file_result["steps"].values())
            file_result["ok"] = all_ok
            if all_ok:
                mark_as_processed(f["path"])
                summary["success"] += 1
                update_streamlit_dashboard(f["name"], "✅", "全链路完成")
            else:
                summary["failed"] += 1
                failed_steps = [k for k, v in file_result["steps"].items() if not v]
                summary["failures"].append(file_result)
                update_streamlit_dashboard(f["name"], "⚠️", f"步骤失败: {failed_steps}")

    except TimeoutError as e:
        elapsed = time.time() - start_time
        logger.error(f"  🚨 超时异常 ({elapsed:.1f}s): {e}")
        summary.setdefault("failures", []).append({"name": "PIPELINE", "error": f"Timeout: {e}"})
        push_alert_to_feishu("timeout", f"报告扫描处理超时 ({elapsed:.1f}s): {e}")
    except Exception as e:
        elapsed = time.time() - start_time
        import traceback
        tb = traceback.format_exc()
        logger.error(f"  🚨 流程异常 ({elapsed:.1f}s): {e}\n{tb}")
        summary.setdefault("failures", []).append({"name": "PIPELINE", "error": f"{type(e).__name__}: {e}"})
        push_alert_to_feishu("exception", f"报告扫描处理异常 ({elapsed:.1f}s): {e}", tb)

    # 汇总结算 + 飞书推送
    elapsed = time.time() - start_time
    logger.info("\n" + "=" * 60)
    logger.info(f"📊 处理汇总: 总计{summary['total']} | 成功{summary['success']} | 失败{summary['failed']} | 耗时{elapsed:.1f}s")
    if summary.get("failures"):
        logger.warning(f"失败清单:")
        for fl in summary["failures"]:
            logger.warning(f"  ❌ {fl['name']}: {fl.get('error', fl.get('steps',''))}")
    logger.info(f"产出目录: {FEISHU_SYNC_DIR}")
    logger.info(f"黑名单: {PROCESSED_RECORD_FILE}")
    logger.info("=" * 60 + "\n")

    # 推送汇总指标 + 错误清单 (无文件时跳过)
    if summary["total"] > 0 or summary.get("failures"):
        push_summary_to_feishu(summary, elapsed)

    return summary


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="本地报告自动扫描解析推送系统")
    parser.add_argument("--manual", action="store_true", help="标记为手动触发")
    args = parser.parse_args()

    run_full_pipeline(manual_trigger=args.manual)
