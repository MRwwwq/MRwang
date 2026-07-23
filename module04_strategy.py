#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module04 定策略 — 今日可交易标的池生成

时序: Module03 定方向之后, Layer1 风控之前
流程:
    1. 读取 active_style → 锁定唯一交易模式
    2. 过滤黑名单 (global_rule / memory_failure_signal)
    3. 风格规则收紧选股池 (复用 Module03 的 selected_filtered)
    4. 仓位约束分发 (Module01风格仓位 → Module02情绪修正 → 单票分配)
    5. 全部标的推送 Layer1 风控校验
"""

import logging
import sqlite3
from pathlib import Path
from psy_hit_manager import add_psy_code, psy_hit_codes, get_psy_hit_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s [M04] %(message)s", datefmt="%H:%M:%S")

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"

# ====================== 黑名单加载 ======================

def load_blacklist() -> list:
    """从 agent_memory.db 的 global_rule 表读取黑名单"""
    blacklist = []
    try:
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='global_rule'")
        if cur.fetchone():
            cur.execute("SELECT distinct rule_value FROM global_rule WHERE rule_key='black_stock'")
            rows = cur.fetchall()
            for r in rows:
                blacklist.append(r[0].strip())
        cur.close()
        conn.close()
    except Exception as e:
        logging.warning(f"⚠️ 黑名单读取失败: {e}")
    logging.info(f"  📋 黑名单加载: {len(blacklist)}只")
    return blacklist


def load_failure_signals() -> list:
    """从 memory_failure_signal 表读取高等级失效信号"""
    blocked = []
    try:
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        if "memory_failure_signal" in table_names:
            cur.execute(
                "SELECT ts_code FROM memory_failure_signal "
                "WHERE warning_level >= 5 "
                "GROUP BY ts_code"
            )
            for r in cur.fetchall():
                blocked.append(r[0].strip())
        cur.close()
        conn.close()
    except Exception as e:
        logging.warning(f"⚠️ failure_signal读取失败: {e}")
    return blocked


# ====================== 单票仓位分配 ======================

def calc_per_stock_cap(
    total_cap: int,
    per_stock_max: int,
    stock_count: int,
    sentiment_label: str,
) -> int:
    """
    计算单票仓位上限 (取三者的最小值):
    1. 总仓/标的数 (分散)
    2. 风格单票上限
    3. 情绪阶段强制约束
    """
    if stock_count == 0:
        return 0

    dispersed = total_cap // stock_count

    # 情绪阶段对单票的附加约束
    sentiment_per_stock_extra = {
        "ice":       5,    # 冰点单票≤5%
        "recovery":  0,    # 回暖不额外限制
        "boom":      10,   # 高潮单票≤10%
        "recession": 5,    # 退潮单票≤5%
    }
    extra = sentiment_per_stock_extra.get(sentiment_label, 0)

    final = min(dispersed, per_stock_max)
    if extra > 0:
        final = min(final, extra)

    return final


# ====================== 520信号二次复核 ======================

def check_520_secondary_review(stock: dict, main_line: str = "",
                                fund_catalyst: bool = True) -> dict:
    """520二次复核: 主线题材+资金流+黑名单+芒格风控。

    用于M03→M04过程中，所有520合格标的必须通过此复核。
    520多头信号仅作为加分因子，无独立开仓权限。

    stock字段: {code, name, signal_520_passed, is_blacklisted, ...,
                dragon_return_520}

    返回: {"allowed": bool, "reason": str, "weight": float}
    """
    code = stock.get("code", "")
    name = stock.get("name", "")

    # 黑名单一票否决
    if stock.get("is_blacklisted", False):
        return {"allowed": False, "reason": f"黑名单拦截 {code}({name})", "weight": 0}

    # 520信号基础校验
    signal_520_passed = stock.get("signal_520_passed", False)
    if not signal_520_passed:
        return {"allowed": False, "reason": f"{code} 520信号不满足", "weight": 0}

    # 主线题材复核（空=无主线，不硬拦但降权）
    if not main_line or main_line.strip() == "":
        return {"allowed": True, "reason": f"{code} 无主线, 520降级观察",
                "weight": 0.6}

    # 资金流向复核
    if not fund_catalyst:
        return {"allowed": False, "reason": f"{code} 资金面偏空", "weight": 0}

    return {"allowed": True, "reason": f"{code} 520二次复核通过 ✅", "weight": 1.0}


# ====================== 520信号开仓校验（D模式+芒格风控联动） ======================

def check_520_open_signal(stock: dict,
                          signal_520_weight: float = 1.0,
                          sentiment_label: str = "recovery",
                          psy_hit_codes_global: list = None) -> dict:
    """校验520信号+资金催化+黑名单+芒格误判风控，判定是否可生成开仓信号。

    stock字段:
        {code, name, role, signal_520_passed (bool), signal_520_blocked_by (str),
         fund_catalyst (bool, 可选), is_blacklisted (bool),
         dragon_return_520 (dict, 可选)}

    psy_hit_codes_global: 全局psy_hit_codes列表, 用于三重共振拦截

    返回: {"allowed": bool, "reason": str, "weight": float,
           "lolla_blocked": bool, "lolla_details": dict}
    """
    code = stock.get("code", "")
    name = stock.get("name", "")
    lolla_details = {}

    # ───── 第1关: 黑名单一票否决 ─────
    if stock.get("is_blacklisted", False):
        return {"allowed": False, "reason": f"黑名单拦截 {code}({name})",
                "weight": 0, "lolla_blocked": False, "lolla_details": {}}

    # ───── 第2关: 520信号校验 ─────
    signal_520_passed = stock.get("signal_520_passed", False)
    signal_520_blocked_by = stock.get("signal_520_blocked_by", "")
    if not signal_520_passed:
        return {"allowed": False, "reason": f"{code} 520信号未通过: {signal_520_blocked_by[:60]}",
                "weight": 0, "lolla_blocked": False, "lolla_details": {}}

    # ───── 第3关: 资金催化 ─────
    fund = stock.get("fund_catalyst", True)
    if not fund:
        return {"allowed": False, "reason": f"{code} 资金面偏空",
                "weight": 0, "lolla_blocked": False, "lolla_details": {}}

    # ───── 第4关: 芒格误判风控（读取全局psy_hit_codes） ─────
    LOLLA_TRIPLE = {
        "code_04_避免怀疑": "避免怀疑",
        "code_14_损失厌恶": "损失厌恶",
        "code_08_嫉妒猜忌": "嫉妒猜忌",
    }
    codes_source = psy_hit_codes_global or []
    active_lolla = {k: v for k, v in LOLLA_TRIPLE.items() if k in codes_source}

    lolla_details = {
        "active_codes": list(active_lolla.keys()),
        "active_names": list(active_lolla.values()),
        "count": len(active_lolla),
        "total_psy_hit": len(codes_source),
    }
    lolla_blocked = len(active_lolla) >= 2 or len(codes_source) >= 3
    if lolla_blocked:
        return {
            "allowed": False,
            "reason": (
                f"🚫 Lollapalooza风控拦截 {code}: "
                f"{len(active_lolla)}项三重共振({','.join(active_lolla.values())}), "
                f"psy高分{len(codes_source)}项"
            ),
            "weight": 0,
            "lolla_blocked": True,
            "lolla_details": lolla_details,
        }

    # ───── 第5关: 520权重联动 ─────
    # signal_520_weight==0.0 → 不生成开仓
    if signal_520_weight == 0.0:
        return {"allowed": False, "reason": f"{code} 520权重=0.0, 情绪屏蔽不开仓",
                "weight": 0, "lolla_blocked": False, "lolla_details": lolla_details}

    return {
        "allowed": True,
        "reason": f"{code} 520通过+资金催化 OK (M02权重×{signal_520_weight})",
        "weight": signal_520_weight,
        "lolla_blocked": False,
        "lolla_details": lolla_details,
    }


# ====================== 主入口 ======================

def run_module04(
    active_style: str,
    style_name: str,
    total_cap: int,           # 已修正的 final_total_cap (min风格,情绪)
    per_stock_max: int,       # Module01 style里定义的
    stop_loss_pct: float,     # Module01 style里定义的
    sentiment_label: str,     # Module02 情绪标签
    selected_filtered: dict,  # Module03 输出的 {core, fill, latent}
    main_line: str = "",      # M03主线, 用于520二次复核
    signal_520_weight: float = 1.0,  # M02 520权重 (0.0/0.4/1.0)
) -> dict:
    """
    Module04 定策略主入口。

    返回:
        {
            "active_style": str,
            "total_cap": int,
            "per_stock_cap": int,
            "stop_loss_pct": float,
            "standards": {style对应仓位标准},
            "candidates_before_blacklist": int,
            "blacklist_blocked": [被拦截标的],
            "failure_blocked": [高等级失效拦截],
            "tradeable_pool": [
                {code, name, role, alloc_pct, stop_loss}
            ],
            "pool_sent_to_layer1": True,
        }
    """
    logging.info("=" * 50)
    logging.info("Module04 定策略 启动")
    logging.info(f"  风格: {active_style}({style_name}) | 总仓: {total_cap}% | 情绪: {sentiment_label}")

    # 1. 读取风格标准仓位参数
    style_standards = {
        "total": total_cap,
        "per_stock": per_stock_max,
        "stop_loss": stop_loss_pct,
    }
    logging.info(f"  仓位标准: 总{total_cap}% 单票{per_stock_max}% 止损{stop_loss_pct}%")

    # 2. 加载黑名单
    blacklist = load_blacklist()
    failure_blocked_codes = load_failure_signals()

    # 合并拦截列表
    all_blocked = set(blacklist + failure_blocked_codes)

    # 3. 读取 Module03 递送的选股池 (展平)
    all_candidates = []
    for role_key, role_name in [("core", "核心龙头"), ("fill", "补涨备选"), ("latent", "低位潜伏")]:
        for s in selected_filtered.get(role_key, []):
            all_candidates.append({**s, "role_name": role_name})

    logging.info(f"  Module03递送: {len(all_candidates)}只待审")

    # 4. 过滤黑名单
    blacklist_blocked = []
    tradeable = []
    for s in all_candidates:
        code = s.get("code", "")
        # 检查黑名单 (支持.SH/.SZ后缀匹配)
        blocked = False
        for b in all_blocked:
            if b in code or code in b:
                blacklist_blocked.append(code)
                logging.warning(f"  🚫 黑名单拦截: {code}({s.get('name','')}) — 匹配规则:{b}")
                blocked = True
                break
        if not blocked:
            tradeable.append(s)

    logging.info(f"  黑名单拦截: {len(blacklist_blocked)}只 | 可交易池: {len(tradeable)}只")

    # 5. 单票仓位分配
    stock_count = len(tradeable)
    per_stock_cap = calc_per_stock_cap(total_cap, per_stock_max, stock_count, sentiment_label)

    # 6. 生成最终交易池 (含520二次复核 + 芒格风控 + 仓位分配)
    tradeable_pool = []
    lolla_blocked_count = 0
    blacklist_blocked_count = len(blacklist_blocked)
    signal_520_fail_count = 0

    for s in tradeable:
        s["is_blacklisted"] = False  # 已过黑名单

        # 6a. 全部标的执行520二次复核 + 芒格风控联检
        sec_review = check_520_secondary_review(s, main_line=main_line,
                                                 fund_catalyst=True)
        if not sec_review["allowed"]:
            logging.info(f"  ⏭️ 二次复核过滤 {s.get('code','')}: {sec_review['reason']}")
            signal_520_fail_count += 1
            continue

        # 6b. 芒格风控: 三重共振拦截（loss aversion + avoid doubt + jealousy）
        lolla_check = check_520_open_signal(
            s, signal_520_weight=signal_520_weight,
            sentiment_label=sentiment_label,
            psy_hit_codes_global=psy_hit_codes if hasattr(psy_hit_codes, '__iter__') else [],
        )
        if lolla_check["lolla_blocked"]:
            logging.warning(f"  🚫 芒格拦截 {s.get('code','')}: {lolla_check['reason']}")
            lolla_blocked_count += 1
            continue

        # 6c. 仓位分配
        if active_style == "D":
            # D模式: 极小仓潜伏, 强制校验520信号
            alloc_pct = min(per_stock_cap, 3)  # D模式单票≤3%
        else:
            alloc_pct = per_stock_cap

        # 龙回头标记
        dragon = s.get("dragon_return_520", {}).get("signal", False)
        role_name_suffix = " (龙回头)" if dragon else ""

        tradeable_pool.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "role": s.get("role_name", "") + role_name_suffix,
            "alloc_pct": alloc_pct,
            "stop_loss": stop_loss_pct,
            "reason": s.get("reason", ""),
            "signal_520_passed": s.get("signal_520_passed", False),
            "dragon_return": dragon,
        })

    # 统计520信号: 全部候选标的的520通过率
    total_candidates = len(all_candidates)
    blacklisted = len(blacklist_blocked) + len(failure_blocked_codes)
    signal_520_not_pass = signal_520_fail_count
    mood_520_blocked = lolla_blocked_count  # 芒格拦截

    logging.info(f"  📊 520四重锁统计:")
    logging.info(f"     原始候选: {total_candidates}只")
    logging.info(f"     黑名单拦截: {blacklisted}只")
    logging.info(f"     520信号+复核拦截: {signal_520_not_pass}只")
    logging.info(f"     芒格风控拦截: {mood_520_blocked}只")
    logging.info(f"     最终可交易: {len(tradeable_pool)}只")

    # 7. 输出
    logging.info(f"  单票仓位: {per_stock_cap}% (总仓{total_cap}%/{stock_count}只)")
    logging.info(f"  止损: {stop_loss_pct}%")
    pool_str = ", ".join([f"{s['code']}({s['name']},{s['role']},{s['alloc_pct']}%)" for s in tradeable_pool])
    logging.info(f"  可交易池: [{pool_str}]")
    logging.info(f"  ✅ 全部推送 Layer1 风控校验流水线")
    logging.info("Module04 定策略 完成")
    logging.info("=" * 50)

    return {
        "active_style": active_style,
        "total_cap": total_cap,
        "per_stock_cap": per_stock_cap,
        "stop_loss_pct": stop_loss_pct,
        "standards": style_standards,
        "candidates_before_blacklist": len(all_candidates),
        "blacklist_blocked": blacklist_blocked,
        "failure_blocked": failure_blocked_codes,
        "tradeable_pool": tradeable_pool,
        "pool_sent_to_layer1": True,
        "signal_520_weight": signal_520_weight,
        "signal_520_secondary_review_count": signal_520_fail_count,
        "lolla_blocked_count": lolla_blocked_count,
        "dragon_return_count": sum(1 for s in tradeable_pool if s.get("dragon_return")),
    }


# ====================== 测试 ======================
if __name__ == "__main__":
    from psy_hit_manager import clear_all_psy_codes
    clear_all_psy_codes()

    print("\n=== Module04 定策略 测试 ===\n")

    # 模拟输入
    test_input = {
        "active_style": "A",
        "style_name": "龙头连板",
        "total_cap": 20,           # Module01=60, Module02高潮→min=20
        "per_stock_max": 25,
        "stop_loss_pct": 3.0,
        "sentiment_label": "boom", # 高潮
        "selected_filtered": {
            "core": [
                {"code":"600884","name":"杉杉股份","role":"核心龙头","reason":"负极材料龙头,业绩大增"},
            ],
            "fill": [],
            "latent": [],
        }
    }

    result = run_module04(**test_input)
    print(f"\n结果:")
    print(f"  总仓: {result['total_cap']}% | 单票: {result['per_stock_cap']}% | 止损: {result['stop_loss_pct']}%")
    print(f"  候选/拦截/可交易: {result['candidates_before_blacklist']}/{len(result['blacklist_blocked'])}/{len(result['tradeable_pool'])}")
    print(f"  可交易池: {[(s['code'],s['alloc_pct']) for s in result['tradeable_pool']]}")
    print(f"  推送Layer1: {result['pool_sent_to_layer1']}")
