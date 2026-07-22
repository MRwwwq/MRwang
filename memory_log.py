import logging

# 记忆系统日志配置
logging.basicConfig(
    filename="./memory/memory_system.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8"
)
mem_log = logging.getLogger("memory_core")


def add_memory_logging():
    """注入记忆系统全部模块的日志句柄"""
    modules = [
        "config_memory", "memory_db", "memory_redis",
        "memory_vector", "persistent_memory", "agent_demo",
        "memory_distill", "daily_task_runner",
    ]
    mem_log.info("=" * 50)
    mem_log.info("  记忆系统启动")
    mem_log.info(f"  模块: {', '.join(modules)}")
    mem_log.info("=" * 50)
    return mem_log


if __name__ == "__main__":
    add_memory_logging()
    mem_log.info("日志配置验证通过 ✅")
