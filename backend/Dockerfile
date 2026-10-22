FROM python:3.12-slim

WORKDIR /app

# 构建期镜像源：默认国内镜像（本地与国内服务器），CI 用 build-arg 切到官方源。
# 境外 runner 访问国内镜像会因跨境网络超时导致 docker compose build 失败。
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple/

# 编译工具：部分 Python 包可能在本机构建；含 curl 便于排查
# 先将 apt 源切换到国内镜像（避免 deb.debian.org 在国内拉取超时）
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s#deb.debian.org/debian#$APT_MIRROR/debian#g" \
            /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc curl \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖声明与迁移文件，最大化利用 Docker 层缓存
COPY pyproject.toml ./
COPY backend ./backend
COPY alembic.ini ./

# 安装依赖（默认腾讯云 PyPI 镜像加速，适配国内服务器）
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" .

# pydantic-settings 默认会读取 .env；放一个空文件消除告警。
# 真实密钥由 compose 的 env_file 以环境变量形式注入，不会写进镜像层。
RUN : > /app/.env

EXPOSE 8000

CMD ["sh", "-c", "uvicorn aiive.main:app --host 0.0.0.0 --port 8000"]
