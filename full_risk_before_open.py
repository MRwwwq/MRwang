"""
两层风控统一入口：静态硬性风控 → AI动态预判风控
"""
from static_hard_risk_control import StaticHardRiskControl
from dynamic_ai_risk import DynamicAIRiskControl

# 初始化风控实例
static_risk = StaticHardRiskControl()
dynamic_risk = DynamicAIRiskControl()


def full_risk_before_open(stock_row, raw_target_pos):
    """
    多层风控统一入口
    :param stock_row: dict/Series, 含 ts_code / industry
    :param raw_target_pos: float, 原始目标仓位(0~1)
    :return: (allow: bool, log_text: str, final_position: float)
    """
    ts_code = stock_row["ts_code"]
    industry = stock_row["industry"]

    log_list = []

    # 第一层：静态硬性风控，直接拦截超限委托
    static_ok, static_log = static_risk.check_all_static_constraint(ts_code, industry, raw_target_pos)
    log_list.append("===== 静态硬约束风控 =====")
    log_list.append(static_log)
    if not static_ok:
        return False, "\n".join(log_list), 0

    # 第二层：AI动态预判风控，输出自适应仓位系数
    dynamic_ok, dynamic_log, pos_coeff = dynamic_risk.full_dynamic_risk_check(ts_code, industry)
    log_list.append("\n===== AI动态预判风控 =====")
    log_list.append(dynamic_log)
    if not dynamic_ok:
        return False, "\n".join(log_list), 0

    # 计算最终可下单仓位
    final_position = raw_target_pos * pos_coeff
    log_list.append(
        f"\n✅ 多层风控全部通过，原始仓位{raw_target_pos:.4f}，"
        f"自适应调整后仓位{final_position:.4f}"
    )

    return True, "\n".join(log_list), final_position
