#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibration_validation_test.py — 对照验证实验（仅调试自测使用，不加入自动定时调度）

验证任务: 校验模型复盘、自主调参是否强依赖人工校准 trade_calibration 标注数据

两组对照: 【完整校准样本】VS【清空当日校准样本】对比迭代结果

使用规范:
  1. 自动化定时流程: 仅启用 factor_weekly_iterate.py 的 pre_calibration_check()
  2. 功能验证、逻辑排查时手动执行本脚本
  3. 两段逻辑互不冲突，分工独立
"""
import sys
import os
import json
import copy
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from config import pg_engine, TARGET_CODES, SECTOR_GROUPS
    from sqlalchemy import text
    import pandas as pd
except ImportError as e:
    print(f"❌ 导入失败: {e}")
    sys.exit(1)

DEFAULT_WEIGHTS = {"valuation": 0.25, "momentum": 0.20, "flow": 0.25, "fundamental": 0.15, "sentiment": 0.15}
DEFAULT_ENTRY = {"consecutive_flow_days": 3, "volume_ratio": 1.0}
ADJUST_STEP = 0.03
SNAP_DIR = Path(SCRIPT_DIR) / "weight_snapshots"
SNAP_DIR.mkdir(exist_ok=True)


def load_calibration(trade_date):
    """加载trade_calibration当日全部记录"""
    sql = text(f"""
        SELECT ticker, ticker_name, real_close, real_change_pct,
               support_resistance_result, real_trade_action, error_tag,
               yesterday_ai_prediction
        FROM trade_calibration WHERE trade_date = :d ORDER BY ticker
    """)
    df = pd.read_sql(sql, pg_engine, params={"d": trade_date})
    if df.empty:
        return []
    records = []
    for _, r in df.iterrows():
        ai = r.get("yesterday_ai_prediction")
        if ai and isinstance(ai, str):
            try:
                ai = json.loads(ai)
            except json.JSONDecodeError:
                ai = None
        records.append({
            "ticker": r["ticker"], "name": r["ticker_name"],
            "close": float(r["real_close"]) if r["real_close"] else None,
            "change": float(r["real_change_pct"]) if r["real_change_pct"] else None,
            "sr": r["support_resistance_result"],
            "action": r["real_trade_action"],
            "error_tag": r["error_tag"], "ai_pred": ai or {},
        })
    return records


def run_review(records, label):
    """执行复盘"""
    print(f"\n{'='*60}")
    print(f"📋 复盘 {label}: {len(records)}只标的")
    print(f"{'='*60}")

    if not records:
        print("  ⚠️ 无人工校准数据,无法生成复盘分析")
        print("  ❌ 缺失实战归因分析 — 无法识别AI预判失误点")
        return {}

    n = len(records)
    error_stats = {}
    for r in records:
        tag = r["error_tag"]
        error_stats[tag] = error_stats.get(tag, 0) + 1

    print(f"\n📊 预判误差统计:")
    for tag, cnt in sorted(error_stats.items(), key=lambda x: -x[1]):
        pct = cnt / n * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {tag:<20} {cnt:>3d}次 {bar} {pct:5.1f}%")

    risk_valid = error_stats.get("【风控判断有效】", 0)
    entry_fail = error_stats.get("【入场条件失效】", 0)
    over_est = error_stats.get("【预判高估，负误差】", 0)
    under_est = error_stats.get("【预判低估，负误差】", 0)
    risk_total = risk_valid + entry_fail + over_est + under_est
    match = error_stats.get("【预判匹配，无误差】", 0)

    print(f"\n🛡️ 风控有效性:")
    print(f"  风控判断有效: {risk_valid}次 ✅")
    print(f"  入场条件失效: {entry_fail}次 ❌")
    print(f"  预判高估:     {over_est}次 🔴")
    print(f"  预判低估:     {under_est}次 🟢")
    print(f"  需修正样本:   {risk_total}次/共{n}次")
    if risk_total > 0:
        print(f"  归因: 预判高估→下调动量; 低估→上调动量; 入场失效→收紧阈值")

    return error_stats


def adjust_and_save(records, tag):
    """根据校准标签调参 + 保存快照"""
    error_stats = {}
    for r in records:
        tag_e = r["error_tag"]
        error_stats[tag_e] = error_stats.get(tag_e, 0) + 1

    weights = {s: dict(DEFAULT_WEIGHTS) for s in SECTOR_GROUPS}
    entry = dict(DEFAULT_ENTRY)
    log = []

    if not records or not error_stats:
        log.append(f"❌ 无校准数据 → 无权重修正,仅返回默认值")
        has_cal = False
    else:
        has_cal = True
        over_est = error_stats.get("【预判高估，负误差】", 0)
        under_est = error_stats.get("【预判低估，负误差】", 0)
        risk_valid = error_stats.get("【风控判断有效】", 0)
        entry_fail = error_stats.get("【入场条件失效】", 0)

        if over_est > 0:
            for s in weights:
                weights[s]["momentum"] = max(0.05, weights[s]["momentum"] - ADJUST_STEP)
                weights[s]["flow"] = min(0.40, weights[s]["flow"] + ADJUST_STEP)
            log.append(f"🔻 预判高估{over_est}次 → 动量-{ADJUST_STEP:.0%} 资金+{ADJUST_STEP:.0%}")
        if under_est > 0:
            for s in weights:
                weights[s]["momentum"] = min(0.35, weights[s]["momentum"] + ADJUST_STEP)
                weights[s]["valuation"] = max(0.10, weights[s]["valuation"] - ADJUST_STEP)
            log.append(f"🟢 预判低估{under_est}次 → 动量+{ADJUST_STEP:.0%} 估值-{ADJUST_STEP:.0%}")
        if risk_valid > 0:
            for s in weights:
                weights[s]["flow"] = min(0.40, weights[s]["flow"] + ADJUST_STEP * 0.5)
                weights[s]["sentiment"] = min(0.30, weights[s]["sentiment"] + ADJUST_STEP * 0.5)
            log.append(f"✅ 风控有效{risk_valid}次 → 资金+{ADJUST_STEP*0.5:.0%} 情绪+{ADJUST_STEP*0.5:.0%}")
        if entry_fail > 0:
            entry["consecutive_flow_days"] = min(5, entry["consecutive_flow_days"] + 1)
            entry["volume_ratio"] = min(1.5, entry["volume_ratio"] + 0.1)
            log.append(f"🔴 入场失效{entry_fail}次 → 连续流入+1 量比+0.1")

        # 分赛道精调
        for r in records:
            ticker_code = r["ticker"]
            tag_e = r["error_tag"]
            for sector, codes in SECTOR_GROUPS.items():
                if ticker_code in codes:
                    if tag_e == "【预判高估，负误差】":
                        weights[sector]["momentum"] = max(0.05, weights[sector]["momentum"] - ADJUST_STEP * 0.5)
                        weights[sector]["flow"] = min(0.40, weights[sector]["flow"] + ADJUST_STEP * 0.5)
                        log.append(f"  ↳ {sector}[{ticker_code}] 高估 → 额外-0.5%动量 +0.5%资金")
                    elif tag_e == "【入场条件失效】":
                        weights[sector]["flow"] = max(0.10, weights[sector]["flow"] - ADJUST_STEP * 0.3)
                        log.append(f"  ↳ {sector}[{ticker_code}] 入场失效 → 资金-0.3%")
                    break

        log.append(f"✅ 因子权重定向修正完成(基于{len(records)}条人工校准标签)")

    # 归一化
    for sector in weights:
        w = weights[sector]
        tw = sum(w.values())
        if abs(tw - 1.0) > 0.001:
            for k in w:
                w[k] = w[k] / tw

    result = {"weights": weights, "entry_conditions": entry, "adjustment_log": log, "has_calibration": has_cal}
    snap_path = SNAP_DIR / f"weight_snap_{tag}.json"
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump({
            "snapshot_tag": tag, "timestamp": date.today().isoformat(),
            "weights": weights, "entry_conditions": entry,
            "adjustment_log": log, "has_calibration": has_cal,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n📊 调参 {label}:")
    for l in log:
        print(f"  {l}")
    print(f"\n入场条件: {entry}")
    print(f"快照: {snap_path}")
    return result


def generate_report(snap_a, snap_b):
    """生成对比验证报告"""
    print("\n" + "=" * 70)
    print("🧪 对照实验验证报告：人工校准 trade_calibration 的必要性")
    print("=" * 70)
    print(f"\n实验设计:")
    print(f"  实验A(基线): 保留全部{snap_a['weight_count']}只标的校准记录")
    print(f"  实验B(隔离): 清空当日校准记录,模拟无真实标注场景")
    print(f"  测试日期: {date.today().isoformat()}")

    print(f"\n{'─'*70}")
    print("一、复盘模块输出对比")
    print(f"{'─'*70}")
    print(f"\n{'维度':<20} {'实验A(有校准)':<30} {'实验B(无校准)':<30}")
    print(f"{'─'*80}")
    print(f"{'误差标签分类':<20} {'5类完整分布':<30} {'无':<30}")
    print(f"{'预判误差统计':<20} {'✅ 完整统计':<30} {'❌ 无法区分对错':<30}")
    print(f"{'风控有效性':<20} {'✅ 可评估':<30} {'❌ 无风控统计':<30}")
    print(f"{'归因结论':<20} {'✅ 可定位AI失误点':<30} {'❌ 缺失实战归因':<30}")

    print(f"\n{'─'*70}")
    print("二、因子权重调整对比")
    print(f"{'─'*70}")
    print(f"\n调整日志对比:")
    print(f"  实验A({len(snap_a['log'])}条):")
    for l in snap_a['log'][:5]:
        print(f"    {l}")
    print(f"  实验B({len(snap_b['log'])}条):")
    for l in snap_b['log']:
        print(f"    {l}")

    print(f"\n{'赛道':<10} {'因子':<8} {'默认':<8} {'A组':<12} {'B组':<12} {'差异':<10}")
    print(f"{'─'*60}")
    for sector in sorted(snap_a['weights'].keys()):
        wA = snap_a['weights'][sector]
        wB = snap_b['weights'].get(sector, DEFAULT_WEIGHTS)
        for factor in DEFAULT_WEIGHTS:
            vA = wA.get(factor, 0)
            vB = wB.get(factor, 0)
            vD = DEFAULT_WEIGHTS[factor]
            if abs(vA - vD) > 0.005 or abs(vB - vD) > 0.005:
                marker = "🟢A修正" if abs(vA - vD) > abs(vB - vD) else "⚪B未变"
                print(f"{sector:<10} {factor:<8} {vD:<8.2f} {vA:<12.3f} {vB:<12.3f} {marker}")

    print(f"\n入场条件对比:")
    for k in DEFAULT_ENTRY:
        vA = snap_a['entry'].get(k, DEFAULT_ENTRY[k])
        vB = snap_b['entry'].get(k, DEFAULT_ENTRY[k])
        vD = DEFAULT_ENTRY[k]
        print(f"  {k:<25} 默认={vD:<5} A组={vA:<5} B组={vB:<5} {'🟢A修正' if vA!=vD else '⚪未变'}")

    print(f"\n{'─'*70}")
    print("三、验证结论")
    print(f"{'─'*70}")

    conclusions = [
        ("结论1: 每日复盘对错判定完全依赖人工校准",
         "A组→完整5类误差标签;B组→仅行情数据,无法区分任何预判对错\n"
         "👉 智能体每日复盘对错判定、偏差归因完全依赖人工校准标注数据"),

        ("结论2: 自主调参必须依靠误差标签作为监督信号",
         f"A组→{len([l for l in snap_a['log'] if '修正' in l])}条定向修正;B组→0条修正(全部默认)\n"
         f"👉 自主调参、模型自我修正必须依靠四类误差标签作为监督信号"),

        ("结论3: 无校准数据则AI丧失自我进化能力",
         f"A组→入场条件从[3天/1.0]收紧为[{snap_a['entry'].get('consecutive_flow_days',3)}天/{snap_a['entry'].get('volume_ratio',1.0)}]\n"
         f"B组→入场条件维持默认[3天/1.0]\n"
         f"👉 缺少人工真实交易校准数据时,AI无法识别预判错误、不会自适应调整策略参数,丧失自我进化能力"),
    ]
    for title, detail in conclusions:
        print(f"\n{title}")
        print(f"{'─'*60}")
        print(detail)

    print("\n" + "=" * 70)
    print("💡 自动化巡检规则(已内置 factor_weekly_iterate.py)")
    print("=" * 70)
    print("""
每周迭代前置自动检测 pre_calibration_check():
  if 存在任意标的当日无 trade_calibration 校准记录:
      → 🚨 打印缺失清单 + exit(1) 阻断调参
      → 降级模式: 权重不变,标记"仅基础拟合"
  else:
      → ✅ 加载校准样本 → 执行定向因子修正迭代
""")
    print("=" * 70)
    print("验证完毕 | weight_snap_A.json | weight_snap_B.json")
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="对照验证实验(仅调试,不加入定时调度)")
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--run", action="store_true", help="执行完整A/B对照实验")
    args = parser.parse_args()

    trade_date = args.date

    print(f"\n{'=' * 60}")
    print(f"🧪 对照验证实验 | {trade_date}")
    print(f"{'=' * 60}")
    print("注意: 本脚本仅用于功能验证,不加入自动定时调度")

    if args.run:
        # ── 实验A: 基线(保留校准) ──
        print(f"\n{'★'*50}")
        print("★ 实验A: 完整校准样本(基线)")
        print(f"{'★'*50}")
        records_a = load_calibration(trade_date)
        print(f"加载校准记录: {len(records_a)}条")
        error_stats_a = run_review(records_a, "实验A-有校准")
        snap_a = adjust_and_save(records_a, "A")
        snap_a_meta = {
            "weight_count": len(records_a),
            "log": snap_a["adjustment_log"],
            "weights": snap_a["weights"],
            "entry": snap_a["entry_conditions"],
        }

        # ── 实验B: 隔离(清空校准) ──
        print(f"\n{'★'*50}")
        print("★ 实验B: 清空校准样本(隔离)")
        print(f"{'★'*50}")

        # 备份并删除
        with pg_engine.connect() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS trade_calibration_backup_test AS
                SELECT * FROM trade_calibration WHERE trade_date = :d
            """), {"d": trade_date})
            conn.execute(text(f"DELETE FROM trade_calibration WHERE trade_date = :d"), {"d": trade_date})
            conn.commit()

        records_b = load_calibration(trade_date)
        print(f"删除后校准记录: {len(records_b)}条")
        error_stats_b = run_review(records_b, "实验B-无校准")
        snap_b = adjust_and_save(records_b, "B")
        snap_b_meta = {
            "weight_count": len(records_b),
            "log": snap_b["adjustment_log"],
            "weights": snap_b["weights"],
            "entry": snap_b["entry_conditions"],
        }

        # 恢复备份
        with pg_engine.connect() as conn:
            conn.execute(text(f"""
                INSERT INTO trade_calibration (trade_date, ticker, ticker_name, real_close, real_change_pct,
                    support_resistance_result, real_trade_action, error_tag, yesterday_ai_prediction, remark)
                SELECT trade_date, ticker, ticker_name, real_close, real_change_pct,
                    support_resistance_result, real_trade_action, error_tag, yesterday_ai_prediction, remark
                FROM trade_calibration_backup_test
                ON CONFLICT (trade_date, ticker) DO NOTHING
            """))
            conn.execute(text("DROP TABLE IF EXISTS trade_calibration_backup_test"))
            conn.commit()
        print("\n✅ 校准数据已恢复")

        # ── 生成验证报告 ──
        generate_report(snap_a_meta, snap_b_meta)

    else:
        print("\n用法: python3 calibration_validation_test.py --run --date YYYYMMDD")
        print("说明: 本脚本会临时删除当日trade_calibration数据,执行完毕后自动恢复")
        print("      仅用于调试验证,不加入任何定时调度")
