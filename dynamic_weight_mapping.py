#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dynamic_weight_mapping.py — 动态加权多对多映射引擎

废弃固定一对一信号标签连线，采用动态加权多对多信号映射机制。

Module 1: 信号强度打分(0~10) — 独立因子极值打分
Module 2: 关联权重配置(0.2~1.0) — 多因子联动多标签
Module 3: 误判总分计算 — SUM(强度×权重) ≥60 激活调参
Module 4: 动态映射替换固定连线 — 一对多/多对多 + 向量持久化

执行优先级: 高于基础盘后标注流程
"""

import logging
import json
import sqlite3
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [DWM] %(message)s",
                    datefmt="%H:%M:%S")

BASE = Path("/opt/stock_agent")
MEMORY_DB = BASE / "agent_memory.db"
MAPPING_TABLE = "dynamic_signal_mapping"
VECTORS_FILE = BASE / "dynamic_mapping_vectors.json"


# ====================== Module 1: 信号强度打分 ======================

class SignalStrengthScorer:
    """
    信号强度打分标准化引擎 (0~10 分量化赋值)

    打分逻辑: 偏离合理基准区间幅度越大分值越高。
    完全符合安全基准 = 0 分，极端极值风险 = 10 分。
    分值区间锁死整数 0~10，5档风险梯度。

    五档风险梯度:
      0~2: 无/轻微风险  指标处于合理安全区间
      3~4: 轻度风险      小幅偏离基准
      5~6: 中度风险      明显偏离基准，潜在负面逻辑
      7~8: 重度风险      大幅偏离基准，利空确定性高
      9~10: 极端风险     极值偏离/黑天鹅/重大暴雷

    硬性约束:
      1. 单一指标独立打分，不合并不叠加
      2. 单条最高10分，最低0分，无负数
      3. 多因子不叠加原始分，叠加通过衰减×权重实现
    """

    # ===== 1. 量价/资金类信号打分阈值 =====
    TIERS = {
        # 60日阶段涨幅 (%)
        "price_surge_60d": {
            "name": "60日阶段涨幅",
            "category": "量价/资金",
            "tiers": [(0, 30, 1), (30, 60, 4), (60, 100, 6), (100, 180, 8), (180, None, 10)],
            "higher_is_risk": True,
        },
        # 20日阶段涨幅 (%)
        "price_surge_20d": {
            "name": "20日阶段涨幅",
            "category": "量价/资金",
            "tiers": [(0, 15, 1), (15, 30, 4), (30, 50, 6), (50, 80, 8), (80, None, 10)],
            "higher_is_risk": True,
        },
        # 3日涨幅 (%)
        "price_surge_3d": {
            "name": "3日涨幅",
            "category": "量价/资金",
            "tiers": [(0, 8, 1), (8, 15, 4), (15, 25, 6), (25, 40, 8), (40, None, 10)],
            "higher_is_risk": True,
        },
        # 连续放量下跌/资金流出 (日流出强度)
        "institutional_outflow": {
            "name": "主力资金流出(亿/日)",
            "category": "量价/资金",
            "tiers": [(0, 0.5, 1), (0.5, 2, 4), (2, 5, 6), (5, 10, 8), (10, None, 10)],
            "higher_is_risk": True,
        },
        # 量比(异常放量)
        "volume_ratio": {
            "name": "量比(异常放量)",
            "category": "量价/资金",
            "tiers": [(0, 1.5, 1), (1.5, 2.5, 4), (2.5, 4, 6), (4, 6, 8), (6, None, 10)],
            "higher_is_risk": True,
        },
        # 板块vs个股背离(%)
        "board_divergence": {
            "name": "板块vs个股背离(%)",
            "category": "量价/资金",
            "tiers": [(0, 5, 1), (5, 10, 4), (10, 20, 6), (20, 35, 8), (35, None, 10)],
            "higher_is_risk": True,
        },
        # 连续上涨天数
        "consecutive_up_days": {
            "name": "连续上涨天数",
            "category": "量价/资金",
            "tiers": [(0, 5, 1), (5, 8, 4), (8, 12, 6), (12, 18, 8), (18, None, 10)],
            "higher_is_risk": True,
        },
        # 散户流入占比(%)
        "retail_inflow_ratio": {
            "name": "散户流入占比(%)",
            "category": "量价/资金",
            "tiers": [(0, 20, 1), (20, 40, 4), (40, 60, 6), (60, 80, 8), (80, None, 10)],
            "higher_is_risk": True,
        },
        # 短期波动率(%)
        "short_term_volatility": {
            "name": "短期波动率(%)",
            "category": "量价/资金",
            "tiers": [(0, 3, 1), (3, 6, 4), (6, 10, 6), (10, 15, 8), (15, None, 10)],
            "higher_is_risk": True,
        },
        # 换手率(%)
        "turnover_spike": {
            "name": "换手率(%)",
            "category": "量价/资金",
            "tiers": [(0, 5, 1), (5, 10, 4), (10, 20, 6), (20, 35, 8), (35, None, 10)],
            "higher_is_risk": True,
        },
        # 融资买入占比(%)
        "margin_trading_ratio": {
            "name": "融资买入占比(%)",
            "category": "量价/资金",
            "tiers": [(0, 10, 1), (10, 20, 4), (20, 30, 6), (30, 45, 8), (45, None, 10)],
            "higher_is_risk": True,
        },

        # ===== 2. 财务/商誉/负债类信号打分阈值 =====
        # 资产负债率(%)
        "debt_ratio": {
            "name": "资产负债率(%)",
            "category": "财务/商誉/负债",
            "tiers": [(0, 50, 1), (50, 70, 4), (70, 85, 6), (85, 95, 8), (95, None, 10)],
            "higher_is_risk": True,
        },
        # 商誉占净资产比例(%)
        "goodwill_ratio": {
            "name": "商誉/净资产(%)",
            "category": "财务/商誉/负债",
            "tiers": [(0, 15, 1), (15, 30, 4), (30, 50, 6), (50, 80, 8), (80, None, 10)],
            "higher_is_risk": True,
        },
        # 归母净利润同比降幅(%)
        "profit_decline": {
            "name": "净利润同比降幅(%)",
            "category": "财务/商誉/负债",
            # 正值表示下滑幅度：下滑30%=30
            "tiers": [(0, 10, 1), (10, 30, 4), (30, 70, 6), (70, 100, 8), (100, None, 10)],
            "higher_is_risk": True,
        },
        # PE偏离度
        "pe_deviation": {
            "name": "PE偏离度",
            "category": "财务/商誉/负债",
            "tiers": [(0, 15, 1), (15, 30, 4), (30, 50, 6), (50, 80, 8), (80, None, 10)],
            "higher_is_risk": True,
        },

        # ===== 3. 黑天鹅/极端舆情类信号 =====
        "concept_purity": {
            "name": "概念纯度(非题材股判定)",
            "category": "黑天鹅/舆情极端",
            # 越低越危险：纯度<30%=概念跟风
            "tiers": [(80, None, 1), (60, 80, 4), (40, 60, 6), (20, 40, 8), (0, 20, 10)],
            "higher_is_risk": False,  # 越低越危险
        },
    }

    # 10类标签对应的风险因子映射（多对多）
    LABEL_FACTOR_MAP = {
        "预判高估，负误差":     ["price_surge_60d", "price_surge_20d", "board_divergence"],
        "预判低估，负误差":     ["profit_decline", "institutional_outflow", "pe_deviation"],
        "风控判断有效":         ["institutional_outflow", "board_divergence", "debt_ratio"],
        "入场条件失效":         ["price_surge_3d", "volume_ratio", "concept_purity"],
        "区间判断失效":         ["short_term_volatility", "board_divergence", "price_surge_20d"],
        "持仓信号有效":         ["consecutive_up_days", "volume_ratio"],
        "规避信号有效":         ["institutional_outflow", "debt_ratio", "profit_decline"],
        "机会漏判，负误差":     ["price_surge_60d", "price_surge_20d", "volume_ratio"],
        "止损阈值误判":          ["short_term_volatility", "volume_ratio"],
        "止盈阈值偏保守":       ["price_surge_3d", "turnover_spike", "consecutive_up_days"],
    }

    # 动态校准偏移量 (盘后根据人工标注自动调整)
    _calibration_offsets: dict[str, int] = {}
    CALIB_TABLE = "factor_calibration_offsets"

    @classmethod
    def score_factor(cls, factor_name: str, value: float) -> int:
        """
        单因子强度打分(0~10整数)，基于离散分档阈值。

        返回整数 0~10，无小数。
        """
        config = cls.TIERS.get(factor_name)
        if not config:
            return 0

        tiers = config["tiers"]
        higher_is_risk = config.get("higher_is_risk", True)
        offset = cls._calibration_offsets.get(factor_name, 0)

        # 根据分档匹配得分
        if higher_is_risk:
            # 常规: 值越高越危险
            for lo, hi, base_score in tiers:
                if hi is None:
                    if value > lo:
                        raw = base_score
                        break
                else:
                    if lo < value <= hi:
                        raw = base_score
                        break
            else:
                raw = 0
        else:
            # 逆向: 值越低越危险 (如概念纯度)
            for lo, hi, base_score in tiers:
                if hi is None:
                    # 开放上界: 值 >= lo 为最安全档
                    if value >= lo:
                        raw = base_score
                        break
                else:
                    # lo <= value < hi (半开区间)
                    if lo <= value < hi:
                        raw = base_score
                        break
            else:
                raw = 10  # 低于最低档 → 最高风险

        # 应用动态校准偏移，锁死 0~10
        score = max(0, min(10, raw + offset))
        return int(score)

    @classmethod
    def score_all(cls, factors: dict[str, float]) -> dict[str, int]:
        """
        多因子批量打分。

        参数:
            factors: {"factor_name": value, ...}

        返回:
            {"factor_name": score(0~10), ...}
        """
        result = {}
        for name, value in factors.items():
            if name in cls.TIERS:
                result[name] = cls.score_factor(name, value)
        return result

    @classmethod
    def apply_calibration(cls, factor_name: str, error_count: int, total_count: int):
        """
        动态自适应校准。

        盘后根据人工标注样本迭代调整打分阈值:
        - 同一指标多次出现负误差 → 下调1档(同等偏离打分更高, offset+1)
        - 指标长期无误判 → 上调1档(同等偏离打分更低, offset-1)

        参数:
            factor_name: 因子名
            error_count: 该因子出现负误差的次数(最近N日)
            total_count: 该因子总出现次数
        """
        if total_count < 5:
            return  # 样本不足不调整

        error_rate = error_count / total_count
        current = cls._calibration_offsets.get(factor_name, 0)

        if error_rate > 0.6:
            # 负误差率>60% → 打分偏保守, 上调1档
            new_offset = min(3, current + 1)
        elif error_rate < 0.2 and total_count >= 10:
            # 负误差率<20% → 打分偏激进, 下调1档
            new_offset = max(-3, current - 1)
        else:
            return  # 不调整

        cls._calibration_offsets[factor_name] = new_offset
        cls._persist_calibration(factor_name, new_offset)

    @classmethod
    def _persist_calibration(cls, factor_name: str, offset: int):
        """持久化校准偏移"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {cls.CALIB_TABLE} (
                    factor_name TEXT PRIMARY KEY,
                    offset INTEGER DEFAULT 0,
                    updated_at TEXT
                )
            """)
            cur.execute(f"""
                INSERT OR REPLACE INTO {cls.CALIB_TABLE}
                VALUES (?, ?, datetime('now','localtime'))
            """, (factor_name, offset))
            conn.commit()
            conn.close()
        except Exception:
            pass

    @classmethod
    def get_risk_level(cls, score: int) -> tuple[str, str]:
        """获取分值对应的风险等级"""
        if score <= 2: return "无/轻微风险", "🟢"
        if score <= 4: return "轻度风险", "🟡"
        if score <= 6: return "中度风险", "🟠"
        if score <= 8: return "重度风险", "🔴"
        return "极端风险", "🚫"

    @classmethod
    def score_report(cls, factors: dict[str, float]) -> list[dict]:
        """生成打分报告"""
        scores = cls.score_all(factors)
        report = []
        for fname, raw_value in factors.items():
            config = cls.TIERS.get(fname)
            if not config:
                continue
            sc = scores.get(fname, 0)
            level, icon = cls.get_risk_level(sc)
            offset = cls._calibration_offsets.get(fname, 0)
            bar = "█" * sc + "░" * (10 - sc)
            report.append({
                "factor": fname,
                "name": config["name"],
                "category": config["category"],
                "value": raw_value,
                "score": sc,
                "level": level,
                "icon": icon,
                "bar": bar,
                "calibration_offset": offset,
            })
        return report


# ====================== Module 2: 关联权重配置 ======================

class CorrelationWeightConfig:
    """
    动态权重矩阵管理器。

    每条风险信号与误判标签的映射关系独立配置专属关联权重(0.2~1.0)。
    支持多因子联动多标签的自由映射。
    盘后迭代自动更新权重。
    """

    DEFAULT_WEIGHT = 0.5

    def __init__(self):
        self.weight_matrix: dict[str, dict[str, float]] = {}
        self._load()

    def _load(self):
        """从 SQLite 加载权重矩阵"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            # 确保表存在
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {MAPPING_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    factor_name TEXT NOT NULL,
                    label_name TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT {self.DEFAULT_WEIGHT},
                    hit_count INTEGER DEFAULT 0,
                    last_updated TEXT,
                    UNIQUE(factor_name, label_name)
                )
            """)
            conn.commit()

            # 如果表是空的，从 LABEL_FACTOR_MAP 初始化默认权重
            cur.execute(f"SELECT COUNT(*) FROM {MAPPING_TABLE}")
            count = cur.fetchone()[0]
            if count == 0:
                self._init_default_weights(cur)
                conn.commit()

            # 加载全部权重
            cur.execute(f"SELECT factor_name, label_name, weight FROM {MAPPING_TABLE}")
            rows = cur.fetchall()
            for fname, lname, w in rows:
                if fname not in self.weight_matrix:
                    self.weight_matrix[fname] = {}
                self.weight_matrix[fname][lname] = w

            conn.close()
            logging.info(f"  ✅ 权重矩阵加载: {sum(len(v) for v in self.weight_matrix.values())}条映射")
        except Exception as e:
            logging.warning(f"  ⚠️ 权重矩阵加载失败: {e}, 使用默认配置")
            self._build_default_matrix()

    def _init_default_weights(self, cur):
        """从 LABEL_FACTOR_MAP 初始化默认权重"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for label_name, factors in SignalStrengthScorer.LABEL_FACTOR_MAP.items():
            for fname in factors:
                # 强因果(1.0): 主力流出→风控有效, 短期暴涨→预判高估
                weight = 1.0 if self._is_strong_causal(fname, label_name) else 0.5
                cur.execute(
                    f"INSERT OR IGNORE INTO {MAPPING_TABLE} "
                    f"(factor_name, label_name, weight, hit_count, last_updated) "
                    f"VALUES (?, ?, ?, 0, ?)",
                    (fname, label_name, weight, now)
                )

    def _is_strong_causal(self, factor: str, label: str) -> bool:
        """判断是否强因果"""
        strong_pairs = [
            ("price_surge_3d", "预判高估，负误差"),
            ("price_surge_20d", "预判高估，负误差"),
            ("institutional_outflow", "风控判断有效"),
            ("institutional_outflow", "规避信号有效"),
            ("board_divergence", "区间判断失效"),
            ("profit_decline", "预判低估，负误差"),
        ]
        return (factor, label) in strong_pairs

    def _build_default_matrix(self):
        """构建内存默认矩阵"""
        for label_name, factors in SignalStrengthScorer.LABEL_FACTOR_MAP.items():
            for fname in factors:
                if fname not in self.weight_matrix:
                    self.weight_matrix[fname] = {}
                w = 1.0 if self._is_strong_causal(fname, label_name) else self.DEFAULT_WEIGHT
                self.weight_matrix[fname][label_name] = w

    def get_weight(self, factor_name: str, label_name: str) -> float:
        """获取单条映射权重"""
        return self.weight_matrix.get(factor_name, {}).get(label_name, self.DEFAULT_WEIGHT)

    def get_all_weights_for_label(self, label_name: str) -> dict[str, float]:
        """获取某个标签的所有关联因子权重"""
        result = {}
        for fname, labels in self.weight_matrix.items():
            if label_name in labels:
                result[fname] = labels[label_name]
        return result

    def get_all_weights_for_factor(self, factor_name: str) -> dict[str, float]:
        """获取某个因子的所有关联标签权重"""
        return self.weight_matrix.get(factor_name, {})

    def update_weight(self, factor_name: str, label_name: str,
                      misjudge_activated: bool) -> float:
        """
        盘后迭代更新权重。

        多次命中高分误判 → 上调权重(上限1.0)
        长期无匹配误判 → 下调权重(下限0.2)
        """
        current = self.get_weight(factor_name, label_name)

        if misjudge_activated:
            # 命中上调
            new_weight = min(1.0, current + 0.1)
        else:
            # 未命中下调
            new_weight = max(0.2, current - 0.05)

        # 持久化
        self._persist_weight(factor_name, label_name, new_weight, misjudge_activated)

        # 更新内存
        if factor_name not in self.weight_matrix:
            self.weight_matrix[factor_name] = {}
        self.weight_matrix[factor_name][label_name] = new_weight

        return new_weight

    def _persist_weight(self, factor_name: str, label_name: str,
                        weight: float, hit: bool):
        """持久化权重到SQLite"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            cur.execute(f"""
                INSERT INTO {MAPPING_TABLE} (factor_name, label_name, weight, hit_count, last_updated)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(factor_name, label_name) DO UPDATE SET
                    weight = ?,
                    hit_count = hit_count + ?,
                    last_updated = ?
            """, (factor_name, label_name, weight, now, weight, 1 if hit else 0, now))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"  ⚠️ 权重持久化失败: {e}")

    def export_mapping_report(self) -> dict:
        """导出现有映射报告"""
        report = {}
        for label_name in SignalStrengthScorer.LABEL_FACTOR_MAP:
            weights = self.get_all_weights_for_label(label_name)
            report[label_name] = weights
        return report


# ====================== Module 2.3: 信号冲突冗余处理器 ======================

class SignalConflictProcessor:
    """
    信号冲突、冗余标准化处理。

    执行位置: 原始强度打分之后, 时效衰减之前
    处理顺序: 同源冗余合并降噪 → (衰减) → 反向冲突对冲

    四类冗余组:
      1. 量价资金组: 阶段涨幅/放量大跌/资金流出/筹码松动
      2. 财务负债组: 资产负债率/有息负债/流动比率/现金流恶化
      3. 商誉减值组: 商誉占净资产/商誉减值计提/收购亏损
      4. 舆情黑天鹅组: 监管问询/立案/实控人违规/退市风险
    """

    # 四类冗余组映射
    REDUNDANCY_GROUPS = {
        "price_volume": {  # 量价资金组
            "name": "量价资金组",
            "factors": ["price_surge_60d", "price_surge_20d", "price_surge_3d",
                        "institutional_outflow", "volume_ratio", "board_divergence",
                        "consecutive_up_days", "retail_inflow_ratio",
                        "short_term_volatility", "turnover_spike", "margin_trading_ratio"],
        },
        "financial_debt": {  # 财务负债组
            "name": "财务负债组",
            "factors": ["debt_ratio", "pe_deviation"],
        },
        "goodwill": {  # 商誉减值组
            "name": "商誉减值组",
            "factors": ["goodwill_ratio", "profit_decline"],
        },
        "black_swan": {  # 舆情黑天鹅组
            "name": "舆情黑天鹅组",
            "factors": ["concept_purity"],
        },
    }

    # 利好对冲信号 (低分→与利空反向)
    HEDGE_SIGNALS = {
        "profit_decline": {       # 利润正增长为利好
            "inverse": True,       # 值越低(利润降幅小)越利好
        },
        "institutional_outflow": { # 机构净流入为利好
            "inverse": True,
        },
    }

    @classmethod
    def get_group(cls, factor_name: str) -> str:
        """获取因子所属冗余组, None=不在任何组(保留)"""
        for gid, ginfo in cls.REDUNDANCY_GROUPS.items():
            if factor_name in ginfo["factors"]:
                return gid
        return None

    @classmethod
    def deduplicate(cls, raw_scores: dict[str, int]) -> dict[str, int]:
        """
        冗余合并降噪 (默认: 最大值法)。

        同组多条信号仅保留最高分值，其余剔除。
        跨组信号全部保留。

        参数:
            raw_scores: {"factor_name": score(0~10), ...}

        返回:
            去冗余后的信号字典
        """
        # 分组: 每组的最高分因子
        groups: dict[str, list[tuple[str, int]]] = {}
        ungrouped: dict[str, int] = {}

        for fname, score in raw_scores.items():
            gid = cls.get_group(fname)
            if gid:
                if gid not in groups:
                    groups[gid] = []
                groups[gid].append((fname, score))
            else:
                ungrouped[fname] = score

        # 每组取最大值; 若组内有≤2分的低分信号, 也保留(对冲候选)
        deduped = {}
        for gid, members in groups.items():
            if not members:
                continue
            # 最大值(主信号)
            max_member = max(members, key=lambda x: x[1])
            deduped[max_member[0]] = max_member[1]

            # 最小值(对冲候选): 若最小值≤2且与最大值不是同一信号, 保留
            min_member = min(members, key=lambda x: x[1])
            if min_member[1] <= 2 and min_member[0] != max_member[0]:
                deduped[min_member[0]] = min_member[1]

            # 逻辑或剔除: 极端分(9~10)覆盖全组
            # 已经取最大值, 如果最大值>=9就只保留这一个
            # 如果组内有极端值, 其余已在max()时排除

            removed = len(members) - 1
            if removed > 0:
                pass  # 已在日志中体现

        # 合并非冗余信号
        deduped.update(ungrouped)
        return deduped

    @classmethod
    def detect_conflicts(cls, raw_scores: dict[str, int]) -> list[dict]:
        """
        检测冲突对。

        两条信号分属不同大类，风险方向完全相反。
        返回冲突对列表。
        """
        conflicts = []
        factors = list(raw_scores.keys())

        for i in range(len(factors)):
            for j in range(i + 1, len(factors)):
                f1, f2 = factors[i], factors[j]
                g1, g2 = cls.get_group(f1), cls.get_group(f2)

                # 同组不判定冲突(已被去冗余)
                if g1 and g2 and g1 == g2:
                    continue

                s1, s2 = raw_scores[f1], raw_scores[f2]

                # 判定: 一条高分(risk≥5) 另一条低分(hedge≤2) 且不同组
                risk_f, risk_s, hedge_f, hedge_s = None, 0, None, 0

                if s1 >= 5 and s2 <= 2:
                    risk_f, risk_s = f1, s1
                    hedge_f, hedge_s = f2, s2
                elif s2 >= 5 and s1 <= 2:
                    risk_f, risk_s = f2, s2
                    hedge_f, hedge_s = f1, s1

                if risk_f and hedge_f:
                    # 黑天鹅豁免: 9~10分极端信号不参与对冲
                    if risk_s >= 9 and cls.get_group(risk_f) == "black_swan":
                        continue

                    conflicts.append({
                        "risk_factor": risk_f,
                        "risk_score": risk_s,
                        "hedge_factor": hedge_f,
                        "hedge_score": hedge_s,
                        "net_risk": max(0, risk_s - hedge_s),
                    })

        return conflicts

    @classmethod
    def apply_conflict_offset(cls, raw_scores: dict[str, int]) -> dict[str, int]:
        """
        冲突对冲折算。

        对冲修正公式: S_final = max(0, S_risk - S_hedge)
        使用实时分值：前序对冲结果影响后续计算。
        """
        conflicts = cls.detect_conflicts(raw_scores)
        result = dict(raw_scores)

        for c in conflicts:
            risk_f = c["risk_factor"]
            hedge_f = c["hedge_factor"]

            # 使用当前实时分值(前序对冲可能已修改)
            current_risk = result.get(risk_f, 0)
            hedge_score = result.get(hedge_f, 0)

            # 利好分值求和: 多条利好对冲同一条利空时
            hedge_sum = hedge_score

            # 强因果利空上限50%
            from dynamic_weight_mapping import CorrelationWeightConfig
            wc = CorrelationWeightConfig()
            all_weights = wc.get_all_weights_for_factor(risk_f)
            is_strong = any(w >= 1.0 for w in all_weights.values())

            max_offset = current_risk * 0.5 if is_strong else current_risk
            actual_offset = min(hedge_sum, max_offset)

            result[risk_f] = max(0, int(current_risk - actual_offset))
            # 利好消息分值本身保留(仅修正利空)

        return result

    @classmethod
    def process(cls, raw_scores: dict[str, int]) -> dict[str, int]:
        """
        完整处理管道: 冗余合并 → 冲突对冲.

        处理后的信号字典送入时效衰减模块。
        """
        # Step 1: 同源冗余合并降噪
        deduped = cls.deduplicate(raw_scores)

        # Step 2: 反向冲突对冲
        final = cls.apply_conflict_offset(deduped)

        return final

    @classmethod
    def report(cls, raw_scores: dict[str, int]) -> dict:
        """生成处理报告"""
        deduped = cls.deduplicate(raw_scores)
        conflicts = cls.detect_conflicts(raw_scores)
        final = cls.apply_conflict_offset(deduped)

        # 统计去冗余
        removed = []
        for fname in raw_scores:
            if fname not in deduped:
                gid = cls.get_group(fname)
                gname = cls.REDUNDANCY_GROUPS.get(gid, {}).get("name", "未知") if gid else "未知"
                removed.append({"factor": fname, "group": gname, "score": raw_scores[fname]})

        # 冲突抵扣
        offsets = []
        for c in conflicts:
            offsets.append({
                "risk": c["risk_factor"], "risk_score": c["risk_score"],
                "hedge": c["hedge_factor"], "hedge_score": c["hedge_score"],
                "net_risk": c["net_risk"],
            })

        return {
            "input_count": len(raw_scores),
            "after_dedup_count": len(deduped),
            "removed_signals": removed,
            "conflicts_detected": len(conflicts),
            "conflict_offsets": offsets,
            "output_scores": final,
        }


# ====================== Module 2.5: 信号时效衰减控制器 ======================

class SignalDecayController:
    """
    信号时效衰减窗口管控。

    三类衰减规则:
      1️⃣ 量价/资金信号: 15交易日, 每日衰减10%, 到期归零
      2️⃣ 财务/商誉/负债信号: 90交易日, 每月衰减20%, 到期归零
      3️⃣ 黑天鹅/舆情极端风险信号: 永久留存, 不衰减
    """

    # 因子→衰减类别映射
    FACTOR_CATEGORY_MAP = {
        # 1️⃣ 量价/资金信号 (15日, 每日-10%)
        "price_surge_60d":       "price_volume",
        "price_surge_20d":       "price_volume",
        "price_surge_3d":        "price_volume",
        "volume_ratio":          "price_volume",
        "consecutive_up_days":   "price_volume",
        "retail_inflow_ratio":   "price_volume",
        "institutional_outflow": "price_volume",
        "turnover_spike":        "price_volume",
        "short_term_volatility": "price_volume",
        "board_divergence":      "price_volume",

        # 2️⃣ 财务/商誉/负债信号 (90日, 每月-20%)
        "debt_ratio":            "financial",
        "pe_deviation":          "financial",
        "goodwill_ratio":        "financial",
        "profit_decline":        "financial",

        # 3️⃣ 黑天鹅/舆情极端风险 (永久留存)
        "concept_purity":        "black_swan",
    }

    # 衰减规则定义
    DECAY_RULES = {
        "price_volume": {
            "name": "量价/资金信号",
            "valid_window": 15,           # 15交易日
            "decay_unit": "trading_day",  # 按交易日衰减
            "decay_per_unit": 0.10,       # 每交易日衰减10%
            "description": "信号生成当日为满分基准, 每经过1个交易日, 信号原始强度分自动衰减10%, 超出15个交易日后归零",
        },
        "financial": {
            "name": "财务/商誉/负债信号",
            "valid_window": 90,           # 90交易日
            "decay_unit": "calendar_month", # 按自然月衰减
            "decay_per_unit": 0.20,       # 每月衰减20%
            "description": "以财报披露日为基准, 每完整度过1个自然月, 信号原始强度分衰减20%, 超出90个交易日后归零",
        },
        "black_swan": {
            "name": "黑天鹅/舆情极端风险信号",
            "valid_window": None,         # 永久留存
            "decay_unit": "none",
            "decay_per_unit": 0.0,
            "description": "永久留存无时效限制, 仅当上市公司发布官方风险解除公告后手动标记失效",
        },
    }

    # 黑天鹅信号手动标记表
    BLACK_SWAN_TABLE = "black_swan_signals"

    @classmethod
    def get_category(cls, factor_name: str) -> str:
        """获取因子对应的衰减类别"""
        return cls.FACTOR_CATEGORY_MAP.get(factor_name, "price_volume")

    @classmethod
    def get_rule(cls, category: str) -> dict:
        """获取类别对应的衰减规则"""
        return cls.DECAY_RULES.get(category, cls.DECAY_RULES["price_volume"])

    @classmethod
    def calc_decay_factor(cls, factor_name: str,
                           signal_generated_date: str,
                           current_date: str = None) -> float:
        """
        计算信号衰减系数 (0.0 ~ 1.0)。

        参数:
            factor_name: 因子名
            signal_generated_date: 信号生成日期 YYYYMMDD
            current_date: 当前日期 YYYYMMDD (默认当日)

        返回:
            decay_factor: 衰减系数
               1.0 = 无衰减(信号刚生成)
               0.0 = 完全衰减(信号已失效)
               0.5 = 衰减50%
        """
        from datetime import datetime, timedelta
        current_date = current_date or datetime.now().strftime("%Y%m%d")

        category = cls.get_category(factor_name)
        rule = cls.get_rule(category)

        # 黑天鹅永久留存
        if category == "black_swan":
            # 检查是否已被手动标记失效
            if cls._is_black_swan_cleared(factor_name):
                return 0.0
            return 1.0

        # 计算经过的时间单位
        gen_dt = datetime.strptime(signal_generated_date, "%Y%m%d")
        cur_dt = datetime.strptime(current_date, "%Y%m%d")
        delta_days = (cur_dt - gen_dt).days

        # 超期归零
        if delta_days >= rule["valid_window"]:
            return 0.0

        # 按规则衰减 (指数衰减: S = raw × decay_per_unit^elapsed_units)
        if rule["decay_unit"] == "trading_day":
            # 量价/资金: S = raw × 0.9^days
            decay = (1.0 - rule["decay_per_unit"]) ** max(0, delta_days)
        elif rule["decay_unit"] == "calendar_month":
            # 财务: S = raw × 0.8^months
            elapsed_months = max(0, delta_days // 30)
            decay = (1.0 - rule["decay_per_unit"]) ** elapsed_months
        else:
            decay = 1.0
        return max(0.0, decay)

    @classmethod
    def apply_decay_to_score(cls, raw_score: int, decay_factor: float) -> float:
        """对原始强度分应用衰减，返回有效强度分"""
        return round(raw_score * decay_factor, 2)

    @classmethod
    def apply_decay_batch(cls, raw_scores: dict[str, int],
                           signal_dates: dict[str, str],
                           current_date: str = None) -> dict[str, float]:
        """
        批量衰减计算。

        参数:
            raw_scores: {"factor_name": 原始强度分(0~10), ...}
            signal_dates: {"factor_name": "信号生成日期YYYYMMDD", ...}
            current_date: 当前日期

        返回:
            {"factor_name": 有效强度分(经衰减后), ...}
        """
        effective = {}
        for fname, raw in raw_scores.items():
            gen_date = signal_dates.get(fname)
            if not gen_date:
                effective[fname] = float(raw)
                continue
            decay = cls.calc_decay_factor(fname, gen_date, current_date)
            effective[fname] = cls.apply_decay_to_score(raw, decay)
        return effective

    @classmethod
    def _is_black_swan_cleared(cls, factor_name: str) -> bool:
        """检查黑天鹅信号是否已被手动标记解除"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {cls.BLACK_SWAN_TABLE} (
                    factor_name TEXT PRIMARY KEY,
                    cleared_date TEXT,
                    cleared_reason TEXT
                )
            """)
            conn.commit()
            cur.execute(f"SELECT cleared_date FROM {cls.BLACK_SWAN_TABLE} "
                        f"WHERE factor_name=? AND cleared_date IS NOT NULL",
                        (factor_name,))
            result = cur.fetchone()
            conn.close()
            return result is not None
        except Exception:
            return False

    @classmethod
    def clear_black_swan(cls, factor_name: str, reason: str = "") -> bool:
        """手动标记黑天鹅信号失效"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute(f"""
                INSERT OR REPLACE INTO {cls.BLACK_SWAN_TABLE}
                (factor_name, cleared_date, cleared_reason)
                VALUES (?, datetime('now','localtime'), ?)
            """, (factor_name, reason))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logging.warning(f"  ⚠️ 黑天鹅标记失败: {e}")
            return False

    @classmethod
    def decay_report(cls, raw_scores: dict[str, int],
                     signal_dates: dict[str, str],
                     current_date: str = None) -> list[dict]:
        """生成衰减报告(用于日志/调试)"""
        effective = cls.apply_decay_batch(raw_scores, signal_dates, current_date)
        report = []
        for fname in sorted(raw_scores.keys()):
            raw = raw_scores[fname]
            eff = effective.get(fname, 0)
            cat = cls.get_category(fname)
            rule = cls.get_rule(cat)
            report.append({
                "factor": fname,
                "category": rule["name"],
                "raw_score": raw,
                "effective_score": eff,
                "decay_pct": round((1 - eff / max(raw, 1)) * 100),
                "generated": signal_dates.get(fname, "unknown"),
            })
        return report


# ====================== Module 3: 误判总分计算 ======================

class MisjudgmentScoreCalculator:
    """
    误判总分计算引擎。

    单条标的误判总分 = SUM(单风险信号强度分 × 对应映射关联权重)
    总分 ≥ 60 → 高分激活误判，强制进入模型调参修正链路
    总分 < 60 → 低风险误判，仅留存样本
    """

    ACTIVATION_THRESHOLD = 60

    def __init__(self):
        self.scorer = SignalStrengthScorer()
        self.weight_config = CorrelationWeightConfig()

    def calculate(self, factor_values: dict[str, float],
                  signal_dates: dict[str, str] = None,
                  current_date: str = None,
                  target_label: str = None) -> dict:
        """
        计算标的误判总分 (含信号时效衰减)。

        执行序列:
          1. 读取原始信号强度评分 0~10
          2. 匹配信号类别衰减规则
          3. 折算当前有效强度分值
          4. 带入多对多动态加权公式计算误判总分
          5. 判定是否触发高分激活误判

        参数:
            factor_values: {"factor_name": value, ...}
            signal_dates:  {"factor_name": "信号生成日期YYYYMMDD", ...} (可选)
            current_date:  当前日期 YYYYMMDD (默认当日)
            target_label:  目标标签名(可选)

        返回:
            {
                "total_score": float,          # 总分(经衰减)
                "raw_total_score": float,      # 原始总分(无衰减)
                "is_activated": bool,           # ≥60激活
                "decay_applied": bool,          # 是否应用了衰减
                "decay_report": [...],          # 衰减明细
                ...
            }
        """
        # Step 1: 原始信号强度打分
        raw_strengths = self.scorer.score_all(factor_values)
        raw_scores_int = {k: int(v) for k, v in raw_strengths.items()}

        # Step 2~3: 衰减折算 (如有信号日期)
        decay_applied = bool(signal_dates)
        if signal_dates:
            effective_strengths = SignalDecayController.apply_decay_batch(
                raw_scores_int, signal_dates, current_date)
        else:
            effective_strengths = {k: float(v) for k, v in raw_scores_int.items()}

        logging.info(f"  📊 信号强度(原始): {raw_strengths}")
        if decay_applied:
            logging.info(f"  📊 信号强度(有效): {effective_strengths}")

        # 衰减报告
        decay_rep = []
        if signal_dates:
            decay_rep = SignalDecayController.decay_report(
                raw_scores_int, signal_dates, current_date)

        # Step 4: 逐标签加权计算 (使用有效强度)
        details = {}
        labels_to_check = ([target_label] if target_label
                           else list(SignalStrengthScorer.LABEL_FACTOR_MAP.keys()))

        for label in labels_to_check:
            label_score = 0.0
            factor_contrib = {}
            mapped_factors = SignalStrengthScorer.LABEL_FACTOR_MAP.get(label, [])

            for fname in mapped_factors:
                strength = effective_strengths.get(fname, 0)
                if strength == 0:
                    continue
                weight = self.weight_config.get_weight(fname, label)
                contrib = round(strength * weight, 2)
                factor_contrib[fname] = {
                    "effective_strength": strength,
                    "raw_strength": raw_scores_int.get(fname, 0),
                    "weight": round(weight, 2),
                    "contrib": contrib,
                }
                label_score += contrib

            details[label] = {
                "score": round(label_score, 2),
                "activated": label_score >= self.ACTIVATION_THRESHOLD,
                "factor_contrib": factor_contrib,
            }

        total_score = max(d["score"] for d in details.values()) if details else 0
        is_activated = total_score >= self.ACTIVATION_THRESHOLD

        # 原始总分(无衰减, 用于对比)
        raw_total = 0
        if not signal_dates:
            raw_total = total_score
        else:
            # 用原始强度重算一次
            for label in labels_to_check:
                label_raw = 0.0
                mapped_factors = SignalStrengthScorer.LABEL_FACTOR_MAP.get(label, [])
                for fname in mapped_factors:
                    raw_s = raw_scores_int.get(fname, 0)
                    if raw_s == 0:
                        continue
                    w = self.weight_config.get_weight(fname, label)
                    label_raw += raw_s * w
                raw_total = max(raw_total, label_raw)

        result = {
            "total_score": round(total_score, 2),
            "raw_total_score": round(raw_total, 2),
            "is_activated": is_activated,
            "decay_applied": decay_applied,
            "decay_report": decay_rep,
            "details": details,
        }

        level = "🔴高分激活" if is_activated else "🟢低风险"
        decay_note = f" (衰减后)" if decay_applied else ""
        logging.info(f"  📊 误判总分: {total_score:.1f}{decay_note} | {level} | "
                     f"激活标签: {[k for k,v in details.items() if v['activated']]}")
        return result

    def get_label_factor_contributions(self, factor_values: dict[str, float],
                                        label_name: str) -> list[dict]:
        """获取某标签下各因子贡献排序(用于调参)"""
        strengths = self.scorer.score_all(factor_values)
        contribs = []
        for fname, strength in strengths.items():
            weight = self.weight_config.get_weight(fname, label_name)
            if weight > 0:
                contribs.append({
                    "factor": fname,
                    "strength": strength,
                    "weight": weight,
                    "contrib": round(strength * weight, 2),
                })
        contribs.sort(key=lambda x: x["contrib"], reverse=True)
        return contribs


# ====================== Module 4: 动态映射引擎 ======================

class DynamicMappingEngine:
    """
    动态映射引擎 — 废弃固定一对一连线，替代为动态多对多映射。

    核心能力:
      1. 多风险信号可同时关联多个误判标签
      2. 支持一对多、多对多自由映射
      3. 映射关系随每日标注样本自动增减
      4. 全部持久化到向量知识库
    """

    def __init__(self):
        self.calculator = MisjudgmentScoreCalculator()
        self.weight_config = CorrelationWeightConfig()

    def analyze_stock(self, factor_values: dict[str, float],
                      stock_code: str = "", trade_date: str = "") -> dict:
        """
        单只标的完整动态映射分析。

        返回:
            {
                "stock_code": str,
                "trade_date": str,
                "score_result": {...},       # Module3计算详情
                "activated_labels": [...],   # 高分激活的标签列表
                "top_risk_factors": [...],   # 最大贡献因子(TOP3)
                "recommended_weights": {...},# 建议调整的权重
                "mapping_count": int,        # 参与映射的因子-标签对数
            }
        """
        trade_date = trade_date or datetime.now().strftime("%Y%m%d")

        # Module3计算
        score_result = self.calculator.calculate(factor_values)

        # 提取激活标签
        activated_labels = [
            k for k, v in score_result["details"].items()
            if v["activated"]
        ]

        # TOP3贡献因子(取最高分的激活标签)
        top_factors = []
        if activated_labels:
            first_label = activated_labels[0]
            contribs = self.calculator.get_label_factor_contributions(
                factor_values, first_label)
            top_factors = [c["factor"] for c in contribs[:3]]

        # 权重自动调整建议
        recommended_weights = {}
        for label in activated_labels:
            contribs = self.calculator.get_label_factor_contributions(
                factor_values, label)
            for c in contribs:
                # 激活的因子建议上调权重
                if c["strength"] >= 6:
                    new_w = self.weight_config.update_weight(
                        c["factor"], label, True)
                    if c["factor"] not in recommended_weights:
                        recommended_weights[c["factor"]] = {}
                    recommended_weights[c["factor"]][label] = {
                        "old_weight": c["weight"],
                        "new_weight": round(new_w, 2),
                    }

        # 映射计数
        mapping_count = sum(
            len(v) for v in self.weight_config.weight_matrix.values()
        )

        result = {
            "stock_code": stock_code,
            "trade_date": trade_date,
            "score_result": score_result,
            "activated_labels": activated_labels,
            "top_risk_factors": top_factors,
            "recommended_weights": recommended_weights,
            "mapping_count": mapping_count,
        }

        return result

    def batch_analyze(self, stocks_data: list[dict]) -> list[dict]:
        """批量分析多只标的"""
        results = []
        for sd in stocks_data:
            result = self.analyze_stock(
                factor_values=sd.get("factors", {}),
                stock_code=sd.get("code", ""),
                trade_date=sd.get("date", ""),
            )
            sd["dynamic_mapping"] = result
            results.append(result)
        return results

    def export_mapping_graph(self) -> dict:
        """导出现有映射图(用于可视化/调试)"""
        return self.weight_config.export_mapping_report()


# ====================== Module 3.5: 正向对冲安全信号库 ======================

class PositiveHedgeLibrary:
    """
    正向对冲安全信号库 — 12条标准化正向利好信号。

    核心用途: 抵消模型乐观类误判扣分，平衡风控阈值。
    典型场景: 周期股短期暴涨触发误判扣分时，匹配金价长期上行信号反向对冲。

    执行顺序(调度逻辑):
      1. 先计算全部利空/异常误判分值
      2. 再遍历正向对冲安全信号库匹配标的
      3. 执行分值抵扣
      4. 输出对冲前后最终风控分值
    """

    # 12条标准化正向对冲信号
    HEDGE_SIGNALS = {
        # ===== ① 资金面 =====
        "H01_机构持续净流入": {
            "id": "H01",
            "category": "资金面",
            "condition": "连续5日+特大单累计净流入>3亿",
            "max_deduction": 8,   # 最高可抵扣分数
            "weight": 0.8,
        },
        "H02_北向资金持续增持": {
            "id": "H02",
            "category": "资金面",
            "condition": "北向资金连续3季度增持+持仓占比提升",
            "max_deduction": 6,
            "weight": 0.7,
        },
        # ===== ② 产业落地 =====
        "H03_产能投产落地": {
            "id": "H03",
            "category": "产业落地",
            "condition": "新建产线正式投产+产能爬坡至60%+",
            "max_deduction": 7,
            "weight": 0.8,
        },
        "H04_订单大额兑现": {
            "id": "H04",
            "category": "产业落地",
            "condition": "在手订单同比增长>30%+预收账款大幅增加",
            "max_deduction": 7,
            "weight": 0.7,
        },
        "H05_新项目量产爬坡": {
            "id": "H05",
            "category": "产业落地",
            "condition": "新产品/新项目量产+出货量连续3月环比增长",
            "max_deduction": 6,
            "weight": 0.6,
        },
        # ===== ③ 大宗商品周期 =====
        "H06_金价长期上行": {
            "id": "H06",
            "category": "大宗商品周期",
            "condition": "国际金价处于>120日上行通道+央行持续增持",
            "max_deduction": 10,   # 周期股核心对冲, 最高抵扣
            "weight": 1.0,         # 强因果权重
        },
        "H07_工业金属供需缺口": {
            "id": "H07",
            "category": "大宗商品周期",
            "condition": "铜/铝等工业金属全球库存持续下降+需求缺口>5%",
            "max_deduction": 8,
            "weight": 0.8,
        },
        "H08_资源品价格上行周期": {
            "id": "H08",
            "category": "大宗商品周期",
            "condition": "标的对应资源品处于>60日上行趋势+期货贴水",
            "max_deduction": 8,
            "weight": 0.8,
        },
        # ===== ④ 财务基本面 =====
        "H09_ROE持续修复": {
            "id": "H09",
            "category": "财务基本面",
            "condition": "ROE连续3季度环比提升+绝对值>行业均值",
            "max_deduction": 6,
            "weight": 0.6,
        },
        "H10_毛利率持续抬升": {
            "id": "H10",
            "category": "财务基本面",
            "condition": "毛利率连续4季度提升+同比>3个百分点",
            "max_deduction": 5,
            "weight": 0.5,
        },
        "H11_经营性现金流改善": {
            "id": "H11",
            "category": "财务基本面",
            "condition": "经营性现金流连续3期为正+覆盖流动负债>1.2x",
            "max_deduction": 5,
            "weight": 0.5,
        },
        # ===== ⑤ 政策/行业红利 =====
        "H12_行业扶持政策落地": {
            "id": "H12",
            "category": "政策/行业红利",
            "condition": "行业国家级扶持政策正式发文+财政补贴到账",
            "max_deduction": 7,
            "weight": 0.7,
        },
    }

    # 乐观类误判标签列表 (可抵扣的目标标签)
    OPTIMISTIC_LABELS = [
        "预判高估，负误差",
        "入场条件失效",
        "区间判断失效",
        "止损阈值误判",
        "止盈阈值偏保守",
    ]

    def __init__(self):
        self.matched_signals: list[dict] = []

    def match_for_stock(self, stock_code: str,
                         sector: str = "",
                         factors: dict = None) -> list[dict]:
        """
        为标的正向匹配安全信号。

        参数:
            stock_code: 标的代码
            sector: 赛道分类 (贵金属/锂电/医疗/半导体等)
            factors: 标的因子数据 (用于条件判定)

        返回:
            [{"id":"H06","name":"金价长期上行","deduction":8,...}]
        """
        matched = []

        # 贵金属周期股 → 匹配 H06(金价长期上行) + H08(资源品周期)
        if "黄金" in sector or "贵金属" in sector:
            matched.append({
                "id": "H06",
                "name": "金价长期上行",
                "category": "大宗商品周期",
                "max_deduction": self.HEDGE_SIGNALS["H06_金价长期上行"]["max_deduction"],
                "weight": self.HEDGE_SIGNALS["H06_金价长期上行"]["weight"],
                "condition_matched": "国际金价处于>120日上行通道",
                "confidence": "高",
            })
            matched.append({
                "id": "H08",
                "name": "资源品价格上行周期",
                "category": "大宗商品周期",
                "max_deduction": 5,  # 部分重叠, 降低额度
                "weight": 0.5,
                "condition_matched": "黄金处于长期上行趋势",
                "confidence": "中",
            })

        # 机构资金持续净流入
        if factors:
            inst_outflow = factors.get("institutional_outflow", 0)
            if inst_outflow < -3:  # 负值表示净流入
                matched.append({
                    "id": "H01",
                    "name": "机构持续净流入",
                    "category": "资金面",
                    "max_deduction": self.HEDGE_SIGNALS["H01_机构持续净流入"]["max_deduction"],
                    "weight": self.HEDGE_SIGNALS["H01_机构持续净流入"]["weight"],
                    "condition_matched": f"机构净流入{abs(inst_outflow):.1f}亿",
                    "confidence": "高",
                })

            # ROE持续修复
            roe = factors.get("roe", 0)
            if roe > 8:
                matched.append({
                    "id": "H09",
                    "name": "ROE持续修复",
                    "category": "财务基本面",
                    "max_deduction": self.HEDGE_SIGNALS["H09_ROE持续修复"]["max_deduction"],
                    "weight": self.HEDGE_SIGNALS["H09_ROE持续修复"]["weight"],
                    "condition_matched": f"ROE={roe:.1f}%",
                    "confidence": "中",
                })

            # 毛利率信号
            gross_margin = factors.get("gross_margin", 0)
            if gross_margin > 20:
                matched.append({
                    "id": "H10",
                    "name": "毛利率持续抬升",
                    "category": "财务基本面",
                    "max_deduction": 3,
                    "weight": 0.4,
                    "condition_matched": f"毛利率{gross_margin:.1f}%",
                    "confidence": "中",
                })

            # 经营性现金流
            ocf = factors.get("operating_cash_flow", 0)
            if ocf > 0:
                matched.append({
                    "id": "H11",
                    "name": "经营性现金流改善",
                    "category": "财务基本面",
                    "max_deduction": 3,
                    "weight": 0.3,
                    "condition_matched": "经营性现金流为正",
                    "confidence": "低",
                })

        self.matched_signals = matched
        return matched

    def calculate_offset(self, risk_total_score: float,
                          stock_code: str = "",
                          sector: str = "",
                          factors: dict = None) -> dict:
        """
        计算正向对冲抵扣。

        参数:
            risk_total_score: 当前误判总分(未对冲前)
            stock_code: 标的代码
            sector: 赛道
            factors: 因子数据

        返回:
            {
                "original_score": float,       # 原始误判总分
                "offset_score": float,          # 对冲抵扣分
                "final_score": float,           # 对冲后最终分
                "activation_overridden": bool,  # 是否因对冲解除激活
                "matched_signals": [...],       # 匹配信号明细
                "offset_detail": {              # 每条抵扣明细
                    "signal_name": deduction
                }
            }
        """
        matched = self.match_for_stock(stock_code, sector, factors)
        total_offset = 0.0
        offset_detail = {}

        for sig in matched:
            deduction = sig["max_deduction"] * sig["weight"]
            total_offset += deduction
            offset_detail[sig["name"]] = {
                "deduction_raw": sig["max_deduction"],
                "weight": sig["weight"],
                "deduction_actual": round(deduction, 1),
                "confidence": sig.get("confidence", "低"),
            }

        # 总抵扣不超过原始分
        total_offset = min(total_offset, risk_total_score)

        final_score = max(0, risk_total_score - total_offset)
        was_activated = risk_total_score >= 60
        is_activated = final_score >= 60
        activation_overridden = was_activated and not is_activated

        result = {
            "original_score": round(risk_total_score, 2),
            "offset_score": round(total_offset, 2),
            "final_score": round(final_score, 2),
            "activation_overridden": activation_overridden,
            "matched_signals": matched,
            "offset_detail": offset_detail,
        }

        # 日志输出
        if matched:
            detail_str = " + ".join(
                [f"{s['name']}(-{s['max_deduction']*s['weight']:.1f})" for s in matched])
            logging.info(f"  🟢 正向对冲: 匹配{len(matched)}条 | {detail_str}")
            logging.info(f"  📊 对冲前{result['original_score']:.1f} → 对冲后{result['final_score']:.1f} "
                         f"(抵扣{result['offset_score']:.1f})")
            if activation_overridden:
                logging.info(f"  ⚠️ 对冲解除激活! 原≥60高分已降至<60")

        return result


# ====================== Module 3.6: Lollapalooza三色共振分级 ======================

class LollapaloozaTierController:
    """
    分级Lollapalooza共振三色阈值体系。

    统计单只标的同时激活的高分误判条目数量，按数量划分三级共振梯度。
    执行顺序: 负面误判→正向对冲→统计有效项→匹配等级→风控限制
    """

    # 三色阈值定义
    TIERS = {
        "GREEN": {
            "label": "🟢 GREEN 无共振缓冲区",
            "threshold": (0, 2),
            "action": "无任何开仓、持仓限制，正常参与调参、信号开仓",
            "position_limit": 1.0,     # 仓位上限100%
            "new_open_allowed": True,
            "force_liquidate": False,
            "remove_from_pool": False,
        },
        "YELLOW": {
            "label": "🟡 YELLOW 中度共振",
            "threshold": (3, 5),
            "action": "禁止新建仓位；存量持仓上限3%，超出部分减持",
            "position_limit": 0.03,     # 持仓上限3%
            "new_open_allowed": False,
            "force_liquidate": False,
            "remove_from_pool": False,
        },
        "RED": {
            "label": "🔴 RED 重度共振",
            "threshold": (6, None),
            "action": "强制清仓持仓降至0；永久禁止开仓直至回落≤2项",
            "position_limit": 0.0,      # 持仓归零
            "new_open_allowed": False,
            "force_liquidate": True,
            "remove_from_pool": True,   # 剔除交易池
        },
    }

    def __init__(self):
        self.hedge_lib = PositiveHedgeLibrary()

    def assess(self, misjudgment_scores: dict[str, float],
               score_detail: dict = None,
               stock_code: str = "",
               stock_name: str = "",
               sector: str = "",
               trade_date: str = "",
               factors: dict = None,
               persist: bool = True) -> dict:
        """
        执行完整三色共振评估。

        执行顺序:
          1. 先统计原始高分误判条目
          2. 匹配正向对冲安全信号库抵扣
          3. 统计对冲后有效高分条目
          4. 匹配共振等级 → 执行风控约束

        参数:
            misjudgment_scores: {label_name: score} 各标签误判得分
            score_detail: calculate() 返回的完整详情(含activated状态)
            stock_code: 标的代码
            sector: 赛道
            factors: 因子数据

        返回:
            {
                "stock_code": str,
                "raw_high_count": int,       # 原始高分项数
                "hedge_offset_score": float,  # 正向对冲抵扣分
                "effective_high_count": int, # 对冲后有效高分项数
                "tier": "GREEN"/"YELLOW"/"RED",
                "label": "🟢 GREEN ...",
                "action": "风控动作描述",
                "position_limit": float,
                "new_open_allowed": bool,
                "force_liquidate": bool,
                "remove_from_pool": bool,
                "hedge_detail": {...},       # 对冲明细
                "high_items": [...],         # 有效高分标签列表
            }
        """
        # Step 1: 原始高分项统计
        raw_high_items = [
            label for label, score in misjudgment_scores.items()
            if score >= 60
        ]
        raw_high_count = len(raw_high_items)

        # Step 2: 正向对冲
        total_raw = sum(misjudgment_scores.values())
        hedge_result = self.hedge_lib.calculate_offset(
            risk_total_score=total_raw,
            stock_code=stock_code,
            sector=sector,
            factors=factors,
        )

        # Step 3: 对冲后有效高分项统计
        # 使用score_detail中的activated状态为主(最精确)
        if score_detail and score_detail.get("details"):
            effective_high_items = [
                k for k, v in score_detail["details"].items()
                if v.get("activated", False)
            ]
        elif hedge_result["final_score"] < 60:
            # 对冲后总分不到60, 无有效高分
            effective_high_items = []
        else:
            # 兜底: 按剩余总分比例估算仍可能是高分的项
            ratio = max(0, hedge_result["final_score"] / max(total_raw, 1))
            effective_high_items = [
                label for label in raw_high_items
                if misjudgment_scores[label] * ratio >= 60
            ]

        # 最终计数
        effective_high_count = len(effective_high_items)

        # Step 4: 匹配三色等级
        tier_key, tier_info = self._match_tier(effective_high_count)

        result = {
            "stock_code": stock_code,
            "raw_high_count": raw_high_count,
            "hedge_offset_score": hedge_result["offset_score"],
            "effective_high_count": effective_high_count,
            "tier": tier_key,
            "label": tier_info["label"],
            "action": tier_info["action"],
            "position_limit": tier_info["position_limit"],
            "new_open_allowed": tier_info["new_open_allowed"],
            "force_liquidate": tier_info["force_liquidate"],
            "remove_from_pool": tier_info["remove_from_pool"],
            "hedge_detail": hedge_result["offset_detail"],
            "high_items": effective_high_items,
        }

        # 日志输出 (完整推送不截断)
        self._log_result(result, raw_high_items, hedge_result)

        # 持久化到 stock_daily_score
        if persist:
            self._persist_assessment(result, stock_name, trade_date)

        return result

    def _match_tier(self, high_count: int) -> tuple[str, dict]:
        """根据高分项数量匹配共振等级"""
        for tier_key, tier_info in self.TIERS.items():
            lo, hi = tier_info["threshold"]
            if hi is None:
                if high_count >= lo:
                    return tier_key, tier_info
            else:
                if lo <= high_count <= hi:
                    return tier_key, tier_info
        return "GREEN", self.TIERS["GREEN"]

    def _log_result(self, result: dict,
                    raw_items: list[str],
                    hedge_result: dict) -> None:
        """完整日志输出"""
        tier = result["tier"]
        marker = "🔴" if tier in ("YELLOW", "RED") else "🟢"

        log_lines = [
            f"\n  {'='*55}",
            f"  {marker} Lollapalooza共振评估 [{result['stock_code']}]",
            f"  {'='*55}",
            f"  原始高分项: {result['raw_high_count']}项",
        ]

        if raw_items:
            log_lines.append(f"  原始高分标签: {', '.join(raw_items)}")

        log_lines.extend([
            f"  正向对冲抵扣: {result['hedge_offset_score']:.1f}分",
            f"  对冲后有效高分项: {result['effective_high_count']}项",
        ])

        if result["effective_high_count"] > 0:
            log_lines.append(f"  有效高分标签: {', '.join(result['high_items'])}")

        log_lines.extend([
            f"  共振等级: {result['label']}",
            f"  风控动作: {result['action']}",
            f"  仓位上限: {result['position_limit']:.0%}",
            f"  新开仓限制: {'禁止' if not result['new_open_allowed'] else '允许'}",
            f"  强制清仓: {'是' if result['force_liquidate'] else '否'}",
            f"  剔除交易池: {'是' if result['remove_from_pool'] else '否'}",
            f"  {'='*55}",
        ])

        for line in log_lines:
            logging.info(line)

    def _persist_assessment(self, result: dict, stock_name: str = "",
                            trade_date: str = "") -> None:
        """持久化共振评估结果到 stock_daily_score"""
        from datetime import datetime
        trade_date = trade_date or datetime.now().strftime("%Y%m%d")
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            # 确保表存在
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_daily_score (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    ts_code TEXT NOT NULL,
                    stock_name TEXT,
                    sector TEXT,
                    raw_total_score REAL DEFAULT 0,
                    raw_high_count INTEGER DEFAULT 0,
                    offset_score REAL DEFAULT 0,
                    active_error_count INTEGER DEFAULT 0,
                    resonance_level TEXT DEFAULT 'GREEN',
                    risk_limit_action TEXT,
                    position_limit_pct REAL DEFAULT 100.0,
                    new_open_allowed INTEGER DEFAULT 1,
                    force_liquidate INTEGER DEFAULT 0,
                    remove_from_pool INTEGER DEFAULT 0,
                    hedge_detail TEXT,
                    high_items TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    UNIQUE(trade_date, ts_code)
                )
            """)
            import json
            cur.execute("""
                INSERT OR REPLACE INTO stock_daily_score
                (trade_date, ts_code, stock_name, sector,
                 raw_high_count, offset_score, active_error_count,
                 resonance_level, risk_limit_action,
                 position_limit_pct, new_open_allowed,
                 force_liquidate, remove_from_pool,
                 hedge_detail, high_items)
                VALUES (?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?)
            """, (
                trade_date,
                result.get("stock_code", ""),
                stock_name,
                "",
                result["raw_high_count"],
                result["hedge_offset_score"],
                result["effective_high_count"],
                result["tier"],
                result["action"],
                result["position_limit"] * 100,
                1 if result["new_open_allowed"] else 0,
                1 if result["force_liquidate"] else 0,
                1 if result["remove_from_pool"] else 0,
                json.dumps(result.get("hedge_detail", {}), ensure_ascii=False),
                json.dumps(result.get("high_items", []), ensure_ascii=False),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"  ⚠️ 共振评估持久化失败: {e}")

    def batch_assess(self, stocks: list[dict]) -> list[dict]:
        """批量评估多只标的"""
        results = []
        for s in stocks:
            r = self.assess(
                misjudgment_scores=s.get("scores", {}),
                score_detail=s.get("detail"),
                stock_code=s.get("code", ""),
                sector=s.get("sector", ""),
                factors=s.get("factors"),
            )
            results.append(r)
        return results

    def filter_pool(self, pool: list[dict], stocks_results: list[dict]) -> dict:
        """
        根据共振等级过滤交易池。

        返回:
            {
                "allowed": [...],    # 可交易标的
                "removed": [...],    # RED剔除标的
                "restricted": [...]  # YELLOW限仓标的
            }
        """
        code_map = {r["stock_code"]: r for r in stocks_results}
        allowed, removed, restricted = [], [], []

        for stock in pool:
            code = stock.get("code", "")
            cr = code_map.get(code)
            if not cr:
                allowed.append(stock)
                continue

            if cr["remove_from_pool"]:
                removed.append({**stock, "reason": cr["action"]})
            elif not cr["new_open_allowed"]:
                restricted.append({**stock, "position_limit": cr["position_limit"]})
            else:
                allowed.append(stock)

        return {
            "allowed": allowed,
            "removed": removed,
            "restricted": restricted,
            "total_removed": len(removed),
            "total_restricted": len(restricted),
            "total_allowed": len(allowed),
        }


# ====================== 快捷入口 ======================

def run_dynamic_mapping(factor_values: dict[str, float],
                        stock_code: str = "",
                        trade_date: str = "") -> dict:
    """动态加权映射快捷入口"""
    engine = DynamicMappingEngine()
    return engine.analyze_stock(factor_values, stock_code, trade_date)


# ====================== 测试 ======================

if __name__ == "__main__":
    print("\n=== 动态加权多对多映射引擎 测试 ===\n")

    # 模拟山东黄金 (2026-07-22)
    shandong_factors = {
        "price_surge_60d":  14.5,   # 20日涨幅14.5% (用20d近似)
        "price_surge_20d":  14.5,   # 20日涨幅
        "price_surge_3d":   12.2,   # 3日涨幅
        "debt_ratio":       60.4,   # 负债率
        "pe_deviation":     22.1,   # PE
        "volume_ratio":     1.65,   # 量比
        "consecutive_up_days": 3,   # 连涨3日
        "board_divergence": 37.6,   # 板块-23% vs 个股+14.5%
        "retail_inflow_ratio": -4.63, # 散户净卖出
        "institutional_outflow": -1.74, # 特大单净买入(负=流入)
        "turnover_spike":   0,       # 换手率
        "goodwill_ratio":   7.9,     # 商誉占比
        "profit_decline":   0,       # 利润增长
        "short_term_volatility": 5.9, # 短期波动
        "concept_purity":   100,     # 纯度
    }

    print("--- 测试1: 单因子强度打分 ---")
    scores = SignalStrengthScorer.score_all(shandong_factors)
    print(f"{'因子名':<25} {'值':<10} {'得分':<6}")
    print("-" * 45)
    for name, value in shandong_factors.items():
        sc = scores.get(name, 0)
        bar = "█" * sc + "░" * (10 - sc)
        print(f"{name:<25} {value:<10.1f} {sc:<3} {bar}")

    print("\n--- 测试2: 权重矩阵初始化 ---")
    wc = CorrelationWeightConfig()
    report = wc.export_mapping_report()
    total_pairs = sum(len(v) for v in report.values())
    print(f"  映射对数: {total_pairs}")
    for label, factors in list(report.items())[:3]:
        print(f"  📎 {label}: {factors}")

    print("\n--- 测试3: 误判总分计算 ---")
    calc = MisjudgmentScoreCalculator()
    result = calc.calculate(shandong_factors)
    print(f"  总分: {result['total_score']:.1f}")
    print(f"  激活: {result['is_activated']}")
    for label, detail in result["details"].items():
        if detail["score"] > 0:
            print(f"  {label}: {detail['score']:.1f}分 "
                  f"{'🔴激活' if detail['activated'] else '🟢'}"
                  f" | 因子: {list(detail['factor_contrib'].keys())}")

    print("\n--- 测试4: 综合标的分析 ---")
    engine = DynamicMappingEngine()
    analysis = engine.analyze_stock(
        shandong_factors, stock_code="600547.SH", trade_date="20260722"
    )
    print(f"  标的: {analysis['stock_code']}")
    print(f"  总分: {analysis['score_result']['total_score']}")
    print(f"  激活标签: {analysis['activated_labels']}")
    print(f"  TOP风险因子: {analysis['top_risk_factors']}")
    print(f"  映射对数: {analysis['mapping_count']}")
    if analysis['recommended_weights']:
        print(f"  权重调整: {len(analysis['recommended_weights'])}项")
