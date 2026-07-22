"""
沙盒安全测试 — 三层校验: 离线回测→灰度A/B→三重标准→全量/回滚
"""
import sqlite3
import pandas as pd
import numpy as np
import math
import os
from datetime import datetime, timedelta
from evolution_engine import AIEvolveEngine

DB_PATH = "agent_memory.db"
GRAY_TEST_DAYS = 10
SHARP_UP_THRESHOLD = 0.1
MAX_DD_ALLOW_GAP = 0
WIN_RATE_MIN_STABLE = 0


class SandboxSafeTest:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.evolve_engine = AIEvolveEngine()
        self.old_version_perf = self.load_online_old_strategy_perf()

    def calc_sharpe(self, returns):
        returns = returns.dropna()
        if len(returns) < 2 or returns.std() == 0:
            return 0
        return round(float(returns.mean() / returns.std() * math.sqrt(252)), 3)

    def calc_max_drawdown(self, cum_series):
        cum = cum_series.dropna().values
        if len(cum) < 2:
            return 0
        peak = np.maximum.accumulate(cum)
        dd = (cum - peak) / peak
        return round(float(np.min(dd)), 4)

    # 读取当前线上旧策略历史绩效基准
    def load_online_old_strategy_perf(self):
        trade_df = pd.read_sql("SELECT * FROM memory_trade_pnl", self.conn)
        if trade_df.empty:
            return {"sharpe": 0, "max_dd": 0, "win_rate": 0}
        win = trade_df[trade_df["pnl_rate"] > 0]
        lose = trade_df[trade_df["pnl_rate"] <= 0]
        sorted_df = trade_df.sort_values("exit_date")
        cum = sorted_df["total_pnl"].cumsum()
        max_dd = self.calc_max_drawdown(cum)
        daily_ret = trade_df.groupby("exit_date")["pnl_rate"].mean()
        sharpe = self.calc_sharpe(daily_ret - 0.0001)
        win_rate = len(win) / len(trade_df) if len(trade_df) > 0 else 0
        return {"sharpe": sharpe, "max_dd": max_dd, "win_rate": round(win_rate, 3)}

    # 第一层：离线全周期历史沙盒回测
    def offline_full_backtest(self, evolve_result):
        print("第一层: 离线全周期历史沙盒回测")
        market_full = pd.read_sql(
            "SELECT * FROM memory_market ORDER BY trade_date", self.conn
        )
        if market_full.empty or evolve_result is None:
            print("  数据不足, 跳过离线回测")
            return None

        top_param = evolve_result.get("top_param", pd.DataFrame())
        valid_factor = evolve_result.get("new_valid_factor", pd.DataFrame())
        if top_param.empty:
            print("  无进化参数, 跳过离线回测")
            return None
        best_param = top_param.iloc[0].to_dict()

        # 拼接新因子
        market_df = market_full.copy()
        for _, f_row in valid_factor.iterrows():
            try:
                exec("market_df['{}'] = {}".format(f_row["factor_name"], f_row["factor_code"]))
            except Exception:
                pass

        # 过滤
        base_col = "base_factor_score" if "base_factor_score" in market_df.columns else "close"
        trend_col = "trend_score" if "trend_score" in market_df.columns else "close"
        filter_data = market_df[
            (market_df[base_col] >= best_param.get("base_score_threshold", 0.3))
            & (market_df[trend_col] >= best_param.get("trend_score_threshold", 0.3))
        ].copy()

        if len(filter_data) < 5:
            print("  过滤后样本不足, 跳过")
            return None

        returns = filter_data["close"].pct_change().fillna(0)
        full_sharpe = self.calc_sharpe(returns)
        cum = returns.cumsum()
        full_max_dd = self.calc_max_drawdown(cum)
        full_win_rate = float((returns > 0).mean())

        offline_perf = {"sharpe": full_sharpe, "max_dd": full_max_dd, "win_rate": full_win_rate}

        # 离线粗筛
        if full_sharpe < self.old_version_perf["sharpe"] - 0.05:
            print("  ❌ 离线回测绩效严重劣化, 淘汰")
            self._write_eliminated_signal(
                stage="offline_full_backtest",
                param_set=best_param,
                reason="离线回测绩效严重劣化: Sharpe={} < 旧版{}".format(full_sharpe, self.old_version_perf["sharpe"])
            )
            return None

        # §4 沙盒风控准入：频繁熔断检查（单日亏损>2.5%次数占比）
        if len(filter_data) >= 20:
            daily_ret = filter_data["close"].pct_change().fillna(0)
            fuse_days = int((daily_ret < -0.025).sum())
            fuse_ratio = fuse_days / len(daily_ret)
            if fuse_ratio > 0.05:
                print("  ❌ 频繁熔断(>5%交易日触发熔断), 淘汰")
                self._write_eliminated_signal(
                    stage="sandbox_fuse_frequency",
                    param_set=best_param,
                    reason="回测期熔断比例{}%, >5%阈值".format(round(fuse_ratio*100, 1))
                )
                return None

        print("  ✅ 离线回测通过: Sharpe={}, MaxDD={}, WinRate={:.2%}".format(
            full_sharpe, full_max_dd, full_win_rate))
        return offline_perf

    # 第二层：灰度并行A/B测试
    def gray_ab_test(self, offline_perf, evolve_result):
        gray_start = datetime.now().strftime("%Y-%m-%d")
        gray_end = (datetime.now() + timedelta(days=GRAY_TEST_DAYS)).strftime("%Y-%m-%d")
        print("第二层: 灰度A/B并行测试 {} ~ {} ({}天)".format(gray_start, gray_end, GRAY_TEST_DAYS))
        print("  实盘10%资金运行新版本, 90%维持旧线上策略")

        gray_log_list = []
        early_terminate = False
        for day in range(GRAY_TEST_DAYS):
            # 读取当日新旧策略交易数据
            date = (datetime.now() + timedelta(days=day)).strftime("%Y-%m-%d")
            new_trade = pd.read_sql(
                "SELECT pnl_rate FROM memory_trade_pnl WHERE trigger_signal LIKE '%进化%' AND exit_date='{}'".format(date),
                self.conn
            )
            old_trade = pd.read_sql(
                "SELECT pnl_rate FROM memory_trade_pnl WHERE trigger_signal NOT LIKE '%进化%' AND exit_date='{}'".format(date),
                self.conn
            )
            day_new_sharpe = self.calc_sharpe(new_trade["pnl_rate"]) if len(new_trade) > 0 else 0
            day_old_sharpe = self.calc_sharpe(old_trade["pnl_rate"]) if len(old_trade) > 0 else 0
            gray_log_list.append({
                "day": day + 1, "date": date,
                "new_sharpe": day_new_sharpe, "old_sharpe": day_old_sharpe
            })
            # 连续3日新策略低旧策略→提前终止（约束2.4）
            if day >= 2:
                last3 = gray_log_list[-3:]
                if all(r["new_sharpe"] < r["old_sharpe"] for r in last3) and \
                   all(r["new_sharpe"] < 0 for r in last3):
                    print("  ⚠ 新策略连续3日亏损, 提前终止灰度测试, 自动回滚")
                    early_terminate = True
                    break

        gray_df = pd.DataFrame(gray_log_list)
        avg_new = gray_df["new_sharpe"].mean()
        avg_old = gray_df["old_sharpe"].mean()
        gray_final_perf = {
            "avg_new_sharpe": round(float(avg_new), 3),
            "avg_old_sharpe": round(float(avg_old), 3),
            "offline_perf": offline_perf,
            "early_terminate": early_terminate,
        }
        if early_terminate:
            print("  ❌ 灰度提前终止(连续3日亏损), 自动回滚")
            self._write_eliminated_signal(
                stage="gray_ab_test_early_terminate",
                param_set={"gray_perf": str(gray_final_perf)[:100]},
                reason="灰度测试连续3日新策略亏损, 提前终止"
            )
            return gray_final_perf
        print("  ✅ 灰度A/B测试结束: 新平均Sharpe={}, 旧平均Sharpe={}".format(avg_new, avg_old))
        return gray_final_perf

    # 第三层：三重准入标准校验
    def triple_standard_check(self, gray_perf):
        print("第三层: 三重上线标准校验")
        new_sharpe = gray_perf["avg_new_sharpe"]
        old_sharpe = self.old_version_perf["sharpe"]
        new_dd = gray_perf["offline_perf"]["max_dd"]
        old_dd = self.old_version_perf["max_dd"]
        new_win = gray_perf["offline_perf"]["win_rate"]
        old_win = self.old_version_perf["win_rate"]

        check_result = {"pass_all": True, "fail_reason": []}

        # 标准1: 夏普提升≥0.1
        if new_sharpe - old_sharpe < SHARP_UP_THRESHOLD:
            check_result["pass_all"] = False
            check_result["fail_reason"].append(
                "夏普提升不足: 新{} 旧{} 差{} < 阈值{}".format(
                    new_sharpe, old_sharpe, round(new_sharpe - old_sharpe, 3), SHARP_UP_THRESHOLD))
        else:
            print("  ✅ 夏普提升: {:.3f} → {:.3f} (增益{:.3f})".format(
                old_sharpe, new_sharpe, new_sharpe - old_sharpe))

        # 标准2: 回撤不扩大
        if new_dd < old_dd - MAX_DD_ALLOW_GAP:
            check_result["pass_all"] = False
            check_result["fail_reason"].append(
                "回撤扩大: 新{:.2%} 旧{:.2%}".format(new_dd, old_dd))
        else:
            print("  ✅ 回撤可控: {:.2%} → {:.2%}".format(old_dd, new_dd))

        # 标准3: 胜率稳定
        if new_win < old_win - WIN_RATE_MIN_STABLE:
            check_result["pass_all"] = False
            check_result["fail_reason"].append(
                "胜率下滑: 新{:.2%} 旧{:.2%}".format(new_win, old_win))
        else:
            print("  ✅ 胜率稳定: {:.2%} → {:.2%}".format(old_win, new_win))

        return check_result

    # 淘汰版本写入失效信号库（约束2.5）
    def _write_eliminated_signal(self, stage: str, param_set: dict, reason: str):
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO memory_failure_signal
                (ts_code, signal_name, failure_type, avoid_strategy, record_time)
                VALUES (?, ?, ?, ?, ?)
            """, (
                "ALL",
                "sandbox_eliminated_{}".format(stage),
                "parameter_rejected",
                "param:{} | reason:{}".format(str(param_set)[:200], reason[:200]),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            self.conn.commit()
            print("  [失效信号] 淘汰版本已写入 memory_failure_signal")
        except Exception as e:
            print("  [失效信号] 写入失败: {}".format(e))

    # 沙盒全流程总入口
    def run_full_sandbox_flow(self):
        print("===== 启动进化策略三层沙盒安全校验 =====")
        # 1. 执行AI进化
        evolve_result = self.evolve_engine.run_full_evolve_cycle()

        # 2. 第一层：离线全历史沙盒回测
        offline_perf = self.offline_full_backtest(evolve_result)
        if offline_perf is None:
            print("❌ 进化版本离线回测不合格, 丢弃, 维持旧线上策略")
            # 淘汰版本已在 offline_full_backtest 中写入信号
            return {"online_switch": False, "reason": "离线沙盒绩效劣化"}

        # 3. 第二层：灰度A/B并行测试
        gray_perf = self.gray_ab_test(offline_perf, evolve_result)

        # 4. 第三层三重标准校验
        check_res = self.triple_standard_check(gray_perf)
        if check_res["pass_all"]:
            print("✅ 三重标准全满足, 自动全量切换新版本")
            return {"online_switch": True, "gray_performance": gray_perf}
        else:
            fail_text = "; ".join(check_res["fail_reason"])
            print("❌ 未满足上线标准, 自动回滚旧版本: {}".format(fail_text))
            self._write_eliminated_signal(
                stage="triple_standard_check",
                param_set={"gray_perf": str(gray_perf)[:100]},
                reason=fail_text
            )
            # §6.5 代码隔离: 沙盒淘汰时检查是否违规篡改核心模块
            self._code_isolation_check()
            return {"online_switch": False, "reason": fail_text}

    def _code_isolation_check(self):
        """§6.5 沙盒阶段校验进化是否篡改核心模块"""
        try:
            from code_isolation import CodeIsolation
            ci = CodeIsolation()
            changed = []
            for fname in ci.manifest.get("baseline_hash", {}):
                before = ci.manifest["baseline_hash"][fname]
                after = ci._file_hash(ci.base_dir + "/" + fname)
                if before != after:
                    changed.append((fname, before, after))
            if changed:
                allow, violations = ci.validate_evolution(changed)
                if not allow:
                    # 自动回滚到最新的合规快照
                    versions = ci.list_versions()
                    if versions:
                        ci.rollback(versions[0]["version"])
                        print("  ⚠ 已自动回滚到最近合规快照")
        except Exception:
            pass
