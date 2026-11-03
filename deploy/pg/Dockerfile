# AIive 记忆检索 PostgreSQL 镜像：pgvector（Dense）+ ParadeDB pg_search（BM25）
#
# 基础镜像沿用仓库既有 pgvector/pgvector:pg16，512 维向量与 HNSW 索引行为不变；
# 仅额外安装 ParadeDB 官方发布的 pg_search 扩展二进制。
#
# 版本锁定：pg_search 的包名与 PostgreSQL 主版本、发行版代号强绑定。
# 升级时同时修改 PARADEDB_VERSION 与 compose 中的镜像标签，并重跑
# tests/integration/pg/test_pg_bm25_recall.py。
ARG PG_MAJOR=16
FROM pgvector/pgvector:pg16

ARG PG_MAJOR=16
ARG PARADEDB_VERSION=0.26.4
# 构建期镜像源：默认国内镜像；境外 CI 传 --build-arg APT_MIRROR=deb.debian.org。
ARG APT_MIRROR=mirrors.aliyun.com
# 允许离线/代理环境覆盖扩展包下载地址（指向内部制品库或本地文件 URL）。
ARG PG_SEARCH_DEB_URL=""

RUN set -eux; \
    if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
        sed -i "s#deb.debian.org/debian#${APT_MIRROR}/debian#g; s#security.debian.org/debian-security#${APT_MIRROR}/debian-security#g" \
            /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null || true; \
        sed -i "s#apt.postgresql.org/pub/repos/apt#${APT_MIRROR}/postgresql/repos/apt#g" \
            /etc/apt/sources.list.d/*.list 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    arch="$(dpkg --print-architecture)"; \
    url="${PG_SEARCH_DEB_URL}"; \
    if [ -z "${url}" ]; then \
        url="https://github.com/paradedb/paradedb/releases/download/v${PARADEDB_VERSION}/postgresql-${PG_MAJOR}-pg-search_${PARADEDB_VERSION}-1PARADEDB-bookworm_${arch}.deb"; \
    fi; \
    curl -fsSL -o /tmp/pg_search.deb "${url}"; \
    apt-get install -y /tmp/pg_search.deb; \
    rm -f /tmp/pg_search.deb; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

# pg_search 必须通过 shared_preload_libraries 预加载；官方 postgres 镜像会把它
# 追加进 postgresql.conf.sample，容器 initdb 之后仍然生效。
RUN echo "shared_preload_libraries = 'pg_search'" >> /usr/share/postgresql/${PG_MAJOR}/postgresql.conf.sample
