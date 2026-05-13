#!/bin/bash
# 交换机智能巡检告警系统 - 容器入口脚本
# 支持 daemon / scan / test 三种运行模式

set -e

# === 环境变量默认值 ===
MODE="${1:-daemon}"
CONFIG="/app/config.yaml"

# === 检查配置文件 ===
if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 配置文件不存在: $CONFIG"
    echo "请挂载 config.yaml 到 /app/config.yaml"
    exit 1
fi

# === 检查 syslog 目录是否挂载 ===
SYSLOG_DIR=$(python3 -c "
import yaml
with open('$CONFIG', 'r') as f:
    c = yaml.safe_load(f)
print(c.get('syslog', {}).get('base_dir', '/var/log/switches'))
" 2>/dev/null || echo "/var/log/switches")

if [ ! -d "$SYSLOG_DIR" ]; then
    echo "WARNING: syslog 目录不存在: $SYSLOG_DIR"
    echo "请确保已通过 NFS 挂载或卷映射该目录"
fi

# === 创建必要目录 ===
mkdir -p /app/state /app/archive /app/logs

# === 打印启动信息 ===
echo "========================================"
echo "  交换机智能巡检告警系统"
echo "========================================"
echo "  运行模式:  $MODE"
echo "  配置文件:  $CONFIG"
echo "  Syslog目录: $SYSLOG_DIR"
echo "  时间:      $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "========================================"

# === 执行 ===
case "$MODE" in
    daemon)
        echo "[启动] 持续监控模式..."
        exec python3 -u monitor.py -c "$CONFIG" -m daemon
        ;;
    scan)
        echo "[启动] 一次性全量扫描..."
        exec python3 -u monitor.py -c "$CONFIG" -m scan
        ;;
    test)
        echo "[启动] 通知测试..."
        exec python3 -u monitor.py -c "$CONFIG" -m test
        ;;
    *)
        echo "ERROR: 未知运行模式: $MODE"
        echo "用法: docker run ... <daemon|scan|test>"
        exit 1
        ;;
esac
