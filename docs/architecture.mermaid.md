# QCLAW 四层联动量化风控智能体 — 完整架构图

```mermaid
flowchart LR
classDef mq fill:#e1f5fe
classDef core fill:#d0ecff
classDef error fill:#ffdddd
classDef async stroke-dasharray:5 5
classDef sandbox fill:#fff2cc
classDef vector fill:#e6ffe6

A[MQ消息入口]:::mq --> B[SIGNAL_EXTRACT 信号提取]
B --> C[MISJUDGE_MATCH 误判RAG匹配]
C --> D[RULE_SCORE_ENGINE 四层打分引擎]:::core

subgraph FAISS向量记忆库:::vector
M1[短期记忆｜15日滚动索引]
M2[长期记忆｜永久爆雷案例索引]
end

D <-->|异步检索同类案例，修正风险权重| FAISS向量记忆库
D --> L2[L2动态加权｜时效衰减+正向对冲]
L2 --> L3[L3 Lollapalooza分级判定<br/>中度/重度区分，取消一刀切禁仓]
L3 --> G[赛道差异化阈值判定三色等级]
G --> H[自适应仓位系数输出]
H --> I[topic.risk.score 发布]
I --> J[POSITION_DECISION 交易决策]
I --> K[EVOLUTION_AGENT｜分级共振即时进化]:::async
I --> L[AsyncLogger 异步SHAP归因日志落盘]:::async
K -->|重度样本自动写入长期记忆库| FAISS向量记忆库

subgraph EVOLUTION_AGENT沙盒【离线每周调度】:::sandbox
S1[读取长短记忆样本] --> S2[参数组合遍历回测<br/>评估误拦截/漏风控指标]
S2 --> S3[最优动态权重、阈值筛选]
S3 --> S4[生成变更报告｜等待人工审核上线]
end

D -->|执行异常| L1[结构化日志报错]:::error
L1 --> M[DLQ死信队列]
M --> N[监控告警]
```

## 节点与规范对应表

| § | 规范 | 图中节点 | 代码文件 |
|:-:|:-----|:---------|:---------|
| §1.1 | 5微服务流水线 | A→B→C→D→H→I→J | `mq_bus.py` + `microservice_orchestrator.py` |
| §1.2 | MQ: DLQ/幂等/1200ms | M/DLQ/N | `mq_bus.py` |
| §2.1 | 三级故障降级 | 集成于D内部 | `service_fault_degradation.py` |
| §2.2 | 因子漂移监控 | 集成于D内部(Step3前置) | `service_factor_drift.py` |
| §2.3 | 双频风控刷新 | H→I链路 | `service_dual_frequency.py` |
| §3 L0 | 宏观系数0.7/1.0/1.3 | D内部 | `rule021_dual_branch.py` + `rule_score_engine.py` |
| §3 L1 | Rule021双分支5维+计算公式 | D内部 | `rule021_dual_branch.py` |
| §3 L2 | 双矩阵+衰减+对冲+FAISS | L2节点 | `rule_score_engine.py` + `weight_dispatch.py` |
| §3 L3 | Lollapalooza分级(中度/重度) | L3节点 | `rule_score_engine.py` |
| §4 | 三色阈值+仓位系数 | G→H节点 | `rule_score_engine.py` + `position_decision.py` |
| §5.1 | FAISS短期15d+长期永久 | M1+M2 子图 | `service_faiss_memory.py` |
| §5.2 | 分级共振即时进化 | K节点→FAISS | `service_evolution_agent.py` |
| §5.3 | 每周离线沙盒调优 | S1→S4 子图 | `service_sandbox_tuning.py` |
| §5.4 | 动态加权调度(双矩阵) | D内部(集成) | `service_weight_dispatch.py` |
| §6 | SHAP 9字段(含Lolla标签) | L异步落盘 | `service_shap_trace.py` |
| §7 | rule021边界(仅L0+L1) | D内部(委托引擎) | `rule021_dual_branch.py` |
```
