# memory_redis.py
import json
from config_memory import REDIS_HOST, REDIS_PORT, REDIS_DB


class ShortMemoryRedis:
    def __init__(self):
        self._real_redis = None
        self._fake_redis = None
        self._init_client()

    def _init_client(self):
        # 优先用真实Redis，不可用时降级fakeredis
        try:
            import redis as _redis
            self._real_redis = _redis.Redis(
                host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                decode_responses=True, socket_connect_timeout=2
            )
            self._real_redis.ping()
        except Exception:
            self._real_redis = None
            try:
                import fakeredis
                self._fake_redis = fakeredis.FakeStrictRedis(decode_responses=True)
            except ImportError:
                self._fake_redis = None

    @property
    def client(self):
        return self._real_redis or self._fake_redis

    # 存入当日实时持仓、临时信号
    def set_cache(self, key: str, data: dict, expire=86400):
        c = self.client
        if c:
            c.setex(key, expire, json.dumps(data))

    def get_cache(self, key: str):
        c = self.client
        if not c:
            return None
        raw = c.get(key)
        return json.loads(raw) if raw else None

    # 清空当日临时缓存（收盘执行）
    def clear_today_cache(self):
        c = self.client
        if c:
            c.delete("today_hold", "today_signal_list")
