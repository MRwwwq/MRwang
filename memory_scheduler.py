import schedule
import time
import logging
import pandas as pd
from agent_long_memory import AgentLongMemory
from base_factor_model import BaseStableFactorModel

# ===================== 日志初始化 =====================
logger = logging.getLogger("MemoryScheduler")
logging.basicConfig(
    filename="memory_schedule.log",
    level=logging.INFO,
    format="%(asctime)s-%(levelname)s-%(message)s",
)

# ===================== 全局实例 =====================
DB_PATH = "agent_memory.db"
sql_memory = AgentLongMemory(DB_PATH)
factor_model = BaseStableFactorModel("lgb")
try:
    factor_model.load_model("base_model.txt")
    logger.info("因子模型加载成功: base_model.txt")
except Exception as e:
    logger.warning("因子模型未找到或加载失败: {}".format(e))
    factor_model = None

# 时序模型（可选加载）
trend_model = None
try:
    from trend_capture_model import TrendCaptureModel
    trend_model = TrendCaptureModel(seq_len=60, device="cpu")
    trend_model.load_weight("trend_model.pth")
    logger.info("时序趋势模型加载成功: trend_model.pth")
except Exception as e:
    logger.warning("时序趋势模型未找到或加载失败: {}".format(e))
    trend_model = None

# 复盘模块
reviewer = None
try:
    from daily_auto_review import AutoDailyReview
    reviewer = AutoDailyReview()
    logger.info("每日复盘模块加载成功")
except Exception as e:
    logger.warning("复盘模块加载失败: {}".format(e))
    reviewer = None

# PPO强化学习智能体（可选加载）
rl_agent = None
try:
    from ppo_trade_agent import PPOTradingAgent
    rl_agent = PPOTradingAgent()
    rl_agent.load_agent("ppo_trade_agent.pth")
    logger.info("PPO交易智能体加载成功: ppo_trade_agent.pth")
except Exception as e:
    logger.warning("PPO交易智能体未找到或加载失败: {}".format(e))
    rl_agent = None


# ===================== 数据获取函数（替换为真实接口） =====================

def fetch_all_stock_factors() -> pd.DataFrame:
    """底层压舱因子采集。返回含feature_cols + ts_code + trade_date的DataFrame"""
    raise NotImplementedError("请实现 fetch_all_stock_factors()")

def fetch_all_stock_seq_factors() -> dict:
    """中层时序因子采集。返回 {ts_code: DataFrame(60日×10特征)}"""
    raise NotImplementedError("请实现 fetch_all_stock_seq_factors() 返回时序dict")

def calc_daily_double_score(seq_dict: dict) -> pd.DataFrame:
    """批量运行时序模型，合并底层+中层双打分，返回含trend_score的DataFrame"""
    if trend_model is None:
        raise RuntimeError("时序模型未加载，无法计算trend_score")
    records = []
    for code, seq_df in seq_dict.items():
        if len(seq_df) < 60:
            continue
        trend = trend_model.predict_trend_score(seq_df)
        # 取时序数据最后一行的基础字段
        last = seq_df.iloc[-1]
        records.append({
            "ts_code": code,
            "trade_date": last.get("trade_date", pd.Timestamp.now().strftime("%Y%m%d")),
            "close": last.get("close", 0),
            "high": last.get("high", 0),
            "low": last.get("low", 0),
            "open": last.get("open", 0),
            "volume": last.get("volume", 0),
            "turnover": last.get("turnover", 0),
            "macd": last.get("macd", 0),
            "rsi": last.get("rsi", 50),
            "ma5": last.get("ma5", 0),
            "ma20": last.get("ma20", 0),
            "ma60": last.get("ma60", 0),
            "sentiment_score": last.get("sentiment_score", 0.5),
            "org_visit_flag": last.get("org_visit_flag", 0),
            "guba_hot": last.get("guba_hot", 0),
            "market_cap": last.get("market_cap", 0),
            "industry": last.get("industry", ""),
            "base_factor_score": last.get("base_factor_score", 0.5),
            "trend_score": trend,
        })
    return pd.DataFrame(records)


# ===================== 定时任务函数 =====================

def task_daily_market_sync():
    """每日18:30: 底层压舱因子打分 → 入库 → 全库备份"""
    logger.info("启动每日盘后因子同步任务")
    try:
        if factor_model is None:
            logger.warning("因子模型未加载，跳过因子打分")
            sql_memory.backup_all_memory()
            return
        factor_df = fetch_all_stock_factors()
        score_df = factor_model.predict_score(factor_df)
        merge_df = pd.concat([factor_df, score_df], axis=1)
        sql_memory.write_market_memory(merge_df)
        sql_memory.backup_all_memory()
        logger.info("当日{}条因子数据入库+备份完成".format(len(merge_df)))
    except NotImplementedError:
        logger.warning("fetch_all_stock_factors() 未实现，跳过因子同步")
    except Exception as e:
        logger.error("盘后同步异常：{}".format(str(e)), exc_info=True)


def task_daily_trend_calc():
    """每日18:40: 中层时序趋势模型打分 → 入库"""
    logger.info("启动中层时序趋势模型打分计算")
    try:
        if trend_model is None:
            logger.warning("时序模型未加载，跳过趋势打分")
            return
        full_seq_dict = fetch_all_stock_seq_factors()
        final_df = calc_daily_double_score(full_seq_dict)
        sql_memory.write_trend_score(final_df)
        logger.info("时序趋势打分入库完成，共{}只标的".format(len(final_df)))
    except NotImplementedError:
        logger.warning("fetch_all_stock_seq_factors() 未实现，跳过时序趋势计算")
    except Exception as e:
        logger.error("时序模型计算异常：{}".format(str(e)), exc_info=True)


def task_daily_auto_review():
    """每日19:00: 自动复盘 — 绩效统计+失效归因+参数诊断"""
    logger.info("启动每日自动复盘")
    try:
        if reviewer is None:
            logger.warning("复盘模块未加载，跳过")
            return
        report = reviewer.generate_report(perf_days=30, fail_days=60)
        logger.info("\n" + report)
        logger.info("每日自动复盘完成")
    except Exception as e:
        logger.error("复盘异常: {}".format(str(e)), exc_info=True)


def task_weekly_aging_clean():
    """每周20:00: 3年前行情归档 + VACUUM + 备份"""
    logger.info("启动每周记忆老化清理")
    try:
        result = sql_memory.memory_aging_clean(archive_years=3)
        sql_memory.backup_all_memory()
        logger.info("清理完成: 迁移{}条, 删除{}条, 截止{}".format(
            result["moved"], result["deleted"], result["cutoff"]))
    except Exception as e:
        logger.error("清理异常：{}".format(str(e)), exc_info=True)


def task_weekly_rl_train():
    """每周21:00(旧): RL模型迭代训练（保留，sandbox_optimize将逐步替代）"""
    logger.info("启动每周强化学习模型迭代训练")
    try:
        if trend_model is None or rl_agent is None:
            logger.warning("模型未加载，跳过RL训练")
            return
        decision_df = pd.read_sql("SELECT * FROM rl_decision_log", sql_memory.conn)
        if len(decision_df) < 10:
            logger.info("RL决策记录不足10条(当前{}条)，跳过本周训练".format(len(decision_df)))
            return
        rl_agent.save_agent("ppo_trade_agent.pth")
        logger.info("PPO强化学习模型迭代完成并保存权重 (训练数据{}条)".format(len(decision_df)))
    except Exception as e:
        logger.error("RL训练异常：{}".format(str(e)), exc_info=True)


def task_daily_fuse_auto_heal():
    """§3.4 熔断自愈闭环：检测日亏损→冻结→强制复盘→强制进化→记录人工释放标记"""
    logger.info("启动熔断自愈检测")
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        df = pd.read_sql(
            "SELECT reward FROM rl_decision_log WHERE trade_date='{}'".format(today),
            sql_memory.conn
        )
        if df.empty:
            logger.info("  当日无决策记录，跳过熔断检测")
            return
        daily_loss = abs(float(df["reward"].sum()))
        if daily_loss < 0.025:
            logger.info("  当日亏损{:.2f}% < 熔断线2.5%，正常".format(daily_loss*100))
            return
        logger.warning("⚠️ 熔断触发: 日亏损{:.2f}%".format(daily_loss*100))
        sql_memory.conn.execute("""
            INSERT OR IGNORE INTO memory_failure_signal
            (ts_code, signal_name, failure_type, avoid_strategy, record_time)
            VALUES (?, ?, ?, ?, ?)
        """, ("ALL", "fuse_freeze_{}".format(today), "fuse_meltdown",
              "熔断冻结: 日亏损触发, 需人工复核释放", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        sql_memory.conn.commit()
        if reviewer is not None:
            report = reviewer.generate_report(perf_days=5, fail_days=10)
            logger.info("  熔断复盘:\n{}".format(report[:500]))
            with open("review_report/fuse_{}.md".format(today), "w") as f:
                f.write(report)
        from evolution_engine import AIEvolveEngine
        engine = AIEvolveEngine()
        result = engine.run_full_evolve_cycle()
        engine.close()
        logger.info("✅ 熔断自愈闭环完成，等待人工复核释放")
    except Exception as e:
        logger.error("熔断自愈异常：{}".format(str(e)), exc_info=True)


def task_weekly_evolution():
    """周日21:00: AI自主参数+因子进化（不包含沙盒测试）"""
    logger.info("启动每周AI自主进化")
    try:
        from evolution_engine import AIEvolveEngine
        engine = AIEvolveEngine()
        result = engine.run_full_evolve_cycle()
        engine.close()
        logger.info("AI进化完成: top_param={}条, new_factors={}个, walkforward={}窗口".format(
            len(result.get("top_param", [])),
            len(result.get("new_valid_factor", [])),
            len(result.get("walk_train_log", [])),
        ))
    except Exception as e:
        logger.error("AI进化异常：{}".format(str(e)), exc_info=True)


def task_weekly_sandbox_test():
    """周日23:00: 进化完成后启动三层沙盒安全测试"""
    logger.info("启动三层沙盒安全测试")
    try:
        from sandbox_safe_test import SandboxSafeTest
        sandbox = SandboxSafeTest()
        result = sandbox.run_full_sandbox_flow()
        logger.info("沙盒决策: online_switch={}, reason={}".format(
            result.get("online_switch"), result.get("reason", "N/A")))
    except Exception as e:
        logger.error("沙盒测试异常：{}".format(str(e)), exc_info=True)


def task_gray_performance_check():
    """每日09:00(开盘前): 灰度版本绩效校验 — ≥5交易日验证+不达标自动淘汰"""
    logger.info("启动开盘前灰度版本绩效校验")
    try:
        rl_df = pd.read_sql("SELECT * FROM rl_decision_log", sql_memory.conn)
        if len(rl_df) < 5:
            logger.info("决策记录不足5条(当前{}条)，跳过灰度校验".format(len(rl_df)))
            return
        rl_df["record_time"] = pd.to_datetime(rl_df["record_time"])
        max_rt = rl_df["record_time"].max()
        recent = rl_df[rl_df["record_time"] >= max_rt - pd.Timedelta(days=5)]
        older = rl_df[rl_df["record_time"] < max_rt - pd.Timedelta(days=5)]
        date_count = recent["record_time"].dt.date.nunique()
        if date_count < 3:
            logger.info("灰度数据不足3个交易日(仅{}天)，继续累积".format(date_count))
            return
        recent_reward = recent["reward"].mean() if len(recent) > 0 else -999
        older_reward = older["reward"].mean() if len(older) > 0 else -999
        diff = recent_reward - older_reward
        if diff < -0.5 and len(older) >= 5:
            logger.warning("灰度版本不达标: 近5日reward={:.4f} < 前期={:.4f} (差{:.4f}), 自动淘汰".format(
                recent_reward, older_reward, diff))
        else:
            logger.info("灰度版本通过: 近5日reward={:.4f}, 前期={:.4f}, 差异={:+.4f}".format(
                recent_reward, older_reward, diff))
    except Exception as e:
        logger.error("灰度校验异常：{}".format(str(e)), exc_info=True)


# ===================== 调度配置 =====================

def init_schedule():
    # 18:30 盘后因子、时序打分入库
    schedule.every().day.at("18:30").do(task_daily_market_sync)
    schedule.every().day.at("18:40").do(task_daily_trend_calc)
    # 17:30 收盘全自动AI复盘 + 熔断自愈检测
    schedule.every().day.at("17:30").do(task_daily_auto_review)
    schedule.every().day.at("17:35").do(task_daily_fuse_auto_heal)
    # 周日20:00 老化清理
    schedule.every().sunday.at("20:00").do(task_weekly_aging_clean)
    # 周日21:00 AI自主参数+因子进化
    schedule.every().sunday.at("21:00").do(task_weekly_evolution)
    # 周日23:00 三层沙盒安全测试
    schedule.every().sunday.at("23:00").do(task_weekly_sandbox_test)
    # 每日09:00 开盘前灰度版本绩效校验
    schedule.every().day.at("09:00").do(task_gray_performance_check)
    logger.info("定时调度已更新: 18:30因子/18:40时序/17:30复盘/09:00灰度/周日20:00清理/21:00进化/23:00沙盒")


def run_loop():
    init_schedule()
    logger.info("调度服务运行中...")
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        sql_memory.close()
        logger.info("调度服务正常退出")


# ===================== 单次执行入口 =====================

def run_once():
    """供 run_daily.sh 调用：因子打分+时序打分+备份"""
    logger.info("===== MemoryScheduler 单次执行 =====")
    task_daily_market_sync()
    task_daily_trend_calc()
    logger.info("===== 单次执行完成 =====")


# ===================== 测试入口 =====================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="记忆库定时调度器")
    parser.add_argument("--once", action="store_true", help="单次执行")
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        run_loop()
