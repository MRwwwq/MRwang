#!/bin/bash
# restore_memory.sh — 记忆库应急恢复脚本
# ====================================================
# 从 memory_backup 指定日期恢复全部记忆文件
#
# 用法:
#   ./restore_memory.sh                           # 列出可用备份
#   ./restore_memory.sh 2026-07-18                 # 恢复指定日期
#   ./restore_memory.sh --latest                   # 恢复最近一次备份
# ====================================================

BACKUP_ROOT="./memory_backup"
MEM_DIR="./memory"

if [ ! -d "$BACKUP_ROOT" ]; then
    echo "❌ 备份目录不存在: $BACKUP_ROOT"
    exit 1
fi

# 无参数: 列出可用备份
if [ $# -eq 0 ]; then
    echo "可用备份:"
    ls -1d "$BACKUP_ROOT"/memory_* 2>/dev/null | while read f; do
        name=$(basename "$f")
        size=$(du -sh "$f" 2>/dev/null | cut -f1)
        echo "  $name  ($size)"
    done
    echo ""
    echo "用法: ./restore_memory.sh YYYY-MM-DD"
    echo "      ./restore_memory.sh --latest"
    exit 0
fi

# --latest: 找最近备份
if [ "$1" = "--latest" ]; then
    RESTORE_DIR=$(ls -1d "$BACKUP_ROOT"/memory_* 2>/dev/null | sort -r | head -1)
    if [ -z "$RESTORE_DIR" ]; then
        echo "❌ 无可用备份"
        exit 1
    fi
    echo "最近备份: $(basename "$RESTORE_DIR")"
else
    RESTORE_DIR="$BACKUP_ROOT/memory_$1"
fi

if [ ! -d "$RESTORE_DIR" ]; then
    echo "❌ 备份不存在: $RESTORE_DIR"
    exit 1
fi

# 确认恢复
echo "将从以下位置恢复:"
ls -lh "$RESTORE_DIR"
echo ""
echo "覆盖目标: $MEM_DIR/"
read -p "确认恢复? (y/N): " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "已取消"
    exit 0
fi

# 先备份当前(防止误操作)
CURRENT_BACKUP="${BACKUP_ROOT}/pre_restore_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$CURRENT_BACKUP"
cp -r "$MEM_DIR"/. "$CURRENT_BACKUP"/ 2>/dev/null
echo "📦 当前记忆已备份到: $CURRENT_BACKUP"

# 执行恢复
cp -r "$RESTORE_DIR"/. "$MEM_DIR"/ 2>/dev/null
echo "✅ 恢复完成: $(basename "$RESTORE_DIR") → $MEM_DIR/"
echo ""
echo "恢复内容:"
ls -lh "$MEM_DIR"
echo ""
echo "重启智能体后自动加载恢复的记忆"
