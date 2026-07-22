#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module01 定风格 — 盘前底层定位

时序: 盘前集合竞价结束后，全系统第一步
执行: clear_all_psy_codes() → Module01 → Module02 → ... → Layer1

输出:
    active_style: str       — 最终匹配风格 (A/B/C/D)
    user_style_form: dict   — 用户三项表单原始值
    style_position_cap: dict— 仓位上限 {total, per_stock, stop_loss}
"""

import logging
from psy_hit_manager import add_psy_code, remove_psy_code, clear_all_psy_codes, psy_hit_codes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [M01] %(message)s", datefmt="%H:%M:%S")

# ====================== 四套标准匹配库 ======================

STYLE_LIBRARY = {
    "A": {
        "name": "龙头连板",
        "trade_period": "超短隔日",
        "capital_type": "游资连板",
        "position_cap": {"total": 60, "per_stock": 25, "stop_loss": 3.0},
    },
    "B": {
        "name": "首板套利",
        "trade_period": "短线波段",
        "capital_type": "量化轮动",
        "position_cap": {"total": 40, "per_stock": 15, "stop_loss": 2.5},
    },
    "C": {
        "name": "中军趋势",
        "trade_period": "中线趋势",
        "capital_type": "机构抱团",
        "position_cap": {"total": 70, "per_stock": 30, "stop_loss": 4.0},
    },
    "D": {
        "name": "兜底混合",
        "trade_period": None,       # 不匹配任意组合
        "capital_type": None,
        "position_cap": {"total": 25, "per_stock": 10, "stop_loss": 2.0},
    },
}

# ====================== 判定逻辑 ======================

def match_style(trade_period: str, capital_type: str) -> tuple:
    """
    匹配四套标准风格。
    返回: (style_key, style_name, position_cap)
    """
    for key, lib in STYLE_LIBRARY.items():
        if key == "D":
            continue  # D是兜底，最后检查
        if lib["trade_period"] == trade_period and lib["capital_type"] == capital_type:
            return key, lib["name"], lib["position_cap"]

    # 无完全匹配 → 兜底D
    return "D", STYLE_LIBRARY["D"]["name"], STYLE_LIBRARY["D"]["position_cap"]


def validate_user_form(form: dict) -> list[str]:
    """
    校验用户仓位/止损是否越界。
    返回: 触发的心理误判编码列表
    """
    codes = []
    style_key = form.get("_matched_style", "D")
    caps = STYLE_LIBRARY.get(style_key, STYLE_LIBRARY["D"])["position_cap"]

    user_total = form.get("total_pct", 0)
    user_stop = form.get("stop_loss", 0)

    if user_total > caps["total"]:
        codes.append("code_12_自视过高")
        logging.warning(f"⚠️ 总仓{user_total}% > 风格上限{caps['total']}% → add code_12")

    if user_stop > caps["stop_loss"]:
        codes.append("code_14_损失厌恶")
        logging.warning(f"⚠️ 止损{user_stop}% > 风格上限{caps['stop_loss']}% → add code_14")

    return codes


def run_module01(
    trade_period: str,
    capital_type: str,
    user_total_pct: int,
    user_per_stock_pct: int,
    user_stop_loss: float,
) -> dict:
    """
    Module01 定风格主入口。

    参数:
        trade_period: "超短隔日" / "短线3-5天" / "中线趋势" / "长线"
        capital_type: "游资连板" / "机构抱团" / "量化轮动"
        user_total_pct / user_per_stock_pct / user_stop_loss: 用户自选仓位

    返回:
        {
            "active_style": "A"|"B"|"C"|"D",
            "style_name": "龙头连板"|...,
            "user_style_form": {...},
            "position_cap": {"total": ..., "per_stock": ..., "stop_loss": ...},
            "psy_codes_added": [...],
            "matched": True|False,
        }
    """
    logging.info("=" * 50)
    logging.info("Module01 定风格 启动")
    logging.info(f"  用户填报: 周期={trade_period}, 资金={capital_type}, 总仓={user_total_pct}%, 单票={user_per_stock_pct}%, 止损={user_stop_loss}%")

    # 1. 匹配风格
    style_key, style_name, pos_cap = match_style(trade_period, capital_type)
    matched = style_key != "D"

    # 2. 构建user_style_form（含匹配结果）
    user_form = {
        "trade_period": trade_period,
        "capital_type": capital_type,
        "total_pct": user_total_pct,
        "per_stock_pct": user_per_stock_pct,
        "stop_loss": user_stop_loss,
        "_matched_style": style_key,
    }

    # 3. 心理误判编码处理
    added_codes = []

    if matched:
        # A/B/C 匹配成功 → 移除 code_05
        remove_psy_code("code_05_避免不一致")
        logging.info(f"  ✅ 匹配风格 {style_key}({style_name}) → remove code_05")
    else:
        # D 不匹配 → 添加 code_05 永久保留
        add_psy_code("code_05_避免不一致")
        added_codes.append("code_05_避免不一致")
        logging.info(f"  ⚠️ 不匹配, 兜底 D → add code_05 (永久保留)")

    # 4. 仓位/止损越界校验
    validation_codes = validate_user_form(user_form)
    for c in validation_codes:
        add_psy_code(c)
        added_codes.append(c)

    # 5. 输出日志
    logging.info(f"  → active_style: {style_key}({style_name})")
    logging.info(f"  → position_cap: 总仓{pos_cap['total']}% 单票{pos_cap['per_stock']}% 止损{pos_cap['stop_loss']}%")
    logging.info(f"  → psy_hit_codes 当前: {psy_hit_codes}")
    logging.info("Module01 定风格 完成")
    logging.info("=" * 50)

    return {
        "active_style": style_key,
        "style_name": style_name,
        "user_style_form": user_form,
        "position_cap": pos_cap,
        "psy_codes_added": added_codes,
        "matched": matched,
    }


# ====================== 独立测试 ======================
if __name__ == "__main__":
    print("Module01 定风格 — 测试\n")

    # 盘前初始化
    clear_all_psy_codes()

    # 测试用例
    test_cases = [
        # (周期, 资金, 总仓, 单票, 止损, 标签)
        ("超短隔日", "游资连板", 60, 25, 3.0, "A-完全匹配"),
        ("超短隔日", "游资连板", 80, 25, 5.0, "A-仓位超限+止损过大"),
        ("短线波段", "量化轮动", 40, 15, 2.5, "B-完全匹配"),
        ("中线趋势", "机构抱团", 50, 20, 3.0, "C-仓位适中"),
        ("长线", "游资连板", 30, 15, 2.0, "D-不匹配"),
        ("短线3-5天", "机构抱团", 50, 20, 3.0, "D-不匹配(混搭)"),
    ]

    for i, (period, capital, total, per, stop, label) in enumerate(test_cases):
        clear_all_psy_codes()
        print(f"\n--- 测试{i+1}: {label} ---")
        result = run_module01(period, capital, total, per, stop)
        print(f"  风格: {result['active_style']}({result['style_name']}) | "
              f"仓位: {result['position_cap']} | "
              f"psy: {psy_hit_codes}")
