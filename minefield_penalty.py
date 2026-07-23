#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
minefield_penalty.py — 记忆库历史雷区加权惩罚标准化机制

核心规则:
  1. 记忆库内同一赛道存在≥3条历史【预判高估，负误差】→ 标记高危雷区赛道
  2. 高危雷区赛道内个股 Rule021 最终原始风险分 ×1.5 惩罚系数
  3. 安全赛道系数 = 1.0，分值不变
  4. 兑现公告每条扣减 10 分，分值兜底 0 分

完整计算链路:
  五维打分 → 阶梯附加分 → 兑现扣减 → 雷区惩罚(×1.5/×1.0) → 最终原始分 → 全链路

边界约束:
  - 惩罚倍率仅作用于 Rule021 原始信号分，不修改阶梯加分/兑现扣减规则
  - 乘算后分值无封顶，真实放大赛道历史崩盘风险
  - 双分支共用同一套雷区判定和惩罚规则
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [MINE] %(message)s",
                    datefmt="%H:%M:%S")

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
MINE_TABLE = "sector_minefield_records"


class MinefieldPenaltyController:
    """
    历史雷区加权惩罚控制器。

    提供两项核心功能:
      1. 兑现公告扣减: 每条实测兑现公告扣10分, 兜底0分
      2. 雷区惩罚倍率: 同一赛道≥3次诱多崩盘→×1.5

    使用方式:
        mpc = MinefieldPenaltyController()
        result = mpc.apply_penalty(
            sector="固态电池",
            score_before=57,
            deduction_count=2,      # 2条兑现公告
        )
        # → final_score = max(0, 57-20) × 1.5 = 55.5
    """

    MINE_THRESHOLD = 3
    PENALTY_MULTIPLIER = 1.5
    DEDUCTION_PER_EVENT = 10

    def __init__(self):
        self._ensure_table()

    # ===================== 数据库管理 =====================

    def _ensure_table(self):
        """确保 sector_minefield_records 表存在"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {MINE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sector TEXT NOT NULL,
                ts_code TEXT NOT NULL,
                error_label TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(sector, ts_code, trade_date)
            )
        """)
        conn.commit()
        conn.close()

    # ===================== 注册雷区事件 =====================

    def register_error(self, ts_code: str, sector: str,
                       trade_date: str = None,
                       error_label: str = "预判高估，负误差") -> bool:
        """
        注册一次赛道诱多崩盘记录。

        在盘后校准发现"预判高估，负误差"标签时调用此接口。
        """
        trade_date = trade_date or datetime.now().strftime("%Y%m%d")
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute(f"""
                INSERT OR IGNORE INTO {MINE_TABLE}
                (sector, ts_code, error_label, trade_date)
                VALUES (?, ?, ?, ?)
            """, (sector, ts_code, error_label, trade_date))
            conn.commit()
            affected = cur.rowcount > 0
            conn.close()
            if affected:
                count = self.count_mine_events(sector)
                if count >= self.MINE_THRESHOLD:
                    logging.warning(
                        f"  ⚠️ 赛道[{sector}]雷区事件已达{count}次→触发×{self.PENALTY_MULTIPLIER}惩罚")
            return True
        except Exception as e:
            logging.warning(f"  ⚠️ 雷区事件注册失败: {e}")
            return False

    def register_from_calibration(self, error_label: str,
                                   ts_code: str, sector: str,
                                   trade_date: str = None) -> bool:
        """
        从盘后校准记录自动注册雷区事件。
        仅在 error_label == "预判高估，负误差" 时生效。
        """
        if "预判高估" not in error_label and "负误差" not in error_label:
            return False
        if not sector:
            return False
        return self.register_error(ts_code, sector, trade_date, error_label)

    # ===================== 雷区判定 =====================

    def count_mine_events(self, sector: str) -> int:
        """统计某赛道的诱多崩盘历史记录数"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute(f"""
                SELECT COUNT(*) FROM {MINE_TABLE}
                WHERE sector = ? AND error_label LIKE '%预判高估%'
            """, (sector,))
            count = cur.fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def is_minefield(self, sector: str) -> bool:
        """判定赛道是否为高危雷区 (≥3次)"""
        if not sector:
            return False
        count = self.count_mine_events(sector)
        result = count >= self.MINE_THRESHOLD
        if result:
            logging.info(
                f"  🚨 赛道[{sector}]为高危雷区 ({count}次≥{self.MINE_THRESHOLD}), "
                f"触发×{self.PENALTY_MULTIPLIER}惩罚")
        return result

    def get_minefield_sectors(self) -> list[dict]:
        """获取所有已标记的高危雷区赛道"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT sector, COUNT(*) as cnt, MAX(trade_date) as last
            FROM {MINE_TABLE}
            WHERE error_label LIKE '%预判高估%'
            GROUP BY sector
            HAVING cnt >= ?
            ORDER BY cnt DESC
        """, (self.MINE_THRESHOLD,))
        rows = cur.fetchall()
        conn.close()
        return [{"sector": r[0], "count": r[1], "last_event": r[2]}
                for r in rows]

    # ===================== 兑现公告扣减 =====================

    def apply_deduction(self, score: float,
                        deduction_count: int = 0) -> float:
        """
        兑现公告扣减: 每条实测公告扣10分, 兜底最低0分。

        参数:
            score: 扣减前分值 (阶梯修正后的分)
            deduction_count: 当期有效实锤兑现公告数量

        返回: 扣减后分值 (≥0)
        """
        if deduction_count <= 0:
            return score
        deduction = deduction_count * self.DEDUCTION_PER_EVENT
        result = max(0, score - deduction)
        if deduction > 0:
            logging.info(
                f"  📋 兑现扣减: {deduction_count}条×{self.DEDUCTION_PER_EVENT}=−{deduction}, "
                f"{score:.1f}→{result:.1f}")
        return result

    # ===================== 雷区惩罚倍率 =====================

    def apply_penalty(self, sector: str,
                      score_after_deduction: float) -> float:
        """
        雷区惩罚倍率: 高危雷区赛道×1.5, 安全赛道×1.0。

        参数:
            sector: 赛道名
            score_after_deduction: 兑现扣减后的分值

        返回: 惩罚后最终分值
        """
        if not sector:
            return score_after_deduction

        multiplier = self.PENALTY_MULTIPLIER if self.is_minefield(sector) else 1.0

        result = round(score_after_deduction * multiplier, 1)
        if multiplier > 1.0:
            logging.info(
                f"  🚨 雷区惩罚: ×{multiplier}, "
                f"{score_after_deduction:.1f}→{result:.1f}")
        return result

    # ===================== 全链路一体化 =====================

    def apply_full(self, sector: str,
                   score_before: float,
                   deduction_count: int = 0) -> dict:
        """
        完整执行链路:
          1. 兑现公告扣减: max(0, score - deduction_count×10)
          2. 雷区惩罚: 高危×1.5 / 安全×1.0

        参数:
            sector: 赛道
            score_before: 阶梯修正后的原始分
            deduction_count: 兑现公告数

        返回:
            {
                "score_before_ladder": float,  # 阶梯修正后(输入)
                "deduction_count": int,
                "score_after_deduction": float,  # 扣减后
                "is_minefield": bool,
                "minefield_count": int,
                "multiplier": float,
                "score_final": float,           # 最终原始信号强度分
                "detail": str,                  # 详细计算过程
            }
        """
        count = self.count_mine_events(sector) if sector else 0
        is_mine = count >= self.MINE_THRESHOLD

        # Step 1: 兑现公告扣减
        after_deduction = self.apply_deduction(score_before, deduction_count)

        # Step 2: 雷区惩罚
        multiplier = self.PENALTY_MULTIPLIER if is_mine else 1.0
        final = round(after_deduction * multiplier, 1)

        # 构建详细计算描述
        parts = [f"阶梯修正分={score_before:.1f}"]
        if deduction_count > 0:
            ded = deduction_count * self.DEDUCTION_PER_EVENT
            parts.append(f"兑现扣减{deduction_count}条−{ded}")
            parts.append(f"扣减后={after_deduction:.1f}")
        if is_mine:
            parts.append(f"🚨雷区惩罚×{multiplier}")
        parts.append(f"最终分={final:.1f}")

        return {
            "score_before_ladder": score_before,
            "deduction_count": deduction_count,
            "score_after_deduction": after_deduction,
            "is_minefield": is_mine,
            "minefield_count": count,
            "multiplier": multiplier,
            "score_final": final,
            "detail": " → ".join(parts),
        }

    # ===================== 报告 =====================

    def report(self) -> dict:
        """导出雷区报告"""
        sectors = self.get_minefield_sectors()
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {MINE_TABLE}")
        total_events = cur.fetchone()[0]
        conn.close()

        return {
            "total_events": total_events,
            "minefield_sectors": len(sectors),
            "minefield_details": sectors,
            "threshold": self.MINE_THRESHOLD,
            "multiplier": self.PENALTY_MULTIPLIER,
        }


# ===================== 快捷入口 =====================

def run_minefield_report() -> dict:
    """雷区状态报告"""
    mpc = MinefieldPenaltyController()
    report = mpc.report()
    print(f"\n🚨 历史雷区报告")
    print(f"  {'='*40}")
    print(f"  总事件: {report['total_events']}")
    print(f"  高危雷区赛道: {report['minefield_sectors']} 个")
    for s in report['minefield_details']:
        print(f"    🚨 {s['sector']}: {s['count']}次, 最近{s['last_event']}")
    print(f"  阈值: ≥{report['threshold']}次触发×{report['multiplier']}惩罚")
    print(f"  {'='*40}")
    return report


# ===================== 测试 =====================

if __name__ == "__main__":
    import sys

    mpc = MinefieldPenaltyController()

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("\n=== MinefieldPenalty 测试 ===\n")

        # 清理测试数据
        conn = sqlite3.connect(str(MEMORY_DB))
        conn.execute(f"DELETE FROM {MINE_TABLE}")
        conn.commit()
        conn.close()

        # 测试1: 安全赛道 (0次→×1.0)
        r1 = mpc.apply_full(sector="银行", score_before=30, deduction_count=0)
        assert r1["is_minefield"] is False
        assert r1["multiplier"] == 1.0
        assert r1["score_final"] == 30.0
        print(f"✅ 测试1: 安全赛道×1.0 → {r1['score_final']}")

        # 测试2: 注册3次引爆雷区
        for i in range(3):
            mpc.register_error(f"T{i:04d}", "固态电池",
                                trade_date=f"202607{10+i:02d}")
        assert mpc.count_mine_events("固态电池") == 3
        assert mpc.is_minefield("固态电池") is True
        print(f"✅ 测试2: 固态电池3次雷区→高危✓")

        # 测试3: 高危雷区赛道 ×1.5
        r3 = mpc.apply_full(sector="固态电池", score_before=40, deduction_count=0)
        assert r3["is_minefield"] is True
        assert r3["multiplier"] == 1.5
        assert r3["score_final"] == 60.0  # 40 × 1.5
        print(f"✅ 测试3: 雷区40×1.5=60.0 → {r3['score_final']}")

        # 测试4: 兑现扣减 (2条-20)
        r4 = mpc.apply_full(sector="固态电池", score_before=57, deduction_count=2)
        assert r4["score_after_deduction"] == 37.0  # 57-20
        assert r4["score_final"] == 55.5  # 37 × 1.5
        assert "兑现扣减" in r4["detail"]
        assert "雷区惩罚" in r4["detail"]
        print(f"✅ 测试4: 案例1(57-20)×1.5=55.5 → {r4['score_final']}")

        # 测试5: 安全赛道无惩罚 (1次)
        mpc.register_error("T9999", "黄金", trade_date="20260720")
        r5 = mpc.apply_full(sector="黄金", score_before=65, deduction_count=1)
        assert r5["is_minefield"] is False  # 仅1次<3
        assert r5["multiplier"] == 1.0
        assert r5["score_final"] == 55.0  # (65-10)×1.0
        print(f"✅ 测试5: 案例2(65-10)×1.0=55.0 → {r5['score_final']}")

        # 测试6: 扣减归零后雷区惩罚
        r6 = mpc.apply_full(sector="固态电池", score_before=18, deduction_count=3)
        assert r6["score_after_deduction"] == 0.0  # max(0, 18-30)
        assert r6["score_final"] == 0.0  # 0 × 1.5
        print(f"✅ 测试6: 案例3(18-30=0)×1.5=0 → {r6['score_final']}")

        # 测试7: 报告
        mpc.register_error("T1111", "固态电池", trade_date="20260725")
        # 4次
        r7 = mpc.apply_full(sector="固态电池", score_before=50, deduction_count=0)
        assert r7["minefield_count"] == 4
        print(f"✅ 测试7: 4次雷区→×1.5, 50×1.5={r7['score_final']}")

        report = mpc.report()
        print(f"✅ 报告: {report['minefield_sectors']}个雷区赛道")

        # 清理
        conn2 = sqlite3.connect(str(MEMORY_DB))
        conn2.execute(f"DELETE FROM {MINE_TABLE}")
        conn2.commit()
        conn2.close()
        print(f"\n🎉 全部 MinefieldPenalty 测试通过")

    elif len(sys.argv) > 1 and sys.argv[1] == "report":
        run_minefield_report()
    else:
        print("用法:")
        print("  python minefield_penalty.py test     # 运行测试")
        print("  python minefield_penalty.py report   # 雷区报告")
