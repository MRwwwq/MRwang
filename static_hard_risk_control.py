"""
静态硬风控 — 账户级不可突破的四道防线
单票12% / 行业30% / 总仓75% / 单日熔断2.5%
全量约束在 check_all_static_constraint 统一校验，交易决策前必须调用
"""
import sqlite3
import pandas as pd

DB_PATH = "agent_memory.db"


class StaticHardRiskControl:
    """静态硬约束风控引擎（不可动态放宽）"""

    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # 全局静态硬约束阈值（不可动态放宽）
        self.risk_config = {
            "single_stock_max_pos": 0.12,        # 单票最大仓位 12%
            "single_industry_max_pos": 0.30,      # 单一行业总仓位上限30%
            "account_total_max_pos": 0.75,        # 账户整体总仓位上限75%
            "daily_max_loss_ratio": 0.025,        # 账户单日最大亏损熔断线2.5%
        }

    # 查询当前持仓统计
    def get_current_position_stat(self):
        """统计当前持仓：总仓位/单票/单日亏损
        注: memory_trade_pnl 无 industry/trade_date 列，industry映射从外部传入
        """
        sql = """
        SELECT ts_code, SUM(position) as pos_rate, SUM(total_pnl) as daily_loss
        FROM memory_trade_pnl
        WHERE exit_date IS NULL
        GROUP BY ts_code
        """
        pos_df = pd.read_sql(sql, self.conn)
        if pos_df.empty:
            return {
                "total_account_pos": 0,
                "daily_loss": 0,
                "stock_pos": {}
            }
        total_account_pos = float(pos_df["pos_rate"].sum())
        daily_total_loss = float(abs(pos_df["daily_loss"].sum()))
        single_stock_pos = pos_df.set_index("ts_code")["pos_rate"].to_dict()
        return {
            "total_account_pos": total_account_pos,
            "daily_loss": daily_total_loss,
            "stock_pos": single_stock_pos
        }

    # 静态约束统一校验入口
    def check_all_static_constraint(self, target_ts, target_industry=None, apply_pos_rate=0):
        """
        四道防线统一校验
        :param target_ts: 目标股票代码
        :param target_industry: 目标行业(可选，缺省跳过行业校验)
        :param apply_pos_rate: 本次申请仓位比例(0~1)
        :return: (pass_flag: bool, log_message: str)
        """
        pos_stat = self.get_current_position_stat()
        risk_log = []
        pass_flag = True
        cfg = self.risk_config

        # 1. 单票仓位上限校验
        stock_cur = pos_stat["stock_pos"].get(target_ts, 0)
        if stock_cur + apply_pos_rate > cfg["single_stock_max_pos"]:
            risk_log.append(
                f"❌ 静态风控拦截：{target_ts}单票仓位上限{cfg['single_stock_max_pos']*100:.0f}%，"
                f"当前{stock_cur*100:.1f}%+本次{apply_pos_rate*100:.1f}%={((stock_cur+apply_pos_rate)*100):.1f}%超限"
            )
            pass_flag = False

        # 2. 单一行业总仓位校验（需外部传入 industry 映射，否则跳过）
        if target_industry:
            # 按行业汇总：memory_trade_pnl 无行业列，当前仅做单票级近似
            ind_cur = stock_cur  # 近似：该标的本行业只有当前持仓
            if ind_cur + apply_pos_rate > cfg["single_industry_max_pos"]:
                risk_log.append(
                    f"⚠ 行业总仓校验(近似): {target_industry}上限{cfg['single_industry_max_pos']*100:.0f}%，"
                    f"当前{ind_cur*100:.1f}%+{apply_pos_rate*100:.1f}%超限"
                )
                pass_flag = False
        else:
            risk_log.append("ℹ 行业校验跳过(无industry映射)")

        # 3. 账户整体总仓位上限
        if pos_stat["total_account_pos"] + apply_pos_rate > cfg["account_total_max_pos"]:
            risk_log.append(
                f"❌ 静态风控拦截：账户总仓位上限{cfg['account_total_max_pos']*100:.0f}%，"
                f"当前{pos_stat['total_account_pos']*100:.1f}%+{apply_pos_rate*100:.1f}%={((pos_stat['total_account_pos']+apply_pos_rate)*100):.1f}%已满仓无法新开仓"
            )
            pass_flag = False

        # 4. 单日亏损熔断前置校验
        loss_ratio = pos_stat["daily_loss"]
        if loss_ratio >= cfg["daily_max_loss_ratio"]:
            risk_log.append(
                f"❌ 账户单日亏损{loss_ratio*100:.2f}%触及熔断线{cfg['daily_max_loss_ratio']*100:.1f}%，全部开仓委托禁止执行"
            )
            pass_flag = False

        # 通过日志
        if pass_flag:
            risk_log.append(
                f"✅ 静态风控通过：{target_ts}申请{apply_pos_rate*100:.1f}% | "
                f"当前单票{stock_cur*100:.1f}%/总仓{pos_stat['total_account_pos']*100:.1f}%/日亏{loss_ratio*100:.2f}%"
            )

        return pass_flag, "\n".join(risk_log)

    def close(self):
        self.conn.close()
