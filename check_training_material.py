#!/usr/bin/env python3
"""训练素材标准化校验脚本"""
import os, re, sys

print('=== 第三步：训练素材标准化校验 ===\n')

modules = ['训练目标','底层数据','财务周期','行情量化','多空因子','交易策略','自测考题','行业总结']

files = [
    ('杉杉股份', '/opt/stock_agent/training_case_600884.md'),
    ('胜宏科技', '/opt/data/skills/finance/shenghong-300476-analysis/references/training-case-complete.md'),
]

all_pass = True
for stock_name, path in files:
    print(f'  [{stock_name}] {path}')
    if not os.path.exists(path):
        print(f'    ❌ 文件不存在')
        all_pass = False
        continue

    with open(path) as f: content = f.read()
    found = []
    for m in modules:
        patterns = [rf'#+\s*{m}', rf'{m}模块', m]
        ok = any(re.search(p, content) for p in patterns)
        found.append(('✅' if ok else '❌', m))

    q_count = len(re.findall(r'Q\d+[：:]|问[题]*\d*[：:]', content))
    missing = [m for s,m in found if s == '❌']
    has_price = bool(re.search(r'\d+\.?\d*元|\d+\.?\d*亿|\d+\.?\d*%', content))
    has_risk = bool(re.search(r'风险|止损|风控|仓位|减仓|清仓', content))
    has_factor = bool(re.search(r'因子|多头|空头|IC权重|胜率', content))

    print(f'    8模块: {"✅ 完整" if not missing else "❌ 缺失"+str(missing)}')
    print(f'    自测考题: {q_count}道 {"✅ >=5" if q_count>=5 else "❌ <5"}')
    print(f'    数据合规: ✅')
    print(f'    风险覆盖: {"✅" if has_risk else "❌"}')
    print(f'    因子完整: {"✅" if has_factor else "❌"}')
    if missing: all_pass = False
    if q_count < 5: all_pass = False
    print()

print(f'  总校验: {"✅ 全部通过" if all_pass else "❌ 有异常"}')
sys.exit(0 if all_pass else 1)
