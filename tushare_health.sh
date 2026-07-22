#!/bin/bash
# Tushare Pro 连通性探针
export PGPASSFILE=/root/.pgpass
python3 -c "
import os, sys, time
sys.path.insert(0, '/opt/stock_agent')
os.environ['PGPASSFILE'] = '/root/.pgpass'
import tushare as ts
content = open('/opt/stock_agent/config.py','rb').read()
for line in content.split(b'\n'):
    if b'TUSHARE_TOKEN' in line:
        q = line.find(b'\"', line.find(b'\"')+1)
        tok = line[line.find(b'\"')+1:q].decode(); ts.set_token(tok); break
pro = ts.pro_api()
t0=time.time()
df=pro.daily(ts_code='300476.SZ',start_date='20260714',end_date='20260715')
elapsed=int((time.time()-t0)*1000)
if df is None or len(df)==0 or elapsed>3000:
    echo \"**[🆘 TUSHARE探针异常]** 响应=${elapsed}ms 数据空=$([ -z \"$df\" ] && echo '是' || echo '否')\"
    exit 1
else:
    echo \"**[✅ TUSHARE探针正常]** 响应=${elapsed}ms 行数=$(echo $df | python3 -c 'import sys; print(len(sys.stdin.read().split(chr(10)))-2)')\"
    exit 0
"
