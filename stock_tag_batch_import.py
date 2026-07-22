# -*- coding: utf-8 -*-
"""
stock_tag_batch_import.py — 标的批量导入+标签绑定+分组策略配置
==================================================================
功能: 按持仓/自选清单导入标的 → 绑定分类标签 → 持久化标签索引
     → 生成分组复盘隔离策略配置

执行时机: 人工启动, 接收标的列表JSON文件或手动输入

标签体系:
  持仓     — 当前实盘持仓 (position)
  短线跟踪 — 短线交易标的 (short_term)
  中线布局 — 中期持有标的 (mid_term)
  风险避雷 — 高风险规避 (risk_avoid)

输出:
  - 标的导入明细报表 (import_manifest_YYYYMMDD.json)
  - 样本库标签更新日志 (sample_tag_update.log)
  - 分组复盘策略配置 (strategy_group_config_YYYYMMDD.json)
"""

import sys
import json
import time
import logging
from datetime import datetime, date
from pathlib import Path

import psycopg2
import pandas as pd

# ── 配置 ──
DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_DIR / "sample_tag_update.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("TagImport")

# 标签ID映射 (dim_concept)
TAG_IDS = {
    "持仓": 4,
    "短线跟踪": 2,
    "中线布局": 1,
    "风险避雷": 3,
}

# 分组复盘隔离 → 因子权重配置
DEFAULT_GROUP_CONFIG = {
    "持仓": {
        "weights": {"valuation": 0.20, "momentum": 0.20, "flow": 0.20, "fundamental": 0.25, "sentiment": 0.15},
        "entry_conditions": {"consecutive_flow_days": 3, "volume_ratio": 1.0},
        "risk_params": {"stop_loss": 0.08, "take_profit": 0.15},
    },
    "中线布局": {
        "weights": {"valuation": 0.30, "momentum": 0.15, "flow": 0.15, "fundamental": 0.30, "sentiment": 0.10},
        "entry_conditions": {"consecutive_flow_days": 3, "volume_ratio": 1.0},
        "risk_params": {"stop_loss": 0.10, "take_profit": 0.20},
    },
    "短线跟踪": {
        "weights": {"valuation": 0.10, "momentum": 0.30, "flow": 0.30, "fundamental": 0.15, "sentiment": 0.15},
        "entry_conditions": {"consecutive_flow_days": 2, "volume_ratio": 1.2},
        "risk_params": {"stop_loss": 0.05, "take_profit": 0.10},
    },
    "风险避雷": {
        "weights": {"valuation": 0.35, "momentum": 0.10, "flow": 0.10, "fundamental": 0.35, "sentiment": 0.10},
        "entry_conditions": {"consecutive_flow_days": 5, "volume_ratio": 0.8},
        "risk_params": {"stop_loss": 0.03, "take_profit": 0.05},
    },
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def get_stock_name_from_api(code):
    """通过Tushare获取股票名称"""
    try:
        import tushare as ts
        pro = ts.pro_api("8f106090fcf57ae1d0d86f330acf03b35b95ec3df5064ea25a768860")
        df = pro.stock_basic(ts_code=f"{code}.SH")
        if df.empty:
            df = pro.stock_basic(ts_code=f"{code}.SZ")
        if not df.empty:
            return df.iloc[0]["name"]
    except:
        pass
    return code


def batch_import_stocks(stock_list):
    """
    批量导入标的至 dim_stock

    stock_list: [{"code":"600884","name":"杉杉股份","sector":"负极材料+偏光片","group":"新能源"}, ...]
    """
    conn = get_conn()
    cur = conn.cursor()
    results = {"imported": [], "skipped": [], "failed": []}

    for item in stock_list:
        code = item["code"]
        name = item.get("name", get_stock_name_from_api(code))
        sector = item.get("sector", "")
        group = item.get("group", "")
        exchange = "SH" if code.startswith("6") or code.startswith("9") else "SZ"

        try:
            cur.execute("""
                INSERT INTO dim_stock (stock_code, stock_name, sector, sector_group, exchange, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (stock_code) DO UPDATE SET
                    stock_name = %s, sector = %s, sector_group = %s, exchange = %s,
                    updated_at = now()
            """, (code, name, sector, group, exchange, name, sector, group, exchange))
            results["imported"].append(code)
            logger.info(f"  ✅ {code} {name}")
        except Exception as e:
            results["failed"].append({"code": code, "error": str(e)})
            logger.error(f"  ❌ {code} 导入失败: {e}")

        time.sleep(0.1)

    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"导入: {len(results['imported'])} | 跳过: {len(results['skipped'])} | 失败: {len(results['failed'])}")
    return results


def bind_tags(stock_tags):
    """
    批量绑定标签

    stock_tags: [{"code":"600884","tags":["中线布局","持仓"]}, ...]
    """
    conn = get_conn()
    cur = conn.cursor()
    tag_records = []

    for item in stock_tags:
        code = item["code"]
        tags = item.get("tags", [])
        for tag_name in tags:
            tag_id = TAG_IDS.get(tag_name)
            if not tag_id:
                logger.warning(f"  ⚠️ {code} 未知标签: {tag_name}")
                continue
            try:
                cur.execute("""
                    INSERT INTO dim_stock_concept (stock_code, concept_id, is_primary)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (stock_code, concept_id) DO NOTHING
                """, (code, tag_id, True))
                tag_records.append({"code": code, "tag": tag_name})
                logger.info(f"  🏷️ {code} ← {tag_name}")
            except Exception as e:
                logger.error(f"  ❌ {code} 标签绑定失败: {e}")

    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"标签绑定: {len(tag_records)}条")
    return tag_records


def clear_tags(code_list=None):
    """清空指定标的全部标签(用于重新绑定)"""
    conn = get_conn()
    cur = conn.cursor()
    if code_list:
        cur.execute("DELETE FROM dim_stock_concept WHERE stock_code IN %s", (tuple(code_list),))
    else:
        cur.execute("DELETE FROM dim_stock_concept")
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"清空标签: {deleted}条")
    return deleted


def generate_strategy_config(stock_tags):
    """生成分组复盘隔离策略配置"""
    group_map = {}
    for item in stock_tags:
        code = item["code"]
        for tag in item.get("tags", []):
            if tag not in group_map:
                group_map[tag] = []
            group_map[tag].append(code)

    config = {
        "generated_at": datetime.now().isoformat(),
        "tagged_stocks": len(stock_tags),
        "groups": {},
        "group_weights": {},
    }

    for tag, codes in group_map.items():
        config["groups"][tag] = sorted(set(codes))
        if tag in DEFAULT_GROUP_CONFIG:
            config["group_weights"][tag] = DEFAULT_GROUP_CONFIG[tag]

    # 写入JSON
    fp = LOG_DIR / f"strategy_group_config_{datetime.now().strftime('%Y%m%d')}.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    logger.info(f"策略配置已写入: {fp}")
    return config


if __name__ == "__main__":
    today = date.today().strftime("%Y%m%d")
    logger.info("=" * 60)
    logger.info(f"📋 标的批量导入+标签绑定 | {today}")
    logger.info("=" * 60)

    # ====== 标的清单 + 标签配置 ======
    # 按实际持仓/自选填写
    stock_tag_list = [
        # === 中线布局(中期持有, 基本面优先) ===
        {"code": "600884", "name": "杉杉股份", "sector": "负极材料+偏光片双龙头", "group": "新能源", "tags": ["中线布局"]},
        {"code": "600547", "name": "山东黄金", "sector": "贵金属避险", "group": "贵金属", "tags": ["中线布局", "持仓"]},
        {"code": "300476", "name": "胜宏科技", "sector": "PCB制造", "group": "PCB制造", "tags": ["中线布局"]},
        {"code": "300693", "name": "盛弘股份", "sector": "消费电子", "group": "消费电子", "tags": ["中线布局"]},
        {"code": "601868", "name": "中国能建", "sector": "新能源电力基建", "group": "新能源", "tags": ["中线布局"]},
        {"code": "600941", "name": "中国移动", "sector": "算力运营商红利", "group": "周期防御", "tags": ["中线布局"]},
        {"code": "600183", "name": "生益科技", "sector": "AI电子材料", "group": "AI科技", "tags": ["中线布局"]},
        {"code": "600585", "name": "海螺水泥", "sector": "周期防御高股息", "group": "周期防御", "tags": ["中线布局"]},

        # === 短线跟踪(技术面+资金流优先) ===
        {"code": "002617", "name": "露笑科技", "sector": "碳化硅概念+光伏", "group": "新能源", "tags": ["短线跟踪", "风险避雷"]},
        {"code": "002044", "name": "美年健康", "sector": "医疗政策反转", "group": "医疗", "tags": ["短线跟踪"]},
        {"code": "300098", "name": "高新兴", "sector": "物联网+车联网", "group": "科技成长", "tags": ["短线跟踪"]},
        {"code": "300433", "name": "蓝思科技", "sector": "消费电子玻璃盖板", "group": "消费电子", "tags": ["短线跟踪"]},
        {"code": "601138", "name": "工业富联", "sector": "AI服务器制造", "group": "AI科技", "tags": ["短线跟踪"]},
        {"code": "000725", "name": "京东方A", "sector": "面板周期复苏", "group": "消费电子", "tags": ["短线跟踪"]},
        {"code": "600487", "name": "亨通光电", "sector": "算力海缆光通信", "group": "AI科技", "tags": ["短线跟踪"]},
        {"code": "000063", "name": "中兴通讯", "sector": "通信设备国产替代", "group": "AI科技", "tags": ["短线跟踪"]},

        # === 新增补充标的 ===
        {"code": "002169", "name": "智光电气", "sector": "新型电网+粤芯半导体", "group": "AI科技", "tags": ["短线跟踪"]},
    ]

    # ====== 执行 ======
    # Step 1: 批量入库(去重)
    logger.info("\n--- Step 1: 批量标的入库 ---")
    stock_list = [{"code": s["code"], "name": s["name"], "sector": s["sector"], "group": s["group"]} for s in stock_tag_list]
    import_result = batch_import_stocks(stock_list)

    # Step 2: 清空旧标签 → 重新绑定
    logger.info("\n--- Step 2: 清空旧标签,重新绑定 ---")
    all_codes = [s["code"] for s in stock_tag_list]
    clear_tags(all_codes)
    tag_result = bind_tags(stock_tag_list)

    # Step 3: 生成分组策略配置
    logger.info("\n--- Step 3: 分组策略配置 ---")
    strategy_config = generate_strategy_config(stock_tag_list)

    # ====== 输出 ======
    manifest = {
        "task": "标的批量导入+标签绑定",
        "timestamp": datetime.now().isoformat(),
        "import_summary": import_result,
        "tag_summary": {"total": len(tag_result), "records": tag_result},
        "group_config": strategy_config["groups"],
        "weight_config": strategy_config["group_weights"],
    }

    manifest_fp = LOG_DIR / f"import_manifest_{today}.json"
    with open(manifest_fp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    logger.info(f"\n{'='*60}")
    logger.info(f"导入明细: {import_result['imported']}")
    logger.info(f"标签更新: {len(tag_result)}条")
    logger.info(f"分组配置: {list(strategy_config['groups'].keys())}")
    logger.info(f"报表已写入: {manifest_fp}")
    logger.info(f"{'='*60}\n")
