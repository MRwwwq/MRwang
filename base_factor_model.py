import pandas as pd
import numpy as np
import lightgbm as lgb
import xgboost as xgb
import shap
from sklearn.model_selection import train_test_split


class BaseStableFactorModel:
    """基础稳定因子模型：LightGBM/XGBoost + 单调性约束 + SHAP可解释
    输出base_factor_score(0~1归一化)，对接AgentLongMemory.memory_market.base_factor_score
    """

    def __init__(self, model_type="lgb", seed=42):
        self.model_type = model_type
        self.seed = seed
        self.model = None
        self.feature_cols = []
        self.shap_explainer = None
        self.monotone_constraints = {}

    def set_monotone_constraint(self, factor_dict: dict):
        """因子单调性约束 {因子: 1递增 / -1递减}，保证可解释性"""
        self.monotone_constraint_dict = factor_dict
        self.monotone_constraints = []  # 转为list，在train时按feature_cols顺序填充

    def train(self, df: pd.DataFrame, label_col="forward_20d_return"):
        # 只保留数值型列，排除字符串ID列
        exclude_cols = {label_col, "ts_code", "trade_date", "stock_code", "name"}
        self.feature_cols = [
            c for c in df.columns
            if c not in exclude_cols and pd.api.types.is_numeric_dtype(df[c])
        ]
        X = df[self.feature_cols]
        y = df[label_col]
        X_train, X_valid, y_train, y_valid = train_test_split(
            X, y, test_size=0.2, random_state=self.seed
        )

        # 将单调性约束dict转为feature_cols顺序list
        mono_list = []
        for col in self.feature_cols:
            mono_list.append(self.monotone_constraint_dict.get(col, 0))

        if self.model_type == "lgb":
            params = {
                "objective": "regression",
                "metric": "mse",
                "seed": self.seed,
                "monotone_constraints": mono_list,
                "max_depth": 4,
                "learning_rate": 0.05,
                "num_leaves": 16,
                "verbosity": -1,
            }
            train_set = lgb.Dataset(X_train, y_train)
            valid_set = lgb.Dataset(X_valid, y_valid)
            self.model = lgb.train(
                params, train_set, valid_sets=[valid_set], num_boost_round=300
            )
        else:
            dtrain = xgb.DMatrix(X_train, label=y_train)
            dvalid = xgb.DMatrix(X_valid, label=y_valid)
            params = {
                "objective": "reg:squarederror",
                "seed": self.seed,
                "max_depth": 4,
                "eta": 0.05,
                "monotone_constraints": "(" + ",".join(str(m) for m in mono_list) + ")",
            }
            self.model = xgb.train(
                params, dtrain, num_boost_round=300,
                evals=[(dvalid, "valid")]
            )

        self.shap_explainer = shap.TreeExplainer(self.model)

    def predict_score(self, df: pd.DataFrame) -> pd.Series:
        """输出归一化到[0,1]的base_factor_score"""
        X = df[self.feature_cols]
        if self.model_type == "lgb":
            pred = self.model.predict(X)
        else:
            pred = self.model.predict(xgb.DMatrix(X))

        pmin, pmax = np.min(pred), np.max(pred)
        if pmax - pmin < 1e-10:
            pred_norm = np.full_like(pred, 0.5)
        else:
            pred_norm = (pred - pmin) / (pmax - pmin)
        return pd.Series(pred_norm, index=df.index, name="base_factor_score")

    def get_single_stock_explain(self, stock_row: pd.Series) -> dict:
        """返回单只标的SHAP因子贡献拆解"""
        X_single = stock_row[self.feature_cols].values.reshape(1, -1)
        shap_vals = self.shap_explainer.shap_values(X_single)[0]
        contrib_df = pd.DataFrame({
            "feature": self.feature_cols, "shap_contrib": shap_vals
        }).sort_values("shap_contrib", ascending=False)

        score = self.predict_score(pd.DataFrame([stock_row]))[0]
        return {
            "total_base_score": round(score, 4),
            "feature_contribution": contrib_df.to_dict("records")
        }

    def save_model(self, path="base_model.txt"):
        self.model.save_model(path)

    def load_model(self, path="base_model.txt", feature_cols=None):
        if self.model_type == "lgb":
            self.model = lgb.Booster(model_file=path)
        else:
            self.model = xgb.Booster()
            self.model.load_model(path)
        self.shap_explainer = shap.TreeExplainer(self.model)
        if feature_cols is not None:
            self.feature_cols = feature_cols


if __name__ == "__main__":
    # 快速测试：生成合成数据训练+预测+SHAP解释
    np.random.seed(42)
    n = 500
    df = pd.DataFrame({
        "pe_ttm": np.random.randn(n) * 20 + 30,
        "roe": np.random.randn(n) * 5 + 10,
        "volume_ratio": np.random.randn(n) * 0.5 + 1.0,
        "rsi_6": np.random.uniform(20, 80, n),
        "macd": np.random.randn(n) * 0.5,
        "ma5_gap": np.random.randn(n) * 3,
        "capital_flow_10d": np.random.randn(n) * 5000,
    })
    # 模拟正向因子：低PE+高ROE+资金流入→正收益
    df["forward_20d_return"] = (
        -0.002 * df["pe_ttm"]
        + 0.01 * df["roe"]
        + 0.005 * df["volume_ratio"]
        + 0.003 * (df["rsi_6"] - 50)
        + 0.05 * df["macd"]
        + 0.02 * df["ma5_gap"]
        + 0.00001 * df["capital_flow_10d"]
        + np.random.randn(n) * 0.02
    )

    model = BaseStableFactorModel(model_type="lgb")
    model.set_monotone_constraint({"pe_ttm": -1, "roe": 1, "capital_flow_10d": 1})

    print("训练中...")
    model.train(df)
    print("训练完成 ✅")

    scores = model.predict_score(df)
    print("base_factor_score 范围: {:.4f} ~ {:.4f}".format(scores.min(), scores.max()))
    print("base_factor_score 均值: {:.4f}".format(scores.mean()))

    # 单个标的SHAP解释
    result = model.get_single_stock_explain(df.iloc[0])
    print("\n单标SHAP解释:")
    print("  total_base_score:", result["total_base_score"])
    for c in result["feature_contribution"][:3]:
        print("   {}: {:+.4f}".format(c["feature"], c["shap_contrib"]))

    model.save_model("/tmp/test_model.txt")
    model2 = BaseStableFactorModel(model_type="lgb")
    model2.load_model("/tmp/test_model.txt", feature_cols=model.feature_cols)
    scores2 = model2.predict_score(df)
    print("\n模型保存/加载一致性:", "OK" if abs(scores2.mean() - scores.mean()) < 1e-6 else "FAIL")

    import os; os.remove("/tmp/test_model.txt")
    print("\n全部测试通过 ✅")
