# P1补齐：分钟K线数据层 — 变更日志
> 日期：2026-07-18 | 优先级：P1 (低于P0 Sina修复)
> 状态：✅ 表结构已创建 ✅ 采集器已部署 ✅ cron已集成 ✅ 计算逻辑已验证

## 变更清单

| # | 变更项 | 文件 | 状态 |
|:-:|:------|:----|:----:|
| 1 | 新建 stock_minute 表(19字段+5索引) | DDL(psql) | ✅ |
| 2 | 分钟K线采集调度器 | `/opt/stock_agent/minute_collector.py` | ✅ |
| 3 | VWAP/MA5/MA20/累计量衍生计算 | minute_collector.py (compute_minute_indicators) | ✅ |
| 4 | 日内振幅实时查询 | minute_collector.py (get_intraday_amplitude) | ✅ |
| 5 | run_daily.sh step 6.6 集成 | `/opt/stock_agent/run_daily.sh` | ✅ |
| 6 | 采集日志 | `/opt/stock_agent/logs/minute_collector.log` | ✅ |

## stock_minute 表结构

```
stock_minute
├── id              SERIAL PK
├── ts_code         VARCHAR(20) NOT NULL  — 股票代码.SH/.SZ
├── stock_code      VARCHAR(10)           — 纯数字
├── trade_date      DATE NOT NULL         — 交易日
├── trade_time      TIMESTAMP NOT NULL    — 分钟时间戳(含日期)
├── open/high/low/close NUMERIC(12,2)    — OHLC
├── vol             NUMERIC(20,0)        — 成交量(手)
├── amount          NUMERIC(20,2)        — 成交额(千元)
├── avg_price       NUMERIC(12,2)        — 分时均价(元/股)
├── vwap            NUMERIC(12,2)        — 量加权均价(元/股)
├── ma5/ma20        NUMERIC(12,2)        — 分钟均线
├── cumulative_vol  NUMERIC(20,0)        — 日内累计量
├── cumulative_amt  NUMERIC(20,2)        — 日内累计额
└── created_at/updated_at

索引:
  UNIQUE (ts_code, trade_time)   ← 去重
  INDEX  (ts_code, trade_date)   ← 日线查询加速
  INDEX  (trade_date)            ← 全量扫描
  INDEX  (stock_code)            ← 无后缀搜索
```

## 采集调度设计

| 参数 | 值 |
|:----|:----|
| 接口 | Tushare Pro stk_mins |
| 限频 | **1req/min** (安全缓冲62s) |
| 采集时段 | 盘后17:00(今日完整交易日数据) |
| 标的范围 | TARGET_CODES 全量(16只) |
| 最差用时 | 16只 × 62s ≈ 16.5分钟 |
| 增量去重 | INSERT ON CONFLICT DO NOTHING |

## 衍生指标计算逻辑

```
VWAP   = cumulative_amt(千元) × 1000 / cumulative_vol(手) × 100
       = Σ成交额(元) / Σ成交量(股)     → 元/股

avg_price = amount(千元) × 1000 / vol(手) × 100
          → 该分钟均价(元/股)

MA5     = AVG(close) OVER(5分钟滚动)   → 分钟级趋势
MA20    = AVG(close) OVER(20分钟滚动)  → 分钟级趋势

日内振幅 = (当日最高价 - 当日最低价) / 当日最低价 × 100%
```

## 使用方式

```bash
# 盘后全量采集(16只,限频62s/只)
python3 minute_collector.py --mode daily --freq 1min

# 单只补采
python3 minute_collector.py --mode single --ts_code 600547.SH --date 20260715

# 历史回填(指定日期范围)
python3 minute_collector.py --mode backfill --start 20260701 --end 20260717

# 查询日内振幅
python3 minute_collector.py --amplitude 600547.SH:20260715
```

## 限频约束(重要)

```
stk_mins API 当前状态:
  - 限制频率: 1次/分钟 (所有freq共用)
  - 超额惩罚: 1次/小时 🔴 (超额后整小时封锁)
  - 当前token分钟权限: ⚠️ 积分可能不足,需确认

规避策略:
  - 62s间隔(含缓冲)
  - 盘后非高峰时段采集(17:00后)
  - 不可盘中实时调用
  - Level-2/QMT通道为长期替代方案
```

## 缺失能力映射

| 缺失能力 | 当前替代 | 长期方案 |
|:--------|:--------|:--------|
| 逐笔成交(tick) | ❌ 不可替代 | 东方财富Level-2 / QMT |
| 盘口5档 | ❌ 不可替代 | Level-2通道 |
| 分钟K线 | ✅ stock_minute表+stk_mins | 同左 |
| 分时均线 | ✅ avg_price/VWAP计算 | 同左 |
| 日内实时振幅 | ✅ get_intraday_amplitude函数 | 同左 |
