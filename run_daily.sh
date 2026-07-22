#!/bin/bash
# run_daily.sh v4 — 全自动化每日流程（含自适应自我修正）
# 采集 → 打分 → 情景推演 → 自我修正 → 交易信号 → 复盘统计 → 日报
set -e
cd /opt/stock_agent

DATE_ARG=${1:-$(date +%Y%m%d)}
LOG_DIR=logs
mkdir -p $LOG_DIR

echo "===== 开始每日流水线: $DATE_ARG =====" | tee -a $LOG_DIR/run_$DATE_ARG.log

# 0. 盘前数据源连通校验 (09:25, 若高风险阻断则退出)
if [ "$(date +%H%M)" = "0925" ]; then
    echo "[step pre] 数据源连通校验..." | tee -a $LOG_DIR/run_$DATE_ARG.log
    python3 data_source_daily_check.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
    # 若出口码非0且输出包含"阻断"则退出
    if [ $? -ne 0 ]; then
        echo "🚨 数据源校验失败, 终止流水线" | tee -a $LOG_DIR/run_$DATE_ARG.log
        exit 1
    fi
fi

# 0. 健康检查 + 赛道数据投喂
echo "[step 0] 全链路自检+赛道数据投喂..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 -c "
from self_correct import run_full_check
r = run_full_check()
print('自检:', '通过' if r.get('all_ok') else '异常:', r.get('checks',{}).keys())
" 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
if [ -f agent_data_feed.py ]; then
    python3 agent_data_feed.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
fi

# 1. 数据采集(批量全量)
echo "[step 1] 采集数据(batch)..."
python3 batch_collect.py $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
echo "-------" >> $LOG_DIR/run_$DATE_ARG.log
# Legacy single-stock import (if batch fails fallback)
if [ -f stock_import_agent.py ]; then
    for code in 600884 002617 600547 002044 300098 300693 300433 601868 300476; do
        tsc="${code}.SH"
        if echo "$code" | grep -q "^0\|^3\|^2"; then tsc="${code}.SZ"; fi
        python3 stock_import_agent.py $tsc $DATE_ARG 2>/dev/null || true
    done
fi

# 2. 打分 + 多情景推演 + 自我修正
echo "[step 2] 打分+推演+修正..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 agent_predict_v2.py $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log

# 3. 交易信号
echo "[step 3] 交易信号..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 agent_trade_v2.py $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log

# 3.5. 盘后校准标准化录入(含前置校验+自动标签+事后逻辑复核)
echo "[step 3.5] 盘后校准录入(三层校验)..."
if [ -f daily_calibration_insert.py ]; then
    python3 daily_calibration_insert.py $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
    CEXIT=$?
    if [ $CEXIT -ne 0 ]; then
        echo "⚠️ 校准录入异常(exit=$CEXIT)，请手动检查 trade_calibration 表完整性"
    fi
fi

# 4. 复盘统计
echo "[step 4] 复盘统计..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 agent_feedback.py "" 30 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log

# 5. 日报
if [ -f daily_json_report.py ]; then
    echo "[step 5] 日报生成..." | tee -a $LOG_DIR/run_$DATE_ARG.log
    python3 daily_json_report.py $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
fi

if [ -f daily_report.py ]; then
    python3 daily_report.py $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
fi

# 6. 报告扫描 (v3自动扫描→解析→Wiki→飞书→回流)
echo "[step 6] 行业报告自动扫描(local_report_auto_scan v3)..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 /opt/stock_agent/local_report_auto_scan.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log

# 6.5 副本自动合并(去重+备份)
echo "[step 6.5] 研报副本自动合并..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 /opt/stock_agent/merge_report_copies.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log

# 6.6 分钟K线采集(限频1req/min, 盘后增量, 仅交易日)
DOW2=$(date +%u)
if [ "$DOW2" -le 5 ] && [ -f minute_collector.py ]; then
    echo "[step 6.6] 分钟K线盘后采集(限频保护)..." | tee -a $LOG_DIR/run_$DATE_ARG.log
    RESULT=$(python3 /opt/stock_agent/minute_collector.py --mode daily --freq 1min 2>&1)
    echo "$RESULT" | tee -a $LOG_DIR/run_$DATE_ARG.log
    if echo "$RESULT" | grep -q "stk_mins_penalty"; then
        echo "   🔴 stk_mins接口惩罚期: 条件不满足, 跳过采集" | tee -a $LOG_DIR/run_$DATE_ARG.log
        echo "   🔴 前置条件: P0 Sina修复 + stk_mins冷却结束" | tee -a $LOG_DIR/run_$DATE_ARG.log
    elif echo "$RESULT" | grep -q "failed"; then
        MEXIT=1
        echo "⚠️ 分钟采集有失败, 检查 minute_collector.log" | tee -a $LOG_DIR/run_$DATE_ARG.log
    fi
fi

# 7. 加权因子迭代（仅周一，含人工校准前置校验）
DOW=$(date +%u)
if [ "$DOW" = "1" ] && [ -f factor_weekly_iterate.py ]; then
    echo "[step 7] 因子权重迭代(含人工校准前置校验)..." | tee -a $LOG_DIR/run_$DATE_ARG.log
    python3 factor_weekly_iterate.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
    FEXIT=$?
    if [ $FEXIT -ne 0 ]; then
        echo "⚠️ 因子迭代因前置校验失败阻断(exit=$FEXIT)" | tee -a $LOG_DIR/run_$DATE_ARG.log
    fi
fi

# 7. 飞书推送
echo "[step 7] 飞书推送..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 -c "
import sys, json
sys.path.insert(0, '/opt/ai-hedge-fund')
from src.tools.feishu_notify import send_v9_report, send_msg

# Read prediction scores from PG
try:
    from config import pg_engine
    import pandas as pd
    from sqlalchemy import text
    sql = text('''SELECT s.ts_code, s.confidence, p.predict_result,
                         s.position, s.risk_score, t.signal
                  FROM stock_predict p
                  JOIN prediction_score s ON s.ts_code = p.ts_code AND s.trade_date = p.trade_date
                  LEFT JOIN strategy_signal t ON t.ts_code = p.ts_code
                  WHERE p.trade_date = :dt
                  ORDER BY s.ts_code''')
    df = pd.read_sql(sql, pg_engine, params={'dt': $DATE_ARG})
    if not df.empty:
        preds = df.to_dict('records')
        send_v9_report(preds)
        send_msg('✅ 量化流水线完成: ' + str(len(preds)) + '只标的')
    else:
        send_msg('⚠️ 量化流水线: 今日无预测数据')
except Exception as e:
    send_msg(f'⚠️ 量化推送异常: {e}')
" 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log

# 8. 记忆系统: 蒸馏+备份 (收盘后)
echo "[step 8] 记忆系统收盘任务(蒸馏+备份)..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 daily_task_runner.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
MEMEXIT=$?
if [ $MEMEXIT -ne 0 ]; then
    echo "⚠️ 记忆系统异常(exit=$MEMEXIT)，检查 memory/memory_system.log" | tee -a $LOG_DIR/run_$DATE_ARG.log
fi

# 9. 复盘: 加权随机选1只 + 横向对比5股
echo "[step 9] 复盘选股+横向对比..." | tee -a $LOG_DIR/run_$DATE_ARG.log
python3 daily_review_picker.py --date $DATE_ARG 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
RV1_EXIT=$?
if [ $RV1_EXIT -ne 0 ]; then
    echo "⚠️ 复盘选股异常(exit=$RV1_EXIT)，继续执行" | tee -a $LOG_DIR/run_$DATE_ARG.log
else
    echo "  ✅ 复盘报告已生成 → reviews/" | tee -a $LOG_DIR/run_$DATE_ARG.log
fi

python3 cross_compare.py 2>&1 | tee -a $LOG_DIR/run_$DATE_ARG.log
RV2_EXIT=$?
if [ $RV2_EXIT -ne 0 ]; then
    echo "⚠️ 横向对比异常(exit=$RV2_EXIT)，继续执行" | tee -a $LOG_DIR/run_$DATE_ARG.log
else
    echo "  ✅ 横向对比报告已生成 → reports/" | tee -a $LOG_DIR/run_$DATE_ARG.log
fi

echo "===== 流水线完成: $DATE_ARG ===== | tee -a $LOG_DIR/run_$DATE_ARG.log
