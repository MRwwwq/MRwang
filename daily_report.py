# -*- coding: utf-8 -*-
"""
daily_report.py — 文本日报生成器（含金油跷跷板模块 + 极端行情标注 + 自校验）
"""
import sys
import json
import os
from datetime import datetime

try:
    from config import TARGET_CODES
except ImportError:
    TARGET_CODES = []

try:
    from self_correct import correct_reporting, PreOutputValidator
    SELF_CORRECT_ACTIVE = True
except ImportError:
    SELF_CORRECT_ACTIVE = False
    def correct_reporting(r): return r


def generate_text_report(code, trade_date):
    """生成文本日报（含金油模块）"""
    lines = []
    lines.append(f"【每日量化报告 - {code}】{trade_date}")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 50)

    # 从stock_predict取最新预测
    try:
        import pandas as pd
        from sqlalchemy import text
        from config import pg_engine

        sql = text("""
            SELECT p.*, d.close, d.pct_chg, d.ma5, d.ma10, d.ma20,
                   d.amount, d.volume_ratio
            FROM stock_predict p
            LEFT JOIN stock_daily d ON d.ts_code = p.ts_code
                AND d.trade_date = p.trade_date
            WHERE p.ts_code = :code
            ORDER BY p.trade_date DESC LIMIT 1
        """)
        df = pd.read_sql(sql, pg_engine, params={"code": code})
        if not df.empty:
            r = df.iloc[0]
            lines.append(f"评分: {r['confidence']} | "
                         f"方向: {r['predict_result']} | "
                         f"仓位: {r['position']}")
            lines.append(f"收盘: {r['close']} | "
                         f"涨跌: {r['pct_chg']}% | "
                         f"量比: {r['volume_ratio']}")
            lines.append(f"MA5: {r['ma5']} | MA10: {r['ma10']} | MA20: {r['ma20']}")
            lines.append(f"理由: {str(r.get('predict_reason',''))[:200]}")
        else:
            lines.append("无预测数据")
    except Exception as e:
        lines.append(f"数据读取异常: {e}")

    # ── 金油跷跷板模块(仅限贵金属赛道) ──
    try:
        from evolution.gold_oil_seesaw import fetch_gold_oil_data, analyze_seesaw_effect, format_gold_oil_module
        SECTOR_LABELS = {
            "600547": "贵金属避险",
        }
        sector = SECTOR_LABELS.get(code, "")
        if "贵金属" in sector:
            data = fetch_gold_oil_data()
            # 获取今日涨跌幅
            stock_pct = None
            try:
                from sqlalchemy import text
                from config import pg_engine
                df_pct = pd.read_sql(
                    text("SELECT pct_chg FROM stock_daily WHERE ts_code=:c ORDER BY trade_date DESC LIMIT 1"),
                    pg_engine, params={"c": code}
                )
                if not df_pct.empty:
                    stock_pct = float(df_pct.iloc[0]['pct_chg'])
            except Exception:
                pass
            impact = analyze_seesaw_effect(data, stock_pct)
            lines.append("")
            lines.append(format_gold_oil_module(data, impact, code))
    except ImportError:
        pass  # 金油模块未安装,跳过
    except Exception as e:
        lines.append(f"\n[金油模块异常: {e}]")

    # ── 极端行情标注检查 ──
    try:
        from self_correct import list_extreme_annotations
        annotations = list_extreme_annotations()
        for ann in annotations:
            if ann["stock"] == code:
                lines.append(f"\n⚠️ 极端行情历史标注: {ann['label']} ({ann['date']})")
                lines.append(f"   场景分类: {ann['scene']}")
                break
    except ImportError:
        pass

    lines.append("\n" + "=" * 50)
    return "\n".join(lines)


def run(code=None, trade_date=None):
    """运行+自校验"""
    dates = trade_date or datetime.now().strftime("%Y%m%d")
    codes = [code] if code else TARGET_CODES[:1]

    report = {}
    for c in codes:
        txt = generate_text_report(c, dates)
        report[c] = txt
        print(txt)

    # 自校验
    if SELF_CORRECT_ACTIVE:
        try:
            validator = PreOutputValidator()
            v_result = validator.validate_report_json(report)
            v = validator.report()
            if not v["ok"]:
                print("\n[validator] 校验发现以下问题:")
                for f in v["fixes"]:
                    print(f"  修正: {f}")
                for w in v["warnings"]:
                    print(f"  警告: {w}")
            else:
                print("[validator] 校验通过 ✅")
        except Exception as e:
            print(f"[validator] 校验异常: {e}")


if __name__ == "__main__":
    code = sys.argv[2] if len(sys.argv) > 2 else None
    dt = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    run(code, dt)
