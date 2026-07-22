import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import math
import os


class AutoDailyReview:
    """每日自动复盘 — 绩效统计+失效归因+沙盒任务生成"""

    def __init__(self, db_path="agent_memory.db", report_save_dir="review_report/"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.report_dir = report_save_dir
        os.makedirs(self.report_dir, exist_ok=True)
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.report_path = "{}/review_{}.md".format(self.report_dir, self.today)
        self.review_window = 30

    # 读取近30日实盘交易数据
    def load_trade_data(self):
        cutoff = (datetime.now() - timedelta(days=self.review_window)).strftime("%Y-%m-%d")
        sql = "SELECT * FROM memory_trade_pnl WHERE exit_date >= '{}'".format(cutoff)
        return pd.read_sql(sql, self.conn)

    # 读取失效信号、黑天鹅、全市场行业行情
    def load_risk_data(self):
        fail_df = pd.read_sql("SELECT * FROM memory_failure_signal", self.conn)
        swan_df = pd.read_sql("SELECT * FROM memory_black_swan", self.conn)
        industry_df = pd.read_sql("SELECT DISTINCT industry, trade_date FROM memory_market", self.conn)
        return fail_df, swan_df, industry_df

    # 自动计算全套账户绩效指标
    def calc_performance(self, trade_df):
        if trade_df.empty:
            return None

        total_pnl = trade_df["total_pnl"].sum()
        win = trade_df[trade_df["pnl_rate"] > 0]
        lose = trade_df[trade_df["pnl_rate"] <= 0]
        win_rate = len(win) / len(trade_df) if len(trade_df) > 0 else 0
        avg_win = win["pnl_rate"].mean() if len(win) > 0 else 0
        avg_loss = lose["pnl_rate"].mean() if len(lose) > 0 else 0
        pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 999

        # 最大回撤(按时间排序的累计收益)
        sorted_df = trade_df.sort_values("exit_date")
        cum = sorted_df["total_pnl"].cumsum()
        running_max = cum.cummax()
        dd_series = (cum - running_max) / running_max.replace(0, float("nan"))
        max_dd = dd_series.min()
        max_dd = 0 if (pd.isna(max_dd) or math.isinf(max_dd)) else max_dd

        # 夏普(按日期聚合)
        daily_ret = trade_df.groupby("exit_date")["pnl_rate"].mean()
        if len(daily_ret) > 1:
            excess = daily_ret.mean() - 0.0001
            sharpe = excess / daily_ret.std() * math.sqrt(252)
        else:
            sharpe = 0

        # 分信号统计
        signal_stat = trade_df.groupby("trigger_signal").agg(
            total_cnt=("trade_id", "count"),
            win_cnt=("pnl_rate", lambda x: float((x > 0).sum())),
            avg_pnl=("pnl_rate", "mean")
        ).reset_index()
        signal_stat["win_rate"] = signal_stat["win_cnt"] / signal_stat["total_cnt"]

        # 分个股统计
        stock_stat = trade_df.groupby("ts_code").agg(
            total_pnl=("total_pnl", "sum"),
            count=("trade_id", "count")
        ).sort_values("total_pnl")

        return {
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 4),
            "profit_loss_ratio": round(pl_ratio, 3),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 3),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "signal_stat": signal_stat,
            "stock_stat": stock_stat,
        }

    # AI自动失效归因，输出优化建议
    def auto_failure_attribution(self, fail_df, swan_df, industry_df):
        attr_list = []
        swan_industry_set = set(swan_df["affected_industry"].dropna().tolist())
        total_rows = len(industry_df) if len(industry_df) > 0 else 1

        for _, row in fail_df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            market_env = str(row.get("market_feature", ""))
            condition = str(row.get("trigger_condition", ""))
            dd = row.get("max_drawdown", 0)

            attr_type = ""
            suggest = ""

            # 1. 黑天鹅/突发利空
            if any(ind in market_env for ind in swan_industry_set):
                attr_type = "突发行业黑天鹅/个股利空"
                suggest = "沙盒增加行业黑名单拦截，限制风险行业总持仓上限"

            # 2. 因子/时序打分失效
            elif "base_factor" in condition or "trend_score" in condition:
                attr_type = "底层因子/中层时序模型失效"
                suggest = "沙盒重训练XGB/LGB、调整LSTM时序窗口、更新注意力权重"

            # 3. 市场风格单一化
            elif len(industry_df[industry_df["industry"] == market_env]) / total_rows > 0.6:
                attr_type = "市场风格极端切换，持仓行业集中"
                suggest = "PPO奖励函数增加行业分散惩罚，均衡行业配置"

            # 4. 流动性不足
            elif "volume" in condition:
                try:
                    vol_val = float([v for v in condition.split(",") if "volume" in v][0].split(":")[-1])
                    if vol_val < 50000:
                        attr_type = "个股流动性不足，滑点亏损"
                        suggest = "新增日均成交额硬性过滤门槛，剔除低流动性小票"
                    else:
                        attr_type = "参数阈值不合理"
                        suggest = "沙盒网格搜索最优打分开仓阈值、止盈止损区间"
                except Exception:
                    attr_type = "参数阈值不合理"
                    suggest = "沙盒网格搜索最优打分开仓阈值、止盈止损区间"
            else:
                attr_type = "策略开仓/风控参数阈值不适配当前市场"
                suggest = "沙盒网格搜索最优打分开仓阈值、止盈止损区间"

            attr_list.append({
                "signal_name": row["signal_name"],
                "ts_code": ts_code,
                "max_drawdown": round(dd, 4),
                "failure_category": attr_type,
                "optimize_todo": suggest,
            })

        return pd.DataFrame(attr_list) if attr_list else pd.DataFrame(
            columns=["signal_name", "ts_code", "max_drawdown", "failure_category", "optimize_todo"]
        )

    # 生成Markdown复盘报告
    def build_report(self, perf_data, attr_df):
        lines = []
        lines.append("# 每日全自动量化复盘报告 {}".format(self.today))
        lines.append("复盘周期：近{}个交易日\n".format(self.review_window))

        lines.append("## 一、账户核心绩效\n")
        lines.append("|指标|数值|")
        lines.append("|----|----|")
        lines.append("|累计总盈亏|{}|".format(perf_data["total_pnl"]))
        lines.append("|整体胜率|{:.2%}|".format(perf_data["win_rate"]))
        lines.append("|盈亏比|{}|".format(perf_data["profit_loss_ratio"]))
        lines.append("|最大回撤|{:.2%}|".format(perf_data["max_drawdown"]))
        lines.append("|年化夏普比率|{}|".format(perf_data["sharpe"]))
        lines.append("|平均盈利单|{:.2%}|".format(perf_data["avg_win"]))
        lines.append("|平均亏损单|{:.2%}|".format(perf_data["avg_loss"]))

        lines.append("\n## 二、各选股信号盈亏统计\n")
        lines.append(perf_data["signal_stat"].to_markdown(index=False))

        lines.append("\n\n## 三、个股盈亏排行（亏损靠前为收益拖累标的）\n")
        lines.append(perf_data["stock_stat"].to_markdown(index=False))

        lines.append("\n\n## 四、AI失效亏损归因分析\n")
        lines.append(attr_df.to_markdown(index=False) if len(attr_df) > 0 else "无失效信号")

        lines.append("\n\n## 五、自动生成沙盒优化任务清单\n")
        todo_unique = attr_df["optimize_todo"].drop_duplicates().tolist() if len(attr_df) > 0 else []
        if todo_unique:
            for i, task in enumerate(todo_unique):
                lines.append("{}. {}".format(i + 1, task))
        else:
            lines.append("无优化任务（策略运行正常）")

        md = "\n".join(lines)
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(md)
        return self.report_path

    # 复盘主入口，定时自动调用
    def run_full_review_task(self):
        print("【{}】启动全自动收盘复盘".format(self.today))
        trade_df = self.load_trade_data()
        if trade_df.empty:
            print("无近期交易数据，复盘终止")
            return None
        fail_df, swan_df, industry_df = self.load_risk_data()
        perf = self.calc_performance(trade_df)
        attr_result = self.auto_failure_attribution(fail_df, swan_df, industry_df)
        report_path = self.build_report(perf, attr_result)

        # ====== 约束5: 失效即时拦截 — 高风险信号自动加入黑名单 ======
        self._auto_block_high_risk_signals(attr_result)

        # ====== 约束3: 记录本轮应迭代的层级（因子→时序→RL轮换） ======
        self._record_iteration_layer(attr_result)

        print("复盘完成，报告输出路径：{}".format(report_path))
        self.conn.close()
        return perf, attr_result, report_path

    # ====== 约束5: 高风险失效信号→即时加入黑名单 ======
    def _auto_block_high_risk_signals(self, attr_df):
        if attr_df.empty or len(attr_df) == 0:
            return
        high_risk = attr_df[attr_df["max_drawdown"] > 0.08]  # 回撤>8%=高风险
        for _, row in high_risk.iterrows():
            rule = {
                "rule_id": "auto_block_{}_{}".format(row["ts_code"], self.today),
                "rule_content": "复盘自动封禁: {} 信号{} 回撤{:.2%} 归因:{}".format(
                    row["ts_code"], row["signal_name"], row["max_drawdown"], row["failure_category"]),
                "risk_level": 10,
                "record_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO global_rule VALUES (?, ?, ?, ?)",
                    (rule["rule_id"], rule["rule_content"], rule["risk_level"], rule["record_time"])
                )
                self.conn.commit()
                print("  ⛔ 自动封禁: {}信号{} (回撤{:.2%})".format(row["ts_code"], row["signal_name"], row["max_drawdown"]))
            except Exception:
                pass

    # ====== 约束3: 记录迭代层级轮换 ======
    def _record_iteration_layer(self, attr_df):
        """轮换选择本次应优化的层级: 因子→时序→RL, 单次仅一层"""
        layer_order = ["factor_model", "trend_model", "rl_agent"]
        # 读取上次迭代的记录
        try:
            last_layer = pd.read_sql(
                "SELECT rule_content FROM global_rule WHERE rule_id='iteration_layer'",
                self.conn
            )
            if len(last_layer) > 0:
                last = str(last_layer.iloc[0]["rule_content"])
                idx = (layer_order.index(last) + 1) % 3 if last in layer_order else 0
            else:
                idx = 0
        except Exception:
            idx = 0
        current_layer = layer_order[idx]
        # 写入轮换记录
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO global_rule VALUES (?, ?, ?, ?)",
                ("iteration_layer", current_layer, 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.conn.commit()
            print("  迭代层级: {} (轮换顺序: 因子→时序→RL)".format(current_layer))
        except Exception:
            pass
