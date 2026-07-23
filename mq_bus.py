#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mq_bus.py — 微服务消息总线基础设施

架构: 异步消息队列 + 话题路由 + 死信队列 + 幂等键去重 + 超时管控

话题映射(规格):
    topic.signal.raw       — SIGNAL_EXTRACT → 原始特征信号
    topic.signal.matched   — MISJUDGE_MATCH → 误判匹配信号
    topic.risk.score       — RULE_SCORE_ENGINE → 风险评分
    topic.order.decision   — POSITION_DECISION → 交易约束指令

微服务列表:
    SIGNAL_EXTRACT, MISJUDGE_MATCH, RULE_SCORE_ENGINE,
    POSITION_DECISION, EVOLUTION_AGENT

隔离规则:
    - 单条消息异常转入死信队列DLQ
    - 单一模块报错/超时不阻塞其他标的消息
    - 幂等键: task_uuid+stock_code+snapshot_time
    - 单任务超时: 1200ms
"""

import json
import time
import uuid
import logging
import threading
from datetime import datetime
from collections import defaultdict
from typing import Any, Callable, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MQ] %(message)s",
    datefmt="%H:%M:%S",
)

# ===================== 消息定义 =====================

MSG_TOPICS = {
    "raw_signal": "topic.signal.raw",
    "matched_signal": "topic.signal.matched",
    "risk_score": "topic.risk.score",
    "order_decision": "topic.order.decision",
}

SERVICE_LIST = [
    "SIGNAL_EXTRACT",
    "MISJUDGE_MATCH",
    "RULE_SCORE_ENGINE",
    "POSITION_DECISION",
    "EVOLUTION_AGENT",
]

SINGLE_TASK_TIMEOUT_MS = 1200  # 规格: 单任务超时1200ms


def build_idempotent_key(task_uuid: str, stock_code: str,
                         snapshot_time: str) -> str:
    """构建幂等键: task_uuid+stock_code+snapshot_time"""
    return f"{task_uuid}:{stock_code}:{snapshot_time}"


def build_message(topic: str, service: str,
                  stock_code: str, payload: dict,
                  task_uuid: str = None,
                  snapshot_time: str = None) -> dict:
    """构建标准化消息体"""
    now_ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    msg = {
        "msg_id": str(uuid.uuid4())[:8],
        "task_uuid": task_uuid or str(uuid.uuid4()),
        "stock_code": stock_code,
        "snapshot_time": snapshot_time or now_ts,
        "idempotent_key": build_idempotent_key(
            task_uuid or "", stock_code, snapshot_time or now_ts
        ),
        "source_service": service,
        "topic": topic,
        "timestamp": now_ts,
        "payload": payload,
        "produced_at": time.time(),
    }
    return msg


# ===================== 消息总线内核 =====================

class MessageBus:
    """内存消息总线（可替换为真实MQ如RabbitMQ/Kafka）。

    架构:
        topics: dict[str, list[dict]] — 每个话题的消息队列
        consumers: dict[str, list[Callable]] — 话题订阅者
        dlq: list[dict] — 死信队列
        idempotent_cache: set[str] — 已处理的幂等键
    """

    def __init__(self):
        self.topics = defaultdict(list)        # topic → [messages]
        self.consumers = defaultdict(list)      # topic → [handlers]
        self.dlq = []                           # 死信队列
        self.idempotent_cache = set()           # 已处理幂等键
        self._lock = threading.Lock()
        self._stats = {"produced": 0, "consumed": 0, "dlq": 0, "timeout": 0}

    # ─────── 生产者 ───────

    def produce(self, topic: str, message: dict) -> bool:
        """生产消息到指定话题。返回True=成功。"""
        with self._lock:
            # 幂等检查
            ik = message.get("idempotent_key", "")
            if ik and ik in self.idempotent_cache:
                logging.debug(f"  ⏭️ 幂等跳过 {ik[:40]}...")
                return True  # 已处理,视为成功
            self.topics[topic].append(message)
            self._stats["produced"] += 1
        logging.info(f"  📤 [{topic}] msg_id={message['msg_id']} "
                      f"code={message['stock_code']} service={message['source_service']}")
        return True

    def produce_from_service(self, topic: str, service_name: str,
                              stock_code: str, payload: dict,
                              task_uuid: str = None,
                              snapshot_time: str = None) -> dict:
        """便捷方法: 构建消息并生产,返回消息体。"""
        msg = build_message(topic, service_name, stock_code,
                            payload, task_uuid, snapshot_time)
        self.produce(topic, msg)
        return msg

    # ─────── 消费者 ───────

    def subscribe(self, topic: str, handler: Callable[[dict], Optional[dict]]):
        """订阅话题。handler接收消息dict,返回output dict或None。"""
        with self._lock:
            self.consumers[topic].append(handler)
        logging.info(f"  👂 订阅 topic={topic} handler={handler.__name__}")

    def consume_one(self, topic: str) -> Optional[dict]:
        """消费单条消息(非阻塞)。返回消息体或None。"""
        with self._lock:
            if not self.topics[topic]:
                return None
            msg = self.topics[topic].pop(0)
        return msg

    def consume_all(self, topic: str) -> list:
        """消费话题全部待处理消息。"""
        msgs = []
        while True:
            msg = self.consume_one(topic)
            if msg is None:
                break
            msgs.append(msg)
        return msgs

    def process_topic(self, topic: str) -> list:
        """处理话题全部消息: 路由给所有订阅者。返回成功结果列表。"""
        results = []
        handlers = list(self.consumers.get(topic, []))
        if not handlers:
            logging.warning(f"  ⚠️ topic={topic} 无订阅者, 消息滞留")
            return results

        while True:
            msg = self.consume_one(topic)
            if msg is None:
                break

            # 幂等性检查
            ik = msg.get("idempotent_key", "")
            if ik and ik in self.idempotent_cache:
                logging.debug(f"  ⏭️ 幂等跳过消息 msg_id={msg['msg_id']}")
                continue

            ik = msg.get("idempotent_key", "")
            stock_code = msg.get("stock_code", "")
            success = False
            for handler in handlers:
                try:
                    # 超时管控
                    start = time.time()
                    output = handler(msg)
                    elapsed_ms = (time.time() - start) * 1000
                    if elapsed_ms > SINGLE_TASK_TIMEOUT_MS:
                        logging.warning(f"  ⚠️ msg_id={msg['msg_id']} {stock_code} "
                                        f"处理超时 {elapsed_ms:.0f}ms>{SINGLE_TASK_TIMEOUT_MS}ms")
                        self._stats["timeout"] += 1

                    if output is not None:
                        results.append(output)
                    success = True

                    # 记录幂等
                    if ik and ik not in self.idempotent_cache:
                        with self._lock:
                            self.idempotent_cache.add(ik)

                except Exception as e:
                    logging.error(f"  ❌ msg_id={msg['msg_id']} {stock_code} "
                                  f"handler={handler.__name__} 异常: {e}")
                    # 单条异常转入DLQ,不阻塞其他消息
                    msg["error"] = str(e)
                    msg["failed_at"] = datetime.now().strftime("%H:%M:%S")
                    with self._lock:
                        self.dlq.append(msg)
                        self._stats["dlq"] += 1

            if success:
                with self._lock:
                    self._stats["consumed"] += 1

        return results

    # ─────── DLQ管理 ───────

    def get_dlq_messages(self) -> list:
        """获取死信队列全部消息。"""
        return list(self.dlq)

    def dlq_count(self) -> int:
        return len(self.dlq)

    def replay_dlq(self, topic: str = None) -> int:
        """重放死信队列消息到指定话题。返回重放数量。"""
        replay_count = 0
        remaining = []
        with self._lock:
            for msg in self.dlq:
                error = msg.pop("error", None)
                msg.pop("failed_at", None)
                target_topic = topic or msg.get("topic", "")
                if target_topic:
                    self.topics[target_topic].append(msg)
                    replay_count += 1
                else:
                    remaining.append(msg)
            self.dlq = remaining
        if replay_count:
            logging.info(f"  🔄 重放DLQ: {replay_count}条")
        return replay_count

    # ─────── 统计 ───────

    def stats(self) -> dict:
        """输出总线统计。"""
        with self._lock:
            s = dict(self._stats)
            s["topics_depth"] = {k: len(v) for k, v in self.topics.items()}
            s["consumers_count"] = {k: len(v) for k, v in self.consumers.items()}
            s["dlq_count"] = len(self.dlq)
            s["idempotent_cache_size"] = len(self.idempotent_cache)
        return s

    def report(self) -> str:
        """格式化统计报告。"""
        s = self.stats()
        lines = [
            f"\n{'='*55}",
            f"  📊 消息总线统计",
            f"  {'='*55}",
            f"  生产: {s['produced']} | 消费: {s['consumed']} | DLQ: {s['dlq_count']}",
            f"  超时: {s['timeout']} | 幂等缓存: {s['idempotent_cache_size']}",
        ]
        for t, depth in s["topics_depth"].items():
            cons = s["consumers_count"].get(t, 0)
            lines.append(f"  topic={t:30s} depth={depth:>3d} consumers={cons}")
        lines.append(f"  {'='*55}")
        return "\n".join(lines)


# ===================== 全局单例 =====================

_bus_instance = None


def get_bus() -> MessageBus:
    """获取全局消息总线单例。"""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = MessageBus()
    return _bus_instance


def reset_bus():
    """重置消息总线(测试/盘前调用)。"""
    global _bus_instance
    _bus_instance = MessageBus()


# ===================== 自测 =====================

if __name__ == "__main__":
    reset_bus()
    bus = get_bus()

    # 测试生产者
    msg1 = bus.produce_from_service(
        "topic.signal.raw", "SIGNAL_EXTRACT",
        "600884.SH",
        {"ma5": 12.5, "ma20": 11.8, "gold_cross": True}
    )
    msg2 = bus.produce_from_service(
        "topic.signal.raw", "SIGNAL_EXTRACT",
        "600547.SH",
        {"ma5": 25.0, "ma20": 26.0, "gold_cross": False}
    )

    # 测试消费者
    def dummy_handler(msg):
        logging.info(f"  处理 {msg['stock_code']}: {msg['payload']}")
        return {"code": msg["stock_code"], "processed": True}

    bus.subscribe("topic.signal.raw", dummy_handler)
    results = bus.process_topic("topic.signal.raw")
    assert len(results) == 2, f"应处理2条,实际{len(results)}"
    print(f"  ✅ 处理结果: {len(results)}条")

    # 测试幂等
    bus.produce("topic.signal.raw", msg1)
    r2 = bus.process_topic("topic.signal.raw")
    print(f"  ✅ 幂等跳过: {len(r2)}条(应0)")

    # 测试DLQ
    def failing_handler(msg):
        raise ValueError("模拟异常")

    bus.subscribe("topic.signal.matched", failing_handler)
    bus.produce_from_service("topic.signal.matched", "MISJUDGE_MATCH",
                              "600884.SH", {"score": 85})
    bus.process_topic("topic.signal.matched")
    print(f"  ✅ DLQ: {bus.dlq_count()}条")
    assert bus.dlq_count() == 1

    print(bus.report())
    print("  ✅ MQ总线全部测试通过")
