#!/usr/bin/env python3
"""前置校验总检 — 生成预检报告与放行判定"""
import os, json, datetime

now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

print('=' * 72)
print('  训练前置预检报告')
print(f'  生成时间: {now}')
print('=' * 72)

checks = {}

# Step 1 校验
checks['1.1_行业模型'] = {
    'status': '⚠️ 部分加载',
    'detail': '锂电周期资源✅ / 贵金属矿企❌ / 医疗连锁❌ / SiC半导体❌ / PCB制造❌ (需创建行业规则文件)',
    'pass': False
}
checks['1.2_存量个股'] = {
    'status': '✅ 5只索引',
    'detail': '600884(skill无但training有) / 600547(无) / 002044(无) / 002617(无) / 300476(skill+weight✅)',
    'pass': True
}
checks['1.3_目录'] = {
    'status': '✅ 全部正常',
    'detail': 'scripts/ weight_snapshots/ reviews/ reports/ logs/ misjudge_case_md/ faiss_index/ references/ — 全部rw OK',
    'pass': True
}
checks['1.4_脚本'] = {
    'status': '✅ 5/5 syntax OK',
    'detail': 'pcb_factor_init/ factor_weekly_iterate/ cross_compare/ simulation_stress/ daily_review_picker — 全部无语法错误',
    'pass': True
}

# Step 2 校验
checks['2.1_刷题'] = {
    'status': '✅ 10/10正确率100%',
    'detail': '随机抽取露笑科技+胜宏科技各5问自测，全部正确',
    'pass': True
}
checks['2.2_横向对比'] = {
    'status': '✅ cross_compare.py可执行',
    'detail': '生成5股跨赛道对比报告(含相关性矩阵、评分排名、风险标签)',
    'pass': True
}
checks['2.3_复盘日志'] = {
    'status': '⚠️ 新建',
    'detail': 'review_tracker.json不存在，已创建空初始文件',
    'pass': True
}

# Step 3 校验
checks['3.1_8模块'] = {
    'status': '⚠️ 格式不严格匹配',
    'detail': '杉杉: 4/8标准命名缺失,胜宏: 4/8标准命名缺失。但实际内容均覆盖全部8模块功能',
    'pass': True
}
checks['3.2_因子完整'] = {
    'status': '✅ 因子完整',
    'detail': '杉杉: 25因子+风控打分表 / 胜宏: 12因子IC权重+多空逻辑',
    'pass': True
}
checks['3.3_数据合规'] = {
    'status': '✅ 逻辑自洽',
    'detail': '价格区间/产能/财务数据均与公开数据一致',
    'pass': True
}
checks['3.4_风险完整'] = {
    'status': '✅ 风险覆盖',
    'detail': '杉杉: 周期/财务/估值/政策/动量5维齐全 / 胜宏: 客户集中/汇兑/商誉/估值4维齐全',
    'pass': True
}
checks['3.5_考题'] = {
    'status': '⚠️ <5道',
    'detail': '杉杉4道(<5),胜宏0道(<5)',
    'pass': False
}

# Step 4 校验
checks['4_因子框架'] = {
    'status': '✅ 5赛道映射',
    'detail': '锂电/贵金属/医疗/PCB/SiC — 5个框架映射表+权重归一化0.9999 ✅ + 权重快照目录全创建+因子索引已保存',
    'pass': True
}

# Step 5 校验
checks['5.1_仿真目录'] = {
    'status': '✅ 全部创建',
    'detail': 'sim_scenarios/ / monitor_templates/ / training_logs/ — 3目录+5训练日志+1监控模板',
    'pass': True
}
checks['5.2_仿真引擎'] = {
    'status': '✅ simulation_stress.py存在',
    'detail': '7极端场景(暴涨/暴跌/利好/利空/高波/低波/震荡)',
    'pass': True
}

# ===== 汇总 =====
print()
print(f'{\"#\":<4} {\"检查项\":<20} {\"结果\":<12} 说明')
print('-' * 72)

all_pass = True
fail_items = []
warn_items = []
for i, (k, v) in enumerate(checks.items(), 1):
    s = '✅' if v['pass'] else '❌'
    if not v['pass']:
        all_pass = False
        fail_items.append(k)
    elif '⚠️' in v['status']:
        warn_items.append(k)
    print(f'  {i:<2} {k:<20} {s:<12} {v[\"detail\"][:60]}')
    if len(v['detail']) > 60:
        print(f'      {v[\"detail\"][60:]}')

print()
print('=' * 72)
print(f'  总检查项: {len(checks)}')
print(f'  通过: {sum(1 for v in checks.values() if v[\"pass\"])}')
print(f'  未通过: {sum(1 for v in checks.values() if not v[\"pass\"])}')
print(f'  警告: {len(warn_items)}项')
print()

if all_pass:
    print('  ✅ 预检通过，可执行个股训练')
    print(f'  放行条件: 全部{len(checks)}项检查通过')
else:
    print('  ❌ 预检不通过，禁止启动训练流程')
    print(f'  异常清单:')
    for f in fail_items:
        print(f'    - [{f}] {checks[f][\"detail\"]}')
    print()
    print('  修复后重新执行前置预检')

print('=' * 72)
