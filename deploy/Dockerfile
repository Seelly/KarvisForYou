FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 安装依赖（利用 Docker 缓存，先复制依赖描述文件）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 复制代码（含 web_static/ 前端页面）
COPY src/ ./src/

# 创建数据目录（多用户数据 + 系统数据）
RUN mkdir -p /app/data/_karvis_system

# 设置数据目录环境变量
ENV DATA_DIR=/app/data

WORKDIR /app/src

# 端口
EXPOSE 9000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/web/login')" || exit 1

# 启动（内置调度器自动启动）
CMD ["uv", "run", "--no-sync", "python", "app.py"]
