#!/bin/sh
# 初始化 PostgreSQL 数据目录（只在首次启动执行），随后交给 supervisord 托管三个服务。
set -eu

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
mkdir -p "$PGDATA" /data /var/log
chown -R postgres:postgres "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[memtest] 初始化 PostgreSQL 数据目录"
    su postgres -c "initdb -D '$PGDATA' -U aiive --auth=trust -E UTF8" >/var/log/initdb.log 2>&1
    # 只监听回环；shared_buffers 收敛到 128MB，避免默认值在 2 GiB 预算里占太多
    cat >> "$PGDATA/postgresql.conf" <<'EOF'
listen_addresses = '127.0.0.1'
shared_buffers = 128MB
max_connections = 40
work_mem = 4MB
maintenance_work_mem = 32MB
fsync = off
synchronous_commit = off
full_page_writes = off
EOF
fi

exec "$@"
