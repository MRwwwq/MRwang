#!/bin/bash
# backup_memory.sh — 记忆库备份脚本 (Linux版)
# ====================================================
# 用法:
#   chmod +x backup_memory.sh
#   ./backup_memory.sh
#   cron 每日收盘后自动执行: 0 16 * * 1-5 /opt/stock_agent/backup_memory.sh
# ====================================================

MEM_DIR="./memory"
BACKUP_ROOT="./memory_backup"
DATE=$(date +%Y-%m-%d)
BACKUP_FOLDER="${BACKUP_ROOT}/memory_${DATE}"

# 创建备份目录
mkdir -p "$BACKUP_ROOT"
mkdir -p "$BACKUP_FOLDER"

# 复制全部记忆文件: 数据库、向量索引
cp -r "$MEM_DIR"/. "$BACKUP_FOLDER"/ 2>/dev/null

# 验证备份完整性
echo "备份文件列表:" >&2
ls -lh "$BACKUP_FOLDER" 2>/dev/null

# 保留最近30天备份，自动清理旧包
find "$BACKUP_ROOT" -maxdepth 1 -type d -name "memory_*" | while read folder; do
    folder_date="${folder##*memory_}"
    cutoff=$(date -d '30 days ago' +%Y-%m-%d)
    if [[ "$folder_date" < "$cutoff" ]]; then
        rm -rf "$folder"
        echo "  清理旧备份: $(basename "$folder")"
    fi
done

echo "======================================"
echo "记忆库备份完成，路径：$BACKUP_FOLDER"
ls -lh "$BACKUP_FOLDER" 2>/dev/null
echo "======================================"
