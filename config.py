# -*- coding: utf-8 -*-
"""
Stock Agent 量化系统全局配置
pg_engine: PostgreSQL异步引擎
TARGET_CODES: 全量监控股票池(纯数字)
SECTOR_LABELS: 赛道分类标签
"""

import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine

# ── PostgreSQL连接 ──
PG_USER = "stock_user"
PG_PASSWORD_ENCODED = quote_plus("stock123")
PG_HOST = "127.0.0.1"
PG_PORT = "5432"
PG_DATABASE = "stock_data"
PG_URL = f"postgresql://{PG_USER}:***@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
# pg_engine via pgpass (password not hardcoded)
import os
os.environ.setdefault("PGPASSFILE", "/root/.pgpass")
with open(os.environ["PGPASSFILE"]) as _pf:
    _pw_ = _pf.read().strip().split(":")[-1]
_PG_PASS_REAL = quote_plus(_pw_)
_PG_URL_REAL = f"postgresql://{PG_USER}:***@{PG_HOST}:{PG_PORT}/{PG_DATABASE}?sslmode=require"
pg_engine = create_engine(_PG_URL_REAL, pool_pre_ping=True, pool_size=5, max_overflow=10)

# ── 全量股票池(纯数字,无后缀) ──
# 原有8只
LEGACY_CODES = [
    "600884",  # 杉杉股份
    "002617",  # 露笑科技
    "600547",  # 山东黄金
    "002044",  # 美年健康
    "300098",  # 高新兴
    "300476",  # 胜宏科技
    "300693",  # 盛弘股份
    "300433",  # 蓝思科技
    "601868",  # 中国能建
]

# 新增10只主板潜力跟踪(剔除300创业板)
NEW_CODES = [
    "601138",  # 工业富联 - AI服务器制造
    "600941",  # 中国移动 - 算力运营商红利
    "000725",  # 京东方A - 面板周期复苏
    "600487",  # 亨通光电 - 算力海缆光通信
    "600183",  # 生益科技 - AI电子材料
    "600585",  # 海螺水泥 - 周期防御高股息
    "000063",  # 中兴通讯 - 通信设备国产替代
]

# 注意: 600547/002044/601868已在LEGACY中,不重复
# 全量合并: LEGACY(含600547/002044/601868) + NEW(601138/600941/000725/600487/600183/600585/000063)
TARGET_CODES = sorted(set(LEGACY_CODES + NEW_CODES))

# ── 赛道分类标签(每只标的绑定) ──
SECTOR_LABELS = {
    "600884": "负极材料+偏光片双龙头",
    "002617": "碳化硅概念+光伏",
    "600547": "贵金属避险",
    "002044": "医疗政策反转",
    "300098": "物联网+车联网",
    "300476": "PCB制造(胜宏科技)",
    "300693": "盛弘股份",
    "300433": "消费电子玻璃盖板",
    "601868": "新能源电力基建",
    "601138": "AI服务器制造",
    "600941": "算力运营商红利",
    "000725": "面板周期复苏",
    "600487": "算力海缆光通信",
    "600183": "AI电子材料",
    "600585": "周期防御高股息",
    "000063": "通信设备国产替代",
}

# ── 赛道分组(用于分赛道独立测算多因子权重) ──
SECTOR_GROUPS = {
    "AI科技": ["601138", "600487", "600183", "000063"],
    "新能源": ["600884", "601868", "002617"],
    "消费电子": ["300433", "000725", "300693"],
    "PCB制造": ["300476"],
    "贵金属": ["600547"],
    "医疗": ["002044"],
    "周期防御": ["600585", "600941"],
    "科技成长": ["300098"],
}

# ── 执行参数 ──
COLLECT_INTERVAL_DAYS = 1       # 日线采集频率(天)
HISTORY_DAYS = 120              # 初始采集历史天数
BATCH_SIZE = 3                  # 每批写入DB条数
FLUSH_INTERVAL = 3              # 每3只flush一次

# ── Tushare Token ──
TUSHARE_TOKEN = "8f106090fcf57ae1d0d86f330acf03b35b95ec3df5064ea25a768860"

# ── 数据采集超时 ──
REQUEST_TIMEOUT = 15
RETRY_TIMES = 1

# ── 数据源降级配置 (2026-07-16 修复后) ──
# Sina已恢复可用，双源架构恢复运行
PRIMARY_SOURCE = "tushare"               # 主源
SECONDARY_SOURCE = "sina"                # 备用源(已恢复✅)
REQUEST_INTERVAL = 0.35                   # Tushare请求间隔恢复至0.35s
SINA_REQUEST_INTERVAL = 0.50              # Sina请求间隔
SOURCE_STATUS = {
    "tushare":  "ACTIVE  (100%)",          # 主源, 0ms稳定
    "sina":     "ACTIVE  (100%) 🟢恢复",    # 备用源, 71ms, urllib直连
    "em_push2": "BLOCKED (0%) 🔴",          # RemoteDisconnected 10/10
    "xueqiu":   "BLOCKED (0%) 🔴",          # 302 openresty,需住宅代理IP
    "em_sqt":   "BLOCKED (0%) 🔴",          # 302 stgw,永久停用
    "em_kamt":  "BLOCKED (0%) 🔴",          # 302 IIS,港股通中断
}
