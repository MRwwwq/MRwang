# 另类舆情层 — 完整能力矩阵 (2026-07-18)

## 数据可达性总表

| 数据类 | 接口 | 存量 | 状态 |
|:------|:----|:----:|:----:|
| 龙虎榜日汇总 | `top_list(trade_date)` | 87条/日 | ✅ |
| 龙虎榜机构明细 | `top_inst(trade_date)` | 958条/日 | ✅ |
| 新闻列表+摘要 | `ak.stock_news_em(symbol)` | 10条/只 | ✅ |
| 新闻正文抓取 | `requests+BS4(contentbox)` | 逐篇(2~4s) | ✅ |
| 全市场公告 | `ak.stock_notice_report(date)` | 2,284条/日 | ✅ |
| 互动易IR问答 | `ak.stock_irm_cninfo(symbol)` | 0~214条/只 | ✅ |
| 情感分析 | `fin_sentiment.py` | 本地引擎 | ✅ |
| 新闻+情感流水线 | `news_sentiment_pipeline.py` | JSON输出 | ✅ |

## 代码示例

### 1. 新闻列表+正文+情感 (一站式)
```bash
python3 /opt/stock_agent/news_sentiment_pipeline.py 600547 --full --max 10
```

### 2. 全市场公告筛选
```python
import akshare as ak
df = ak.stock_notice_report(date="20260715")
# 筛选代码
my_stocks = df[df['代码'].astype(str).str.contains('600547|600884|002044')]
```

### 3. 互动易IR
```python
df = ak.stock_irm_cninfo(symbol="002044")
print(df[['问题','回答内容','提问时间']])
```

### 4. 龙虎榜
```python
import tushare as ts
pro = ts.pro_api(TOKEN)
top = pro.top_list(trade_date='20260715')     # 上榜汇总
inst = pro.top_inst(trade_date='20260715')    # 机构明细
```

### 5. 情感分析单独使用
```python
from fin_sentiment import FinSentiment
sa = FinSentiment()
result = sa.analyze("山东黄金套保浮亏利润不及预期")
# {label:'利空', score:0.35, neg_words:['浮亏','不及预期']}
```
