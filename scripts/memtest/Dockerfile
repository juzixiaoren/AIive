# 单容器 2 GiB 内存实测环境：PostgreSQL 16 + pgvector、TEI 本地 Embedding、AIive Backend
#
# 为什么把三件事塞进一个容器：cgroup 内存限制只能按"整机"施加，只有让全部常驻
# 进程共享同一个 cgroup，2 GiB 上限才是对整套服务的约束（而不是每容器各 2 GiB）。
# 容器内 /sys/fs/cgroup/memory.{current,peak,events,swap.current} 即整套服务的真实用量。
#
# 构建：docker build -f scripts/memtest/Dockerfile -t aiive-memtest .
# 运行：见 scripts/memtest/run_memtest.sh
# 固定 bookworm 变体：pgvector/pgvector:pg16 基础镜像是 Debian 12（glibc 2.36），
# 默认 python:3.12-slim 已切到 trixie（glibc 2.41），混用会找不到 GLIBC_2.38。
FROM python:3.12-slim-bookworm AS python_runtime

FROM pgvector/pgvector:pg16

# 与基础镜像同为 Debian bookworm，直接搬 Python 3.12 运行时（避免自建编译链）
COPY --from=python_runtime /usr/local /usr/local
# TEI 推理进程（Rust 单二进制），与 compose 中的 embedding 服务同版本
COPY --from=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 \
     /usr/local/bin/text-embeddings-router /usr/local/bin/text-embeddings-router
# TEI 的 CPU 后端依赖镜像内的 OpenMP 运行库，单搬二进制会缺 libiomp5.so
COPY --from=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 \
     /usr/lib/x86_64-linux-gnu/libiomp5.so /usr/lib/x86_64-linux-gnu/libiomp5.so
COPY --from=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 \
     /usr/lib/x86_64-linux-gnu/libomp.so.5 /usr/lib/x86_64-linux-gnu/libomp.so.5

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple/
RUN if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
        sed -i "s#deb.debian.org/debian#${APT_MIRROR}/debian#g" \
            /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl supervisor procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 先装依赖（只依赖 pyproject + backend 包），再拷脚本：
# 改脚本不会让 pip 层失效，重复构建只需几秒。
COPY pyproject.toml ./
COPY backend ./backend
COPY alembic.ini ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" . && : > /app/.env
COPY scripts/rag_eval ./scripts/rag_eval
COPY scripts/memtest ./scripts/memtest

ENV PGDATA=/var/lib/postgresql/data
ENV PYTHONUNBUFFERED=1

COPY scripts/memtest/supervisord.conf /etc/supervisord.conf
COPY scripts/memtest/entrypoint.sh /usr/local/bin/memtest-entrypoint.sh
RUN chmod +x /usr/local/bin/memtest-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/memtest-entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/supervisord.conf"]
