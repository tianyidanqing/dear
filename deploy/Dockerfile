# 后端 API 服务镜像（虚拟机 / 容器通用）
# 构建：docker build -f deploy/Dockerfile -t schedule-agent-api .
# 运行：docker run -d -p 8000:8000 -v schedule-data:/var/lib/schedule-agent schedule-agent-api
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCHEDULE_DATA_DIR=/var/lib/schedule-agent

WORKDIR /app

# 先只装依赖，充分利用镜像层缓存
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"

# 运行期数据独立于镜像，容器重建不丢（VM 上可换成 bind mount 到数据盘）
VOLUME ["/var/lib/schedule-agent"]
EXPOSE 8000

# 非 root 运行：容器/VM 安全基线
RUN useradd -r -u 10001 -m -d /var/lib/schedule-agent agent \
    && chown -R agent:agent /var/lib/schedule-agent
USER agent

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["schedule-agent-api"]
