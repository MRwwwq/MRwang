"""
全栈集成交易管道 — §5强制执行链路（不可调整、不可倒置）
第1层: 静态硬约束拦截 → 第2层: AI动态预判风控仓位修正
第3层: 底层多因子打分筛选 → 第4层: 中层时序趋势打分筛选
第5层: 双层记忆风控校验 → 第6层: PPO强化学习自适应决策下单
"""
import sys; sys.path.insert(0, "/opt/stock_agent")
import pandas as pd
import numpy as np
from base_factor_model import BaseStableFactorModel
from trend_capture_model import TrendCaptureModel
from agent_long_memory import AgentLongMemory
from memory_faiss_stub import TradeVectorMemory
from ppo_trade_agent import PPOTradingAgent
from static_hard_risk_control import StaticHardRiskControl
from dynamic_ai_risk import DynamicAIRiskControl

DB_PATH = "/opt/stock_agent/agent_memory.db"

# ====== 全局模块初始化 ======
sql_memory = AgentLongMemory(db_path=DB_PATH)
vec_memory = TradeVectorMemory(db_path=DB_PATH)
factor_model = BaseStableFactorModel(model_type="lgb")
trend_model = TrendCaptureModel(seq_len=60, device="cpu")
rl_agent = PPOTradingAgent()

for m, p in [(factor_model, "base_model.txt"), (trend_model, "trend_model.pth"), (rl_agent, "ppo_trade_agent.pth")]:
    try:
        if hasattr(m, "load_model"): m.load_model(p)
        elif hasattr(m, "load_weight"): m.load_weight(p)
        elif hasattr(m, "load_agent"): m.load_agent(p)
    except Exception:
        pass


# ====== §5 强制执行链路 ======

def full_integrated_pipeline(stock_row: pd.Series):
    """
    §5固定链路: 静态→动态→因子→时序→记忆→PPO
    输入: stock_row (ts_code, industry, base_factor_score, trend_score,
                     sentiment_score, rsi, macd, trade_date)
    输出: (allow_trade, log_text, decision_record)
    """
    ts_code = stock_row["ts_code"]
    industry = stock_row["industry"]
    base_score = stock_row["base_factor_score"]
    trend_score = stock_row["trend_score"]
    sentiment_score = stock_row["sentiment_score"]
    rsi = stock_row["rsi"]
    macd = stock_row["macd"]

    log_list = []
    allow_trade = True

    # ====== 第1层：静态硬约束拦截（§2） ======
    static_risk = StaticHardRiskControl()
    static_ok, static_log = static_risk.check_all_static_constraint(ts_code, industry, 0.10)
    static_risk.close()
    log_list.append("【第1层·静态硬约束】\n" + static_log)
    if not static_ok:
        return False, "\n".join(log_list), None

    # ====== 第2层：AI动态预判风控仓位修正（§3） ======
    dynamic_risk = DynamicAIRiskControl()
    dynamic_ok, dynamic_log, pos_coeff = dynamic_risk.full_dynamic_risk_check(ts_code, industry)
    dynamic_risk.close()
    log_list.append("【第2层·AI动态预判风控】\n" + dynamic_log)
    if not dynamic_ok or pos_coeff <= 0:
        return False, "\n".join(log_list), None

    # pos_coeff 作为后续所有模型仓位天花板
    log_list.append(f"  → 仓位天花板: {pos_coeff:.4f}")

    # ====== 第3层：底层多因子硬性过滤（§5.3） ======
    if base_score < 0.4:
        return False, "\n".join(log_list) + f"\n❌ 底层打分不足0.4({base_score:.3f})", None
    log_list.append(f"【第3层·底层压舱】base_score={base_score:.4f}")

    # ====== 第4层：中层时序趋势过滤（§5.4） ======
    if trend_score < 0.35:
        return False, "\n".join(log_list) + f"\n❌ 趋势打分不足0.35({trend_score:.3f})", None
    log_list.append(f"【第4层·中层时序】trend_score={trend_score:.4f}")

    # ====== 第5层：双层记忆风控校验（§5.5） ======
    sql_allow, sql_log = sql_memory.pre_open_check(
        {"rsi": rsi, "macd": macd}, industry, sentiment_score,
        "五层风控管道", ts_code, base_score, trend_score
    )
    log_list.append("【第5层·记忆风控校验】\n" + sql_log)
    if not sql_allow:
        return False, "\n".join(log_list), None

    # ====== 市场环境参数（供PPO使用） ======
    try:
        market_df = pd.read_sql(
            "SELECT close FROM memory_market WHERE ts_code='000001' ORDER BY trade_date DESC LIMIT 5",
            sql_memory.conn)
        if len(market_df) >= 2:
            market_chg = float(market_df["close"].iloc[0] / market_df["close"].iloc[1] - 1)
            vol = float(market_df["close"].pct_change().std())
        else:
            market_chg, vol = 0.0, 0.01
    except:
        market_chg, vol = 0.0, 0.01

    # ====== 第6层：PPO强化学习决策（受pos_coeff约束）（§5.6） ======
    state = rl_agent.build_state(
        base_score=base_score, trend_score=trend_score,
        vol=vol, max_drawdown=0.02, hold_pnl=0.01,
        market_chg=market_chg, plate_corr=0.03,
    )
    action, _, _ = rl_agent.get_action(state)
    reward = rl_agent.calculate_reward(0.01, 0.02, market_chg, action)

    # 仓位计算：PPO动作 × 风控天花板
    raw_positions = [0, 0.10, 0.25, 0.35]
    if market_chg > 0.015:
        raw_positions = [0, 0.20, 0.45, 0.70]
    elif market_chg < -0.03:
        raw_positions = [0, 0, 0, 0]
        log_list.append("⚠️ 大盘单日跌幅超3%，强制暂停开仓")
    position_rate = min(raw_positions[action], pos_coeff * 0.75)
    # 止盈止损根据市场环境调整
    take_profit = 0.09 if market_chg > 0.015 else 0.05
    stop_loss = 0.025 if market_chg > 0.015 else 0.035

    # ====== 决策记录入库 ======
    decision_record = {
        "ts_code": ts_code,
        "trade_date": stock_row["trade_date"],
        "base_score": base_score,
        "trend_score": trend_score,
        "market_change": round(market_chg, 4),
        "volatility": round(vol, 4),
        "action": action,
        "reward": round(reward, 4),
        "position_rate": position_rate,
        "pos_coeff": pos_coeff,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }
    sql_memory.write_rl_decision(decision_record)
    log_list.append(
        f"【第6层·PPO决策】动作={action}, 风控系数={pos_coeff:.2f}, "
        f"最终仓位={position_rate:.2%}, 止盈={take_profit:.0%}, 止损={stop_loss:.0%}"
    )
    log_list.append("✅ §5全链路校验完成")

    return allow_trade, "\n".join(log_list), decision_record


# ====== 平仓归档 ======

def archive_after_close(trade_info: dict, market_env: str):
    sql_memory.after_close_archive(trade_info, market_env)
    sql_memory.write_old_trade_memory({
        "ts_code": trade_info["ts_code"],
        "signal": "五层风控管道",
        "pnl_rate": trade_info["pnl_rate"],
        "market_vec": str([trade_info.get(k, 0) for k in ("rsi","macd","base_factor_score","trend_score")]),
    })


if __name__ == "__main__":
    test_row = pd.Series({
        "ts_code": "600884", "trade_date": "2026-07-19",
        "industry": "电气设备", "base_factor_score": 0.58,
        "trend_score": 0.42, "sentiment_score": 0.55,
        "rsi": 35.0, "macd": -0.25,
    })
    allow, log, decision = full_integrated_pipeline(test_row)
    print(f"allow={allow}")
    for line in log.split("\n"):
        print("  " + line)
    if decision:
        print(f"\nposition_rate={decision['position_rate']:.2%}, pos_coeff={decision['pos_coeff']:.2f}")
