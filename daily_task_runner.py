# daily_task_runner.py
import os
import subprocess
from memory_distill import get_distill_train_dataset
from memory_log import mem_log
import torch


# ========= 配置区 =========
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_SCRIPT_WIN = os.path.join(PROJECT_ROOT, "backup_memory.bat")
BACKUP_SCRIPT_LINUX = os.path.join(PROJECT_ROOT, "backup_memory.sh")
SAVE_MODEL_PATH = "./model/trade_knowledge.pth"
os.makedirs("./model", exist_ok=True)


def run_memory_distill():
    """执行记忆蒸馏，把历史经验固化进模型权重"""
    print("===== 开始每日记忆蒸馏训练 =====")
    X_train, y_train = get_distill_train_dataset()
    print(f"蒸馏数据集总量：{len(X_train)} 条历史交易记忆")
    mem_log.info(f"每日蒸馏启动，训练样本数：{len(X_train)}")

    # --------------------------
    # 此处替换为你自己的模型训练代码（XGB/LSTM/Transformer均可）
    # 示例占位：仅演示保存权重逻辑
    dummy_model = torch.nn.Linear(X_train.shape[1], 1)
    torch.save(dummy_model.state_dict(), SAVE_MODEL_PATH)
    # --------------------------
    print(f"蒸馏完成，固化记忆模型保存至：{SAVE_MODEL_PATH}")


def run_backup():
    """自动调用备份脚本，区分系统"""
    print("===== 开始执行记忆库全量备份 =====")
    import platform
    sys_plat = platform.system()
    if sys_plat == "Windows":
        subprocess.Popen([BACKUP_SCRIPT_WIN], cwd=PROJECT_ROOT)
    else:
        subprocess.run(["bash", BACKUP_SCRIPT_LINUX], cwd=PROJECT_ROOT)
    print("备份任务后台运行完成")


def daily_after_close_all_task():
    """收盘统一入口：蒸馏 + 备份 一键执行"""
    # 1. 蒸馏固化记忆到模型
    run_memory_distill()
    # 2. 全量备份sqlite、向量索引文件
    run_backup()
    print("===== 今日收盘记忆全流程任务全部结束 =====")


if __name__ == "__main__":
    # 收盘后手动/程序自动调用此函数
    daily_after_close_all_task()
