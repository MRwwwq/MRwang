#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module03 定方向 — 主线板块与选股分层

时序: Module02 定情绪之后执行
输入:
    - main_line: 主线板块名称
    - driver_type: 驱动逻辑 (政策/业绩/题材/技术)
    - stocks: 候选股票列表 [{code, name, role, reason}]
输出:
    - main_line_info: 主线判定结果
    - selected_stocks: 分层选股结果 (龙头/补涨/潜伏)
    - stock_limit: 风格对应选股限制
    - psy_codes: 心理误判编码
"""

import logging
from psy_hit_manager import add_psy_code, psy_hit_codes, get_psy_hit_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s [M03] %(message)s", datefmt="%H:%M:%S")

# ====================== 驱动逻辑分类 ======================

DRIVER_TYPE_MAP = {
    "政策": "中长期可持续政策驱动(有效周期≥6个月)",
    "业绩": "业绩预增/超预期/景气度反转",
    "题材": "短期情绪题材炒作(生命周期≤2周)",
    "技术": "技术突破/产品量产/产能落地",
}

# 长期有效驱动(≥6个月)——不触发code_10/不归类为纯短期题材
LONG_TERM_DRIVERS = {"政策", "业绩", "技术"}
SHORT_TERM_DRIVERS = {"题材"}

# ====================== 风格对应选股限制 ======================

STYLE_STOCK_RULES = {
    "A": {
        "name": "龙头连板",
        "allow_role": ["核心龙头"],
        "forbid_role": ["补涨备选", "低位潜伏"],
        "desc": "连板模式只做龙头，严禁跟风补涨与潜伏",
    },
    "B": {
        "name": "首板套利",
        "allow_role": ["补涨备选"],
        "forbid_role": ["核心龙头", "低位潜伏"],
        "desc": "套利模式只做补涨，不追龙头、不做潜伏",
    },
    "C": {
        "name": "中军趋势",
        "allow_role": ["低位潜伏"],
        "forbid_role": ["核心龙头", "补涨备选"],
        "desc": "趋势模式只做潜伏中军，不追短线龙头",
    },
    "D": {
        "name": "兜底混合",
        "allow_role": ["低位潜伏"],
        "forbid_role": ["核心龙头", "补涨备选"],
        "desc": "混合模式仅允许极小仓潜伏",
    },
}

# ====================== 剔除标准 ======================

def should_eliminate(stock: dict) -> tuple:
    """
    判定个股是否应剔除。
    stock字段: {code, name, role, reason, volume_rating, is_liability, driver_type?, ...}
    返回: (eliminate: bool, reason: str)
    """
    reason = stock.get("reason", "").lower()

    # 杂毛：无辨识度、非板块核心
    if stock.get("role") == "杂毛":
        return True, "杂毛股——无板块辨识度"

    # 无量阴跌：成交量持续低于20日均量50%
    vol_ratio = stock.get("volume_ratio", 1.0)
    if vol_ratio < 0.5:
        return True, f"无量阴跌——量比{vol_ratio:.2f}<0.5"

    # 利空：业绩亏损/减持/诉讼/监管
    liability_keywords = ["st", "退", "亏损", "减持", "诉讼", "监管", "问询", "*", "警示"]
    for kw in liability_keywords:
        if kw in reason or kw in stock.get("name", "").lower():
            return True, f"利空股——含\"{kw}\""

    return False, ""


# ====================== 心理编码判定 ======================

def apply_psy_for_direction(
    main_line: str,
    driver_type: str,
    active_style: str,
    eliminated_count: int,
) -> list:
    """
    方向相关心理误判编码。
    """
    added = []

    # 无主线混沌 → code_23
    if not main_line or main_line.strip() == "":
        add_psy_code("code_23_市场噪音废话")
        added.append("code_23_市场噪音废话")
        logging.info(f"  🧠 无主线混沌→add code_23")

    # 纯短期题材 → code_10
    if driver_type in SHORT_TERM_DRIVERS:
        add_psy_code("code_10_简单联想")
        added.append("code_10_简单联想")
        logging.info(f"  🧠 纯短期题材→add code_10")

    # 周期与逻辑错配(code_05)：长线+题材 / 短线+政策长线逻辑
    if active_style == "D":
        # D类本身就是不匹配，已有code_05，无需重复
        pass
    elif driver_type == "题材":
        # 题材驱动在非超短模式下→code_05
        if active_style in ("C",):
            add_psy_code("code_05_避免不一致")
            added.append("code_05_避免不一致")
            logging.info(f"  🧠 趋势+题材错配→add code_05")

    return added


# ====================== 主入口 ======================

def run_module03(
    main_line: str,
    driver_type: str,
    driver_detail: str,
    candidate_stocks: list,
    active_style: str,
) -> dict:
    """
    Module03 定方向主入口。

    参数:
        main_line: 主线板块名称 (空串=无主线混沌)
        driver_type: 驱动逻辑 (政策/业绩/题材/技术)
        driver_detail: 驱动具体描述
        candidate_stocks: [
            {"code":"600XXX","name":"XX","role":"核心龙头",
             "reason":"xx", "volume_ratio":1.2, "market_cap":"xx"}
        ]
        active_style: Module01输出的"A"|"B"|"C"|"D"

    返回:
        {
            "main_line": 主线,
            "driver_type": 驱动,
            "driver_detail": 描述,
            "driver_validity": "长期(≥6月)"|"短期(≤2周)",
            "stocks_raw": 原始候选数,
            "eliminated": [...剔除记录],
            "selected": { 分层选股结果
                "core": [...],
                "fill": [...],
                "latent": [...],
            },
            "style_rule": 风格对应选股规则,
            "psy_codes_added": [...]
        }
    """
    logging.info("=" * 50)
    logging.info("Module03 定方向 启动")
    logging.info(f"  主线: {main_line or '❌无主线混沌'}")
    logging.info(f"  驱动: {driver_type} - {driver_detail}")
    logging.info(f"  候选: {len(candidate_stocks)}只 | 风格: {active_style}")

    # 1. 驱动有效周期判定
    driver_validity = "长期(≥6月)" if driver_type in LONG_TERM_DRIVERS else "短期(≤2周)"

    # 2. 选股剔除
    eliminated = []
    kept = []
    for s in candidate_stocks:
        elim, elim_reason = should_eliminate(s)
        if elim:
            eliminated.append({**s, "elim_reason": elim_reason})
            logging.info(f"  ❌ 剔除 {s.get('code','')} ({s.get('name','')}): {elim_reason}")
        else:
            kept.append(s)

    # 3. 分层
    selected = {
        "core":   [s for s in kept if s.get("role") == "核心龙头"],
        "fill":   [s for s in kept if s.get("role") == "补涨备选"],
        "latent": [s for s in kept if s.get("role") == "低位潜伏"],
    }

    # 4. 风格对应选股限制校验
    style_rule = STYLE_STOCK_RULES.get(active_style, STYLE_STOCK_RULES["D"])
    allow_roles = style_rule["allow_role"]
    forbid_roles = style_rule["forbid_role"]

    # 检查是否有越界选股
    violations = []
    for role_name, role_key in [("核心龙头", "core"), ("补涨备选", "fill"), ("低位潜伏", "latent")]:
        if role_name in forbid_roles and len(selected[role_key]) > 0:
            violations.append(f"{style_rule['name']}禁止选{role_name}, 但入选{len(selected[role_key])}只")
            logging.warning(f"  ⚠️ 违规: {violations[-1]}")

    # 按风格规则收紧选股池 (仅保留允许的角色)
    filtered = {}
    for role_key, role_name in [("core", "核心龙头"), ("fill", "补涨备选"), ("latent", "低位潜伏")]:
        if role_name in allow_roles:
            filtered[role_key] = selected[role_key]
        else:
            filtered[role_key] = []

    # 5. 心理编码
    psy_added = apply_psy_for_direction(main_line, driver_type, active_style, len(eliminated))

    # 6. 输出
    logging.info(f"  → 驱动有效周期: {driver_validity}")
    logging.info(f"  → 保留/剔除: {len(kept)}/{len(eliminated)} 只")
    logging.info(f"  → 分层: 龙头{len(filtered['core'])} 补涨{len(filtered['fill'])} 潜伏{len(filtered['latent'])}")
    logging.info(f"  → 风格规则: {style_rule['desc']}")
    if violations:
        logging.warning(f"  → 违规: {violations}")
    logging.info(f"  → psy_hit_codes 当前: {psy_hit_codes}")
    logging.info("Module03 定方向 完成")
    logging.info("=" * 50)

    return {
        "main_line": main_line or "无主线(混沌)",
        "driver_type": driver_type,
        "driver_detail": driver_detail,
        "driver_validity": driver_validity,
        "stocks_raw": len(candidate_stocks),
        "eliminated": eliminated,
        "selected_raw": selected,
        "selected_filtered": filtered,
        "violations": violations,
        "style_rule": style_rule,
        "psy_codes_added": psy_added,
    }


# ====================== 测试 ======================
if __name__ == "__main__":
    from psy_hit_manager import clear_all_psy_codes
    clear_all_psy_codes()

    test_cases = [
        {
            "name": "A模式-有主线业绩驱动",
            "main_line": "固态电池",
            "driver_type": "业绩",
            "driver_detail": "2026H1业绩预增262~334%",
            "style": "A",
            "stocks": [
                {"code":"600884","name":"杉杉股份","role":"核心龙头","reason":"负极材料龙头,业绩大增","volume_ratio":1.6},
                {"code":"300476","name":"胜宏科技","role":"补涨备选","reason":"PCB跟涨","volume_ratio":0.8},
                {"code":"000***","name":"杂毛票","role":"杂毛","reason":"非板块核心","volume_ratio":0.3},
            ]
        },
        {
            "name": "C模式+题材错配",
            "main_line": "锂电",
            "driver_type": "题材",
            "driver_detail": "固态电池概念炒作",
            "style": "C",
            "stocks": [
                {"code":"600884","name":"杉杉股份","role":"低位潜伏","reason":"回调到位","volume_ratio":0.6},
            ]
        },
        {
            "name": "无主线混沌",
            "main_line": "",
            "driver_type": "题材",
            "driver_detail": "无明确主线",
            "style": "D",
            "stocks": []
        },
    ]

    for tc in test_cases:
        clear_all_psy_codes()
        print(f"\n--- {tc['name']} ---")
        r = run_module03(tc["main_line"], tc["driver_type"], tc["driver_detail"], tc["stocks"], tc["style"])
        print(f"  主线: {r['main_line']} | 驱动: {r['driver_type']}({r['driver_validity']})")
        print(f"  保留: 龙头{r['selected_filtered']['core']} 补涨{r['selected_filtered']['fill']} 潜伏{r['selected_filtered']['latent']}")
        print(f"  剔除: {len(r['eliminated'])}只")
        print(f"  psy: {psy_hit_codes}")
