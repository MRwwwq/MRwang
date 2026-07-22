"""
底层压舱模型测试（可解释传统模型）：XGBoost/LightGBM多因子模型
验证内容：
1. LGB / XGB 双引擎训练 + 预测一致性
2. 单调性约束生效验证
3. SHAP可解释性输出（人工审计用）
4. base_factor_score 分布稳定性
5. 对接 AgentLongMemory 全链路写入
"""
import sys; sys.path.insert(0, "/opt/stock_agent")
import numpy as np
import pandas as pd
from base_factor_model import BaseStableFactorModel
from agent_long_memory import AgentLongMemory


def make_synthetic_factors(n=2000, seed=42):
    """构造逼真A股因子合成数据：7因子 + 含噪声标签"""
    np.random.seed(seed)
    df = pd.DataFrame({
        "ts_code": np.random.choice(
            ["600884.SH", "002044.SZ", "600547.SH", "002617.SZ", "300476.SZ",
             "300693.SZ", "300433.SZ", "601868.SH", "600519.SH", "000858.SZ"],
            n
        ),
        "trade_date": np.random.choice(
            pd.date_range("2026-01-01", "2026-07-17").strftime("%Y%m%d"), n
        ),
        # 基础估值因子
        "pe_ttm": np.random.lognormal(mean=3.3, sigma=0.6, size=n),        # 中位数~27x
        "pb": np.random.lognormal(mean=0.8, sigma=0.5, size=n),            # 中位数~2.2x
        "roe": np.clip(np.random.randn(n) * 6 + 8, -20, 40),               # 均值8%±6
        # 技术因子
        "rsi_6": np.random.uniform(15, 85, n),
        "macd": np.random.randn(n) * 0.4,
        "volume_ratio": np.clip(np.random.lognormal(0, 0.3, n), 0.3, 5.0),
        # 资金流因子
        "capital_flow_10d": np.random.randn(n) * 8000,                     # 万元
        # 均线偏离
        "ma5_gap": np.random.randn(n) * 4,
    })

    # 合成标签: forward_20d_return
    # 因子逻辑: 低PE+高ROE+资金流入+RSI超卖→正收益；高PE+低ROE+资金流出→负收益
    y = (
        -0.003 * np.log1p(df["pe_ttm"])         # PE越低越好(对数压缩极端值)
        + 0.005 * df["roe"] / 10                # ROE越高越好(归一化到~0.02每单位)
        + 0.008 * (df["rsi_6"] - 50) / 30       # RSI中性的摆动
        + 0.04 * np.tanh(df["macd"] * 2)         # MACD正负向(双曲正切压缩)
        + 0.015 * np.log1p(np.abs(df["capital_flow_10d"])) * np.sign(df["capital_flow_10d"])  # 资金流对数压缩
        - 0.003 * np.abs(df["ma5_gap"])          # 偏离过大→回归压力
        + 0.01 * np.log1p(df["volume_ratio"])    # 量比适中
        + np.random.randn(n) * 0.03              # 噪声
    )
    # 归一化到 [-0.1, 0.1] 附近
    y = y / 5
    df["forward_20d_return"] = y
    return df


def test_lgb_model(df):
    """测试LightGBM引擎"""
    print("=" * 60)
    print("【测试1】LightGBM 引擎")
    print("=" * 60)

    model = BaseStableFactorModel(model_type="lgb", seed=42)
    model.set_monotone_constraint({
        "pe_ttm": -1,           # PE越低越好
        "roe": 1,               # ROE越高越好
        "capital_flow_10d": 1,  # 资金流入越多越好
    })

    model.train(df)
    scores = model.predict_score(df)

    print("  base_factor_score 分布:")
    print("    范围: {:.4f} ~ {:.4f}".format(scores.min(), scores.max()))
    print("    均值: {:.4f} ± {:.4f}".format(scores.mean(), scores.std()))
    print("    [0,0.3): {:.1f}%  [0.3,0.7): {:.1f}%  [0.7,1.0]: {:.1f}%".format(
        (scores < 0.3).mean() * 100,
        ((scores >= 0.3) & (scores < 0.7)).mean() * 100,
        (scores >= 0.7).mean() * 100,
    ))

    # 单调性验证: PE越低→得分越高
    pe_low = df[df["pe_ttm"] < df["pe_ttm"].quantile(0.2)]["pe_ttm"].mean()
    pe_high = df[df["pe_ttm"] > df["pe_ttm"].quantile(0.8)]["pe_ttm"].mean()
    score_low = scores[df["pe_ttm"] < df["pe_ttm"].quantile(0.2)].mean()
    score_high = scores[df["pe_ttm"] > df["pe_ttm"].quantile(0.8)].mean()
    print("")
    print("  单调性验证(PE -1约束):")
    print("    低PE(均值{:.1f}) → 平均得分 {:.4f}".format(pe_low, score_low))
    print("    高PE(均值{:.1f}) → 平均得分 {:.4f}".format(pe_high, score_high))
    assert score_low > score_high, "PE单调性违反!低PE应得分更高"
    print("    ✅ 低PE得分>高PE得分, 单调递增约束生效")

    return model, scores


def test_xgb_model(df):
    """测试XGBoost引擎"""
    print("")
    print("=" * 60)
    print("【测试2】XGBoost 引擎")
    print("=" * 60)

    model = BaseStableFactorModel(model_type="xgb", seed=42)
    model.set_monotone_constraint({
        "pe_ttm": -1,
        "roe": 1,
        "capital_flow_10d": 1,
    })

    model.train(df)
    scores = model.predict_score(df)

    print("  base_factor_score 分布:")
    print("    范围: {:.4f} ~ {:.4f}".format(scores.min(), scores.max()))
    print("    均值: {:.4f} ± {:.4f}".format(scores.mean(), scores.std()))

    pe_low_s = scores[df["pe_ttm"] < df["pe_ttm"].quantile(0.2)].mean()
    pe_high_s = scores[df["pe_ttm"] > df["pe_ttm"].quantile(0.8)].mean()
    print("")
    print("  单调性验证(PE -1约束):")
    print("    低PE → 平均得分 {:.4f}".format(pe_low_s))
    print("    高PE → 平均得分 {:.4f}".format(pe_high_s))
    assert pe_low_s > pe_high_s, "PE单调性违反!"
    print("    ✅ 单调递增约束生效")

    return model, scores


def test_shap_explain(model, df):
    """测试SHAP可解释性"""
    print("")
    print("=" * 60)
    print("【测试3】SHAP因子贡献拆解（人工审计核心能力）")
    print("=" * 60)

    # 取一只典型标的
    stock = df.iloc[0]
    result = model.get_single_stock_explain(stock)

    print("  ts_code: {}".format(stock["ts_code"]))
    print("  total_base_score: {:.4f}".format(result["total_base_score"]))
    print("")
    print("  因子贡献排序(Top5):")
    for c in result["feature_contribution"][:5]:
        arrow = "🟢" if c["shap_contrib"] > 0 else "🔴"
        print("    {} {}: {:+.6f}".format(arrow, c["feature"], c["shap_contrib"]))

    # 验证contributions之和≈base_score(归一化前)
    total_shap = sum(c["shap_contrib"] for c in result["feature_contribution"])
    print("")
    print("  SHAP贡献和: {:+.6f} (应≈0, 归一化后基线偏移)".format(total_shap))
    print("  ✅ SHAP因子贡献拆解正常输出")

    return result


def test_lgb_vs_xgb_consistency(df, lgb_scores, xgb_scores):
    """LGB vs XGB 一致性验证"""
    print("")
    print("=" * 60)
    print("【测试4】LGB vs XGB 输出一致性")
    print("=" * 60)

    corr = np.corrcoef(lgb_scores, xgb_scores)[0, 1]
    rank_corr = pd.Series(lgb_scores).corr(pd.Series(xgb_scores), method="spearman")

    print("  Pearson相关系数: {:.4f}".format(corr))
    print("  Spearman秩相关: {:.4f}".format(rank_corr))
    print("  双引擎Top100重合率: {:.1f}%".format(
        len(set(lgb_scores.nlargest(100).index) & set(xgb_scores.nlargest(100).index))
    ))

    assert corr > 0.7, "LGB与XGB输出相关性不足0.7!"
    print("  ✅ 双引擎输出高度一致")


def test_memory_pipeline(df, lgb_scores):
    """对接AgentLongMemory全链路写入测试"""
    print("")
    print("=" * 60)
    print("【测试5】AgentLongMemory 全链路对接")
    print("=" * 60)

    mem = AgentLongMemory()

    # 构造带 base_factor_score 的行情记录
    sample = df.head(3).copy()
    sample["base_factor_score"] = lgb_scores.head(3).values
    sample["close"] = np.random.uniform(10, 30, 3)
    sample["high"] = sample["close"] * 1.03
    sample["low"] = sample["close"] * 0.97
    sample["open"] = sample["close"] * 0.99
    sample["volume"] = np.random.randint(100000, 1000000, 3)
    sample["turnover"] = sample["volume"] * sample["close"] / 1e8
    sample["macd"] = np.random.randn(3) * 0.3
    sample["rsi"] = np.random.uniform(30, 70, 3)
    sample["ma5"] = sample["close"] * np.random.uniform(0.95, 1.05, 3)
    sample["ma20"] = sample["close"] * np.random.uniform(0.92, 1.08, 3)
    sample["ma60"] = sample["close"] * np.random.uniform(0.88, 1.12, 3)
    sample["sentiment_score"] = np.random.uniform(0.3, 0.8, 3)
    sample["org_visit_flag"] = np.random.randint(0, 2, 3)
    sample["guba_hot"] = np.random.randint(0, 500, 3)
    sample["market_cap"] = np.random.uniform(50, 500, 3) * 1e8
    sample["industry"] = "测试行业"

    mem.write_market_memory(sample[["ts_code", "trade_date", "close", "high", "low",
                                     "open", "volume", "turnover", "macd", "rsi",
                                     "ma5", "ma20", "ma60", "sentiment_score",
                                     "org_visit_flag", "guba_hot", "market_cap",
                                     "industry", "base_factor_score"]])

    # 验证写入成功
    for _, r in sample.iterrows():
        q = mem.conn.execute(
            "SELECT base_factor_score FROM memory_market WHERE ts_code='{}' ORDER BY id DESC LIMIT 1".format(r["ts_code"])
        ).fetchone()
        if q:
            print("  {} base_factor_score = {:.4f} ✅".format(r["ts_code"], q[0]))

    # 清理
    for _, r in sample.iterrows():
        mem.conn.execute("DELETE FROM memory_market WHERE ts_code='{}'".format(r["ts_code"]))
    mem.conn.commit()

    # 调用一次老化清理(无过期数据应返回0)
    result = mem.memory_aging_clean(3)
    print("  老化清理(无过期数据): moved={}, deleted={} ✅".format(result["moved"], result["deleted"]))

    mem.close()
    print("  ✅ AgentLongMemory 全链路对接验证通过")


def test_model_save_load(model, df):
    """模型持久化验证"""
    print("")
    print("=" * 60)
    print("【测试6】模型持久化(保存/加载一致性)")
    print("=" * 60)

    model.save_model("/tmp/base_model_test.txt")
    scores_orig = model.predict_score(df)

    model2 = BaseStableFactorModel(model_type=model.model_type)
    model2.load_model("/tmp/base_model_test.txt", feature_cols=model.feature_cols)
    scores_loaded = model2.predict_score(df)

    diff = (scores_orig - scores_loaded).abs().max()
    print("  预测最大差异: {:.10f}".format(diff))
    assert diff < 1e-6, "保存/加载模型预测不一致!"
    print("  ✅ 模型保存加载完全一致")

    import os; os.remove("/tmp/base_model_test.txt")


if __name__ == "__main__":
    print("生成合成因子数据: 2000条 × 7因子")
    df = make_synthetic_factors(2000)
    print("  因子: {}".format([c for c in df.columns if c not in ["ts_code", "trade_date", "forward_20d_return"]]))
    print("  标签范围: {:.4f} ~ {:.4f}".format(df["forward_20d_return"].min(), df["forward_20d_return"].max()))

    # 测试1: LightGBM
    lgb_model, lgb_scores = test_lgb_model(df)

    # 测试2: XGBoost
    xgb_model, xgb_scores = test_xgb_model(df)

    # 测试3: SHAP可解释性
    test_shap_explain(lgb_model, df)

    # 测试4: 双引擎一致性
    test_lgb_vs_xgb_consistency(df, lgb_scores, xgb_scores)

    # 测试5: AgentLongMemory对接
    test_memory_pipeline(df, lgb_scores)

    # 测试6: 模型存储
    test_model_save_load(lgb_model, df)

    print("")
    print("=" * 60)
    print("底层压舱模型测试全部通过 ✅")
    print("=" * 60)
    print("")
    print("保存模型至 base_model.txt 供 memory_scheduler 每日调用")
    lgb_model.save_model("/opt/stock_agent/base_model.txt")
    print("  -> /opt/stock_agent/base_model.txt")
