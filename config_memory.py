# config_memory.py
import os

# 存储路径
DB_PATH = "./memory/trading_memory.db"
VECTOR_INDEX_PATH = "./memory/trade_vector.index"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0

# 记忆权重配置
RECENT_WEIGHT = 0.6       # 近半年新样本权重
HISTORY_WEIGHT = 0.4      # 多年历史记忆权重
TOP_K_SIMILAR = 80        # 每次决策检索相似样本数量
PROFIT_THRESHOLD = 0.03   # 盈利标记优质记忆阈值
LOSS_THRESHOLD = -0.02    # 亏损标记负面记忆阈值

# 文件夹自动创建
os.makedirs("./memory", exist_ok=True)
