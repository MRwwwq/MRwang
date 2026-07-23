#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module05 机械离场规则 — 盘中持续轮询

时序: Module04 定策略之后, 盘中以固定频率轮询
执行模式: 盘中间隔轮询 (建议 1~3分钟/次)
适用场景: 持仓标的的离场判定, 完全机械化, 无人为干预

规则分类:
    一、符合预期止盈
    二、不及预期止损 (3条无条件卖)
    三、扛单不止损自动追加心理编码
"""

import logging
import time
from datetime import datetime
from typing import Optional
from psy_hit_manager import add_psy_code, get_psy_hit_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s [M05] %(message)s", datefmt="%H:%M:%S")


# ====================== 520多日追踪状态 ======================

# 全局520追踪状态: {code: {"ma5_break_days": 0, "ma20_break_days": 0,
#                            "last_ma5": None, "last_ma20": None}}
_TRACK_520_STATE: dict = {}


def reset_520_tracking():
    """重置520追踪状态（盘前调用）。"""
    global _TRACK_520_STATE
    _TRACK_520_STATE = {}


def track_520_multi_day(
    code: str,
    current_price: float,
    ma5: float = None,
    ma20: float = None,
) -> dict:
    """520多日追踪（有效跌破MA5/连续跌破MA20）。

    持续跟踪:
      - MA5跌破连续天数 (有效跌破=收盘<MA5)
      - MA20跌破连续天数 (收盘<MA20)

    返回:
        {
            "warning_level": "none"|"ma5_break"|"ma5_break_2days"|"ma20_break"|"ma20_break_2days"|"death_cross",
            "ma5_break_days": int,
            "ma20_break_days": int,
            "detail": str,
        }
    """
    global _TRACK_520_STATE
    if code not in _TRACK_520_STATE:
        _TRACK_520_STATE[code] = {"ma5_break_days": 0, "ma20_break_days": 0,
                                   "from_date": ""}

    state = _TRACK_520_STATE[code]

    # 更新MA5跌破天数
    if ma5 is not None and current_price < ma5:
        state["ma5_break_days"] += 1
    else:
        state["ma5_break_days"] = 0  # 收回后重置

    # 更新MA20跌破天数
    if ma20 is not None and current_price < ma20:
        state["ma20_break_days"] += 1
    else:
        state["ma20_break_days"] = 0

    _TRACK_520_STATE[code] = state

    # 判定预警级别
    ma5_days = state["ma5_break_days"]
    ma20_days = state["ma20_break_days"]

    # 死叉: MA5 < MA20 (优先级最高)
    if ma5 is not None and ma20 is not None and ma5 < ma20:
        return {
            "warning_level": "death_cross",
            "ma5_break_days": ma5_days,
            "ma20_break_days": ma20_days,
            "detail": f"MA5({ma5:.2f})<MA20({ma20:.2f}) 死叉形成, 强制清仓",
        }

    # 连续2日跌破MA20
    if ma20_days >= 2:
        return {
            "warning_level": "ma20_break_2days",
            "ma5_break_days": ma5_days,
            "ma20_break_days": ma20_days,
            "detail": f"连续{ma20_days}日收盘<MA20({ma20:.2f}), 清仓止损预警",
        }

    # 有效跌破MA5且次日无法收回(≥2日)
    if ma5_days >= 2:
        return {
            "warning_level": "ma5_break_2days",
            "ma5_break_days": ma5_days,
            "ma20_break_days": ma20_days,
            "detail": f"连续{ma5_days}日收盘<MA5({ma5:.2f}), 短线减仓预警",
        }

    # 单日跌破MA5
    if ma5_days == 1:
        return {
            "warning_level": "ma5_break",
            "ma5_break_days": 1,
            "ma20_break_days": ma20_days,
            "detail": f"收盘{current_price:.2f}<MA5({ma5:.2f}), 短线减仓预警(第1日)",
        }

    # 单日跌破MA20
    if ma20_days == 1:
        return {
            "warning_level": "ma20_break",
            "ma5_break_days": 0,
            "ma20_break_days": 1,
            "detail": f"收盘{current_price:.2f}<MA20({ma20:.2f}), 清仓预警(第1日)",
        }

    return {
        "warning_level": "none",
        "ma5_break_days": 0,
        "ma20_break_days": 0,
        "detail": "均线多头, 价格运行正常",
    }

class ExitSignal:
    """单只持仓的离场信号集合"""

    NONE = "none"               # 无信号, 继续持有
    TAKE_PROFIT = "take_profit" # 止盈
    STOP_LOSS = "stop_loss"     # 触及止损位
    BREAK_LOW = "break_low"     # 跌破日内支撑低点
    BREAK_MIN = "break_min"     # 跌破日内最低价
    BOUNCE_FAIL = "bounce_fail" # 反弹无力突破成本
    # 520均线监控信号
    BREAK_MA5 = "break_ma5"     # 有效跌破MA5（短线减仓预警）
    BREAK_MA20 = "break_ma20"   # 跌破MA20（清仓止损预警）
    DEATH_CROSS = "death_cross" # 520死叉（强制清仓）


def check_exit(
    code: str,
    name: str,
    current_price: float,
    cost_price: float,
    stop_loss_pct: float,
    today_low: float,
    today_support: Optional[float] = None,
    session_high: Optional[float] = None,
    position_alloc_pct: Optional[float] = None,
    ma5: Optional[float] = None,
    ma20: Optional[float] = None,
) -> dict:
    """
    单标的离场检查（含520均线监控）。

    参数:
        code: 标的代码
        name: 标的名称
        current_price: 当前实时价格
        cost_price: 持仓成本价
        stop_loss_pct: 止损比例 (如 3.0 = 3%)
        today_low: 当日最低价 (盘中实时更新)
        today_support: 日内支撑低点 (技术面判定)
        session_high: 当日最高价 (用于反弹判定)
        position_alloc_pct: 持仓仓位占比
        ma5: MA5均线值（520监控）
        ma20: MA20均线值（520监控）

    返回:
        {
            "code": str,
            "name": str,
            "signal": ExitSignal.*,
            "signal_cn": str,
            "reason": str,
            "action": "卖出" | "持有",
            "price_info": {...},
            "psy_codes_added": [...],
            "ma5_warning": str,    # 520均线预警信息
            "ma20_warning": str,
        }
    """
    info = {
        "code": code,
        "name": name,
        "current_price": current_price,
        "cost_price": cost_price,
        "stop_loss_pct": stop_loss_pct,
        "today_low": today_low,
        "today_support": today_support or today_low,
    }

    # 亏损比例
    loss_pct = (current_price - cost_price) / cost_price * 100
    # 当前价相对于日内最低的反弹幅度
    bounce_from_low = (current_price - today_low) / today_low * 100 if today_low > 0 else 0

    # ========== 一、符合预期止盈 ==========
    # 跌破成本价 → 止盈出局 (保护利润)
    if loss_pct <= 0 and current_price < cost_price:
        return {
            **info,
            "signal": ExitSignal.TAKE_PROFIT,
            "signal_cn": "止盈",
            "reason": f"跌破成本价 {cost_price:.2f}, 当前{current_price:.2f}, 保护利润离场",
            "action": "卖出",
            "psy_codes_added": [],
        }

    # ========== 二、不及预期止损 (3条无条件卖) ==========

    # 规则1: 跌破日内支撑低点 → 无条件卖
    if today_support and current_price < today_support:
        return {
            **info,
            "signal": ExitSignal.BREAK_LOW,
            "signal_cn": "跌破日内支撑",
            "reason": f"跌破日内支撑低点 {today_support:.2f}, 当前{current_price:.2f}, 无条件卖出",
            "action": "卖出",
            "psy_codes_added": [],
        }

    # 规则2: 跌破日内最低价 → 无条件卖 (创新低)
    if current_price < today_low:
        return {
            **info,
            "signal": ExitSignal.BREAK_MIN,
            "signal_cn": "跌破日内最低",
            "reason": f"跌破日内最低价 {today_low:.2f}, 当前{current_price:.2f}, 创日内新低, 无条件卖出",
            "action": "卖出",
            "psy_codes_added": [],
        }

    # 规则3: 反弹无力突破成本 → 无条件卖
    if (
        loss_pct < 0                          # 处于亏损
        and session_high is not None
        and session_high < cost_price          # 日内最高都没到成本
        and bounce_from_low < 2                # 从低点反弹不足2%
    ):
        return {
            **info,
            "signal": ExitSignal.BOUNCE_FAIL,
            "signal_cn": "反弹无力",
            "reason": f"日内最高{session_high:.2f}<成本{cost_price:.2f}, 反弹仅{bounce_from_low:.1f}%, 反弹无力突破成本, 无条件卖出",
            "action": "卖出",
            "psy_codes_added": [],
        }

    # ========== 三、触及止损位 ==========
    if abs(loss_pct) >= stop_loss_pct:
        return {
            **info,
            "signal": ExitSignal.STOP_LOSS,
            "signal_cn": "触及止损",
            "reason": f"亏损{abs(loss_pct):.1f}% ≥ 止损{stop_loss_pct}%, 当前{current_price:.2f} vs 成本{cost_price:.2f}, 执行止损",
            "action": "卖出",
            "psy_codes_added": [],
        }

    # ========== 四、520均线监控（多日追踪） ==========
    ma5_warning = ""
    ma20_warning = ""
    if ma5 is not None and ma20 is not None:
        # 520多日追踪
        tracking = track_520_multi_day(code, current_price, ma5, ma20)

        if tracking["warning_level"] == "death_cross":
            return {
                **info,
                "signal": ExitSignal.DEATH_CROSS,
                "signal_cn": "520死叉清仓",
                "reason": tracking["detail"],
                "action": "卖出",
                "psy_codes_added": [],
                "ma5_warning": f"MA5({ma5:.2f})<MA20({ma20:.2f})",
                "ma20_warning": "520死叉, 强制清仓",
                "520_tracking": tracking,
            }

        if tracking["warning_level"] == "ma20_break_2days":
            # 连续2日跌破MA20 → 清仓离场预警
            add_psy_code("code_04_避免怀疑")
            return {
                **info,
                "signal": ExitSignal.BREAK_MA20,
                "signal_cn": "连续跌破MA20清仓",
                "reason": tracking["detail"],
                "action": "卖出",
                "psy_codes_added": ["code_04_避免怀疑"],
                "ma5_warning": "",
                "ma20_warning": tracking["detail"],
                "520_tracking": tracking,
            }

        if tracking["warning_level"] == "ma5_break_2days":
            # 连续2日跌破MA5 → 短线减仓预警 (加心理编码对冲损失厌恶)
            add_psy_code("code_14_损失厌恶")
            ma5_warning = tracking["detail"]
            ma20_warning = ""
            # 返回减仓预警但不强制卖出

        if tracking["warning_level"] == "ma5_break":
            ma5_warning = tracking["detail"]
            ma20_warning = ""

        if tracking["warning_level"] == "ma20_break":
            ma5_warning = ""
            ma20_warning = tracking["detail"]

    # ========== 无信号, 继续持有 ==========
    return {
        **info,
        "signal": ExitSignal.NONE,
        "signal_cn": "无信号",
        "reason": "均线多头, 价格运行正常",
        "action": "持有",
        "psy_codes_added": [],
        "ma5_warning": ma5_warning,
        "ma20_warning": ma20_warning,
        "520_tracking": {"warning_level": "none", "ma5_break_days": 0, "ma20_break_days": 0, "detail": "均线多头, 价格运行正常"},
    }


# ====================== 轮询引擎 ======================

def polling_cycle(
    positions: list,
    stop_loss_pct: float,
    interval_seconds: int = 60,
    max_cycles: int = 0,  # 0=无限循环
    price_fetcher=None,   # 外部价格获取回调函数 f(code) -> (current_price, today_low, today_support, session_high)
) -> list:
    """
    盘中持续轮询离场信号。

    参数:
        positions: [{code, name, cost_price, alloc_pct}]
        stop_loss_pct: 统一止损比例
        interval_seconds: 轮询间隔秒数
        max_cycles: 最大轮询次数 (0=无限)
        price_fetcher: 外部获取实时价格的函数

    返回:
        [每条离场记录]
    """
    cycle = 0
    all_exits = []

    logging.info(f"🔄 Module05 盘中轮询启动: {len(positions)}只持仓, 间隔{interval_seconds}s")

    while max_cycles == 0 or cycle < max_cycles:
        cycle += 1
        now = datetime.now().strftime("%H:%M:%S")
        logging.info(f"  ── 轮询#{cycle} [{now}] ──")
        cycle_exits = []

        for pos in positions:
            code = pos["code"]
            name = pos["name"]
            cost = pos["cost_price"]
            alloc = pos.get("alloc_pct", 0)

            # 获取实时价格 (外部回调或模拟)
            if price_fetcher:
                price_info = price_fetcher(code)
                if price_info is None:
                    continue
                cur_price, today_low, support, session_high = price_info
            else:
                # 无外部数据源时跳过轮询
                logging.warning(f"  ⚠️ 无价格数据源, 跳过 {code}")
                continue

            # 离场检查
            result = check_exit(
                code=code, name=name,
                current_price=cur_price, cost_price=cost,
                stop_loss_pct=stop_loss_pct,
                today_low=today_low,
                today_support=support,
                session_high=session_high,
                position_alloc_pct=alloc,
            )

            # 记录
            if result["action"] == "卖出":
                logging.info(f"  🔴 离场信号 [{code} {name}]: {result['signal_cn']} | {result['reason']}")
                cycle_exits.append(result)
                all_exits.append(result)
            else:
                logging.info(f"  🟢 持有 [{code} {name}]: {result['reason']}")

            # ========== 扛单不止损追加心理编码 ==========
            # 实际亏损已超止损线但 check_exit 被跳过 → 需要额外检测
            loss_pct = (cur_price - cost) / cost * 100
            if abs(loss_pct) >= stop_loss_pct and result["action"] != "卖出":
                add_psy_code("code_14_损失厌恶")
                result["psy_codes_added"].append("code_14_损失厌恶")
                logging.warning(f"  🧠 扛单死扛 [{code}] 亏损{abs(loss_pct):.1f}%≥止损{stop_loss_pct}% 未执行 → add code_14")

        # 本轮结果
        sold_count = len(cycle_exits)
        if sold_count > 0:
            logging.info(f"  本轮离场: {sold_count}只 | 累计离场: {len(all_exits)}/{len(positions)}只")
        else:
            logging.info(f"  本轮无离场")

        # 全部离场则终止
        remaining = len(positions) - len(all_exits)
        if remaining <= 0:
            logging.info(f"  ✅ 全部持仓已离场, 轮询终止")
            break

        # 等待下一轮
        if max_cycles == 0 or cycle < max_cycles:
            time.sleep(interval_seconds)

    logging.info(f"🔄 Module05 轮询结束: {cycle}轮, 共{len(all_exits)}次离场")
    return all_exits


# ====================== 单次离场快照(非轮询版) ======================

def snapshot_exit_check(positions: list, stop_loss_pct: float, price_fetcher) -> list:
    """
    一次性快照检查 (非轮询, 用于收盘前或定时检查).
    返回: 所有触发离场信号的标的信息
    """
    exits = []
    for pos in positions:
        code = pos["code"]
        price_info = price_fetcher(code)
        if price_info is None:
            continue
        cur_price, today_low, support, session_high = price_info

        result = check_exit(
            code=code, name=pos.get("name", code),
            current_price=cur_price, cost_price=pos["cost_price"],
            stop_loss_pct=stop_loss_pct,
            today_low=today_low,
            today_support=support,
            session_high=session_high,
            position_alloc_pct=pos.get("alloc_pct"),
        )
        if result["action"] == "卖出":
            exits.append(result)
    return exits


# ====================== 测试 ======================
if __name__ == "__main__":
    from psy_hit_manager import clear_all_psy_codes, psy_hit_codes
    clear_all_psy_codes()

    print("=== Module05 机械离场规则 测试 ===\n")

    # 模拟价格获取
    mock_prices = iter([
        (10.80, 10.75, 10.75, 11.30),  # 跌破日内支撑 (支撑=10.75, 当前=10.80? 不,略高于)
        (10.70, 10.75, 10.75, 11.30),  # 跌破日内支撑10.75
        (11.20, 11.00, 11.00, 11.30),  # 反弹无力(最高11.30<成本11.50)
        (10.50, 10.80, 10.80, 11.00),  # 跌破日内最低
        (9.50, 10.00, 10.00, 10.20),   # 触及止损(亏损17%>3%)
        (11.40, 11.00, 11.00, 11.55),  # 正常持有
    ])

    def mock_fetcher(code):
        try:
            return next(mock_prices)
        except StopIteration:
            return None

    # 模拟持仓
    positions = [
        {"code": "600884", "name": "杉杉", "cost_price": 11.50, "alloc_pct": 10},
        {"code": "600547", "name": "山东黄金", "cost_price": 25.00, "alloc_pct": 8},
    ]

    print("测试: 单次快照检查")
    results = snapshot_exit_check(positions, 3.0, mock_fetcher)
    for r in results:
        print(f"  🔴 {r['code']} {r['name']}: {r['signal_cn']} | {r['reason'][:50]}...")

    print(f"\npsy_hit_codes: {psy_hit_codes}")
