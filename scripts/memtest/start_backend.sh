#!/bin/sh
# 等 PostgreSQL 就绪 → 建库 → 跑迁移 → 启动 uvicorn（单 worker，避免重复加载模型/进程）。
set -eu
until pg_isready -h 127.0.0.1 -p 5432 -U aiive >/dev/null 2>&1; do sleep 1; done
psql -h 127.0.0.1 -U aiive -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='aiive'" | grep -q 1 \
    || psql -h 127.0.0.1 -U aiive -d postgres -c "CREATE DATABASE aiive"
cd /app && alembic upgrade head
# 容器内监听 0.0.0.0：宿主侧只把 18000 发布到 127.0.0.1，不对外暴露。
exec uvicorn aiive.main:app --host 0.0.0.0 --port 8000 --workers 1
