#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
psy_hit_manager.py — 心理误判编码全局管理器

全局变量:
    psy_hit_codes: list[str]  — 全流程触发的芒格心理误判编码，自动去重

原子操作:
    add_psy_code(code)        — 不存在则新增，存在则跳过
    remove_psy_code(code)     — 仅 Module01 有权限调用，存在则移除
    get_psy_hit_count()       — 返回当前误判总数，只读
    clear_all_psy_codes()     — 仅盘前初始化执行，盘中禁止

权限规则:
    - Module01/02/03: 只能新增，不能删除
    - Module01 额外拥有 remove_psy_code 权限 (A/B切换场景)
    - Layer1 风控: 只读 get_psy_hit_count(), 不能修改
    - 所有编码变动记录日志

流转规则:
    盘前 clear_all_psy_codes() → Module01/02/03 根据判定新增 → Layer1 只读计数
"""

import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [psy_hit] %(message)s",
    datefmt="%H:%M:%S"
)

# ====================== 全局变量 ======================
psy_hit_codes: list[str] = []

# ====================== 原子操作 ======================

def add_psy_code(code: str) -> None:
    """新增心理误判编码（自动去重）"""
    global psy_hit_codes
    if not isinstance(code, str) or len(code.strip()) == 0:
        return
    if code not in psy_hit_codes:
        psy_hit_codes.append(code)
        logging.info(f"🟢 psy_hit +1 [{code}] 当前共{len(psy_hit_codes)}项")
    else:
        logging.debug(f"⏭️ psy_hit 重复跳过 [{code}]")


def remove_psy_code(code: str) -> None:
    """移除指定心理误判编码（仅Module01 A/B切换场景调用）"""
    global psy_hit_codes
    if code in psy_hit_codes:
        psy_hit_codes.remove(code)
        logging.info(f"🔴 psy_hit -1 [{code}] 剩余{len(psy_hit_codes)}项")
    else:
        logging.warning(f"⚠️ psy_hit 移除不存在的编码 [{code}]")


def get_psy_hit_count() -> int:
    """返回当前心理误判触发总数（Layer1风控只读调用）"""
    return len(psy_hit_codes)


def clear_all_psy_codes() -> None:
    """清空全部心理误判编码（仅盘前初始化执行，盘中禁止）"""
    global psy_hit_codes
    count = len(psy_hit_codes)
    psy_hit_codes = []
    logging.info(f"🧹 psy_hit 清空完毕 (移除{count}项) — {'盘前初始化' if count > 0 else '无历史数据'}")
