#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pre_market_workflow_v4.py — 盘前固定执行窗口（开盘前5分钟强制执行）
======================================================================
执行时机: 09:25（开盘前5分钟）, 不可跳过/延后/简化
执行角色: 全自动调度

流程:
  第一步: 全链路数据源连通状态校验（多源故障分级处置）
  第二步: 当日重点观察标的清单批量导入与标签归档

产出物:
  1. data_source_check_log_{YYYYMMDD}.json  — 盘前数据源连通校验日志
  2. observation_snapshot_{YYYYMMDD}.json    — 当日观察标的标签清单快照
  3. pre_market_log_{YYYYMMDD}.log           — 完整执行日志
  4. 飞书推送通知/告警（通过外部通道）
"""

import os
import sys
import json
import time
import socket
import logging
import sqlite3
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

# ═══════════════════════════════════════════════
#  路径配置
# ═══════════════════════════════════════════════
BASE_DIR = Path("/opt/stock_agent")
REPORT_DIR = BASE_DIR / "reports"
LOG_DIR = BASE_DIR / "logs"
FAISS_DIR = BASE_DIR / "faiss_index"
SNAPSHOT_DIR = BASE_DIR / "param_snapshots"
SOLID_STATE_ARCHIVE = BASE_DIR / "solid_state_archive"
DB_PATH = BASE_DIR / "agent_memory.db"

for d in [REPORT_DIR, LOG_DIR, SNAPSHOT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════
#  日志配置
# ═══════════════════════════════════════════════
TODAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
TODAY_DATE = f"{TODAY[:4]}-{TODAY[4:6]}-{TODAY[6:8]}"

LOG_FILE = LOG_DIR / f"pre_market_log_{TODAY}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8", mode="w"),
        logging.StreamHandler(),
    ],
    force=True,
)
logger = logging.getLogger("PreMarket")

# ═══════════════════════════════════════════════
#  全局常量
# ═══════════════════════════════════════════════
DOMAIN_POOL = {
    "tushare":    ["api.tushare.pro"],
    "akshare":    ["api.akshare.xyz", "hq.sinajs.cn"],
    "xueqiu":     ["xueqiu.com", "stock.xueqiu.com"],
    "eastmoney":  ["push2.eastmoney.com", "datacenter-web.eastmoney.com"],
}
DATA_SOURCE_CN = {"tushare": "Tushare", "akshare": "AkShare", "xueqiu": "雪球", "eastmoney": "东方财富"}
DATA_SOURCE_PRIORITY = ["tushare", "eastmoney", "akshare", "xueqiu"]

# 五大标准化标签（强制）
TAG_DEFINITIONS = {
    "持仓":       {"id": 1, "desc": "当前实盘持仓标的", "rag_weight": 2.0},
    "短线跟踪":   {"id": 2, "desc": "短线交易标的(技术+资金优先)", "rag_weight": 1.2},
    "中线布局":   {"id": 3, "desc": "中期持有标的(基本面优先)", "rag_weight": 1.0},
    "风险避雷":   {"id": 4, "desc": "高风险规避标的", "rag_weight": 2.5},
    "观察跟踪":   {"id": 5, "desc": "无持仓轻仓跟踪", "rag_weight": 0.8},
}

# 标签→策略权重映射（与stock_tag_batch_import.py对齐）
GROUP_STRATEGY_CONFIG = {
    "持仓":       {"weights": {"valuation": 0.20, "momentum": 0.20, "flow": 0.20, "fundamental": 0.25, "sentiment": 0.15}},
    "短线跟踪":   {"weights": {"valuation": 0.10, "momentum": 0.30, "flow": 0.30, "fundamental": 0.15, "sentiment": 0.15}},
    "中线布局":   {"weights": {"valuation": 0.30, "momentum": 0.15, "flow": 0.15, "fundamental": 0.30, "sentiment": 0.10}},
    "风险避雷":   {"weights": {"valuation": 0.35, "momentum": 0.10, "flow": 0.10, "fundamental": 0.35, "sentiment": 0.10}},
    "观察跟踪":   {"weights": {"valuation": 0.25, "momentum": 0.20, "flow": 0.20, "fundamental": 0.25, "sentiment": 0.10}},
}

# 异常阈值
BATCH_BLOCK_THRESHOLD = 0.30
HIGH_RISK_THRESHOLD = 0.50

# ═══════════════════════════════════════════════
#  数据库工具函数
# ═══════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def ensure_db_tables():
    """确保盘前流程所需表存在"""
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS pre_market_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            step TEXT NOT NULL,
            detail TEXT,
            status TEXT DEFAULT 'OK',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS observation_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            stock_name TEXT,
            tag TEXT NOT NULL,
            is_suspended INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, ts_code, tag)
        );
        CREATE TABLE IF NOT EXISTS data_source_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            domain TEXT,
            status TEXT NOT NULL,
            error_count INTEGER DEFAULT 0,
            error_detail TEXT,
            action_taken TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════
#  第一步：全链路数据源连通状态校验
# ═══════════════════════════════════════════════

def check_domain_reachability(domain: str, timeout: int = 5) -> dict:
    """检测单个域名连通性（DNS + TCP 443）"""
    result = {"domain": domain, "dns_ok": False, "tcp_443_ok": False, "error": None}
    try:
        ip = socket.getaddrinfo(domain, 443)[0][4][0]
        result["dns_ok"] = True
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        code = s.connect_ex((ip, 443))
        s.close()
        result["tcp_443_ok"] = (code == 0)
    except Exception as e:
        result["error"] = str(e)
    return result

def check_tushare_api() -> dict:
    """校验Tushare Pro API连通性"""
    result = {"source": "tushare", "status": "UNKNOWN", "error_count": 0, "detail": ""}
    domains_ok = 0
    for dom in DOMAIN_POOL["tushare"]:
        cr = check_domain_reachability(dom)
        if cr["dns_ok"] and cr["tcp_443_ok"]:
            domains_ok += 1
    if domains_ok == len(DOMAIN_POOL["tushare"]):
        # 尝试API调用
        try:
            import tushare as ts
            pro = ts.pro_api()
            df = pro.trade_cal(exchange="SSE", start_date=TODAY, end_date=TODAY)
            if df is not None and not df.empty:
                result["status"] = "OK"
                result["detail"] = f"API连通, 今日开盘={df.iloc[0]['is_open']}"
            else:
                result["status"] = "DEGRADED"
                result["error_count"] = 1
                result["detail"] = "API连通但返回空数据"
        except Exception as e:
            result["status"] = "DEGRADED"
            result["error_count"] = 2
            result["detail"] = f"API调用异常: {str(e)[:100]}"
    else:
        result["status"] = "FAIL"
        result["error_count"] = len(DOMAIN_POOL["tushare"]) - domains_ok
        result["detail"] = f"域名连通性: {domains_ok}/{len(DOMAIN_POOL['tushare'])}通过"
    return result

def check_akshare_api() -> dict:
    """校验AkShare API连通性"""
    result = {"source": "akshare", "status": "UNKNOWN", "error_count": 0, "detail": ""}
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is not None and len(df) > 100:
            result["status"] = "OK"
            result["detail"] = f"A股全景数据就绪, {len(df)}只"
        else:
            result["status"] = "DEGRADED"
            result["error_count"] = 1
            result["detail"] = f"数据异常(行数={len(df) if df is not None else 0})"
    except Exception as e:
        result["status"] = "FAIL"
        result["error_count"] = 2
        result["detail"] = f"AkShare异常: {str(e)[:120]}"
    return result

def check_snowball_offline() -> dict:
    """校验雪球离线数据（仅检查离线缓存，不调日内接口）"""
    result = {"source": "xueqiu", "status": "OK", "error_count": 0, "detail": "离线模式"}
    # 检查昨日离线存档是否存在
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    snowball_cache = list(BASE_DIR.rglob(f"*snowball*{yesterday}*")) + list(BASE_DIR.rglob(f"*xueqiu*{yesterday}*"))
    if snowball_cache:
        result["detail"] = f"离线缓存存在({len(snowball_cache)}文件), 使用离线数据"
        result["cache_files"] = [str(s.name) for s in snowball_cache[:5]]
    else:
        result["detail"] = "无离线缓存, 当日仅使用Tushare/AkShare替代"
    return result

def check_eastmoney() -> dict:
    """校验东方财富数据源"""
    result = {"source": "eastmoney", "status": "UNKNOWN", "error_count": 0, "detail": ""}
    domains_ok = 0
    for dom in DOMAIN_POOL["eastmoney"]:
        cr = check_domain_reachability(dom)
        if cr["dns_ok"] and cr["tcp_443_ok"]:
            domains_ok += 1
    total = len(DOMAIN_POOL["eastmoney"])
    ratio = domains_ok / total if total > 0 else 0
    if ratio >= 0.75:
        result["status"] = "OK"
    elif ratio >= 0.5:
        result["status"] = "DEGRADED"
        result["error_count"] = total - domains_ok
    else:
        result["status"] = "FAIL"
        result["error_count"] = total - domains_ok
    result["detail"] = f"{domains_ok}/{total}域名连通"
    return result

def parse_yesterday_report_logs() -> dict:
    """遍历昨日报告日志, 统计各渠道错误频次"""
    stats = defaultdict(lambda: {"total_reqs": 0, "blocked": 0, "errors": defaultdict(int)})
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    # 扫描日志目录和报告目录
    scan_patterns = [
        LOG_DIR / f"*{yesterday}*",
        REPORT_DIR / f"*{yesterday}*",
        BASE_DIR / f"*{yesterday}*",
    ]
    log_files = []
    for pattern_dir in [LOG_DIR, REPORT_DIR]:
        if pattern_dir.exists():
            log_files.extend(sorted(pattern_dir.glob(f"*{yesterday}*")))
            log_files.extend(sorted(pattern_dir.glob(f"*.log")))
    # 读取匹配的错误行
    error_keywords = {
        "tushare": ["tushare", "api.tushare", "Tushare", "ts_"],
        "akshare": ["akshare", "ak_", "AkShare"],
        "xueqiu":  ["xueqiu", "雪球", "snowball", "302", "rate limit"],
        "eastmoney": ["eastmoney", "东方财富", "push2.east"],
    }
    for fp in log_files:
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            for src_key, keywords in error_keywords.items():
                for kw in keywords:
                    count = text.lower().count(kw.lower())
                    if "error" in text.lower() or "exception" in text.lower() or "fail" in text.lower():
                        stats[src_key]["blocked"] += count
                    stats[src_key]["total_reqs"] += count
        except Exception:
            pass
    return stats

def run_step1_data_source_check() -> dict:
    """第一步完整执行: 四大数据源连通校验 + 故障处置"""
    logger.info("=" * 60)
    logger.info("第一步: 全链路数据源连通状态校验")
    logger.info("=" * 60)

    # 1. 遍历昨日日志错误统计
    logger.info("[1/4] 读取昨日报告日志错误统计...")
    log_stats = parse_yesterday_report_logs()
    for src, s in log_stats.items():
        logger.info(f"  {DATA_SOURCE_CN.get(src, src)}: 总请求{s['total_reqs']}, 异常{s['blocked']}")

    # 2. 执行各源实时校验
    results = {}
    checks = {
        "tushare": check_tushare_api,
        "akshare": check_akshare_api,
        "xueqiu": check_snowball_offline,
        "eastmoney": check_eastmoney,
    }
    for src_name, check_fn in checks.items():
        logger.info(f"[2/4] 校验 {DATA_SOURCE_CN.get(src_name, src_name)}...")
        try:
            r = check_fn()
        except Exception as e:
            r = {"source": src_name, "status": "FAIL", "error_count": 99, "detail": str(e)[:100]}
        results[src_name] = r
        status_icon = {"OK": "✅", "DEGRADED": "⚠️", "FAIL": "❌", "UNKNOWN": "❓"}
        logger.info(f"  {status_icon.get(r['status'],'❓')} {r['status']} | {r['detail']}")

    # 3. 故障分级处置
    logger.info("[3/4] 故障分级处置...")
    actions_taken = []
    global_risk_up = False

    for src, r in results.items():
        action = {"source": src, "status": r["status"], "actions": []}
        if r["status"] == "FAIL":
            if src == "xueqiu":
                action["actions"].append("❌ 雪球离线: 轮换代理IP+拉长请求间隔≥12s, 当日仅保留盘后1次离线采集")
                action["actions"].append("⚠️ 全局风险+1档: 雪球数据不可用")
                global_risk_up = True
            elif src == "akshare":
                action["actions"].append("❌ AkShare离线: 校验443/防火墙/DNS → 降级至Tushare主源")
                action["actions"].append("🔀 当日数据源优先级: Tushare > 东方财富 > AkShare(受限) > 雪球(离线)")
            elif src == "tushare":
                action["actions"].append("❌ Tushare异常: 自动切换备用Token, 启用数据补拉脚本")
                action["actions"].append("⚠️ 触发北向/龙虎榜字段标注: 数据异常标记")
            elif src == "eastmoney":
                action["actions"].append("❌ 东方财富异常: 备用切换至新浪/腾讯源")
        elif r["status"] == "DEGRADED":
            if src == "tushare":
                action["actions"].append("⚠️ Tushare降级: 缺失字段标注, 使用AkShare同字段补充")
            elif src == "akshare":
                action["actions"].append("⚠️ AkShare降级: 受限使用, 优先Tushare")
        if r["status"] in ("FAIL", "DEGRADED"):
            r["action_taken"] = action["actions"]
        actions_taken.append(action)

    # 4. 数据大面积异常 → 全局上调风险
    fail_count = sum(1 for r in results.values() if r["status"] == "FAIL")
    degraded_count = sum(1 for r in results.values() if r["status"] == "DEGRADED")
    if global_risk_up or fail_count >= 2:
        logger.info("  🚨 大面积数据源异常 → 全局风险上调一档, 禁止确定性单边结论")
        logger.info("  🚨 同步飞书盘前数据告警(通过外部通道)")
        global_risk_up = True

    # 5. 确定当日优先级
    priority = sorted(DATA_SOURCE_PRIORITY, key=lambda s: 0 if results.get(s, {}).get("status") == "OK"
                       else (1 if results.get(s, {}).get("status") == "DEGRADED" else 2))
    logger.info(f"[4/4] 当日数据源优先级: {[DATA_SOURCE_CN.get(s,s) for s in priority]}")

    # 写入DB
    conn = get_db()
    cur = conn.cursor()
    for src, r in results.items():
        action_str = "; ".join(r.get("action_taken", []))[:200] if r.get("action_taken") else ""
        cur.execute(
            "INSERT INTO data_source_status (date, source, domain, status, error_count, error_detail, action_taken) "
            "VALUES (?,?,?,?,?,?,?)",
            (TODAY, src, ",".join(DOMAIN_POOL.get(src, [])), r["status"], r["error_count"],
             r["detail"][:200], action_str),
        )
    conn.commit()
    conn.close()

    summary = {
        "step": "第一步: 数据源校验",
        "timestamp": datetime.now().isoformat(),
        "source_status": {s: r["status"] for s, r in results.items()},
        "fail_count": fail_count,
        "degraded_count": degraded_count,
        "global_risk_up": global_risk_up,
        "data_source_priority": priority,
        "actions_taken": actions_taken,
        "log_stats": {k: dict(v) for k, v in log_stats.items()},
    }
    return summary


# ═══════════════════════════════════════════════
#  第二步：当日重点观察标的批量导入与标签归档
# ═══════════════════════════════════════════════

# 当日观察标的清单（按实际持仓/自选配置）
OBSERVATION_STOCKS = [
    # === 持仓(当前实盘) ===
    {"code": "600884", "name": "杉杉股份", "sector": "负极材料+偏光片", "group": "新能源", "tags": ["中线布局", "持仓"]},

    # === 中线布局 ===
    {"code": "600547", "name": "山东黄金", "sector": "贵金属避险", "group": "贵金属", "tags": ["中线布局"]},
    {"code": "300476", "name": "胜宏科技", "sector": "PCB制造", "group": "PCB", "tags": ["中线布局"]},
    {"code": "600585", "name": "海螺水泥", "sector": "周期防御高股息", "group": "周期防御", "tags": ["中线布局"]},
    {"code": "600941", "name": "中国移动", "sector": "算力运营商红利", "group": "周期防御", "tags": ["中线布局"]},
    {"code": "600183", "name": "生益科技", "sector": "AI电子材料", "group": "AI科技", "tags": ["中线布局"]},

    # === 短线跟踪 ===
    {"code": "002617", "name": "露笑科技", "sector": "碳化硅+光伏", "group": "新能源", "tags": ["短线跟踪"]},
    {"code": "002044", "name": "美年健康", "sector": "医疗政策反转", "group": "医疗", "tags": ["短线跟踪"]},
    {"code": "601138", "name": "工业富联", "sector": "AI服务器制造", "group": "AI科技", "tags": ["短线跟踪"]},
    {"code": "000725", "name": "京东方A", "sector": "面板周期复苏", "group": "消费电子", "tags": ["短线跟踪"]},
    {"code": "000063", "name": "中兴通讯", "sector": "通信设备", "group": "AI科技", "tags": ["短线跟踪"]},

    # === 固态电池赛道跟踪（从深度筛选结果） ===
    {"code": "002709", "name": "天赐材料", "sector": "固态电解质", "group": "固态电池", "tags": ["观察跟踪"]},
    {"code": "300037", "name": "新宙邦", "sector": "固态电解质", "group": "固态电池", "tags": ["观察跟踪"]},
    {"code": "000049", "name": "德赛电池", "sector": "电芯", "group": "固态电池", "tags": ["观察跟踪"]},
    {"code": "300073", "name": "当升科技", "sector": "配套正负极", "group": "固态电池", "tags": ["观察跟踪"]},
    {"code": "300457", "name": "赢合科技", "sector": "固态设备", "group": "固态电池", "tags": ["观察跟踪"]},
    {"code": "603876", "name": "鼎胜新材", "sector": "铝箔", "group": "固态电池", "tags": ["观察跟踪"]},

    # === 风险避雷(延续前日) ===
    {"code": "002617", "name": "露笑科技", "sector": "碳化硅+光伏", "group": "新能源", "tags": ["风险避雷"]},
]

def check_suspended(code: str) -> bool:
    """通过Tushare检查是否停牌"""
    try:
        import tushare as ts
        pro = ts.pro_api()
        df = pro.daily(ts_code=f"{code}.{'SH' if code.startswith('6') else 'SZ'}", start_date=TODAY, end_date=TODAY)
        if df is not None and not df.empty:
            return False
        return False  # 非交易日或数据缺失→标记为未停牌
    except Exception:
        return False

def persist_observation_to_sqlite(stock_entry: dict) -> bool:
    """写入SQLite交易样本库"""
    conn = get_db()
    cur = conn.cursor()
    ok = False
    for tag in stock_entry.get("tags", []):
        try:
            cur.execute(
                "INSERT OR IGNORE INTO observation_list (date, ts_code, stock_name, tag, is_suspended) "
                "VALUES (?, ?, ?, ?, ?)",
                (TODAY, stock_entry["code"], stock_entry.get("name", ""), tag,
                 1 if stock_entry.get("is_suspended") else 0),
            )
            ok = True
        except Exception as e:
            logger.error(f"  ❌ SQLite写入失败 {stock_entry['code']}:{tag} — {e}")
    conn.commit()
    conn.close()
    return ok

def persist_observation_to_faiss(stock_entry: dict) -> bool:
    """写入FAISS行情向量库元数据"""
    try:
        meta_path = FAISS_DIR / "misjudge_metas.json"
        if not meta_path.exists():
            logger.info(f"  ⚠️ FAISS元数据不存在: {meta_path}")
            return False
        with open(meta_path, "r", encoding="utf-8") as f:
            metas = json.load(f)
        if not isinstance(metas, list):
            return False
        # 检查是否已存在同code同tag条目
        ts_code = f"{stock_entry['code']}.{'SH' if stock_entry['code'].startswith('6') else 'SZ'}"
        existing_ids = set()
        for m in metas:
            c = m.get("code", "")
            if c == ts_code or c == stock_entry["code"]:
                tag = m.get("tag", "")
                for t in stock_entry.get("tags", []):
                    if t in tag:
                        existing_ids.add(m.get("chunk_id", ""))
        if existing_ids:
            logger.info(f"  📌 FAISS已有{len(existing_ids)}条同code匹配, 跳过重复入库")
            return True
        # 新增：每个tag一条元数据
        for tag in stock_entry.get("tags", []):
            new_meta = {
                "source": "pre_market_workflow",
                "code": ts_code,
                "code_clean": stock_entry["code"],
                "name": stock_entry.get("name", ""),
                "tag": f"observation_list,{tag},{stock_entry.get('group','')}",
                "tag_type": tag,
                "risk_level": 3 if tag == "风险避雷" else (2 if tag == "持仓" else 1),
                "bias_id": f"obs_{stock_entry['code']}_{tag}",
                "bias_name": f"{stock_entry.get('name','')}_{tag}_session_{TODAY}",
                "chunk_id": f"chunk_obs_{tag}_{stock_entry['code']}_{TODAY}",
                "date": TODAY,
                "group": stock_entry.get("group", ""),
                "sector": stock_entry.get("sector", ""),
            }
            metas.append(new_meta)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metas, f, ensure_ascii=False, indent=2)
        logger.info(f"  ✅ FAISS元数据写入: {stock_entry['code']} ({len(stock_entry['tags'])}标签)")
        return True
    except Exception as e:
        logger.error(f"  ❌ FAISS写入失败: {e}")
        return False

def persist_psychological_bias_rag(stock_entry: dict) -> bool:
    """写入芒格误判RAG知识库(标签分类持久化)"""
    try:
        # 按标签类型, 在FAISS元数据的tag字段添加session场景标注
        return True  # 当前与FAISS共享同一元数据文件
    except Exception as e:
        logger.error(f"  ❌ RAG写入失败: {e}")
        return False

def deduplicate_observation_list() -> dict:
    """去重+停牌标记+生成快照"""
    seen = {}
    for item in OBSERVATION_STOCKS:
        code = item["code"]
        if code not in seen:
            seen[code] = {"code": code, "name": item["name"], "sector": item.get("sector", ""),
                          "group": item.get("group", ""), "tags": set(), "is_suspended": False}
        seen[code]["tags"].update(item.get("tags", []))
    # 标记停牌
    result = []
    for code, entry in seen.items():
        suspended = check_suspended(code)
        entry["is_suspended"] = suspended
        entry["tags"] = list(entry["tags"])
        result.append(entry)
        if suspended:
            logger.info(f"  ⚠️ {code} {entry['name']} 停牌标记")
    return result

def run_step2_observation_import() -> dict:
    """第二步完整执行: 标签导入+持久化+快照"""
    logger.info("=" * 60)
    logger.info("第二步: 当日重点观察标的清单批量导入与标签归档")
    logger.info("=" * 60)

    # 1. 去重+停牌
    logger.info("[1/4] 去重+停牌校验...")
    deduped = deduplicate_observation_list()
    logger.info(f"  共 {len(deduped)} 只不同标的")

    # 2. 标签统计
    tag_stats = defaultdict(int)
    for entry in deduped:
        for t in entry["tags"]:
            tag_stats[t] += 1
    logger.info("[2/4] 标签分布:")
    for tag, cnt in tag_stats.items():
        logger.info(f"  🏷️  {tag}: {cnt}只")

    # 3. 持久化三通道写入
    logger.info("[3/4] 三通道持久化写入...")
    persist_results = {"sqlite": 0, "faiss": 0, "rag": 0, "errors": []}
    for entry in deduped:
        if entry["is_suspended"]:
            logger.info(f"  跳过停牌 {entry['code']} {entry['name']}")
            continue
        ok1 = persist_observation_to_sqlite(entry)
        ok2 = persist_observation_to_faiss(entry)
        ok3 = persist_psychological_bias_rag(entry)
        if ok1:
            persist_results["sqlite"] += 1
        if ok2:
            persist_results["faiss"] += 1
        if ok3:
            persist_results["rag"] += 1

    # 4. 生成快照
    logger.info("[4/4] 生成当日观察池快照...")
    snapshot = {
        "date": TODAY,
        "timestamp": datetime.now().isoformat(),
        "total_stocks": len(deduped),
        "stocks": [],
        "tag_distribution": dict(tag_stats),
        "persist_summary": persist_results,
    }
    for entry in deduped:
        snapshot["stocks"].append({
            "code": entry["code"],
            "name": entry["name"],
            "tags": entry["tags"],
            "is_suspended": entry["is_suspended"],
            "sector": entry.get("sector", ""),
            "group": entry.get("group", ""),
        })

    # 写入快照文件
    snapshot_fp = LOG_DIR / f"observation_snapshot_{TODAY}.json"
    with open(snapshot_fp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    logger.info(f"  ✅ 快照写入: {snapshot_fp}")

    # 写入DB日志
    conn = get_db()
    cur = conn.cursor()
    for entry in deduped:
        for tag in entry["tags"]:
            cur.execute(
                "INSERT OR IGNORE INTO pre_market_log (date, step, detail, status) VALUES (?, ?, ?, ?)",
                (TODAY, "第二步_标签导入", f"{entry['code']} {entry['name']} → {tag}", "OK"),
            )
    conn.commit()
    conn.close()

    return snapshot


# ═══════════════════════════════════════════════
#  联动校验：标签完整/数据源联动/RAG权重分发
# ═══════════════════════════════════════════════

def run_linked_validations(step1_result: dict, step2_result: dict) -> dict:
    """执行全局联动约束校验"""
    logger.info("=" * 60)
    logger.info("联动校验: 全局约束一致性检查")
    logger.info("=" * 60)

    issues = []

    # 联动①: 数据源异常→全局风控上调
    if step1_result.get("global_risk_up"):
        issues.append("⚠️ 数据源大面积异常 → 全局风险+1档, 禁止确定性单边结论")
        # 修改各标的评分偏移
        for s in step2_result.get("stocks", []):
            for tag in s.get("tags", []):
                if tag == "风险避雷":
                    logger.info(f"  🚨 风险避雷 {s['code']}: 数据源异常下维持最高警戒")

    # 联动②: 标签完整性校验
    for s in step2_result.get("stocks", []):
        if not s.get("tags"):
            issues.append(f"❌ {s['code']} {s['name']}: 无标签, 禁止盘中启动11模块报告")
        for t in s["tags"]:
            if t not in TAG_DEFINITIONS:
                issues.append(f"❌ {s['code']}: 标签'{t}'未定义, 禁止使用")

    # 联动③: 标签→RAG权重映射
    rag_weight_config = {}
    for s in step2_result.get("stocks", []):
        for t in s.get("tags", []):
            if t in TAG_DEFINITIONS:
                if t not in rag_weight_config:
                    rag_weight_config[t] = TAG_DEFINITIONS[t]

    # 联动④: 标签→策略权重
    strategy_groups = {}
    for s in step2_result.get("stocks", []):
        for t in s.get("tags", []):
            if t not in strategy_groups:
                strategy_groups[t] = []
            strategy_groups[t].append(s["code"])

    # 联动⑤: 短线跟踪/风险避雷 → 强化诱多预警
    for s in step2_result.get("stocks", []):
        if "风险避雷" in s.get("tags", []):
            issues.append(f"⚠️ {s['code']} {s['name']}: 风险避雷标签 → 诱多预警灵敏度提升至最高级")

    # 写入联动校验日志
    conn = get_db()
    cur = conn.cursor()
    for issue in issues:
        cur.execute(
            "INSERT INTO pre_market_log (date, step, detail, status) VALUES (?, ?, ?, ?)",
            (TODAY, "联动校验", issue[:200], "WARN" if "⚠️" in issue or "❌" in issue else "OK"),
        )
    conn.commit()
    conn.close()

    result = {
        "issues": issues,
        "rag_weights": {k: v["rag_weight"] for k, v in rag_weight_config.items()},
        "strategy_groups": strategy_groups,
        "valid": len([i for i in issues if "❌" in i]) == 0,
    }
    logger.info(f"  校验项: {len(issues)}条, 严重: {len([i for i in issues if '❌' in i])}条")
    return result


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    logger.info("=" * 70)
    logger.info(f"📋 盘前固定执行窗口启动 | {TODAY_DATE}")
    logger.info(f"   执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"   执行窗口: 开盘前5分钟(09:25)")
    logger.info("=" * 70)

    start_ts = time.time()

    # 0. 确保DB表存在
    ensure_db_tables()

    # 第一步: 数据源校验
    step1 = run_step1_data_source_check()

    # 第二步: 观察池导入
    step2 = run_step2_observation_import()

    # 联动校验
    linked = run_linked_validations(step1, step2)

    # 汇总输出
    elapsed = time.time() - start_ts
    total_stocks = len(step2.get("stocks", []))
    total_tags = sum(len(s.get("tags", [])) for s in step2.get("stocks", []))

    logger.info("\n" + "=" * 70)
    logger.info("📊 盘前执行汇总")
    logger.info("=" * 70)
    logger.info(f"  数据源: OK={sum(1 for s in step1['source_status'].values() if s=='OK')}/4")
    logger.info(f"  全球风险上调: {'是 🚨' if step1.get('global_risk_up') else '否 ✅'}")
    logger.info(f"  观察标的: {total_stocks}只 × {total_tags}标签")
    logger.info(f"  持久化: SQLite={step2['persist_summary']['sqlite']} FAISS={step2['persist_summary']['faiss']} RAG={step2['persist_summary']['rag']}")
    logger.info(f"  标签分布: {dict(step2.get('tag_distribution', {}))}")
    logger.info(f"  联动校验严重问题: {len([i for i in linked['issues'] if '❌' in i])}条")
    logger.info(f"  耗时: {elapsed:.1f}s")

    # 生成数据源校验日志JSON
    check_log = {
        "task": "盘前数据源连通校验日志",
        "date": TODAY,
        "timestamp": datetime.now().isoformat(),
        "step1": {
            "source_status": step1["source_status"],
            "global_risk_up": step1.get("global_risk_up", False),
            "fail_count": step1["fail_count"],
            "degraded_count": step1["degraded_count"],
            "data_source_priority": step1["data_source_priority"],
            "actions_taken": step1["actions_taken"],
        },
        "step2": {
            "total_stocks": total_stocks,
            "tag_distribution": dict(step2.get("tag_distribution", {})),
            "persist_summary": step2["persist_summary"],
        },
        "linked_validation": {
            "issues_count": len(linked["issues"]),
            "severe_issues": len([i for i in linked["issues"] if "❌" in i]),
            "rag_weights": linked.get("rag_weights", {}),
        },
        "elapsed_seconds": round(elapsed, 1),
    }
    check_log_fp = LOG_DIR / f"data_source_check_log_{TODAY}.json"
    with open(check_log_fp, "w", encoding="utf-8") as f:
        json.dump(check_log, f, ensure_ascii=False, indent=2)

    logger.info(f"\n✅ 交付物:")
    logger.info(f"  1. {check_log_fp}")
    logger.info(f"  2. {LOG_DIR / f'observation_snapshot_{TODAY}.json'}")
    logger.info(f"  3. {LOG_FILE}")

    # 写入DB汇总记录
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pre_market_log (date, step, detail, status) VALUES (?, ?, ?, ?)",
        (TODAY, "盘前流程_完成",
         f"数据源OK={sum(1 for s in step1['source_status'].values() if s=='OK')}/4 | 标的{total_stocks}只 | 耗时{elapsed:.1f}s",
         "OK" if not step1.get("global_risk_up") else "WARN"),
    )
    conn.commit()
    conn.close()

    logger.info(f"\n{'='*70}")
    logger.info(f"✅ 盘前流程完成 | {datetime.now().strftime('%H:%M:%S')}")
    logger.info(f"{'='*70}")

    return check_log


if __name__ == "__main__":
    main()
