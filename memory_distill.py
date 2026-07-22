# memory_distill.py
import numpy as np
from config_memory import RECENT_WEIGHT, HISTORY_WEIGHT
from persistent_memory import PersistentMemory


def get_distill_train_dataset():
    memory = PersistentMemory()
    all_samples = memory.db.get_all_samples()
    memory.close_all()

    # 分层加权采样：新记忆+老记忆混合，避免遗忘历史规律
    good_samples = [s for s in all_samples if s["tag"] == "good"]
    bad_samples = [s for s in all_samples if s["tag"] == "bad"]
    normal_samples = [s for s in all_samples if s["tag"] == "normal"]

    # 加权构造训练集
    train_set = []

    # 近期样本加权
    for s in good_samples:
        train_set.extend([s] * int(RECENT_WEIGHT * 10))

    # 多年历史记忆加权留存
    for s in bad_samples:
        train_set.extend([s] * int(HISTORY_WEIGHT * 10))

    train_set.extend(normal_samples)

    # 拆分特征+标签
    X = np.array([i["feature"] for i in train_set], dtype=np.float32)
    # 标签：盈利1，亏损0
    y = np.array([1 if i["tag"] == "good" else 0 for i in train_set])

    return X, y


if __name__ == "__main__":
    X_train, y_train = get_distill_train_dataset()
    print(f"蒸馏数据集大小：{X_train.shape[0]} 条记忆样本")
    # 此处接入你的XGBoost/神经网络训练代码，每日收盘自动蒸馏更新权重
