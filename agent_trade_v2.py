# -*- coding: utf-8 -*-
"""
agent_trade_v2.py — 交易信号生成器（含自适应修正）
基于预测评分 + 多情景推演 + 自我修正，生成日内交易信号
"""
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sqlalchemy import text

try:
    from config import pg_engine, TARGET_CODES
except ImportError:
    pg_engine = None
    TARGET_CODES = []

try:
    from self_correct import correct_trading, run_full_check
    SELF_CORRECT_ACTIVE = True
except ImportError:
    SELF_CORRECT_ACTIVE = False
    def correct_trading(code, r): return r
    def run_full_check(code=None): return {}


# ── ts_code格式统一 ──

def _ensure_tscode(code):
    c = str(code).replace(".SH","").replace(".SZ","")
    suffix = ".SH" if c.startswith("6") or c.startswith("9") else ".SZ"
    return c + suffix

# ── 信号生成 ──

def get_latest_predict(code):
    """查最新一条预测结果"""
    if pg_engine is None:
        return None
    sql = text("""
        SELECT trade_date, confidence, predict_result, position,
               predict_reason, self_corrected
        FROM stock_predict
        WHERE ts_code = :code
        ORDER BY trade_date DESC LIMIT 1
    """)
    df = pd.read_sql(sql, pg_engine, params={"code": _ensure_tscode(code)})
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "trade_date": row["trade_date"],
        "confidence": float(row["confidence"]),
        "predict_result": int(row["predict_result"]),
        "position": row["position"] if pd.notna(row.get("position")) else "0%",
        "predict_reason": row["predict_reason"] or "",
        "self_corrected": bool(row.get("self_corrected", False)),
    }


def get_daily_price(code):
    """取最新收盘价"""
    if pg_engine is None:
        return None, None
    sql = text("""
        SELECT close, trade_date FROM stock_daily
        WHERE ts_code = :code ORDER BY trade_date DESC LIMIT 1
    """)
    df = pd.read_sql(sql, pg_engine, params={"code": _ensure_tscode(code)})
    if df.empty:
        return None, None
    return float(df["close"].iloc[0]), df["trade_date"].iloc[0]


def get_moneyflow_3d(code):
    """最近3日资金净额合计(万元)"""
    if pg_engine is None:
        return 0.0
    sql = text("""
        SELECT SUM(net_mf_amount) as total FROM (
            SELECT net_mf_amount FROM stock_money_flow
            WHERE ts_code = :code ORDER BY trade_date DESC LIMIT 3
        ) sub
    """)
    df = pd.read_sql(sql, pg_engine, params={"code": _ensure_tscode(code)})
    return float(df["total"].iloc[0]) if not pd.isna(df["total"].iloc[0]) else 0.0


def gen_signal(code, trade_date):
    """
    综合预测结果+自适应修正 生成交易信号
    """
    pred = get_latest_predict(code)
    if pred is None:
        return {"ts_code": code, "trade_date": trade_date,
                "signal": "hold", "direction": "neutral",
                "weight": 0, "reason": "no_predict_data"}

    confidence = pred["confidence"]
    pos = pred["position"]
    pred_val = pred["predict_result"]
    self_corrected = pred.get("self_corrected", False)

    close, dt = get_daily_price(code)
    mf_3d = get_moneyflow_3d(code)

    redline_triggers = []
    redline_override = False

    # ── 减仓红线检查 ──
    # 红线1: 连续3日主力累计净流出超2亿 → 减仓50%
    if mf_3d < -20000:
        redline_triggers.append(f"3日净流{mf_3d/10000:.1f}亿>2亿→减仓50%")
        redline_override = True

    # ── 信号方向 ──
    direction = "neutral"
    signal = "hold"
    if redline_override:
        direction = "sell"
        signal = "reduce"
        weight = 0.5 if pos != "0%" else 0.0
    else:
        if pred_val == 1:
            direction = "buy"
            signal = "open"
        elif pred_val == -1:
            direction = "sell"
            signal = "close"
        else:
            direction = "neutral"
            signal = "hold"
        weight = float(pos.replace("%", "")) / 100.0 if pos else 0.0

    # ── 第6层：两层风控统一校验（静态硬风控+AI动态预判） ──
    from full_risk_before_open import full_risk_before_open
    if signal == "open" and weight > 0:
        try:
            stock_row = {"ts_code": str(code), "industry": ""}
            ok, _, final_pos = full_risk_before_open(stock_row, weight)
            if not ok or final_pos <= 0:
                direction = "neutral"
                signal = "hold"
                weight = 0.0
                correction_note = "(risk_blocked)"
            else:
                weight = final_pos
                correction_note = "(risk_adjusted)"
        except Exception:
            pass

    # 自适应修正折扣
    correction_note = ""
    if SELF_CORRECT_ACTIVE and self_corrected:
        correction_note = "(self_corrected)"

    result = {
        "ts_code": _ensure_tscode(code),
        "trade_date": trade_date,
        "signal": signal,
        "direction": direction,
        "weight": weight,
        "confidence": confidence,
        "close_price": close or 0,
        "moneyflow_3d": round(mf_3d / 10000, 2),
        "redline_triggers": "; ".join(redline_triggers) if redline_triggers else "none",
        "redline_active": redline_override,
        "self_corrected": self_corrected,
        "reason": f"{direction}/{signal}(conf={confidence}{correction_note})",
    }

    # ── 自适应修正 ──
    if SELF_CORRECT_ACTIVE:
        try:
            result = correct_trading(code, result)
        except Exception as e:
            result["self_correct_error"] = str(e)

    return result


def stream_trade(dt):
    """批量生成8只股票交易信号"""
    if pg_engine is None:
        print("[trade] no DB")
        return

    # 开盘前自检
    if SELF_CORRECT_ACTIVE:
        try:
            hc = run_full_check()
            if hc.get("all_ok"):
                print("[self_correct] 交易前自检通过")
        except Exception as e:
            print(f"[self_correct] 交易自检异常: {e}")

    buf = []
    for code in TARGET_CODES:
        signal = gen_signal(code, dt)
        buf.append(signal)
        icon = "🔴" if signal["direction"] == "sell" else \
               "🟢" if signal["direction"] == "buy" else "⚪"
        sc = "🔧" if signal.get("self_corrected") else " "
        print(f"{sc}{icon} {code} {signal['direction']} "
              f"weight={signal['weight']:.0%} conf={signal['confidence']} "
              f"flow_3d={signal['moneyflow_3d']:.1f}亿")
        if signal.get("redline_active"):
            print(f"  ⚠ 红线触发: {signal['redline_triggers']}")

    # 写入strategy_signal表
    df_out = pd.DataFrame(buf)
    try:
        df_out.to_sql("strategy_signal", pg_engine,
                       if_exists="replace", index=False,
                       method="multi")
        print(f"[trade] 写入{len(buf)}条信号 OK")
    except Exception as e:
        print(f"[trade] 写入失败: {e}")

    del df_out, buf


if __name__ == "__main__":
    dt = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    stream_trade(dt)
