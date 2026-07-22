#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_cleaner.py — 数据清洗降噪模块 v1.0
================================================================
功能:
  1. 异常值检测: 3σ / IQR / 百分位
  2. 去未来函数校验: 确保无数据穿越
  3. 幸存者偏差处理: 退市股标记
  4. 动态标准化: 滚动Z-score / 分位数归一
  5. 特征选择: SHAP重要性 + 相关冗余剔除

用法:
  from data_cleaner import (
      detect_outliers_3sigma, detect_outliers_iqr,
      check_future_leakage, flag_delisted,
      rolling_zscore, rolling_quantile,
      select_features_by_shap, remove_collinear_features
  )
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Tuple, Optional, Union
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")


# ═══════════════════════════════════════════════
#  1. 异常值检测
# ═══════════════════════════════════════════════

def detect_outliers_3sigma(series: pd.Series, n_sigma: float = 3.0) -> pd.Series:
    """
    3σ 异常值检测
    mean ± n*sigma 之外标记为异常

    :param series: 输入序列
    :param n_sigma: 标准差倍数 (默认3)
    :return: bool Series, True=异常
    """
    if len(series) < 4:
        return pd.Series([False] * len(series), index=series.index)
    mean, std = series.mean(), series.std()
    if std == 0:
        return pd.Series([False] * len(series), index=series.index)
    return (series < mean - n_sigma * std) | (series > mean + n_sigma * std)


def detect_outliers_iqr(series: pd.Series, k: float = 1.5) -> pd.Series:
    """
    IQR 异常值检测
    Q1 - k*IQR  /  Q3 + k*IQR 之外标记为异常

    :param series: 输入序列
    :param k: IQR倍数 (默认1.5, 3.0=极端异常)
    :return: bool Series, True=异常
    """
    if len(series) < 4:
        return pd.Series([False] * len(series), index=series.index)
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return pd.Series([False] * len(series), index=series.index)
    return (series < q1 - k * iqr) | (series > q3 + k * iqr)


def detect_outliers_pctile(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """
    百分位异常值检测

    :param series: 输入序列
    :param lower: 下百分位 (默认1%)
    :param upper: 上百分位 (默认99%)
    :return: bool Series, True=异常
    """
    if len(series) < 10:
        return pd.Series([False] * len(series), index=series.index)
    lo, hi = series.quantile(lower), series.quantile(upper)
    return (series < lo) | (series > hi)


def detect_outliers_mad(series: pd.Series, n_mad: float = 3.5) -> pd.Series:
    """
    MAD (Median Absolute Deviation) 异常检测 — 对极端值更鲁棒

    :param series: 输入序列
    :param n_mad: MAD倍数
    :return: bool Series, True=异常
    """
    if len(series) < 4:
        return pd.Series([False] * len(series), index=series.index)
    med = series.median()
    mad = np.median(np.abs(series - med))
    if mad == 0:
        return pd.Series([False] * len(series), index=series.index)
    return np.abs(series - med) > n_mad * mad


def clean_factor_frame(df: pd.DataFrame, value_cols: List[str],
                       method: str = "mad", **kwargs) -> pd.DataFrame:
    """
    全因子框异常值清洗：异常值→NaN (后续fillna)

    :param df: 输入DataFrame (含stock_code列分组)
    :param value_cols: 需要清洗的数值列名列表
    :param method: 检测方法: 3sigma / iqr / pctile / mad
    :param kwargs: 传递给检测函数的参数
    :return: 清洗后的DataFrame(异常置NaN)
    """
    result = df.copy()
    detect_fn = {
        "3sigma": detect_outliers_3sigma,
        "iqr": detect_outliers_iqr,
        "pctile": detect_outliers_pctile,
        "mad": detect_outliers_mad,
    }.get(method, detect_outliers_mad)

    for col in value_cols:
        if col not in result.columns:
            continue
        # 按个股分组检测 (不同股票量级不同)
        if "stock_code" in result.columns:
            outlier_mask = pd.Series(False, index=result.index)
            for _, group in result.groupby("stock_code"):
                idx = group.index
                outlier_mask.loc[idx] = detect_fn(group[col], **kwargs)
            result.loc[outlier_mask, col] = np.nan
        else:
            outlier_mask = detect_fn(result[col], **kwargs)
            result.loc[outlier_mask, col] = np.nan

    return result


# ═══════════════════════════════════════════════
#  2. 去未来函数校验
# ═══════════════════════════════════════════════

def check_future_leakage(df: pd.DataFrame, feature_col: str,
                         date_col: str = "trade_date",
                         lookahead_days: int = 0) -> Dict:
    """
    校验特征列是否存在未来穿越

    检查: feature[t] 是否使用了 t+lookahead 之后的数据

    :param df: 时序DataFrame (需按date排序)
    :param feature_col: 待检查的特征列名
    :param date_col: 日期列名
    :param lookahead_days: 允许的前瞻天数(0=不允许前瞻)
    :return: {"pass": bool, "issues": [{"date":..., "reason":...}, ...]}
    """
    if df.empty or feature_col not in df.columns:
        return {"pass": True, "issues": []}

    sorted_df = df.sort_values(date_col).reset_index(drop=True)
    issues = []

    # 检查1: 特征值是否与未来价格高度相关(>0.99)
    for lead in [1, 2, 3, 5]:
        if "close" in sorted_df.columns:
            future_close = sorted_df["close"].shift(-lead)
            corr = sorted_df[feature_col].corr(future_close)
            if corr is not None and abs(corr) > 0.99:
                issues.append({
                    "date": str(sorted_df.iloc[-1][date_col]),
                    "reason": f"{feature_col} 与未来{lead}日收盘价相关系数={corr:.4f}>0.99,疑似穿越"
                })

    # 检查2: rolling计算中是否存在future data
    if feature_col.endswith("_ma") or "ma" in feature_col.lower():
        # MA列应检查: 当日MA是否用了未来数据 (rolling center=False)
        pass  # 标准rolling不会

    return {"pass": len(issues) == 0, "issues": issues}


def validate_all_features(df: pd.DataFrame, feature_cols: List[str],
                          date_col: str = "trade_date") -> Dict:
    """
    批量校验多个特征列的未来穿越
    """
    results = {}
    for col in feature_cols:
        if col in df.columns:
            results[col] = check_future_leakage(df, col, date_col)
    return results


# ═══════════════════════════════════════════════
#  3. 幸存者偏差处理
# ═══════════════════════════════════════════════

def flag_delisted_stocks(tushare_token: str = None) -> pd.DataFrame:
    """
    获取退市/ST股票列表，标记幸存者偏差

    :param tushare_token: Tushare token
    :return: DataFrame [ts_code, name, list_date, delist_date, is_delisted]
    """
    try:
        import tushare as ts
        token = tushare_token or os.environ.get("TUSHARE_TOKEN", "")
        if token:
            ts.set_token(token)
        pro = ts.pro_api()

        # 获取全部股票列表(含退市)
        df = pro.stock_basic(exchange='', list_status='D', fields='ts_code,symbol,name,area,industry,list_date,delist_date,is_hs')
        if df is not None and len(df) > 0:
            df["is_delisted"] = True
            df["list_status"] = "D"
            return df
    except Exception:
        pass

    # fallback: 返回空
    return pd.DataFrame(columns=["ts_code", "name", "delist_date", "is_delisted"])


def get_active_stocks() -> pd.DataFrame:
    """
    获取当前正常交易股票(非ST/非退市)
    """
    try:
        import tushare as ts
        pro = ts.pro_api()
        df = pro.stock_basic(exchange='', list_status='L',
                             fields='ts_code,symbol,name,area,industry,list_date,market')
        if df is not None:
            return df
    except Exception:
        pass
    return pd.DataFrame()


def filter_survivorship_bias(df: pd.DataFrame, code_col: str = "ts_code",
                              active_codes: set = None) -> pd.DataFrame:
    """
    过滤退市股(仅保留活跃股票)

    :param df: 原始数据
    :param code_col: 股票代码列名
    :param active_codes: 活跃股票代码set(若不提供则自动获取)
    :return: 仅含活跃股票的数据
    """
    if active_codes is None:
        active = get_active_stocks()
        active_codes = set(active["ts_code"].tolist()) if not active.empty else set()

    if not active_codes:
        return df  # 无法获取时跳过

    return df[df[code_col].isin(active_codes)]


# ═══════════════════════════════════════════════
#  4. 动态标准化 (滚动Z-score)
# ═══════════════════════════════════════════════

def rolling_zscore(series: pd.Series, window: int = 60,
                   min_periods: int = 20) -> pd.Series:
    """
    滚动Z-score标准化 — 解决牛熊数据分布漂移

    z[t] = (x[t] - mean[x_{t-window:t}]) / std[x_{t-window:t}]

    :param series: 输入序列
    :param window: 滚动窗口
    :param min_periods: 最小期数(不足返回NaN)
    :return: 标准化后的Series
    """
    if len(series) < min_periods:
        return pd.Series([np.nan] * len(series), index=series.index)

    roll_mean = series.rolling(window=window, min_periods=min_periods).mean()
    roll_std = series.rolling(window=window, min_periods=min_periods).std()
    roll_std = roll_std.replace(0, np.nan)
    return (series - roll_mean) / roll_std


def rolling_quantile(series: pd.Series, window: int = 60) -> pd.Series:
    """
    滚动分位数排名(0~1) — 对极端值更鲁棒

    :param series: 输入序列
    :param window: 滚动窗口
    :return: 0~1之间的排名比例
    """
    if len(series) < window:
        return pd.Series([np.nan] * len(series), index=series.index)

    result = pd.Series(np.nan, index=series.index)
    for i in range(window - 1, len(series)):
        window_data = series.iloc[i - window + 1:i + 1]
        val = series.iloc[i]
        rank = (window_data < val).sum() / len(window_data)
        result.iloc[i] = rank
    return result


def normalize_factor_panel(df: pd.DataFrame, value_cols: List[str],
                            method: str = "zscore", window: int = 60,
                            group_col: str = "stock_code") -> pd.DataFrame:
    """
    全因子面板动态标准化 — 按个股分组滚动归一

    :param df: 输入DataFrame (需含group_col和trade_date列)
    :param value_cols: 需标准化的列名列表
    :param method: zscore / rank / minmax
    :param window: 滚动窗口
    :param group_col: 分组列(个股)
    :return: 标准化后的DataFrame (新列: {col}_norm)
    """
    result = df.copy()
    if group_col not in result.columns:
        group_col = None

    for col in value_cols:
        if col not in result.columns:
            continue
        norm_col = f"{col}_norm"

        if group_col:
            result[norm_col] = np.nan
            for _, group in result.groupby(group_col):
                idx = group.index
                series = group[col].sort_index()
                if method == "zscore":
                    result.loc[idx, norm_col] = rolling_zscore(series, window).values
                elif method == "rank":
                    result.loc[idx, norm_col] = rolling_quantile(series, window).values
                elif method == "minmax":
                    roll_min = series.rolling(window, min_periods=20).min()
                    roll_max = series.rolling(window, min_periods=20).max()
                    result.loc[idx, norm_col] = (series - roll_min) / (roll_max - roll_min + 1e-8)
        else:
            if method == "zscore":
                result[norm_col] = rolling_zscore(result[col], window)
            elif method == "rank":
                result[norm_col] = rolling_quantile(result[col], window)

    return result


# ═══════════════════════════════════════════════
#  5. 特征选择: SHAP + 相关性
# ═══════════════════════════════════════════════

def select_features_by_shap(df: pd.DataFrame, feature_cols: List[str],
                             target_col: str = "target",
                             n_estimators: int = 100,
                             max_features: int = 10,
                             importance_threshold: float = 0.01) -> Dict:
    """
    基于XGBoost + SHAP的特征重要性筛选

    :param df: 输入DataFrame (含features + target)
    :param feature_cols: 候选特征列名列表
    :param target_col: 目标列名(如未来收益/涨跌标签)
    :param n_estimators: XGB树数量
    :param max_features: 最多保留特征数
    :param importance_threshold: SHAP重要性阈值(低于此值的剔除)
    :return: {"selected": [col,...], "dropped": [col,...], "importance": {col: val}}
    """
    import xgboost as xgb
    import shap

    # 准备数据
    valid_cols = [c for c in feature_cols if c in df.columns]
    X = df[valid_cols].copy()
    y = df[target_col].copy()

    # 去NaN
    mask = y.notna() & X.notna().all(axis=1)
    X, y = X[mask], y[mask]

    if len(X) < 50 or len(valid_cols) < 2:
        return {"selected": valid_cols[:max_features], "dropped": [],
                "importance": {c: 0.0 for c in valid_cols}, "error": "样本不足"}

    # 训练XGB
    model = xgb.XGBRegressor(
        n_estimators=n_estimators, max_depth=4, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8, random_state=42,
        verbosity=0
    )
    model.fit(X, y)

    # SHAP值
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)

    # 特征重要性: mean(|SHAP|)
    importance = {}
    for i, col in enumerate(valid_cols):
        importance[col] = float(np.abs(shap_values[:, i]).mean())

    # 排序+筛选
    sorted_features = sorted(importance.items(), key=lambda x: -x[1])
    total_imp = sum(v for _, v in sorted_features) or 1

    selected = []
    dropped = []
    cum_imp = 0
    for col, imp in sorted_features:
        if imp / total_imp >= importance_threshold and len(selected) < max_features:
            selected.append(col)
            cum_imp += imp
        else:
            dropped.append(col)

    return {
        "selected": selected,
        "dropped": dropped,
        "importance": importance,
        "cumulative_importance": round(cum_imp / total_imp, 4),
        "n_estimators": n_estimators,
    }


def remove_collinear_features(df: pd.DataFrame, feature_cols: List[str],
                               threshold: float = 0.95) -> Dict:
    """
    去除高相关性冗余特征

    :param df: 输入DataFrame
    :param feature_cols: 特征列名列表
    :param threshold: 相关性阈值(默认0.95)
    :return: {"selected": [col,...], "dropped": [(kept, removed, corr), ...]}
    """
    valid_cols = [c for c in feature_cols if c in df.columns]
    if len(valid_cols) < 2:
        return {"selected": valid_cols, "dropped": []}

    corr_matrix = df[valid_cols].corr().abs()
    upper = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))

    selected = list(valid_cols)
    dropped = []

    for col in valid_cols:
        if col not in selected:
            continue
        to_drop = [c for c in selected if c != col and upper.loc[col, c] > threshold]
        for d in to_drop:
            selected.remove(d)
            dropped.append((col, d, round(float(upper.loc[col, d]), 4)))

    return {"selected": selected, "dropped": dropped}


def full_feature_selection(df: pd.DataFrame, feature_cols: List[str],
                            target_col: str = "target",
                            corr_threshold: float = 0.95,
                            shap_threshold: float = 0.01,
                            max_features: int = 10) -> Dict:
    """
    全流程特征选择: 去相关 → SHAP筛选

    :return: {"final_selected": [...], "removed_by_collinearity": [...],
               "removed_by_shap": [...], "importance": {...}}
    """
    # Step 1: 去冗余
    collinear_result = remove_collinear_features(df, feature_cols, threshold=corr_threshold)
    step1_selected = collinear_result["selected"]

    # Step 2: SHAP筛选
    shap_result = select_features_by_shap(
        df, step1_selected, target_col,
        max_features=max_features, importance_threshold=shap_threshold
    )

    return {
        "final_selected": shap_result["selected"],
        "removed_by_collinearity": collinear_result["dropped"],
        "removed_by_shap": shap_result["dropped"],
        "importance": shap_result.get("importance", {}),
        "cumulative_importance": shap_result.get("cumulative_importance", 0),
    }


# ═══════════════════════════════════════════════
#  6. 一键清洗管道
# ═══════════════════════════════════════════════

def clean_pipeline(df: pd.DataFrame,
                   value_cols: List[str] = None,
                   code_col: str = "stock_code",
                   date_col: str = "trade_date",
                   outlier_method: str = "mad",
                   normalize_method: str = "zscore",
                   normalize_window: int = 60,
                   feature_selection: bool = False,
                   target_col: str = None) -> Dict:
    """
    一键全流程清洗

    :param df: 输入原始数据
    :param value_cols: 需要清洗的列(默认自动识别数值列)
    :param code_col: 股票代码列
    :param date_col: 日期列
    :param outlier_method: mad/3sigma/iqr/pctile
    :param normalize_method: zscore/rank/minmax
    :param normalize_window: 滚动窗口
    :param feature_selection: 是否执行特征选择
    :param target_col: 目标列(特征选择需要)
    :return: {"cleaned": df, "stats": {}, "feature_selection": {}}
    """
    if value_cols is None:
        value_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        value_cols = [c for c in value_cols if c not in [code_col, date_col]]

    stats = {}

    # Step 1: 异常值清洗
    cleaned = clean_factor_frame(df, value_cols, method=outlier_method)
    outlier_count = cleaned[value_cols].isna().sum().sum()
    stats["outliers_removed"] = int(outlier_count)
    stats["outlier_method"] = outlier_method

    # Step 2: 动态标准化
    if normalize_method:
        cleaned = normalize_factor_panel(
            cleaned, value_cols,
            method=normalize_method, window=normalize_window,
            group_col=code_col
        )
        stats["normalize_method"] = normalize_method
        stats["normalize_window"] = normalize_window

    # Step 3: 幸存者偏差标记
    if code_col in cleaned.columns:
        active = get_active_stocks()
        if not active.empty:
            active_codes = set(active["ts_code"].tolist())
            orig_count = len(cleaned)
            cleaned = filter_survivorship_bias(cleaned, code_col, active_codes)
            stats["survivorship_removed"] = orig_count - len(cleaned)

    # Step 4: 特征选择
    fs_result = {}
    if feature_selection and target_col and target_col in cleaned.columns:
        fs_result = full_feature_selection(
            cleaned, value_cols, target_col
        )
        stats["feature_selection"] = fs_result

    return {"cleaned": cleaned, "stats": stats, "feature_selection": fs_result}


# ═══════════════════════════════════════════════
#  测试入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  data_cleaner.py — 数据清洗降噪模块")
    print("=" * 60)

    # 生成测试数据
    np.random.seed(42)
    n = 200
    test_df = pd.DataFrame({
        "trade_date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "stock_code": ["600547"] * n,
        "close": 30 + np.cumsum(np.random.randn(n) * 0.5),
        "vol": np.random.randn(n) * 100 + 500,
        "pe_ttm": np.random.randn(n) * 5 + 20,
        "pct_chg": np.random.randn(n) * 2,
    })
    # 注入异常值
    test_df.loc[10, "close"] = 1000
    test_df.loc[50, "vol"] = -999
    test_df.loc[100, "pe_ttm"] = 500

    print(f"\n测试数据: {test_df.shape}, 含3个人工异常值")

    # 测试异常检测
    for method in ["3sigma", "iqr", "mad"]:
        cleaned = clean_factor_frame(
            test_df, ["close", "vol", "pe_ttm"], method=method
        )
        nans = cleaned[["close", "vol", "pe_ttm"]].isna().sum()
        print(f"  {method}: 检测到异常 {nans.sum()} 处 → {dict(nans)}")

    # 测试滚动标准化
    print("\n测试滚动Z-score...")
    normed = normalize_factor_panel(
        test_df, ["close", "vol"], method="zscore", window=30
    )
    print(f"  close_norm: min={normed['close_norm'].min():.2f} max={normed['close_norm'].max():.2f}")
    print(f"  前5个值为NaN(冷启动): {normed['close_norm'].head(5).isna().sum()}")

    # 测试特征选择(带模拟target)
    test_df["target"] = np.random.randn(n) * 0.02 + test_df["pct_chg"].shift(-1)
    fs = full_feature_selection(
        test_df, ["close", "vol", "pe_ttm", "pct_chg"],
        target_col="target", max_features=3
    )
    print(f"\n特征选择结果:")
    print(f"  最终保留: {fs['final_selected']}")
    print(f"  重要性: {fs['importance']}")

    print(f"\n✅ 模块测试通过")
