FROM python:3.12-slim

LABEL maintainer="ops-team"
LABEL description="交换机智能巡检告警系统 - 实时日志监控 + 钉钉告警 + AI 分析"

# 安装系统依赖（NFS 客户端工具、时区数据）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nfs-common \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 工作目录
WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY monitor.py .
COPY notifier.py .
COPY analyzer.py .
COPY reviewer.py .
COPY config.yaml .
COPY patterns/ ./patterns/

# 创建数据目录
RUN mkdir -p /var/log/switches /app/state /app/archive /app/logs

# 入口脚本
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# 健康检查：检查进程是否存活
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "monitor.py" > /dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["daemon"]
