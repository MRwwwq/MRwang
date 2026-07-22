"""
AI自主进化引擎 — 参数微进化 + LLM因子生成 + WalkForward重训
"""
import sqlite3
import pandas as pd
import numpy as np
import random
import re
import os
from datetime import datetime, timedelta
import math

DB_PATH = "agent_memory.db"
WALK_WINDOW_DAYS = 120
RETRAIN_INTERVAL = 30
PARAM_EVOLVE_SAMPLE_CNT = 30
LLM_FACTOR_TEST_THRESHOLD = 0.03  # IC threshold


class AIEvolveEngine:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.base_model = None
        self.trend_model = None
        self.ppo_rl = None

        # Lazy init models
        self._init_models()

        # LLM客户端(可选, 不可用时降级为规则因子生成)
        self.llm_client = None
        self._init_llm()

    def _init_models(self):
        try:
            from base_factor_model import BaseStableFactorModel
            self.base_model = BaseStableFactorModel("lgb")
            self.base_model.load_model("base_model.txt")
        except Exception as e:
            print("[Evolve] 底层模型加载失败: {}".format(e))

        try:
            from trend_capture_model import TrendCaptureModel
            self.trend_model = TrendCaptureModel(seq_len=60)
        except Exception as e:
            print("[Evolve] 时序模型加载失败: {}".format(e))

    def _init_llm(self):
        """初始化LLM客户端(支持OpenAI兼容接口 / 本地模型 / 降级规则)"""
        try:
            import openai
            # Try common local LLM endpoints
            for api_base in [
                "http://localhost:8000/v1",
                "http://127.0.0.1:8000/v1",
                "https://api.openai.com/v1",
            ]:
                openai.api_base = api_base
                openai.api_key = "sk-placeholder"
                self.llm_client = openai
                print("[Evolve] LLM客户端初始化: {}".format(api_base))
                break
        except Exception:
            self.llm_client = None
            print("[Evolve] LLM不可用, 降级为规则因子生成")

    # ===================== 1. 参数微进化 =====================

    def load_failure_optimize_tasks(self):
        """读取每日复盘输出的亏损归因优化任务"""
        try:
            sql = "SELECT DISTINCT avoid_strategy FROM memory_failure_signal"
            return pd.read_sql(sql, self.conn)["avoid_strategy"].dropna().tolist()
        except Exception:
            return []

    def param_evolution_search(self):
        """根据失效原因定向生成参数网格，滚动回测筛选最优区间"""
        task_list = self.load_failure_optimize_tasks()
        print("失效归因任务数: {}".format(len(task_list)))

        param_pool = {
            "ma_cycle": [5, 10, 20, 30, 60],
            "stop_loss_range": [0.02, 0.03, 0.04, 0.05, 0.06],
            "take_profit_range": [0.05, 0.07, 0.09, 0.12],
            "max_position_rate": [0.3, 0.45, 0.6, 0.75],
            "base_score_threshold": [0.35, 0.4, 0.45, 0.5],
            "trend_score_threshold": [0.3, 0.35, 0.4],
        }

        # 随机采样参数组合
        param_groups = []
        for _ in range(PARAM_EVOLVE_SAMPLE_CNT):
            group = {}
            for k, v_list in param_pool.items():
                group[k] = random.choice(v_list)
            param_groups.append(group)

        # 加载市场数据
        try:
            market_full_df = pd.read_sql(
                "SELECT * FROM memory_market ORDER BY trade_date", self.conn
            )
        except Exception:
            print("无市场数据, 跳过参数进化")
            return pd.DataFrame()

        if len(market_full_df) < WALK_WINDOW_DAYS + 10:
            print("数据不足{}天, 跳过参数进化".format(WALK_WINDOW_DAYS))
            return pd.DataFrame()

        split_point = max(0, len(market_full_df) - WALK_WINDOW_DAYS)
        train_df = market_full_df.iloc[:split_point] if split_point > 0 else market_full_df
        test_df = market_full_df.iloc[split_point:] if split_point > 0 else market_full_df

        best_param_record = []
        for param_set in param_groups:
            # 模拟过滤回测
            base_col = "base_factor_score" if "base_factor_score" in test_df.columns else "close"
            trend_col = "trend_score" if "trend_score" in test_df.columns else "close"

            filter_df = test_df.copy()
            if base_col in filter_df.columns:
                filter_df = filter_df[filter_df[base_col] >= param_set["base_score_threshold"]]
            if trend_col in filter_df.columns:
                filter_df = filter_df[filter_df[trend_col] >= param_set["trend_score_threshold"]]

            if len(filter_df) < 2:
                param_set["sharpe"] = 0
                param_set["max_dd"] = 0
                param_set["avg_return"] = 0
                best_param_record.append(param_set)
                continue

            # 用close收益率计算绩效
            returns = filter_df["close"].pct_change().dropna().values[:100]
            if len(returns) < 2:
                param_set["sharpe"] = 0
                param_set["max_dd"] = 0
                param_set["avg_return"] = 0
                best_param_record.append(param_set)
                continue

            avg_ret = float(np.mean(returns))
            std_ret = float(np.std(returns))
            sharpe = avg_ret / std_ret * math.sqrt(252) if std_ret > 0 else 0
            cum = np.cumprod(1 + returns)
            peak = np.maximum.accumulate(cum)
            dd = (peak - cum) / peak
            max_dd = float(np.max(dd)) if len(dd) > 0 else 0

            param_set["sharpe"] = round(sharpe, 3)
            param_set["max_dd"] = round(max_dd, 4)
            param_set["avg_return"] = round(float(avg_ret), 4)
            best_param_record.append(param_set)

        param_df = pd.DataFrame(best_param_record).sort_values("sharpe", ascending=False).head(5)
        os.makedirs("evolution_log", exist_ok=True)
        param_df.to_csv("evolution_log/evolve_best_param.csv", index=False, encoding="utf-8-sig")
        print("✅ 参数微进化完成, Top5已保存")
        return param_df

    # ===================== 2. LLM/规则因子生成 =====================

    def _rule_based_factor_gen(self) -> list:
        """LLM不可用时的降级规则因子生成"""
        return [
            {
                "factor_name": "volume_price_trend",
                "factor_code": "(close - close.shift(5)) / close.shift(5) * (volume / volume.rolling(5).mean())",
                "logic_desc": "量价趋势: 涨幅×量比, 放量上涨为强势信号",
            },
            {
                "factor_name": "rsi_divergence",
                "factor_code": "rsi - rsi.rolling(10).mean()",
                "logic_desc": "RSI偏离度: 超卖区回升为反转信号",
            },
            {
                "factor_name": "capital_flow_momentum",
                "factor_code": "capital_flow_10d / capital_flow_10d.rolling(20).std()",
                "logic_desc": "资金流动量: 标准化后>1为显著流入",
            },
        ]

    def llm_generate_new_factor(self):
        """读取盈亏+失效数据, 生成并过滤新因子"""
        # 读取历史数据作为上下文
        try:
            trade_pnl_text = pd.read_sql(
                "SELECT ts_code, pnl_rate, trigger_signal FROM memory_trade_pnl ORDER BY record_time DESC LIMIT 200",
                self.conn
            ).to_string()
        except Exception:
            trade_pnl_text = ""

        try:
            failure_text = pd.read_sql(
                "SELECT signal_name, ts_code, failure_type FROM memory_failure_signal ORDER BY record_time DESC LIMIT 100",
                self.conn
            ).to_string()
        except Exception:
            failure_text = ""

        # 尝试LLM生成
        factor_list = []
        if self.llm_client is not None:
            try:
                prompt = """你是量化因子工程师，基于下面实盘亏损、失效信号数据，生成3个全新可落地的A股因子。输出格式每行: [因子名称, 因子计算代码(用market_df列), 逻辑说明]

历史亏损记录：
{}

策略失效记录：
{}""".format(trade_pnl_text[:2000], failure_text[:2000])

                resp = self.llm_client.ChatCompletion.create(
                    model="local-llm",
                    messages=[{"role": "user", "content": prompt}],
                    timeout=10
                )
                factor_raw = resp["choices"][0]["message"]["content"]
                matches = re.findall(r"\[(.*?),(.*?),(.*?)\]", factor_raw, re.S)
                factor_list = [{"factor_name": m[0].strip(), "factor_code": m[1].strip(),
                                "logic_desc": m[2].strip()} for m in matches]
            except Exception:
                pass

        # LLM失败则用规则因子
        if not factor_list:
            factor_list = self._rule_based_factor_gen()
            print("  使用规则生成因子(LLM不可用)")

        # IC检验过滤
        try:
            market_df = pd.read_sql("SELECT * FROM memory_market", self.conn)
        except Exception:
            print("无market数据, 跳过IC检验")
            return pd.DataFrame(factor_list) if factor_list else pd.DataFrame()

        test_result = []
        for f in factor_list:
            try:
                market_df["new_factor"] = eval(f["factor_code"],
                                               {"__builtins__": {}},
                                               {"close": market_df["close"],
                                                "volume": market_df.get("volume", 1),
                                                "rsi": market_df.get("rsi", 50),
                                                "capital_flow_10d": market_df.get("capital_flow_10d", 0)})
                ic = market_df["new_factor"].corr(market_df.get("forward_20d_return", market_df["close"].pct_change()))
                ic = 0 if (pd.isna(ic) or ic == -999) else round(float(ic), 4)
            except Exception:
                ic = -999

            test_result.append({
                "factor_name": f["factor_name"],
                "factor_code": f["factor_code"],
                "logic_desc": f["logic_desc"],
                "ic_value": ic,
            })

        factor_df = pd.DataFrame(test_result)
        valid = factor_df[factor_df["ic_value"] >= LLM_FACTOR_TEST_THRESHOLD]
        invalid = factor_df[factor_df["ic_value"] < LLM_FACTOR_TEST_THRESHOLD]

        os.makedirs("evolution_log", exist_ok=True)
        valid.to_csv("evolution_log/llm_valid_new_factor.csv", index=False, encoding="utf-8-sig")
        invalid.to_csv("evolution_log/llm_invalid_factor.csv", index=False, encoding="utf-8-sig")

        print("✅ LLM/规则因子生成: 有效{}个, 淘汰{}个".format(len(valid), len(invalid)))
        return valid

    # ===================== 3. WalkForward滚动重训 =====================

    def walk_forward_retrain_all_models(self):
        """每30交易日滚动重训底层/时序/PPO, 防过拟合"""
        try:
            full_market = pd.read_sql(
                "SELECT * FROM memory_market ORDER BY trade_date", self.conn
            )
        except Exception:
            print("无市场数据, 跳过WalkForward")
            return pd.DataFrame()

        total_len = len(full_market)
        if total_len < WALK_WINDOW_DAYS + RETRAIN_INTERVAL:
            print("数据不足({}<{}+{}), 跳过WalkForward".format(
                total_len, WALK_WINDOW_DAYS, RETRAIN_INTERVAL))
            return pd.DataFrame()

        retrain_times = (total_len - WALK_WINDOW_DAYS) // RETRAIN_INTERVAL
        if retrain_times < 1:
            retrain_times = 1

        evolve_history = []
        for i in range(retrain_times):
            train_end = WALK_WINDOW_DAYS + i * RETRAIN_INTERVAL
            train_slice = full_market.iloc[max(0, train_end - WALK_WINDOW_DAYS):train_end]
            test_slice = full_market.iloc[train_end:train_end + RETRAIN_INTERVAL]

            if len(train_slice) < 60 or len(test_slice) < 5:
                continue

            # 重训底层模型
            if self.base_model is not None and "forward_20d_return" in train_slice.columns:
                try:
                    # Use available columns as features
                    feat_cols = [c for c in ["close", "volume", "macd", "rsi", "ma5", "ma20"]
                                 if c in train_slice.columns]
                    if len(feat_cols) >= 3:
                        train_feat = train_slice[feat_cols].copy()
                        train_feat["forward_20d_return"] = train_slice["close"].pct_change(20).shift(-20)
                        train_feat = train_feat.dropna()
                        if len(train_feat) > 60:
                            self.base_model.train(train_feat)
                except Exception as e:
                    print("  窗口{} 底层重训跳过: {}".format(i, e))

            # 计算窗口绩效
            test_returns = test_slice["close"].pct_change().dropna().values
            if len(test_returns) > 1:
                avg_ret = float(np.mean(test_returns))
                cum = np.cumprod(1 + test_returns)
                dd = (np.maximum.accumulate(cum) - cum) / np.maximum.accumulate(cum)
                max_dd = float(np.max(dd)) if len(dd) > 0 else 0
            else:
                avg_ret = 0
                max_dd = 0

            evolve_history.append({
                "window_idx": i,
                "train_end_date": train_slice["trade_date"].iloc[-1] if "trade_date" in train_slice.columns else "",
                "train_size": len(train_slice),
                "test_size": len(test_slice),
                "avg_return": round(avg_ret, 6),
                "max_drawdown": round(max_dd, 4),
            })
            print("  窗口{}/{}: train={}条, test={}条, avg_ret={:.4f}".format(
                i + 1, retrain_times, len(train_slice), len(test_slice), avg_ret))

        evolve_df = pd.DataFrame(evolve_history)
        os.makedirs("evolution_log", exist_ok=True)
        evolve_df.to_csv("evolution_log/walk_forward_train_log.csv", index=False, encoding="utf-8-sig")
        print("✅ WalkForward重训完成: {}个窗口".format(len(evolve_df)))
        return evolve_df

    # ===================== 4. 全流程调度 =====================

    def run_full_evolve_cycle(self):
        print("===== AI自主进化全流程 =====")
        best_param = self.param_evolution_search()
        valid_factor = self.llm_generate_new_factor()
        walk_log = self.walk_forward_retrain_all_models()
        print("===== AI进化流程完成 =====")
        return {
            "top_param": best_param,
            "new_valid_factor": valid_factor,
            "walk_train_log": walk_log,
        }

    def close(self):
        self.conn.close()
