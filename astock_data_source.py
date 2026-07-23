#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
astock_data_source.py — AStock Data Toolkit 集成适配器
=====================================================
功能: 将 astock-data-toolkit 本地 Parquet 数据湖接入 QCLAW 数据平面

架构定位:
          ┌─────────────────┐
          │  astock-data-   │
          │  toolkit         │  ← crontab 每周自动更新
          │  (本地Parquet)   │
          └──────┬──────────┘
                 │ pandas.Series
          ┌──────▼──────────┐
          │ astock_data_    │
          │ source.py        │  ← 本适配器: 统一get_*()接口
          │ (缓存层+降级)    │
          └──────┬──────────┘
                 │ dict (QCLAW标准字段)
          ┌──────▼──────────┐
          │ module00_data   │
          │ / SIGNAL_EXTRACT│
          └─────────────────┘

数据降级链:
  Astock本地Parquet → Tushare Pro → AkShare → 腾讯行情 → 降级返回
  (astock最快: 本地读盘2ms, Tushare: 300ms+网络)

使用:
  from astock_data_source import AstockSource
  src = AstockSource()
  ohlcv = src.get_ohlcv("600547")        # 最新240条日线
  val   = src.get_valuation("600547")    # PE/PB/市值
  fin   = src.get_financial("600547")    # 季度财务
  all   = src.get_full("600547")         # 合并全部 → QCLAW dict
"""

import os
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger("AstockSource")

# ═══════════════════════════════════════════════
#  路径配置
# ═══════════════════════════════════════════════

ASTOCK_HOME = os.environ.get(
    "ASTOCK_HOME", ""
)
if not ASTOCK_HOME:
    # 优先检测 D盘挂载点 /opt/qclaw, 其次原路径
    for candidate in [
        "/opt/qclaw/astock-data-toolkit/astock_data",
        "/opt/astock-data-toolkit/astock_data",
    ]:
        p = Path(candidate)
        if p.parent.exists():
            ASTOCK_HOME = candidate
            break
    else:
        ASTOCK_HOME = "/opt/qclaw/astock-data-toolkit/astock_data"  # 默认指向D盘

ASTOCK_REPO = Path(ASTOCK_HOME).parent  # repo根目录

DATA_DIR = Path(ASTOCK_HOME)
DATA_DIR.mkdir(parents=True, exist_ok=True)

PARQUET_FILES = {
    "daily_ohlcv": DATA_DIR / "daily_ohlcv.parquet",
    "valuation":   DATA_DIR / "valuation_daily.parquet",
    "financial":   DATA_DIR / "financial_quarterly.parquet",
    "stock_list":  DATA_DIR / "stock_list.parquet",
    "dividend":    DATA_DIR / "dividend_history.parquet",
    "index":       DATA_DIR / "index_daily.parquet",
}

# ═══════════════════════════════════════════════
#  数据加载 (延迟加载 + LRU缓存)
# ═══════════════════════════════════════════════

_cache = {}  # {table_name: pd.DataFrame}


def _load_parquet(table_key: str) -> Optional[pd.DataFrame]:
    """按需加载 Parquet 并缓存。"""
    if table_key in _cache:
        return _cache[table_key]

    path = PARQUET_FILES.get(table_key)
    if not path or not path.exists():
        logger.warning(f"Parquet 不存在: {path} (请运行 astock-data-toolkit 首轮下载)")
        return None

    try:
        df = pd.read_parquet(path)
        logger.info(f"  加载 {table_key}: {len(df)} 行, {list(df.columns[:8])}...")
        _cache[table_key] = df
        return df
    except Exception as e:
        logger.error(f"  加载 {table_key} 失败: {e}")
        return None


def clear_cache():
    """清空缓存 (每日流水线开始前调用)。"""
    _cache.clear()
    logger.info("  数据缓存已清空")


def get_data_health() -> dict:
    """检查各表数据健康度 (用于service_pre_market.py)。"""
    return {
        name: {
            "exists": path.exists(),
            "size_mb": round(path.stat().st_size / 1e6, 2) if path.exists() else 0,
        }
        for name, path in PARQUET_FILES.items()
    }


# ═══════════════════════════════════════════════
#  标准化代码转换
# ═══════════════════════════════════════════════

def _normalize_code(code: str) -> str:
    """统一为6位纯数字。"""
    return code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "").strip()


def _add_suffix(code: str) -> str:
    """纯数字→带交易所后缀 (astock内部格式)。"""
    c = _normalize_code(code)
    if c.startswith("6") or c.startswith("9"):
        return c + ".SH"
    else:
        return c + ".SZ"


# ═══════════════════════════════════════════════
#  数据查询接口
# ═══════════════════════════════════════════════

def get_ohlcv(code: str, days: int = 240) -> Optional[pd.DataFrame]:
    """
    获取个股日线 OHLCV (前复权)，兼容 M00 模块。

    Args:
        code: 6位股票代码 (600547 或 600547.SH)
        days: 返回最近N个交易日

    Returns:
        DataFrame 含 close/volume/high/low/open/amount 列,
        按日期倒序(最新行在前), M00可直接传入
    """
    df = _load_parquet("daily_ohlcv")
    if df is None:
        return None

    code_fmt = _add_suffix(code)
    sub = df[df["code"] == code_fmt].copy()
    if sub.empty:
        # 试纯数字
        sub = df[df["code"] == _normalize_code(code)].copy()
    if sub.empty:
        return None

    sub = sub.sort_values("date", ascending=False).head(days)
    # 确保列名与 QCLAW M00 兼容
    rename = {}
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c not in sub.columns:
            rename[c] = c  # 用原名
    sub = sub.reset_index(drop=True)
    return sub


def get_valuation(code: str) -> Optional[dict]:
    """
    获取最新估值数据。

    Returns:
        {pe_ttm, pb, ps_ttm, pcf_ttm, total_mv, total_share}
    """
    df = _load_parquet("valuation")
    if df is None:
        return None

    code_fmt = _add_suffix(code)
    sub = df[df["code"] == code_fmt]
    if sub.empty:
        sub = df[df["code"] == _normalize_code(code)]
    if sub.empty:
        return None

    sub = sub.sort_values("date", ascending=False)
    latest = sub.iloc[0]
    return {
        "pe_ttm": float(latest.get("pe_ttm", 0)) if pd.notna(latest.get("pe_ttm", np.nan)) else None,
        "pb": float(latest.get("pb", 0)) if pd.notna(latest.get("pb", np.nan)) else None,
        "ps_ttm": float(latest.get("ps_ttm", 0)) if pd.notna(latest.get("ps_ttm", np.nan)) else None,
        "pcf_ttm": float(latest.get("pcf_ttm", 0)) if pd.notna(latest.get("pcf_ttm", np.nan)) else None,
        "total_mv": float(latest.get("total_mv", 0)) if pd.notna(latest.get("total_mv", np.nan)) else None,
        "total_share": float(latest.get("total_share", 0)) if pd.notna(latest.get("total_share", np.nan)) else None,
        "date": str(latest.get("date", "")),
    }


def get_financial(code: str) -> Optional[dict]:
    """
    获取最新财务数据 (季度)。

    Returns:
        {roe, revenue, net_profit, revenue_growth, profit_growth, ...}
    """
    df = _load_parquet("financial")
    if df is None:
        return None

    code_fmt = _add_suffix(code)
    sub = df[df["code"] == code_fmt]
    if sub.empty:
        sub = df[df["code"] == _normalize_code(code)]
    if sub.empty:
        return None

    sub = sub.sort_values("date", ascending=False)
    latest = sub.iloc[0]
    # 尝试找同比字段 (列名可能不同)
    revenue_growth = None
    profit_growth = None
    for col in latest.index:
        if "revenue" in col.lower() and "yoy" in col.lower():
            revenue_growth = float(latest[col]) if pd.notna(latest[col]) else None
        if "profit" in col.lower() and "yoy" in col.lower():
            profit_growth = float(latest[col]) if pd.notna(latest[col]) else None

    return {
        "roe": float(latest.get("roe", 0)) if pd.notna(latest.get("roe", np.nan)) else None,
        "revenue": float(latest.get("revenue", 0)) if pd.notna(latest.get("revenue", np.nan)) else None,
        "net_profit": float(latest.get("net_profit", 0)) if pd.notna(latest.get("net_profit", np.nan)) else None,
        "revenue_growth": revenue_growth,
        "profit_growth": profit_growth,
        "date": str(latest.get("date", "")),
    }


def get_stock_name(code: str) -> Optional[str]:
    """获取股票名称。"""
    df = _load_parquet("stock_list")
    if df is None:
        return None
    code_fmt = _add_suffix(code)
    sub = df[df["code"] == code_fmt]
    if sub.empty:
        sub = df[df["code"] == _normalize_code(code)]
    if sub.empty:
        return None
    return str(sub.iloc[0].get("name", ""))


def get_index_daily() -> Optional[pd.DataFrame]:
    """获取沪深300日线 (用于宏观信号)。"""
    df = _load_parquet("index")
    if df is None:
        return None
    return df.sort_values("date", ascending=False)


# ═══════════════════════════════════════════════
#  全量聚合接口: 一次调用获取 M00 + 基本面
# ═══════════════════════════════════════════════

def get_full_stock_data(code: str) -> Optional[Dict[str, Any]]:
    """
    聚合 OHLCV + 估值 + 财务 → QCLAW 标准 dict。

    返回格式 (直接供 service_signal_extract.run_signal_extract 使用):
    {
        "name": str,
        "close": float, "volume": float,
        "ma5": float, "ma20": float, "ma20_slope": float,
        "gold_cross": bool, "dead_cross": bool, "above_ma20": bool,
        "vol_ratio": float,
        "pe_ttm": float, "pb": float, "roe": float,
        "profit_growth": float, "revenue_growth": float,
        ...
    }
    """
    # 1. 获取OHLCV并计算M00特征
    ohlcv = get_ohlcv(code, days=240)
    if ohlcv is None or ohlcv.empty:
        logger.warning(f"[{code}] OHLCV 数据不可用")
        return None

    # 调用 M00 计算
    from module00_data import compute_520_features, get_latest_features

    # 确保有 close 列
    if "close" not in ohlcv.columns or ohlcv["close"].isna().all():
        logger.warning(f"[{code}] OHLCV close 为空")
        return None

    # 按日期正序 (M00需要)
    df_asc = ohlcv.sort_values("date").reset_index(drop=True)
    features = get_latest_features(df_asc)

    # 2. 估值
    val = get_valuation(code) or {}

    # 3. 财务
    fin = get_financial(code) or {}

    # 4. 名称
    name = get_stock_name(code) or ""

    # 5. 合并
    result = {
        "name": name,
        "code": _normalize_code(code),
        **features,
        **val,
        **fin,
        # 标记数据源
        "_data_source": "astock_parquet",
        "_timestamp": datetime.now().isoformat(),
    }
    # 去重key (估值覆盖M00同名冲突)
    result.pop("date", None)

    logger.info(f"[{code}] 全量数据聚合完成: {len(features)}特征 + {len(val)}估值 + {len(fin)}财务")
    return result


def get_full_batch(codes: list) -> Dict[str, Optional[Dict]]:
    """
    批量获取多个标的 (并行读盘, 非并行网络调用)。

    Args:
        codes: ["600547", "300476", "002617"]

    Returns:
        {code: dict_or_None}
    """
    clear_cache()  # 批量时确保新鲜
    result = {}
    for code in codes:
        try:
            result[code] = get_full_stock_data(code)
        except Exception as e:
            logger.error(f"[{code}] 批量采集失败: {e}")
            result[code] = None
    return result


# ═══════════════════════════════════════════════
#  自测
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print("=" * 60)
    print("  AStock Data Source 集成适配器 自测")
    print("=" * 60)

    # 检查数据完整性
    health = get_data_health()
    print("\n数据文件健康度:")
    for name, info in health.items():
        status = "✅" if info["exists"] else "❌"
        size = f"{info['size_mb']}MB" if info["exists"] else "不存在"
        print(f"  {status} {name}: {size}")

    # 测试个股查询
    for code in ["600547", "300476", "002617", "600884"]:
        print(f"\n--- {code} ---")
        full = get_full_stock_data(code)
        if full:
            print(f"  名称: {full.get('name', '?')}")
            print(f"  数据源: {full.get('_data_source', '?')}")
            print(f"  收盘: {full.get('close', '?')}")
            print(f"  PE: {full.get('pe_ttm', '?')}  PB: {full.get('pb', '?')}")
            print(f"  MA5: {full.get('ma5', '?')}  MA20: {full.get('ma20', '?')}")
            print(f"  ROE: {full.get('roe', '?')}")
        else:
            print(f"  ❌ 无数据 (请先运行 astock-data-toolkit 首轮下载)")
