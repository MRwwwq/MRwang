# -*- coding: utf-8 -*-
"""
config.example.py — 项目配置模板

使用方式:
    cp config.example.py config.py
    然后编辑 config.py 填入真实凭据

⚠️ config.py 已在 .gitignore 中，不会提交到 GitHub
"""

from urllib.parse import quote_plus
from sqlalchemy import create_engine

# ── Tushare Pro Token (https://tushare.pro 注册获取) ──
# 替换为你的真实Token，不要带引号外的空格
TUSHARE_TOKEN = "your_tushare_token_here"

# ── PostgreSQL 连接 ──
PG_USER = "stock_user"
PG_PASSWORD = "your_password_here"
PG_PASSWORD_ENCODED = quote_plus(PG_PASSWORD)
PG_HOST = "127.0.0.1"
PG_PORT = "5432"
PG_DATABASE = "stock_data"

# 自动构建连接URL (密码会被编码)
_PG_URL = f"postgresql://{PG_USER}:{PG_PASSWORD_ENCODED}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
pg_engine = create_engine(_PG_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)

# ── 全量监控股票池 (纯数字, 无后缀) ──
TARGET_CODES = sorted([
    # 原有8只
    "600884", "002617", "600547", "002044",
    "300098", "300476", "300693", "300433", "601868",
    # 新增7只
    "601138", "600941", "000725", "600487", "600183", "600585", "000063",
])

# ── 赛道分类标签 ──
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

# ── 赛道分组 ──
SECTOR_GROUPS = {
    "AI科技":   ["601138", "600487", "600183", "000063"],
    "新能源":   ["600884", "601868", "002617"],
    "消费电子": ["300433", "000725", "300693"],
    "PCB制造":  ["300476"],
    "贵金属":   ["600547"],
    "医疗":     ["002044"],
    "周期防御": ["600585", "600941"],
    "科技成长": ["300098"],
}

# ── 数据采集参数 ──
PRIMARY_SOURCE = "tushare"          # 主数据源
SECONDARY_SOURCE = "sina"           # 备用数据源
REQUEST_TIMEOUT = 15                # 请求超时(秒)
RETRY_TIMES = 1                     # 失败重试次数
REQUEST_INTERVAL = 0.35             # Tushare请求间隔
SINA_REQUEST_INTERVAL = 0.50        # Sina请求间隔
COLLECT_INTERVAL_DAYS = 1           # 采集频率
HISTORY_DAYS = 120                  # 初始历史天数
