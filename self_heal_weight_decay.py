#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
self_heal_weight_decay.py — 误判自愈标记自动权重衰减单元

核心功能:
  乐观类误判(预判高估/机会漏判)在行情充分回调或利空落地后，
  自动下调该样本对应映射关系的关联权重，实现历史误判自主消解，
  无需人工修改参数与标签。

执行序列(流水线位置):
  1. 同源冗余信号合并降噪                  ← SignalConflictProcessor
  2. 信号时效衰减折算有效强度S              ← SignalDecayController
  3. 反向冲突信号对冲分值抵扣                ← SignalConflictProcessor
  ─────────────────────────────────────────
  4. 校验乐观误判触发条件，添加误判自愈标记  ← SelfHealWeightDecayUnit  ↘
  5. 带自愈标记映射关系自动衰减关联权重W     ← SelfHealWeightDecayUnit  → 两步合并
  ─────────────────────────────────────────
  6. 动态加权计算总误判得分                  ← MisjudgmentScoreCalculator
  7. 阈值判定是否激活高分误判迭代            ← MisjudgmentScoreCalculator

数据库配套:
  self_heal_samples — 每条乐观负误差样本独立存储自愈标记、触发日期、
                      当前权重、累计衰减次数、自愈完成状态
"""

import logging
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [SELF-HEAL] %(message)s",
                    datefmt="%H:%M:%S")

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
SELF_HEAL_TABLE = "self_heal_samples"


class SelfHealWeightDecayUnit:
    """
    误判自愈标记自动权重衰减单元。

    核心机制:
      对"预判高估，负误差"和"机会漏判，负误差"两类乐观误判样本，
      当行情充分回调或利空正式落地后，自动下调对应映射权重，
      权重下限0.2后标记[自愈完成]终止衰减。

    隔离规则:
      - 黑天鹅/退市预警/财务造假实锤 → 禁止触发自愈
      - 悲观类/有效类标签 → 不参与自愈
      - 单一因子独立衰减，互不影响

    状态流转:
      NEW (触发→注册)
        │
        ├── 每5个交易日 → 衰减15%, decay_count++
        │   │
        │   └── weight ≤ 0.2 → 标记 COMPLETED [自愈完成]
        │
        └── 黑天鹅信号手动清除 → 直接标记 COMPLETED
    """

    # ---------- 目标标签 (乐观误判，可自愈) ----------
    TARGET_LABELS = {"预判高估，负误差", "机会漏判，负误差"}

    # ---------- 豁免标签 (悲观/有效/正确，禁止自愈) ----------
    EXEMPT_LABELS = {
        "预判低估，负误差",   # 悲观类：低估了上涨动能，应保留
        "风控判断有效",       # 风控正确，不能自愈
        "入场条件失效",       # 入场条件问题，非乐观类
        "区间判断失效",       # 区间判断问题，非乐观类
        "持仓信号有效",       # 持仓信号正确
        "规避信号有效",       # 规避信号正确
        "止损阈值误判",       # 止损相关，非乐观类
        "止盈阈值偏保守",     # 止盈相关，非乐观类
    }

    # ---------- 自愈触发规则 ----------
    TRIGGER_RULES = {
        "预判高估，负误差": {
            "conditions": [
                "标的自信号生成日累计回调幅度≥25%",
                "对应利空事件正式落地披露（减值、处罚、亏损财报）",
            ],
            "logic": "OR",  # 任一条件满足即可触发
        },
        "机会漏判，负误差": {
            "conditions": [
                "行情冲高后深度回调，回吐本轮上涨幅度≥60%",
                "短期驱动利好完全落地兑现，无持续性上行逻辑",
            ],
            "logic": "OR",
        },
    }

    # ---------- 权重衰减配置 ----------
    DECAY_CYCLE_DAYS = 5           # 5个交易日一个周期
    SINGLE_DECAY_RATIO = 0.15      # 每周期衰减15%
    WEIGHT_FLOOR = 0.2             # 权重下限
    FINISH_TAG = "自愈完成"         # 完成标记

    # ---------- 黑天鹅/极端风险关键词（禁止触发自愈） ----------
    BLACK_SWAN_KEYWORDS = [
        "退市", "退市预警", "财务造假", "实锤", "立案",
        "ST", "*ST", "暂停上市", "终止上市",
    ]

    def __init__(self):
        self._ensure_table()

    # ===================== 数据库管理 =====================

    def _ensure_table(self):
        """确保 self_heal_samples 表存在"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SELF_HEAL_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code TEXT NOT NULL,
                label_name TEXT NOT NULL,
                factor_name TEXT NOT NULL,
                trigger_date TEXT NOT NULL,         -- 自愈触发日期 YYYYMMDD
                trigger_condition TEXT,              -- 触发的具体条件描述
                black_swan_checked INTEGER DEFAULT 0, -- 黑天鹅豁免校验结果
                initial_weight REAL NOT NULL,        -- 触发时原始权重
                current_weight REAL NOT NULL,         -- 当前权重(衰减后)
                decay_count INTEGER DEFAULT 0,        -- 累计衰减次数
                last_decay_date TEXT,                 -- 上次衰减日期
                is_completed INTEGER DEFAULT 0,       -- 是否自愈完成
                completed_date TEXT,                  -- 完成日期
                completion_reason TEXT,               -- 完成原因
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(ts_code, label_name, factor_name)
            )
        """)
        conn.commit()
        conn.close()

    def _load_sample(self, ts_code: str, label_name: str,
                     factor_name: str) -> Optional[dict]:
        """加载单条自愈样本"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ts_code, label_name, factor_name, trigger_date,
                   trigger_condition, black_swan_checked,
                   initial_weight, current_weight, decay_count,
                   last_decay_date, is_completed, completed_date,
                   completion_reason
            FROM {SELF_HEAL_TABLE}
            WHERE ts_code=? AND label_name=? AND factor_name=?
        """, (ts_code, label_name, factor_name))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "ts_code": row[0],
            "label_name": row[1],
            "factor_name": row[2],
            "trigger_date": row[3],
            "trigger_condition": row[4],
            "black_swan_checked": bool(row[5]),
            "initial_weight": row[6],
            "current_weight": row[7],
            "decay_count": row[8],
            "last_decay_date": row[9],
            "is_completed": bool(row[10]),
            "completed_date": row[11],
            "completion_reason": row[12],
        }

    def _save_sample(self, sample: dict) -> bool:
        """保存/更新自愈样本记录"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute(f"""
                INSERT OR REPLACE INTO {SELF_HEAL_TABLE}
                (ts_code, label_name, factor_name, trigger_date,
                 trigger_condition, black_swan_checked,
                 initial_weight, current_weight, decay_count,
                 last_decay_date, is_completed, completed_date,
                 completion_reason)
                VALUES (?,?,?,?, ?,?,?,?, ?,?,?,?,?)
            """, (
                sample["ts_code"],
                sample["label_name"],
                sample["factor_name"],
                sample.get("trigger_date", ""),
                sample.get("trigger_condition", ""),
                1 if sample.get("black_swan_checked") else 0,
                round(sample.get("initial_weight", 0.5), 4),
                round(sample.get("current_weight", 0.5), 4),
                sample.get("decay_count", 0),
                sample.get("last_decay_date", ""),
                1 if sample.get("is_completed") else 0,
                sample.get("completed_date", ""),
                sample.get("completion_reason", ""),
            ))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logging.error(f"  ❌ 自愈样本持久化失败: {e}")
            return False

    # ===================== 黑天鹅豁免校验 =====================

    def _check_black_swan_exempt(self, stock_code: str,
                                 extra_text: str = "") -> bool:
        """
        检查是否属于黑天鹅/极端风险。
        返回 True = 属于黑天鹅 → 禁止触发自愈。
        """
        combined = f"{stock_code} {extra_text}".lower()
        for kw in self.BLACK_SWAN_KEYWORDS:
            if kw.lower() in combined:
                return True
        return False

    # ===================== 触发条件校验 =====================

    def check_trigger_condition(self, label_name: str,
                                price_data: dict = None,
                                event_data: dict = None) -> tuple[bool, str]:
        """
        校验某个标签是否满足自愈触发条件。

        参数:
            label_name: 误差标签名
            price_data: {
                "signal_price": float,     # 信号生成日价格
                "current_price": float,    # 当前价格
                "peak_price": float,       # 信号生成后最高价(冲高)
                "current_from_signal": float,  # 距信号日涨跌幅(%)
                "retrace_pct": float,      # 冲高回吐幅度(%)
            }
            event_data: {
                "bad_news_landed": bool,   # 利空事件落地
                "catalyst_exhausted": bool, # 催化剂兑现
            }

        返回:
            (triggered: bool, condition_desc: str)
        """
        # 非目标标签 → 禁止触发
        if label_name not in self.TARGET_LABELS:
            return False, f"非乐观类标签({label_name})，跳过自愈校验"

        rule = self.TRIGGER_RULES.get(label_name)
        if not rule:
            return False, f"未找到触发规则: {label_name}"

        triggered_conditions = []

        if label_name == "预判高估，负误差":
            if price_data:
                decline_pct = price_data.get("current_from_signal", 0)
                if decline_pct <= -25:
                    triggered_conditions.append(
                        f"累计回调{decline_pct:.1f}%≥25%")
            if event_data and event_data.get("bad_news_landed"):
                triggered_conditions.append(
                    "对应利空事件正式落地披露")

        elif label_name == "机会漏判，负误差":
            if price_data:
                retrace = price_data.get("retrace_pct", 0)
                if retrace >= 60:
                    triggered_conditions.append(
                        f"回吐幅度{retrace:.1f}%≥60%")
            if event_data and event_data.get("catalyst_exhausted"):
                triggered_conditions.append(
                    "短期驱动利好兑现，无持续性上行逻辑")

        if triggered_conditions:
            cond_text = " | ".join(triggered_conditions)
            logging.info(f"  🔄 自愈触发条件满足 [{label_name}]: {cond_text}")
            return True, cond_text

        return False, "触发条件未满足"

    # ===================== 注册自愈样本 =====================

    def register_self_heal(self, ts_code: str, label_name: str,
                           factor_name: str,
                           initial_weight: float,
                           trigger_condition: str,
                           extra_black_swan_text: str = "") -> dict:
        """
        注册一条新的自愈样本。

        执行流程:
          1. 黑天鹅豁免校验 (极端风险不放行)
          2. 重复注册检查 (已存在则跳过)
          3. 写入数据库

        返回:
            {
                "registered": bool,
                "ts_code": str,
                "label_name": str,
                "factor_name": str,
                "initial_weight": float,
                "current_weight": float,
                "status": "registered" | "exists" | "blocked_black_swan",
                "reason": str,
            }
        """
        # Step 1: 黑天鹅豁免校验
        if self._check_black_swan_exempt(ts_code, extra_black_swan_text):
            msg = f"黑天鹅/极端风险豁免，禁止自愈 [{ts_code}/{factor_name}]"
            logging.warning(f"  ⛔ {msg}")
            return {
                "registered": False,
                "ts_code": ts_code,
                "label_name": label_name,
                "factor_name": factor_name,
                "initial_weight": initial_weight,
                "current_weight": initial_weight,
                "status": "blocked_black_swan",
                "reason": msg,
            }

        # Step 2: 重复检查
        existing = self._load_sample(ts_code, label_name, factor_name)
        if existing:
            if not existing["is_completed"]:
                return {
                    "registered": False,
                    "ts_code": ts_code,
                    "label_name": label_name,
                    "factor_name": factor_name,
                    "initial_weight": existing["initial_weight"],
                    "current_weight": existing["current_weight"],
                    "status": "exists_active",
                    "reason": "自愈样本已存在且活跃中",
                }
            # 已完成的样本可重新注册（新周期）
            initial_weight = existing["current_weight"]

        today = datetime.now().strftime("%Y%m%d")
        sample = {
            "ts_code": ts_code,
            "label_name": label_name,
            "factor_name": factor_name,
            "trigger_date": today,
            "trigger_condition": trigger_condition,
            "black_swan_checked": True,
            "initial_weight": round(initial_weight, 4),
            "current_weight": round(initial_weight, 4),
            "decay_count": 0,
            "last_decay_date": today,
            "is_completed": False,
            "completed_date": "",
            "completion_reason": "",
        }

        ok = self._save_sample(sample)
        result = {
            "registered": ok,
            "ts_code": ts_code,
            "label_name": label_name,
            "factor_name": factor_name,
            "initial_weight": sample["initial_weight"],
            "current_weight": sample["current_weight"],
            "status": "registered" if ok else "save_failed",
            "reason": "" if ok else "数据库写入失败",
        }

        if ok:
            # 同步初始权重到主映射表
            self._sync_weight_to_main_table(
                ts_code, factor_name, label_name, initial_weight)

        if ok:
            logging.info(
                f"  ✅ 自愈样本已注册 [{ts_code}/{factor_name}→{label_name}] "
                f"权重={initial_weight:.2f}, 条件={trigger_condition[:40]}")

        return result

    # ===================== 按周期执行衰减 =====================

    def apply_daily_decay(self, ts_code: str = None,
                          trade_date: str = None) -> dict:
        """
        对所有活跃自愈样本执行周期衰减。

        衰减规则:
          每满 DECAY_CYCLE_DAYS(5) 个交易日，
          权重 = max(WEIGHT_FLOOR, current_weight × (1 - SINGLE_DECAY_RATIO))
          到达下限 → 标记[自愈完成]

        参数:
            ts_code: 若指定，只衰减该标的的样本
            trade_date: 当前交易日 YYYYMMDD

        返回:
            {
                "checked": int,           # 检查的活跃样本数
                "decayed": int,           # 执行衰减次数
                "completed": int,         # 本次新完成的次数
                "details": [...],         # 每条衰减明细
            }
        """
        trade_date = trade_date or datetime.now().strftime("%Y%m%d")
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()

        # 查询所有活跃(未完成)样本
        query = f"""
            SELECT id, ts_code, label_name, factor_name,
                   current_weight, decay_count, last_decay_date
            FROM {SELF_HEAL_TABLE}
            WHERE is_completed = 0
        """
        params = []
        if ts_code:
            query += " AND ts_code = ?"
            params.append(ts_code)
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        result = {
            "checked": len(rows),
            "decayed": 0,
            "completed": 0,
            "details": [],
        }

        # 无需交易日历简化：用日期差/5取整判断
        today_dt = datetime.strptime(trade_date, "%Y%m%d")

        for row in rows:
            sid, s_ts_code, s_label, s_factor, cur_w, dcount, last_decay = row

            # 计算距上次衰减的交易日数(用自然日/1.4近似)
            last_dt = datetime.strptime(last_decay, "%Y%m%d")
            elapsed_days = (today_dt - last_dt).days
            trading_days = int(elapsed_days * 1.4)  # 自然日→交易日近似

            if trading_days < self.DECAY_CYCLE_DAYS:
                continue  # 未满一个周期

            # 执行衰减
            cycles = trading_days // self.DECAY_CYCLE_DAYS
            new_weight = cur_w
            actual_cycles = 0
            for _ in range(min(cycles, 10)):  # 单次最多10周期(防过大)
                new_weight = max(
                    self.WEIGHT_FLOOR,
                    new_weight * (1 - self.SINGLE_DECAY_RATIO)
                )
                actual_cycles += 1
                if new_weight <= self.WEIGHT_FLOOR + 0.001:
                    break

            new_weight = round(new_weight, 4)

            # 判断是否完成
            is_done = new_weight <= self.WEIGHT_FLOOR + 0.001
            completed_date = trade_date if is_done else ""
            completion_reason = self.FINISH_TAG if is_done else ""

            # 更新数据库
            conn2 = sqlite3.connect(str(MEMORY_DB))
            cur2 = conn2.cursor()
            cur2.execute(f"""
                UPDATE {SELF_HEAL_TABLE}
                SET current_weight = ?,
                    decay_count = decay_count + ?,
                    last_decay_date = ?,
                    is_completed = ?,
                    completed_date = ?,
                    completion_reason = ?
                WHERE id = ?
            """, (new_weight, actual_cycles, trade_date,
                  1 if is_done else 0,
                  completed_date, completion_reason,
                  sid))
            conn2.commit()
            conn2.close()

            result["decayed"] += 1
            decay_detail = {
                "id": sid,
                "ts_code": s_ts_code,
                "label_name": s_label,
                "factor_name": s_factor,
                "before_weight": round(cur_w, 4),
                "after_weight": new_weight,
                "decay_cycles": actual_cycles,
                "total_decay_count": dcount + actual_cycles,
                "is_completed": is_done,
            }
            result["details"].append(decay_detail)

            # 同步到主映射表 dynamic_signal_mapping
            self._sync_weight_to_main_table(
                s_ts_code, s_factor, s_label, new_weight)

            if is_done:
                result["completed"] += 1
                logging.info(
                    f"  🏁 自愈完成 [{s_ts_code}/{s_factor}->{s_label}] "
                    f"{cur_w:.3f}->{new_weight:.3f} {self.FINISH_TAG}")
            else:
                logging.info(
                    f"  🔄 权重衰减 [{s_ts_code}/{s_factor}->{s_label}] "
                    f"{cur_w:.3f}->{new_weight:.3f} "
                    f"(第{dcount+actual_cycles}次衰减)")

        return result

    def _sync_weight_to_main_table(self, ts_code,
                                    factor_name,
                                    label_name,
                                    new_weight):
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            cur.execute(f"""
                UPDATE dynamic_signal_mapping
                SET weight = ?,
                    hit_count = hit_count,
                    last_updated = ?
                WHERE factor_name = ? AND label_name = ?
            """, (round(new_weight, 4), now, factor_name, label_name))
            affected = cur.rowcount
            conn.commit()
            conn.close()
            if affected == 0:
                conn2 = sqlite3.connect(str(MEMORY_DB))
                cur2 = conn2.cursor()
                cur2.execute(f"""
                    INSERT OR IGNORE INTO dynamic_signal_mapping
                    (factor_name, label_name, weight, hit_count, last_updated)
                    VALUES (?, ?, ?, 0, ?)
                """, (factor_name, label_name, round(new_weight, 4), now))
                conn2.commit()
                conn2.close()
            return True
        except Exception as e:
            logging.warning(f"  ⚠️ 主表权重同步失败: {e}")
            return False

    # ===================== 获取有效权重 =====================

    def get_effective_weight(self, factor_name: str, label_name: str,
                             ts_code: str = "",
                             default_weight: float = 0.5) -> float:
        """
        获取考虑自愈衰减后的映射关联有效权重。

        查询路径:
          1. 先查该标的+因子+标签是否有活跃自愈样本
          2. 有 → 返回 current_weight (已衰减)
          3. 无 → 返回 default_weight (原始权重)

        参数:
            factor_name: 因子名
            label_name: 标签名
            ts_code: 标的代码
            default_weight: 原始权重(未衰减)

        返回:
            float: 有效权重 (≥ WEIGHT_FLOOR)
        """
        if not ts_code:
            return default_weight

        sample = self._load_sample(ts_code, label_name, factor_name)
        if sample and not sample["is_completed"]:
            return sample["current_weight"]
        return default_weight

    def batch_get_effective_weights(self, ts_code: str,
                                     base_weights: dict[str, dict[str, float]]
                                     ) -> dict[str, dict[str, float]]:
        """
        批量获取某标的全部有效映射权重。

        参数:
            ts_code: 标的代码
            base_weights: {label_name: {factor_name: weight, ...}, ...}

        返回:
            {label_name: {factor_name: effective_weight, ...}, ...}
        """
        result = {}
        for label, factors in base_weights.items():
            result[label] = {}
            for fname, w in factors.items():
                result[label][fname] = self.get_effective_weight(
                    fname, label, ts_code, w)
        return result

    # ===================== 核心入口: 检查+注册一体化 =====================

    def check_and_register(self, ts_code: str, score_result: dict,
                           factor_values: dict = None,
                           price_data: dict = None,
                           event_data: dict = None,
                           extra_black_swan_text: str = "",
                           weight_config=None) -> dict:
        """
        对分析结果检查乐观类误判是否满足自愈条件，如满足则注册。

        参数:
            ts_code: 标的代码
            score_result: DynamicMappingEngine 的 score_result dict
            factor_values: 因子值 (用于提取初始权重)
            price_data: 价格数据 (触发条件判定)
            event_data: 事件数据 (触发条件判定)
            extra_black_swan_text: 额外黑天鹅校验文本
            weight_config: CorrelationWeightConfig 实例 (获取原始权重)

        返回:
            {
                "checked": int,              # 检查的标签数
                "registered": int,           # 新注册数
                "blocked_black_swan": int,   # 被黑天鹅拦截数
                "condition_not_met": int,    # 条件不满足数
                "already_exists": int,       # 已存在数
                "registrations": [...],      # 注册详情
            }
        """
        details = score_result.get("details", {})
        activated_labels = [
            k for k, v in details.items()
            if v.get("activated", False)
        ]

        result = {
            "checked": 0,
            "registered": 0,
            "blocked_black_swan": 0,
            "condition_not_met": 0,
            "already_exists": 0,
            "registrations": [],
        }

        # 只检查目标标签
        for label_name in self.TARGET_LABELS:
            if label_name not in activated_labels:
                continue  # 未激活的不检查
            result["checked"] += 1

            # Step A: 检查触发条件
            triggered, cond_text = self.check_trigger_condition(
                label_name, price_data, event_data)
            if not triggered:
                result["condition_not_met"] += 1
                continue

            # Step B: 获取该标签关联的所有因子
            from dynamic_weight_mapping import SignalStrengthScorer, CorrelationWeightConfig
            mapped_factors = SignalStrengthScorer.LABEL_FACTOR_MAP.get(label_name, [])

            if not mapped_factors:
                continue

            # 为每个关联因子注册自愈样本
            if weight_config is None:
                weight_config = CorrelationWeightConfig()

            for fname in mapped_factors:
                original_w = weight_config.get_weight(fname, label_name)

                reg_result = self.register_self_heal(
                    ts_code=ts_code,
                    label_name=label_name,
                    factor_name=fname,
                    initial_weight=original_w,
                    trigger_condition=cond_text,
                    extra_black_swan_text=extra_black_swan_text,
                )

                result["registrations"].append(reg_result)
                if reg_result["status"] == "registered":
                    result["registered"] += 1
                elif reg_result["status"] == "blocked_black_swan":
                    result["blocked_black_swan"] += 1
                elif reg_result["status"] == "exists_active":
                    result["already_exists"] += 1

        if result["checked"] > 0:
            logging.info(
                f"  📋 自愈检查汇总 [{ts_code}]: "
                f"检查{result['checked']}标签, "
                f"新注册{result['registered']}条, "
                f"已存在{result['already_exists']}条, "
                f"黑天鹅拦截{result['blocked_black_swan']}条, "
                f"条件不满足{result['condition_not_met']}条")

        return result

    # ===================== 报告导出 =====================

    def export_active_samples(self, ts_code: str = None) -> list[dict]:
        """导出活跃(未完成)自愈样本清单"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        query = f"""
            SELECT id, ts_code, label_name, factor_name,
                   trigger_date, trigger_condition,
                   initial_weight, current_weight, decay_count,
                   last_decay_date, is_completed, completed_date
            FROM {SELF_HEAL_TABLE}
            WHERE is_completed = 0
        """
        params = []
        if ts_code:
            query += " AND ts_code = ?"
            params.append(ts_code)
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        samples = []
        for r in rows:
            samples.append({
                "id": r[0],
                "ts_code": r[1],
                "label_name": r[2],
                "factor_name": r[3],
                "trigger_date": r[4],
                "trigger_condition": r[5],
                "initial_weight": r[6],
                "current_weight": r[7],
                "decay_count": r[8],
                "last_decay_date": r[9],
                "is_completed": bool(r[10]),
                "completed_date": r[11],
            })
        return samples

    def export_completed_samples(self, ts_code: str = None) -> list[dict]:
        """导出已完成自愈样本清单"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()
        query = f"""
            SELECT id, ts_code, label_name, factor_name,
                   trigger_date, trigger_condition,
                   initial_weight, current_weight, decay_count,
                   last_decay_date, completed_date, completion_reason
            FROM {SELF_HEAL_TABLE}
            WHERE is_completed = 1
        """
        params = []
        if ts_code:
            query += " AND ts_code = ?"
            params.append(ts_code)
        query += " ORDER BY completed_date DESC LIMIT 50"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        samples = []
        for r in rows:
            samples.append({
                "id": r[0],
                "ts_code": r[1],
                "label_name": r[2],
                "factor_name": r[3],
                "trigger_date": r[4],
                "trigger_condition": r[5],
                "initial_weight": r[6],
                "current_weight": r[7],
                "decay_count": r[8],
                "last_decay_date": r[9],
                "completed_date": r[10],
                "completion_reason": r[11],
            })
        return samples

    def summary(self, ts_code: str = None) -> dict:
        """自愈系统汇总报告"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cur = conn.cursor()

        base_q = " WHERE ts_code=?" if ts_code else ""
        params = [ts_code] if ts_code else []

        # 活跃样本数
        cur.execute(f"""
            SELECT COUNT(*) FROM {SELF_HEAL_TABLE}
            WHERE is_completed=0 {base_q}
        """, params)
        active = cur.fetchone()[0]

        # 已完成数
        cur.execute(f"""
            SELECT COUNT(*) FROM {SELF_HEAL_TABLE}
            WHERE is_completed=1 {base_q}
        """, params)
        completed = cur.fetchone()[0]

        # 总样本
        cur.execute(f"""
            SELECT COUNT(*) FROM {SELF_HEAL_TABLE} {base_q}
        """, params)
        total = cur.fetchone()[0]

        # 按标签分组
        label_groups = {}
        label_q = f"""
            SELECT label_name, is_completed, COUNT(*)
            FROM {SELF_HEAL_TABLE}
            {base_q}
            GROUP BY label_name, is_completed
        """
        cur.execute(label_q, params)
        for row in cur.fetchall():
            key = f"{'✅已完成' if row[1] else '🔄活跃中'} {row[0]}"
            label_groups[key] = row[2]

        conn.close()
        return {
            "ts_code": ts_code or "ALL",
            "total_samples": total,
            "active_samples": active,
            "completed_samples": completed,
            "by_status": label_groups,
        }


# ===================== 快捷入口 =====================

def run_self_heal_daily(trade_date: str = None) -> dict:
    """
    每日执行自愈衰减扫描。

    应在每个交易日开盘前执行一次，
    自动衰减所有活跃自愈样本的权重。
    """
    unit = SelfHealWeightDecayUnit()
    today = trade_date or datetime.now().strftime("%Y%m%d")
    logging.info(f"\n{'='*50}")
    logging.info(f"🏥 自愈衰减扫描 [{today}]")
    logging.info(f"{'='*50}")

    result = unit.apply_daily_decay(trade_date=today)

    # 汇总
    summary = unit.summary()
    logging.info(f"\n  汇总: {summary}")
    logging.info(f"✅ 自愈衰减扫描完成")
    return {
        "trade_date": today,
        "decay_result": result,
        "summary": summary,
    }


# ===================== 测试 =====================

if __name__ == "__main__":
    import sys

    unit = SelfHealWeightDecayUnit()

    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        ts_code = sys.argv[2] if len(sys.argv) > 2 else None
        s = unit.summary(ts_code)
        print(f"\n🏥 自愈系统汇总 ({s['ts_code']})")
        print(f"  总样本: {s['total_samples']}")
        print(f"  活跃中: {s['active_samples']}")
        print(f"  已完成: {s['completed_samples']}")
        if s['by_status']:
            print(f"  分组: {s['by_status']}")

    elif len(sys.argv) > 1 and sys.argv[1] == "decay":
        trade_date = sys.argv[2] if len(sys.argv) > 2 else None
        result = run_self_heal_daily(trade_date)
        print(f"\n✅ 衰减执行: {result['decay_result']['decayed']}条")
        print(f"  新完成: {result['decay_result']['completed']}条")
        for d in result['decay_result']['details'][:5]:
            print(f"  {d['ts_code']}/{d['factor_name']}→{d['label_name']}: "
                  f"{d['before_weight']:.3f}→{d['after_weight']:.3f} "
                  f"{'🏁完成' if d['is_completed'] else ''}")

    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # 场景1: 预判高估 → 回调≥25% → 注册自愈 → 5日后衰减
        print("\n=== 场景1: 预判高估·回调达标 → 注册自愈 ===")
        result1 = unit.check_and_register(
            ts_code="600547.SH",
            score_result={
                "details": {
                    "预判高估，负误差": {
                        "activated": True,
                        "score": 75.0,
                        "factor_contrib": {"price_surge_3d": {"effective_strength": 8, "raw_strength": 8, "weight": 1.0, "contrib": 8.0}},
                    },
                }
            },
            factor_values={"price_surge_3d": 40},
            price_data={
                "current_from_signal": -28.5,   # 回调28.5%≥25% ✓
                "retrace_pct": 0,
            },
        )
        print(f"  检查标签: {result1['checked']}")
        print(f"  新注册: {result1['registered']}")
        for r in result1['registrations'][:3]:
            print(f"  {r['status']}: {r['factor_name']}→{r['label_name']} W={r['initial_weight']}")

        # 场景2: 机会漏判 → 回吐80% → 注册后衰减2周期
        print("\n=== 场景2: 机会漏判·回吐达标 → 注册 → 衰减 ===")
        result2 = unit.check_and_register(
            ts_code="300476.SZ",
            score_result={
                "details": {
                    "机会漏判，负误差": {
                        "activated": True,
                        "score": 68.0,
                        "factor_contrib": {"price_surge_60d": {"effective_strength": 7, "raw_strength": 7, "weight": 0.5, "contrib": 3.5}},
                    },
                }
            },
            factor_values={"price_surge_60d": 50},
            price_data={
                "current_from_signal": -10,
                "retrace_pct": 72,   # 回吐72%≥60% ✓
            },
        )
        print(f"  检查标签: {result2['checked']}")
        print(f"  新注册: {result2['registered']}")
        for r in result2['registrations'][:3]:
            print(f"  {r['status']}: {r['factor_name']}→{r['label_name']} W={r['initial_weight']}")

        # 场景3: 黑天鹅豁免 → 禁止触发自愈
        print("\n=== 场景3: 黑天鹅豁免(退市预警) → 拦截 ===")
        result3 = unit.check_and_register(
            ts_code="600XXX.SH",
            score_result={
                "details": {
                    "预判高估，负误差": {
                        "activated": True,
                        "score": 90.0,
                    },
                }
            },
            price_data={"current_from_signal": -35},
            extra_black_swan_text="退市预警",
        )
        print(f"  拦截数: {result3['blocked_black_swan']}")
        for r in result3['registrations'][:3]:
            print(f"  {r['status']}: {r['factor_name']}→{r['label_name']} | {r['reason'][:40]}")

        # 场景4: 条件不满足 → 不注册
        print("\n=== 场景4: 条件不满足(回调仅15%) → 不注册 ===")
        result4 = unit.check_and_register(
            ts_code="600884.SH",
            score_result={
                "details": {
                    "预判高估，负误差": {
                        "activated": True,
                        "score": 65.0,
                    },
                }
            },
            price_data={
                "current_from_signal": -15,   # 仅回调15% < 25% ✗
                "retrace_pct": 0,
            },
        )
        print(f"  条件不满足: {result4['condition_not_met']}")

        # 场景5: 模拟衰减
        print("\n=== 场景5: 模拟5日后衰减 ===")
        from datetime import timedelta
        fake_date_5d = "20260727"  # 假设5个交易日后
        decay_result = unit.apply_daily_decay(trade_date=fake_date_5d)
        print(f"  执行衰减: {decay_result['decayed']}条")
        print(f"  新完成: {decay_result['completed']}条")
        for d in decay_result['details'][:5]:
            print(f"  {d['ts_code']}/{d['factor_name']}: "
                  f"{d['before_weight']:.3f}→{d['after_weight']:.3f} "
                  f"{'🏁' if d['is_completed'] else ''}")

        # 场景6: 汇总报告
        print("\n=== 场景6: 汇总报告 ===")
        s = unit.summary()
        print(f"  总样本: {s['total_samples']}")
        print(f"  活跃中: {s['active_samples']}")
        print(f"  已完成: {s['completed_samples']}")
        print(f"  分组: {s['by_status']}")

    else:
        print("用法:")
        print("  python self_heal_weight_decay.py test        # 运行6场景测试")
        print("  python self_heal_weight_decay.py summary     # 汇总报告")
        print("  python self_heal_weight_decay.py summary <CODE>  # 单标汇总")
        print("  python self_heal_weight_decay.py decay       # 执行衰减")
        print("  python self_heal_weight_decay.py decay YYYYMMDD  # 指定日期衰减")
