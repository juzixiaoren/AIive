#!/usr/bin/env bash
# PostgreSQL 容器入口：启动前为已有数据卷补齐 pg_search 预加载配置。
#
# 官方 postgres 镜像只在 initdb 时生成 postgresql.conf；已有 PGDATA 升级镜像后
# shared_preload_libraries 不会更新，pg_search 无法加载、CREATE EXTENSION 失败。
# 这里在启动前调用 ensure-preload.sh（幂等、只追加配置、不动数据文件），
# 然后交给官方 entrypoint 继续处理。
set -euo pipefail

PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"
if ! /usr/local/bin/ensure-preload.sh "${PGDATA_DIR}/postgresql.conf"; then
    echo "aiive: 补齐 shared_preload_libraries 失败，pg_search 可能不可用" >&2
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
