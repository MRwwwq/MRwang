# -*- coding: utf-8 -*-
"""
param_bound_control.py — 参数进化边界人工管控脚本
===================================================
功能: 每日自进化微调后执行边界管控, 检测极端异动+回滚+锁定底层风控红线

═══════════════════════════════════════════════
人工限制参数进化边界完整操作规范 (永久归档)
═══════════════════════════════════════════════

1. 执行时机
   每日盘后校准、错误案例录入完成后, 读取当日自动调参生成的参数快照执行管控

2. 两类核心管控动作

   管控1: 拦截极端参数变动, 防止过度拟合
   ─────────────────────────────────────────
   - 人工预设全参数安全变动区间 (如高PE风险阈值限定10~35)
   - 若AI自主调整后数值超出区间, 判定为适配短期行情的极端拟合行为
   - 一键回滚至历史验证稳定的基准参数
   - 快照标记人工回滚记录, 永久留痕

   管控2: 锁死不可修改的底层风控红线, 禁止AI自主删除/改动
   ─────────────────────────────────────────
   强制固定2条核心规则, 参数永久锁定等于3, 任何自动调参不得修改:
   ① 连续3日大额资金流出触发减仓规则 (capital_outflow_days)
   ② 入场必须同时满足3项基础入场条件 (entry_condition_count)
   一旦检测AI改动该类参数, 强制覆盖回锁标准值, 阻断风控规则失效风险

3. 底层作用说明
   单纯依靠AI自主迭代会无限贴合短期单日/单周行情, 产生过度拟合.
   短期回测收益好看, 但切换市场环境后策略全面失效.
   人工划定参数进化安全边界、锁死基础风控底线,
   约束AI优化方向, 保障模型长期行情普适性、稳定性.

4. 联动迭代机制
   周/月度迭代加载参数管控日志, 统计频繁出现极端波动的因子,
   适度缩小该因子下次迭代允许调整幅度, 从源头减少参数失控问题.

═══════════════════════════════════════════════

兜底强制约束:
  当日任意盘后操作未完整完成(校准录入/错误案例/参数管控),
  周度自动调参脚本触发阻断逻辑, 打印缺失清单并 exit(1) 终止迭代.
  仅当日全套人工操作全部录入校验通过, 智能体才能执行复盘与因子权重自适应优化.

三层管控机制:
  第一层: 极端参数检测: 参数超出安全区间 → 自动回滚至稳定基准
  第二层: 风控红线锁定: entry_condition_count/capital_outflow_days 锁定3
  第三层: 人工干预窗口: 管控记录全留痕, 支持手动调整安全区间

执行时机:
  每日: 自动微调后执行 (vendor in evolution_daemon.py)
  手动: python3 param_bound_control.py [YYYYMMDD]

管控参数表:
  factor_param_snap          — 当日自动微调参数快照
  factor_param_stable_base   — 稳定基准参数 (人工固化, 不可自动修改)
"""

import sys
import psycopg2
from datetime import datetime, date
from decimal import Decimal

# ═══════════════════════════════════════════════
#  数据库连接
# ═══════════════════════════════════════════════

DB_CONFIG = {
    "dbname": "stock_data",
    "user": "stock_user",
    "password": "stock123",
    "host": "127.0.0.1",
    "port": "5432",
}


def get_pg_conn():
    """获取数据库连接"""
    return psycopg2.connect(**DB_CONFIG)


# ═══════════════════════════════════════════════
#  第一关: 获取参数快照 + 稳定基准
# ═══════════════════════════════════════════════

def get_param_snapshot(trade_date: str):
    """
    拉取两套参数对照:
      1. 当日最新自动微调参数快照 (factor_param_snap)
      2. 最新稳定基准参数 (factor_param_stable_base)

    :return: (latest_snap, stable_base_dict)
      latest_snap = list[(param_name, param_value, param_type, snap_time)]
      stable_base_dict = {param_name: stable_value}
    """
    conn = get_pg_conn()
    cur = conn.cursor()

    # 当日最新自动微调参数
    cur.execute("""
        SELECT param_name, param_value, param_type, update_snap_time
        FROM factor_param_snap
        WHERE trade_date = %s
        ORDER BY update_snap_time DESC
    """, (trade_date,))
    latest_snap = cur.fetchall()

    # 最近稳定基准参数
    cur.execute("""
        SELECT param_name, param_value
        FROM factor_param_stable_base
    """)
    stable_base = {row[0]: float(row[1]) for row in cur.fetchall()}

    cur.close()
    conn.close()
    return latest_snap, stable_base


# ═══════════════════════════════════════════════
#  第二关: 极端参数校验 (过度拟合检测)
# ═══════════════════════════════════════════════

def check_extreme_param_change(latest_snap, stable_base):
    """
    校验当日快照参数是否超出人工设定安全区间

    管控安全区间:
      pe_risk_threshold:     10 ~ 35     (高PE风险阈值)
      entry_condition_count: 3 ~ 3       (入场条件,锁死3)
      capital_outflow_days:  3 ~ 3       (减仓红线,锁死3)
      chip_support_offset:   0.8 ~ 1.3   (筹码偏移系数)

    :return: [ {param_name, param_cn, new_val, stable_base_val, safe_range, snap_time}, ... ]
    """
    control_config = {
        "pe_risk_threshold":     {"min": 10, "max": 35, "name": "高PE风险阈值"},
        "entry_condition_count": {"min": 3,  "max": 3,  "name": "入场条件数量(底层风控红线)"},
        "capital_outflow_days":  {"min": 3,  "max": 3,  "name": "连续资金流出减仓天数(底层风控红线)"},
        "chip_support_offset":   {"min": 0.8,"max": 1.3,"name": "筹码支撑偏移系数"},
    }

    extreme_list = []
    for param_name, val, ptype, snap_time in latest_snap:
        cfg = control_config.get(param_name)
        if not cfg:
            continue
        val_f = float(val)

        # 超出安全区间 = 极端异动
        if val_f < cfg["min"] or val_f > cfg["max"]:
            base_val = stable_base.get(param_name, 0)
            extreme_list.append({
                "param_name": param_name,
                "param_cn": cfg["name"],
                "new_val": val_f,
                "stable_base_val": base_val,
                "safe_range": f'{cfg["min"]} ~ {cfg["max"]}',
                "snap_time": snap_time,
            })

    return extreme_list


# ═══════════════════════════════════════════════
#  第三关: 底层风控红线锁死校验
# ═══════════════════════════════════════════════

def lock_core_risk_rule_check(latest_snap):
    """
    校验 entry_condition_count 和 capital_outflow_days
    是否被AI自主修改偏离标准值3

    :return: [ {param_cn, param_name, illegal_val, fixed_standard}, ... ]
    """
    core_lock_params = ["entry_condition_count", "capital_outflow_days"]
    violate_list = []

    for p_name, val, ptype, snap_time in latest_snap:
        if p_name in core_lock_params and float(val) != 3.0:
            violate_list.append({
                "param_cn": "入场条件/连续流出减仓风控红线",
                "param_name": p_name,
                "illegal_val": float(val),
                "fixed_standard": 3.0,
            })

    return violate_list


# ═══════════════════════════════════════════════
#  回滚: 极端参数 → 稳定基准值
# ═══════════════════════════════════════════════

def rollback_to_stable_base(trade_date, extreme_items, operator):
    """
    覆写 factor_param_snap 中超标参数为稳定基准值
    标记 rollback_mark + rollback_operator + rollback_time

    :param trade_date: 交易日 YYYYMMDD
    :param extreme_items: 超标参数列表
    :param operator: 操作人
    """
    conn = get_pg_conn()
    cur = conn.cursor()

    for item in extreme_items:
        p_name = item["param_name"]
        base_val = item["stable_base_val"]

        cur.execute("""
            UPDATE factor_param_snap
            SET param_value = %s,
                rollback_mark = '已人工回滚极端参数',
                rollback_operator = %s,
                rollback_time = now()
            WHERE trade_date = %s AND param_name = %s
        """, (base_val, operator, trade_date, p_name))

        print(
            f"\033[91m已回滚极端参数 {item['param_cn']}: "
            f"新值 {item['new_val']} → 稳定基准值 {base_val}\033[0m"
        )

    conn.commit()
    cur.close()
    conn.close()


# ═══════════════════════════════════════════════
#  主管控入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    TODAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    OP_USER = "admin"

    print(f"===== 启动 {TODAY} 参数进化边界人工管控流程 =====")

    # 步骤1: 拉取参数快照 + 稳定基准
    latest_snap, stable_dict = get_param_snapshot(TODAY)

    if not latest_snap:
        print("当日无参数微调快照, 无需管控, 流程结束")
        sys.exit(0)

    print(f"参数快照: {len(latest_snap)}项 | 稳定基准: {len(stable_dict)}项")

    # 步骤2: 检测极端异动
    extreme_items = check_extreme_param_change(latest_snap, stable_dict)

    # 步骤3: 检测底层风控红线篡改
    lock_violate = lock_core_risk_rule_check(latest_snap)

    # 步骤4: 执行回滚
    if extreme_items:
        print(f"\n⚠️ 检测到 {len(extreme_items)} 项极端异动参数")
        rollback_to_stable_base(TODAY, [
            {**item, "stable_base_val": item["stable_base_val"]}
            for item in extreme_items
        ], OP_USER)
    else:
        print("\033[92m✅ 当日无极端异动参数, 无需回滚\033[0m")

    if lock_violate:
        print(f"\n🚨 检测到底层风控红线被AI自主修改 ({len(lock_violate)}项)")
        for v in lock_violate:
            print(f"  {v['param_name']}: {v['illegal_val']} → 强制锁死 {v['fixed_standard']}")
        lock_items = [
            {**v, "stable_base_val": v["fixed_standard"], "new_val": v["illegal_val"]}
            for v in lock_violate
        ]
        rollback_to_stable_base(TODAY, lock_items, OP_USER)
    else:
        print("\033[92m✅ 核心风控规则保持锁死标准, 无违规修改\033[0m")

    print(f"\n✅ {TODAY} 参数边界管控完成, 规避短期行情过度拟合风险\n")
