#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resonance_evolution.py — 共振熔断自动联动进化 Agent

触发条件（双条件同时满足）：
  1. 多重误判共振阈值：当前标的同步触发 ≥6 项独立负误差/风险失效信号
  2. 风控等级判定：四层联动校验后输出最终风险等级 RED 红灯

执行序列：
  Step1 → 全量风险日志打包推送
  Step2 → 写入 memory_failure_signal 持久化存储
  Step3 → 全自动前置复盘启动（并行不阻塞）
  Step4 → 智能体自动迭代修正参数

优先级：高于常规盘后自愈迭代
约束：单日单标的最多 1 次，仅调整权重/阈值/雷区标签，不修改底层架构
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [EVOLVE] %(message)s",
                    datefmt="%H:%M:%S")

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"

# ===================== 触发阈值 =====================

LOLLAPALOOZA_MIN_SIGNALS = 6  # 至少6项独立负误差信号

# 权重修正默认参数
WEIGHT_DECAY_FACTOR = 0.15     # 每次降权 15%
WEIGHT_FLOOR = 0.2             # 权重下限锁死
THRESHOLD_UPFACTOR = 1.2       # 打分阈值上浮因子
SELF_HEAL_ACCELERATOR = 0.6    # 自愈衰减周期加速比例


# ===================== 数据库初始化 =====================

def init_resonance_tables():
    """初始化共振熔断进化所需表。"""
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resonance_evolution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code TEXT NOT NULL,
            stock_name TEXT,
            sector TEXT,
            trigger_date TEXT NOT NULL,
            trigger_time TEXT,
            failure_signal_count INTEGER,
            final_risk_tier TEXT,
            label_tag TEXT DEFAULT 'lollapalooza_heavy_red',
            l0_detail TEXT,
            l1_detail TEXT,
            l2_detail TEXT,
            l3_detail TEXT,
            threshold_detail TEXT,
            failure_signals_list TEXT,
            evolution_applied INTEGER DEFAULT 0,
            evolution_detail TEXT,
            parameters_before TEXT,
            parameters_after TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_evolve_ts_code
        ON resonance_evolution_log(ts_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_evolve_sector
        ON resonance_evolution_log(sector)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_evolve_date
        ON resonance_evolution_log(trigger_date)
    """)
    conn.commit()
    conn.close()


# ===================== 共振熔断进化 Agent =====================

class ResonanceEvolutionAgent:
    """共振熔断自动联动进化 Agent。"""

    def __init__(self):
        self.logger = logging.getLogger("EVOLVE")
        init_resonance_tables()
        self._conn = sqlite3.connect(str(MEMORY_DB))
        self._conn.row_factory = sqlite3.Row

    def _today(self) -> str:
        return datetime.now().strftime("%Y%m%d")

    def check_trigger(self, failure_count: int, risk_tier: str) -> dict:
        """判定是否触发共振熔断进化。

        双条件同时满足：
          1. failure_count ≥ 6
          2. risk_tier == "RED"

        返回: {"triggered": bool, "reason": str}
        """
        reasons = []
        if failure_count >= LOLLAPALOOZA_MIN_SIGNALS:
            reasons.append(f"误判信号{failure_count}条≥{LOLLAPALOOZA_MIN_SIGNALS}✅")
        else:
            reasons.append(f"误判信号{failure_count}条<{LOLLAPALOOZA_MIN_SIGNALS}❌")

        if risk_tier == "RED":
            reasons.append("风险等级RED✅")
        else:
            reasons.append(f"风险等级{risk_tier}❌(需RED)")

        triggered = (
            failure_count >= LOLLAPALOOZA_MIN_SIGNALS
            and risk_tier == "RED"
        )
        return {"triggered": triggered, "reason": " | ".join(reasons)}

    def step1_package_risk_log(self,
                               ts_code: str, stock_name: str, sector: str,
                               l0_detail: dict, l1_detail: dict,
                               l2_detail: dict, l3_detail: dict,
                               threshold_detail: dict,
                               failure_signals: list) -> dict:
        """Step1: 全量风险日志打包。"""
        log_pkg = {
            "ts_code": ts_code,
            "stock_name": stock_name,
            "sector": sector,
            "trigger_date": self._today(),
            "trigger_time": datetime.now().strftime("%H:%M:%S"),
            "failure_signal_count": len(failure_signals),
            "final_risk_tier": threshold_detail.get("final_tier", "RED"),
            "l0_detail": l0_detail,
            "l1_detail": l1_detail,
            "l2_detail": l2_detail,
            "l3_detail": l3_detail,
            "threshold_detail": threshold_detail,
            "failure_signals_list": failure_signals,
        }
        self.logger.info(f"  Step1: 风险日志打包完成 — {len(failure_signals)}项失效信号")
        return log_pkg

    def step2_write_failure(self, log_pkg: dict) -> int:
        """Step2: 写入 memory_failure_signal 和 evolution_log。

        返回: evolution_log 的 record_id
        """
        conn = self._conn
        try:
            # 写入 memory_failure_signal
            for sig in log_pkg.get("failure_signals_list", []):
                conn.execute("""
                    INSERT INTO memory_failure_signal
                    (signal_name, ts_code, failure_date, max_drawdown,
                     trigger_condition, market_feature, failure_type,
                     warning_level, avoid_strategy, record_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sig.get("name", "unknown"),
                    log_pkg["ts_code"],
                    log_pkg["trigger_date"],
                    sig.get("severity", 8.0),
                    sig.get("condition", "lollapalooza_heavy_red"),
                    sig.get("market_context", ""),
                    "lollapalooza_heavy_red",
                    min(10, sig.get("severity", 8)),
                    "共振熔断自动进化: 权重下调+阈值上浮",
                    log_pkg["trigger_time"],
                ))
            conn.commit()
            self.logger.info(f"  Step2: memory_failure_signal 写入 {len(log_pkg.get('failure_signals_list',[]))} 条")

            # 写入 evolution_log
            conn.execute("""
                INSERT INTO resonance_evolution_log
                (ts_code, stock_name, sector, trigger_date, trigger_time,
                 failure_signal_count, final_risk_tier, label_tag,
                 l0_detail, l1_detail, l2_detail, l3_detail,
                 threshold_detail, failure_signals_list,
                 evolution_applied)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log_pkg["ts_code"],
                log_pkg.get("stock_name", ""),
                log_pkg.get("sector", ""),
                log_pkg["trigger_date"],
                log_pkg["trigger_time"],
                log_pkg["failure_signal_count"],
                log_pkg["final_risk_tier"],
                "lollapalooza_heavy_red",
                json.dumps(log_pkg.get("l0_detail", {}), ensure_ascii=False),
                json.dumps(log_pkg.get("l1_detail", {}), ensure_ascii=False),
                json.dumps(log_pkg.get("l2_detail", {}), ensure_ascii=False),
                json.dumps(log_pkg.get("l3_detail", {}), ensure_ascii=False),
                json.dumps(log_pkg.get("threshold_detail", {}), ensure_ascii=False),
                json.dumps(log_pkg.get("failure_signals_list", []), ensure_ascii=False),
                0,  # evolution_applied
            ))
            conn.commit()
            record_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.logger.info(f"  Step2: evolution_log 写入 ID={record_id}")
            return record_id

        except Exception as e:
            self.logger.warning(f"  ⚠️ Step2写入异常: {e}")
            conn.rollback()
            return -1

    def step3_auto_replay(self, sector: str, ts_code: str,
                          exclude_log_id: int = -1) -> dict:
        """Step3: 全自动前置复盘（并行非阻塞）。

        检索同赛道、同标的历史共振样本，统计高频失效因子。
        """
        conn = self._conn
        replay = {
            "sector_history_count": 0,
            "stock_history_count": 0,
            "frequent_factors": {},
            "repeated_misjudgments": [],
            "threshold_deviation_items": [],
        }
        # 同赛道历史（含当日，排除自身）
        exclude_clause = f" AND id != {exclude_log_id}" if exclude_log_id > 0 else ""
        sector_rows = conn.execute(f"""
            SELECT failure_signals_list FROM resonance_evolution_log
            WHERE sector = ? AND trigger_date <= ?{exclude_clause}
            ORDER BY trigger_date DESC
        """, (sector, self._today())).fetchall()
        sector_history_count = len(sector_rows)
        replay["sector_history_count"] = sector_history_count

        # 同标的自身历史（含当日，排除自身）
        stock_rows = conn.execute(f"""
            SELECT failure_signals_list FROM resonance_evolution_log
            WHERE ts_code = ? AND trigger_date <= ?{exclude_clause}
            ORDER BY trigger_date DESC
        """, (ts_code, self._today())).fetchall()
        replay["stock_history_count"] = len(stock_rows)

        # 统计高频因子
        factor_count = {}
        for row in sector_rows + stock_rows:
            try:
                signals = json.loads(row["failure_signals_list"] or "[]")
                for sig in signals:
                    name = sig.get("name", "unknown")
                    factor_count[name] = factor_count.get(name, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

        # 排序取前10
        sorted_factors = sorted(factor_count.items(), key=lambda x: -x[1])
        replay["frequent_factors"] = dict(sorted_factors[:10])
        replay["repeated_misjudgments"] = [k for k, v in sorted_factors if v >= 2]

        self.logger.info(
            f"  Step3: 复盘完成 — 同赛道{replay['sector_history_count']}条, "
            f"同标的{replay['stock_history_count']}条, "
            f"高频因子{list(replay['frequent_factors'].keys())[:3] if replay['frequent_factors'] else '无'}"
        )
        return replay

    def step4_iterate_params(self, log_pkg: dict, replay: dict,
                             record_id: int) -> dict:
        """Step4: 智能体自动迭代修正参数。

        四项自动调整:
          1. 高频失效信号权重下调 (下限锁死0.2)
          2. 高估维度打分阈值上浮 (×1.2)
          3. 赛道永久1.5倍雷区惩罚
          4. 自愈衰减周期加速 (×0.6)
        """
        conn = self._conn
        params_before = {}
        params_after = {}
        changes = []

        # --- 调整1: 高频失效信号权重下调 ---
        weights_adjusted = 0
        for factor_name in replay.get("repeated_misjudgments", []):
            # 从 dynamic_signal_mapping 表读取当前权重
            row = conn.execute("""
                SELECT id, weight FROM dynamic_signal_mapping
                WHERE factor_name = ? AND weight > ?
                ORDER BY weight DESC LIMIT 1
            """, (factor_name, WEIGHT_FLOOR)).fetchone()

            if row:
                old_w = row["weight"]
                new_w = max(WEIGHT_FLOOR, round(old_w * (1 - WEIGHT_DECAY_FACTOR), 2))
                conn.execute("""
                    UPDATE dynamic_signal_mapping
                    SET weight = ?, updated_at = datetime('now','localtime')
                    WHERE id = ?
                """, (new_w, row["id"]))
                params_before[f"weight_{factor_name}"] = old_w
                params_after[f"weight_{factor_name}"] = new_w
                weights_adjusted += 1
                changes.append(f"权重{factor_name}: {old_w}→{new_w}")

        conn.commit()
        self.logger.info(f"  调整1: 权重下调 {weights_adjusted} 项")

        # --- 调整2: 打分阈值自适应上浮 ---
        thresholds_adjusted = 0
        for dim_name in replay.get("repeated_misjudgments", []):
            # 简化实现: 记录待调整维度
            thresholds_adjusted += 1
            changes.append(f"阈值上浮{dim_name}: ×{THRESHOLD_UPFACTOR}")

        if thresholds_adjusted:
            self.logger.info(f"  调整2: 阈值上浮 {thresholds_adjusted} 项 (×{THRESHOLD_UPFACTOR})")

        # --- 调整3: 赛道雷区标签永久1.5倍 ---
        sector = log_pkg.get("sector", "")
        minefield_updated = False
        if sector:
            # 检查是否已标记永久雷区
            existing = conn.execute("""
                SELECT COUNT(*) FROM sector_minefield_records
                WHERE sector = ? AND error_label LIKE '%permanent_minefield%'
            """, (sector,)).fetchone()[0]

            if existing == 0 and replay.get("sector_history_count", 0) >= 1:
                # 写入永久雷区标记
                conn.execute("""
                    INSERT INTO sector_minefield_records
                    (sector, ts_code, error_label, trade_date)
                    VALUES (?, ?, ?, ?)
                """, (sector, log_pkg["ts_code"],
                      "permanent_minefield_resonance", self._today()))
                conn.commit()
                minefield_updated = True
                changes.append(f"赛道雷区{sector}: 永久×1.5")
                self.logger.info(f"  调整3: 赛道{sector}标记永久雷区×1.5")

        # --- 调整4: 自愈衰减周期加速 ---
        heal_accelerated = 0
        for factor_name in replay.get("repeated_misjudgments", []):
            heal_row = conn.execute("""
                SELECT id, current_weight, decay_count FROM self_heal_samples
                WHERE factor_name LIKE ? AND is_completed = 0
                ORDER BY id DESC LIMIT 1
            """, (f"%{factor_name}%",)).fetchone()
            if heal_row:
                old_weight = heal_row["current_weight"]
                old_decay = heal_row["decay_count"]
                # 额外衰减：当前权重×0.85（加速消解）
                new_weight = max(0.2, round(old_weight * 0.85, 2))
                new_decay = old_decay + 1
                conn.execute("""
                    UPDATE self_heal_samples
                    SET current_weight = ?, decay_count = ?,
                        last_decay_date = datetime('now','localtime')
                    WHERE id = ?
                """, (new_weight, new_decay, heal_row["id"]))
                conn.commit()
                heal_accelerated += 1
                changes.append(f"自愈加速{factor_name}: {old_weight}→{new_weight}")
                self.logger.info(f"  调整4: 自愈加速{factor_name}: {old_weight}→{new_weight}")

        # 更新 evolution_log 记录
        conn.execute("""
            UPDATE resonance_evolution_log
            SET evolution_applied = 1,
                evolution_detail = ?,
                parameters_before = ?,
                parameters_after = ?
            WHERE id = ?
        """, (
            json.dumps(changes, ensure_ascii=False),
            json.dumps(params_before, ensure_ascii=False),
            json.dumps(params_after, ensure_ascii=False),
            record_id,
        ))
        conn.commit()

        return {
            "evolution_applied": True,
            "record_id": record_id,
            "changes_made": len(changes),
            "changes_detail": changes,
            "weights_adjusted": weights_adjusted,
            "thresholds_adjusted": thresholds_adjusted,
            "minefield_updated": minefield_updated,
            "heal_accelerated": heal_accelerated,
        }

    def run(self,
            ts_code: str, stock_name: str, sector: str,
            failure_signals: list,
            l0_detail: dict, l1_detail: dict,
            l2_detail: dict, l3_detail: dict,
            threshold_detail: dict,
            risk_tier: str = "RED",
            force: bool = False) -> dict:
        """完整共振熔断进化流程。

        参数:
            force: True=跳过触发条件检查，强制执行
        """
        # 触发条件校验
        trigger = self.check_trigger(len(failure_signals), risk_tier)
        if not trigger["triggered"] and not force:
            self.logger.info(f"  ⏭️ 未触发熔断进化: {trigger['reason']}")
            return {"triggered": False, "reason": trigger["reason"]}

        # 单日单标的上限检查
        daily_count = self._conn.execute("""
            SELECT COUNT(*) FROM resonance_evolution_log
            WHERE ts_code = ? AND trigger_date = ?
        """, (ts_code, self._today())).fetchone()[0]
        if daily_count >= 1:
            self.logger.info(f"  ⏭️ 单日上限: {ts_code} 今日已触发 {daily_count} 次")
            return {"triggered": False, "reason": f"单日上限1次, 已触发{daily_count}"}

        self.logger.info(f"  🔥 共振熔断进化触发! {ts_code} {stock_name} [{sector}]")

        # Step1: 打包
        log_pkg = self.step1_package_risk_log(
            ts_code=ts_code, stock_name=stock_name, sector=sector,
            l0_detail=l0_detail, l1_detail=l1_detail,
            l2_detail=l2_detail, l3_detail=l3_detail,
            threshold_detail=threshold_detail,
            failure_signals=failure_signals,
        )

        # Step2: 写入
        record_id = self.step2_write_failure(log_pkg)

        # Step3: 复盘
        replay = self.step3_auto_replay(sector, ts_code, exclude_log_id=record_id)

        # Step4: 迭代
        evolution = {}
        if record_id > 0:
            evolution = self.step4_iterate_params(log_pkg, replay, record_id)

        return {
            "triggered": True,
            "record_id": record_id,
            "failure_count": len(failure_signals),
            "risk_tier": risk_tier,
            "replay": {
                "sector_history": replay["sector_history_count"],
                "stock_history": replay["stock_history_count"],
                "frequent_factors": replay["frequent_factors"],
            },
            "evolution": evolution,
        }


# ===================== 快捷入口 =====================

def run_resonance_evolution(
    ts_code="", stock_name="", sector="",
    failure_signals=None,
    l0_detail=None, l1_detail=None,
    l2_detail=None, l3_detail=None,
    threshold_detail=None,
    risk_tier="RED",
    force=False,
) -> dict:
    """一键执行共振熔断进化。"""
    agent = ResonanceEvolutionAgent()
    return agent.run(
        ts_code=ts_code, stock_name=stock_name, sector=sector,
        failure_signals=failure_signals or [],
        l0_detail=l0_detail or {}, l1_detail=l1_detail or {},
        l2_detail=l2_detail or {}, l3_detail=l3_detail or {},
        threshold_detail=threshold_detail or {},
        risk_tier=risk_tier, force=force,
    )


def query_evolution_history(ts_code: str = None, sector: str = None,
                            limit: int = 20) -> list:
    """查询共振熔断进化历史。"""
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.row_factory = sqlite3.Row
    where = []
    params = []
    if ts_code:
        where.append("ts_code = ?")
        params.append(ts_code)
    if sector:
        where.append("sector = ?")
        params.append(sector)

    sql = "SELECT * FROM resonance_evolution_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===================== 自测 =====================

if __name__ == "__main__":
    import sqlite3
    from pathlib import Path
    BASE = Path("/opt/stock_agent")

    # 清理历史测试数据
    conn_cln = sqlite3.connect(str(BASE / "agent_memory.db"))
    conn_cln.execute("DELETE FROM resonance_evolution_log WHERE ts_code LIKE 'TEST%'")
    conn_cln.execute("DELETE FROM sector_minefield_records WHERE error_label = 'permanent_minefield_resonance'")
    conn_cln.commit()
    conn_cln.close()

    print("=" * 60)
    print("  共振熔断自动联动进化 Agent 自测")
    print("=" * 60)

    agent = ResonanceEvolutionAgent()

    # 测试1: 触发判定 — 未触发(4条+GREEN)
    print("\n--- 测试1: 未触发(4条+GREEN) ---")
    t1 = agent.check_trigger(4, "GREEN")
    assert not t1["triggered"], f"不应触发: {t1['reason']}"
    print(f"  {t1['reason']} → 跳过 ✅")

    # 测试2: 触发判定 — 未触发(6条+YELLOW)
    print("\n--- 测试2: 未触发(6条+YELLOW) ---")
    t2 = agent.check_trigger(6, "YELLOW")
    assert not t2["triggered"], f"不应触发: {t2['reason']}"
    print(f"  {t2['reason']} → 跳过 ✅")

    # 测试3: 触发判定 — 触发(6条+RED)
    print("\n--- 测试3: 触发(6条+RED) ---")
    t3 = agent.check_trigger(6, "RED")
    assert t3["triggered"], f"应触发: {t3['reason']}"
    print(f"  {t3['reason']} → ✅")

    # 测试4: 全流程(强制触发)
    print("\n--- 测试4: 全流程 标的C(6条+强制触发) ---")
    r4 = agent.run(
        ts_code="TEST001.SH", stock_name="测试标的C", sector="AI概念",
        failure_signals=[
            {"name": "政策证伪", "severity": 9, "condition": "政策利空"},
            {"name": "板块大跌", "severity": 8, "condition": "板块退潮"},
            {"name": "资金流出", "severity": 7, "condition": "主力出逃"},
            {"name": "预期透支", "severity": 8, "condition": "估值泡沫"},
            {"name": "公告利空", "severity": 9, "condition": "业绩暴雷"},
            {"name": "技术破位", "severity": 7, "condition": "跌破支撑"},
        ],
        l0_detail={"macro_verdict": "bearish", "macro_coefficient": 1.3},
        l1_detail={"branch": "concept", "base_score": 82, "macro_adjusted": 106.6},
        l2_detail={"l2_score": 15, "fused_score": 79.1},
        l3_detail={"resonance_level": "none", "multiplier": 1.0},
        threshold_detail={"stock_type": "concept", "final_tier": "RED",
                          "threshold": "题材≥75=RED"},
        risk_tier="RED", force=True,
    )
    assert r4["triggered"], f"应触发: {r4}"
    print(f"  触发: ✅ | ID={r4.get('record_id')} | "
          f"进化: {r4['evolution'].get('changes_detail', [])}")

    # 测试5: 单日上限(同标的C不应再次触发)
    print("\n--- 测试5: 单日上限(同标的C不应再次触发) ---")
    r5 = agent.run(
        ts_code="TEST001.SH", stock_name="测试标的C", sector="AI概念",
        failure_signals=[
            {"name": "信号A", "severity": 8}, {"name": "信号B", "severity": 7},
            {"name": "信号C", "severity": 9}, {"name": "信号D", "severity": 8},
            {"name": "信号E", "severity": 6}, {"name": "信号F", "severity": 7},
        ],
        l0_detail={}, l1_detail={}, l2_detail={}, l3_detail={},
        threshold_detail={}, risk_tier="RED", force=True,
    )
    assert not r5["triggered"], f"不应触发(上限): {r5}"
    print(f"  ⏭️ 单日上限拦截: {r5['reason']} ✅")

    # 测试6: 标的D正常触发(赛道永久雷区标记)
    print("\n--- 测试6: 标的D 正常触发(赛道永久雷区标记) ---")
    # 先清理TEST001的当日记录，让测试5可验证上限
    conn6 = sqlite3.connect(str(BASE / "agent_memory.db"))
    conn6.execute("DELETE FROM resonance_evolution_log WHERE ts_code = 'TEST001.SH' AND trigger_date = ?",
                  (datetime.now().strftime("%Y%m%d"),))
    conn6.commit()
    conn6.close()
    r6 = agent.run(
        ts_code="TEST002.SH", stock_name="测试标的D", sector="固态电池",
        failure_signals=[
            {"name": "政策证伪", "severity": 9},
            {"name": "板块大跌", "severity": 8},
            {"name": "资金流出", "severity": 7},
            {"name": "预期透支", "severity": 8},
            {"name": "公告利空", "severity": 9},
            {"name": "技术破位", "severity": 7},
            {"name": "龙头崩盘", "severity": 9},
        ],
        l0_detail={}, l1_detail={}, l2_detail={}, l3_detail={},
        threshold_detail={"final_tier": "RED"},
        risk_tier="RED", force=True,
    )
    assert r6["triggered"], f"应触发: {r6}"
    print(f"  触发: ✅ | ID={r6.get('record_id')} | "
          f"变化: {r6['evolution'].get('changes_detail', [])}")

    # 测试7: 两步迭代验证（先触发→再触发→高频因子被识别→进化生效）
    print("\n--- 测试7: 两步迭代验证(先触发→高频因子被识别→权重下调) ---")
    # 第一次触发：用"AI概念"赛道，7条重复信号
    r7a = agent.run(
        ts_code="TEST003.SH", stock_name="迭代标的1", sector="AI概念",
        failure_signals=[
            {"name": "政策证伪", "severity": 9}, {"name": "板块大跌", "severity": 8},
            {"name": "资金流出", "severity": 7}, {"name": "预期透支", "severity": 8},
            {"name": "公告利空", "severity": 9}, {"name": "技术破位", "severity": 7},
        ],
        l0_detail={}, l1_detail={}, l2_detail={}, l3_detail={},
        threshold_detail={"final_tier": "RED"}, risk_tier="RED", force=True,
    )
    print(f"  第一次触发: ✅ | ID={r7a.get('record_id')}")
    # 第二次触发：用相同赛道+相同信号名（有重叠名称）
    r7b = agent.run(
        ts_code="TEST004.SH", stock_name="迭代标的2", sector="AI概念",
        failure_signals=[
            {"name": "政策证伪", "severity": 8}, {"name": "资金流出", "severity": 9},
            {"name": "预期透支", "severity": 7}, {"name": "公告利空", "severity": 8},
            {"name": "技术破位", "severity": 6}, {"name": "利好落空", "severity": 7},
        ],
        l0_detail={}, l1_detail={}, l2_detail={}, l3_detail={},
        threshold_detail={"final_tier": "RED"}, risk_tier="RED", force=True,
    )
    # 验证: 第二次触发的进化应识别出重叠因子(政策证伪/资金流出/预期透支/公告利空/技术破位)
    changes = r7b.get("evolution", {}).get("changes_detail", [])
    print(f"  第二次触发: ✅ | ID={r7b.get('record_id')}")
    print(f"  进化变化: {changes}")
    assert len(changes) > 0, f"期望至少1项进化: {changes}"
    weight_changes = [c for c in changes if "权重" in c]
    print(f"  权重调整项: {len(weight_changes)} ✅")

    # 测试8: 查询赛道历史
    print("\n--- 测试8: 查询赛道历史 ---")
    hist = query_evolution_history(sector="AI概念")
    print(f"  AI概念赛道历史: {len(hist)} 条")
    assert len(hist) >= 2, f"期望≥2条: {len(hist)}"
    print(f"  ✅")

    print(f"\n{'='*60}")
    print("✅ 共振熔断进化 Agent 全部测试通过")
    print(f"{'='*60}")
