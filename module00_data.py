#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
module00_data.py — M00 行情预处理

职责: 纯净数据加工，不做逻辑判断。
每轮自动预计算: MA5, MA20, MA20斜率, 5日均量
输出标准化字段供上层模块调用。
"""

import pandas as pd
import numpy as np
import logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [M00] %(message)s",
                    datefmt="%H:%M:%S")


def compute_520_features(df: pd.DataFrame) -> pd.DataFrame:
    """计算520均线系统全部特征。

    输入df: 必须含 close, volume 列
    输出df: 新增以下标准字段

    字段清单:
        ma5         — 5日均线
        ma20        — 20日均线
        ma20_slope  — MA20斜率(3日差分/当前值, %)
        ma20_trend  — True=向上/走平, False=向下
        vol_ma5     — 5日均量
        vol_ratio   — 当日量 / 5日均量
        vol_ok      — True=量比≥1.3
        gold_cross  — MA5上穿MA20
        dead_cross  — MA5下穿MA20
        above_ma20  — 收盘站稳MA20上方
    """
    df = df.copy()
    df['ma5'] = df['close'].rolling(window=5).mean()
    df['ma20'] = df['close'].rolling(window=20).mean()

    # MA20斜率: 3日差分 / 当前值 * 100 (%)
    df['ma20_slope'] = (df['ma20'] - df['ma20'].shift(3)) / df['ma20'] * 100
    df['ma20_trend'] = df['ma20'] > df['ma20'].shift(3)       # 严格向上
    df['ma20_flat_or_up'] = df['ma20_slope'] >= 0              # 走平/向上(斜率≥0)

    # 均量
    df['vol_ma5'] = df['volume'].rolling(window=5).mean()
    df['vol_ratio'] = df['volume'] / df['vol_ma5']
    df['vol_ok'] = df['volume'] > df['vol_ma5'] * 1.3

    # 交叉信号
    df['gold_cross'] = (df['ma5'] > df['ma20']) & (df['ma5'].shift(1) <= df['ma20'].shift(1))
    df['dead_cross'] = (df['ma5'] < df['ma20']) & (df['ma5'].shift(1) >= df['ma20'].shift(1))

    # 收盘站稳MA20
    df['above_ma20'] = df['close'] > df['ma20']

    return df


def get_latest_features(df: pd.DataFrame) -> dict:
    """获取最新交易日的520特征（纯净数据，无逻辑判断）。"""
    df_feat = compute_520_features(df)
    last = df_feat.iloc[-1]
    return {
        "ma5": round(last['ma5'], 2) if pd.notna(last['ma5']) else None,
        "ma20": round(last['ma20'], 2) if pd.notna(last['ma20']) else None,
        "ma20_slope": round(last['ma20_slope'], 3) if pd.notna(last['ma20_slope']) else None,
        "ma20_trend": bool(last['ma20_trend']) if pd.notna(last['ma20_trend']) else None,
        "ma20_flat_or_up": bool(last['ma20_flat_or_up']) if pd.notna(last['ma20_flat_or_up']) else None,
        "vol_ma5": round(last['vol_ma5'], 0) if pd.notna(last['vol_ma5']) else None,
        "vol_ratio": round(last['vol_ratio'], 2) if pd.notna(last['vol_ratio']) else None,
        "vol_ok": bool(last['vol_ok']) if pd.notna(last['vol_ok']) else False,
        "gold_cross": bool(last['gold_cross']) if pd.notna(last['gold_cross']) else False,
        "dead_cross": bool(last['dead_cross']) if pd.notna(last['dead_cross']) else False,
        "above_ma20": bool(last['above_ma20']) if pd.notna(last['above_ma20']) else False,
        "close": float(last['close']),
        "volume": float(last['volume']),
    }


# ===================== 自测 =====================

if __name__ == "__main__":
    import numpy as np
    print("=" * 60)
    print("  M00 行情预处理 自测")
    print("=" * 60)

    # 构造测试数据
    np.random.seed(42)
    dates = 40
    close = 10 + np.cumsum(np.random.randn(dates) * 0.2)
    close = np.maximum(close, 7)
    volume = np.random.randint(50, 200, dates)

    df = pd.DataFrame({"close": close, "volume": volume})
    df_feat = compute_520_features(df)

    last = get_latest_features(df)
    assert 'ma5' in last and 'ma20' in last and 'gold_cross' in last
    print(f"  MA5={last['ma5']} MA20={last['ma20']} 斜率={last['ma20_slope']}%")
    print(f"  金叉={last['gold_cross']} 死叉={last['dead_cross']}")
    print(f"  量比={last['vol_ratio']}x 量ok={last['vol_ok']} MA20趋势={last['ma20_trend']}")
    print("  ✅ 纯净数据输出正确")
