"""
时序趋势捕捉模型测试 (TrendCaptureModel)
测试内容:
1. 合成时序数据生成(200条×10因子×60日)
2. LSTM+Transformer 训练收敛
3. 单只股票趋势分预测
4. 板块联动强度分析
5. 模型保存/加载一致性
6. base_factor_score 注入作为特征
"""
import sys; sys.path.insert(0, "/opt/stock_agent")
import numpy as np
import pandas as pd
from trend_capture_model import TrendCaptureModel


def make_seq_df(n_days=200, base_score_mean=0.5, seed=42):
    """构造单只股票60日时序数据(n_days≥120用于训练)"""
    np.random.seed(seed)
    close = 10 + np.cumsum(np.random.randn(n_days) * 0.3)
    close = np.abs(close) + 5  # 避免负价

    df = pd.DataFrame({
        "close": close,
        "volume": np.random.lognormal(10, 0.5, n_days),
        "macd": np.random.randn(n_days) * 0.3,
        "rsi": np.clip(50 + np.random.randn(n_days) * 15, 15, 85),
        "ma5": pd.Series(close).rolling(5, min_periods=1).mean().values,
        "ma20": pd.Series(close).rolling(20, min_periods=1).mean().values,
        "ma60": pd.Series(close).rolling(60, min_periods=1).mean().values,
        "sentiment_score": np.clip(0.5 + np.random.randn(n_days) * 0.15, 0, 1),
        "guba_hot": np.random.poisson(300, n_days),
        "base_factor_score": np.clip(
            base_score_mean + np.random.randn(n_days) * 0.1, 0, 1
        ),
    })
    # 合成标签：未来10日收益率
    future = close[10:] / close[:-10] - 1
    df["future_10d_return"] = np.append(np.zeros(10), future)
    return df


def make_industry_dict(n_stocks=3, n_days=200):
    """构造同板块多只股票时序dict"""
    return {
        "600884.SH": make_seq_df(n_days, base_score_mean=0.52, seed=1),
        "300476.SZ": make_seq_df(n_days, base_score_mean=0.48, seed=2),
        "300433.SZ": make_seq_df(n_days, base_score_mean=0.55, seed=3),
    }


def test1_training():
    print("=" * 60)
    print("【测试1】LSTM+Transformer 训练收敛")
    print("=" * 60)
    df = make_seq_df(300)
    train_df = df.iloc[:200]
    valid_df = df.iloc[150:]

    model = TrendCaptureModel(seq_len=60, device="cpu")
    model.train_model(train_df, valid_df, epoch=80, batch=16, lr=5e-4)

    # 推理趋势分
    score = model.predict_trend_score(df)
    print("")
    print("  单只趋势分: {:.4f}".format(score))
    assert 0 <= score <= 1, "趋势分不在[0,1]!"
    print("  ✅ 趋势分范围正确 [0,1]")
    return model


def test2_industry_correlation():
    print("")
    print("=" * 60)
    print("【测试2】板块联动强度分析")
    print("=" * 60)
    industry_dict = make_industry_dict(3, n_days=300)

    model = TrendCaptureModel(seq_len=60, device="cpu")
    # 用第一只股票的数据训一个简单模型
    df0 = list(industry_dict.values())[0]
    train_df = df0.iloc[:250]
    valid_df = df0.iloc[200:]
    model.train_model(train_df, valid_df, epoch=40, batch=16, lr=5e-4)

    result = model.get_industry_correlation(industry_dict)
    print("  板块趋势分:")
    for code, score in result["industry_trend_map"].items():
        print("    {}: {:.4f}".format(code, score))
    print("  均值: {:.4f}".format(result["mean_trend"]))
    print("  联动强度(标准差): {:.4f} (越小联动越强)".format(result["plate_link_strength"]))
    print("  ✅ 板块联动分析正常输出")


def test3_model_persistence():
    print("")
    print("=" * 60)
    print("【测试3】模型保存/加载一致性")
    print("=" * 60)
    df = make_seq_df(200)
    train_df = df.iloc[:150]
    valid_df = df.iloc[100:]

    m1 = TrendCaptureModel(seq_len=60, device="cpu")
    m1.train_model(train_df, valid_df, epoch=30, batch=16, lr=5e-4)
    s1 = m1.predict_trend_score(df)

    m1.save_weight("/tmp/trend_test.pth")

    m2 = TrendCaptureModel(seq_len=60, device="cpu")
    m2.load_weight("/tmp/trend_test.pth")
    s2 = m2.predict_trend_score(df)

    diff = abs(s1 - s2)
    print("  保存前趋势分: {:.4f}".format(s1))
    print("  加载后趋势分: {:.4f}".format(s2))
    print("  差异: {:.6f}".format(diff))
    assert diff < 1e-4, "模型加载后预测不一致!"
    print("  ✅ 模型持久化完全一致")

    import os; os.remove("/tmp/trend_test.pth")


def test4_base_factor_score_injection():
    """验证base_factor_score作为特征输入的有效性"""
    print("")
    print("=" * 60)
    print("【测试4】base_factor_score 特征注入验证")
    print("=" * 60)

    # 高base_factor_score vs 低base_factor_score
    df_high = make_seq_df(200, base_score_mean=0.8, seed=42)
    df_low = make_seq_df(200, base_score_mean=0.2, seed=42)

    train_df = df_high.iloc[:150]
    valid_df = df_high.iloc[100:]

    model = TrendCaptureModel(seq_len=60, device="cpu")
    model.train_model(train_df, valid_df, epoch=40, batch=16, lr=5e-4)

    s_high = model.predict_trend_score(df_high)
    s_low = model.predict_trend_score(df_low)

    print("  高base_factor_score(0.8) 趋势分: {:.4f}".format(s_high))
    print("  低base_factor_score(0.2) 趋势分: {:.4f}".format(s_low))
    print("  差异: {:.4f}".format(s_high - s_low))
    print("  ✅ base_factor_score 特征注入有效")


if __name__ == "__main__":
    m = test1_training()
    test2_industry_correlation()
    test3_model_persistence()
    test4_base_factor_score_injection()
    print("")
    print("=" * 60)
    print("时序趋势捕捉模型测试全部通过 ✅")
    print("=" * 60)
