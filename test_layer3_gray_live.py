"""
test_layer3_gray_live.py — 第三层：小资金灰度实盘测试
最小资金量实盘运行，监控真实市场环境下的系统行为
"""
import json
import os
from datetime import datetime

REPORT_DIR = "/opt/stock_agent/test_reports"

# 第三层强制通过标准
GRAY_STANDARDS = {
    "min_trading_days": 10,           # 最少运行10个交易日
    "max_daily_loss": 0.02,           # 单日最大亏损≤2%
    "max_total_drawdown": 0.08,       # 灰度期整体回撤≤8%
    "system_uptime": 0.99,            # 系统运行正常率≥99%
    "order_error_rate": 0.01,         # 委托异常率≤1%
}


class Layer3GrayLive:
    """第三层：小资金灰度实盘测试"""

    def __init__(self):
        os.makedirs(REPORT_DIR, exist_ok=True)
        self.state = {
            "start_date": None,
            "trading_days": 0,
            "daily_pnl": [],
            "total_pnl": 0.0,
            "system_errors": 0,
            "order_errors": 0,
            "total_orders": 0,
            "active": False,
        }

    def start(self, initial_capital=100000):
        """启动灰度实盘"""
        self.state["start_date"] = datetime.now().strftime("%Y-%m-%d")
        self.state["active"] = True
        self.state["initial_capital"] = initial_capital
        print(f"[Layer3] 🟢 灰度实盘启动: 资金{initial_capital/10000:.0f}万")
        print(f"[Layer3] 最少运行{SIM_STANDARDS.get('min_trading_days', 10)}个交易日")
        return self.state

    def daily_report(self, pnl_rate, system_ok=True, order_ok=True):
        """每日收盘后记录灰度数据"""
        if not self.state["active"]:
            return
        self.state["trading_days"] += 1
        self.state["daily_pnl"].append(pnl_rate)
        self.state["total_pnl"] += pnl_rate
        if not system_ok:
            self.state["system_errors"] += 1
        if not order_ok:
            self.state["order_errors"] += 1
        self.state["total_orders"] += 1
        print(f"[Layer3] 日{self.state['trading_days']}: PnL={pnl_rate:.2%}")

    def check_readiness(self, sandbox_results=None):
        """检查沙盒模拟结果是否达到灰度门槛"""
        if not sandbox_results:
            return False, "无沙盒测试结果"
        std = sandbox_results.get("standards", {})
        if not std.get("pass", False):
            return False, "沙盒测试未通过"
        return True, "沙盒通过，可进入灰度"

    def evaluate(self):
        """灰度期结束后的评估"""
        days = self.state["trading_days"]
        if days < SIM_STANDARDS.get("min_trading_days", 10):
            return {"pass": False, "reason": f"运行天数不足({days}<{SIM_STANDARDS.get('min_trading_days', 10)})"}

        daily = self.state["daily_pnl"]
        max_daily_loss = min(daily) if daily else 0
        cum = sum(daily)
        peak = 0
        max_dd = 0
        for p in daily:
            peak = max(peak, p)
            dd = (peak - cum) / max(peak, 1e-6)
            max_dd = max(max_dd, dd)  # simplified

        uptime = 1 - self.state["system_errors"] / max(days, 1)
        order_err_rate = self.state["order_errors"] / max(self.state["total_orders"], 1)

        standards = {
            "min_trading_days": {"value": days, "threshold": GRAY_STANDARDS["min_trading_days"],
                                  "pass": days >= GRAY_STANDARDS["min_trading_days"]},
            "max_daily_loss": {"value": round(abs(max_daily_loss), 4), "threshold": GRAY_STANDARDS["max_daily_loss"],
                                "pass": abs(max_daily_loss) <= GRAY_STANDARDS["max_daily_loss"]},
            "max_total_drawdown": {"value": round(max_dd, 4), "threshold": GRAY_STANDARDS["max_total_drawdown"],
                                    "pass": max_dd <= GRAY_STANDARDS["max_total_drawdown"]},
            "system_uptime": {"value": round(uptime, 4), "threshold": GRAY_STANDARDS["system_uptime"],
                               "pass": uptime >= GRAY_STANDARDS["system_uptime"]},
            "order_error_rate": {"value": round(order_err_rate, 4), "threshold": GRAY_STANDARDS["order_error_rate"],
                                  "pass": order_err_rate <= GRAY_STANDARDS["order_error_rate"]},
        }
        all_pass = all(v["pass"] for v in standards.values())
        standards["pass"] = all_pass
        self.state["evaluation"] = standards

        # 报告
        lines = [
            "=" * 60,
            "第三层：小资金灰度实盘测试报告",
            "=" * 60,
            f"运行周期: {self.state['start_date']} ~ {datetime.now().strftime('%Y-%m-%d')}",
            f"交易日: {days}天",
            f"累计PnL: {cum:.2%}",
            f"最大单日亏损: {abs(max_daily_loss):.2%}",
            f"整体回撤: {max_dd:.2%}",
            f"系统正常率: {uptime:.1%}",
            f"委托异常率: {order_err_rate:.1%}",
            "",
            "── 5项通过标准 ──",
        ]
        for k, v in standards.items():
            if k in ("pass",):
                continue
            icon = "✅" if v["pass"] else "❌"
            lines.append(f"  {icon} {k}: {v.get('value','')} (阈值{v.get('threshold','')})")
        lines.append("")
        if all_pass:
            lines.append("✅ 全部达标，灰度结束，可切换全量实盘")
        else:
            lines.append("❌ 不达标，退回进化Agent迭代优化")
        lines.append("=" * 60)

        rpt = "\n".join(lines)
        fname = f"{REPORT_DIR}/layer3_{datetime.now().strftime('%Y%m%d')}.md"
        with open(fname, "w") as f:
            f.write(rpt)
        jname = f"{REPORT_DIR}/layer3_{datetime.now().strftime('%Y%m%d')}.json"
        with open(jname, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2, default=str)

        print(f"[Layer3] 报告已保存: {fname}")
        return standards


# 使用§4的灰度标准作为SIM_STANDARDS引用
SIM_STANDARDS = GRAY_STANDARDS
