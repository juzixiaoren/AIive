#!/usr/bin/env bash
# 在本地单容器 2 GiB cgroup（关闭 swap）中启动整套 AIive 服务并施加真实负载。
#
# 用法：scripts/memtest/run_memtest.sh
# 可选环境变量：
#   TEI_MAX_CONCURRENCY（默认 4）  TEI_MAX_BATCH_TOKENS（默认 2048）
#   AIIVE_MEMTEST_LIMIT（默认 2g）  SKIP_BUILD=1 复用已构建镜像
#
# 验收口径：容器 cgroup 上限即整套服务上限（postgres + embedding + backend），
# 不访问任何远端服务器，模型来自宿主机 .data/models/embeddings 持久缓存。
set -euo pipefail
# Git Bash 默认把容器内绝对路径（/app/...）改写成 Windows 路径，必须关闭
export MSYS_NO_PATHCONV=1

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

IMAGE=aiive-memtest
CONTAINER=aiive-memtest
LIMIT="${AIIVE_MEMTEST_LIMIT:-2g}"
TEI_MAX_CONCURRENCY="${TEI_MAX_CONCURRENCY:-4}"
TEI_MAX_BATCH_TOKENS="${TEI_MAX_BATCH_TOKENS:-2048}"
EMB_MODEL="${AIIVE_EMBEDDING_MODEL:-BAAI/bge-small-zh-v1.5}"
EMB_REVISION="${AIIVE_EMBEDDING_MODEL_REVISION:-534b6bfaaf500e70bcb9f3771cebc940c23b219d}"
OUT_DIR="$REPO_ROOT/.data/memtest"
mkdir -p "$OUT_DIR"

if [ "${SKIP_BUILD:-0}" != "1" ]; then
    echo "[1/6] 构建镜像 $IMAGE"
    docker build -f scripts/memtest/Dockerfile -t "$IMAGE" .
fi

echo "[2/6] 清理旧容器并启动（memory=$LIMIT, swap 关闭）"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
MODEL_DIR="$(cd .data/models/embeddings && pwd -W 2>/dev/null || pwd)"
START_TS=$(date +%s)
docker run -d --name "$CONTAINER" \
    --memory="$LIMIT" --memory-swap="$LIMIT" --cpus=4 \
    -p 127.0.0.1:18000:8000 -p 127.0.0.1:18081:8081 \
    -v "$MODEL_DIR:/data" \
    -e EMB_MODEL="$EMB_MODEL" \
    -e EMB_REVISION="$EMB_REVISION" \
    -e TEI_MAX_CONCURRENCY="$TEI_MAX_CONCURRENCY" \
    -e TEI_MAX_BATCH_TOKENS="$TEI_MAX_BATCH_TOKENS" \
    -e DATABASE_URL="postgresql+psycopg://aiive@127.0.0.1:5432/aiive" \
    -e HOST=127.0.0.1 -e PORT=8000 \
    -e AIIVE_MEMORY_VECTOR_ENABLED=true \
    -e AIIVE_KNOWLEDGE_VECTOR_ENABLED=true \
    -e AIIVE_EMBEDDING_PROVIDER=local \
    -e AIIVE_EMBEDDING_BASE_URL="http://127.0.0.1:8081/v1" \
    -e AIIVE_EMBEDDING_API_KEY="" \
    -e AIIVE_EMBEDDING_MODEL="$EMB_MODEL" \
    -e AIIVE_EMBEDDING_MODEL_REVISION="$EMB_REVISION" \
    -e AIIVE_EMBEDDING_DIMENSIONS=512 \
    -e AIIVE_EMBEDDING_QUERY_PREFIX="为这个句子生成表示以用于检索相关文章：" \
    -e AIIVE_EMBEDDING_TIMEOUT_SECONDS=10 \
    -e AIIVE_EMBEDDING_BATCH_SIZE=4 \
    -e AIIVE_KNOWLEDGE_VECTOR_MIN_SIMILARITY=0.30 \
    "$IMAGE" >/dev/null

echo "[3/6] 等待 Embedding 与 Backend 就绪（记录启动耗时）"
EMB_READY=0
for _ in $(seq 1 600); do
    if curl -sf -m 2 http://127.0.0.1:18081/health >/dev/null 2>&1; then
        EMB_READY=$(( $(date +%s) - START_TS ))
        break
    fi
    sleep 1
done
BACKEND_READY=0
for _ in $(seq 1 600); do
    if curl -sf -m 2 http://127.0.0.1:18000/health >/dev/null 2>&1; then
        BACKEND_READY=$(( $(date +%s) - START_TS ))
        break
    fi
    sleep 1
done
if [ "$EMB_READY" = "0" ] || [ "$BACKEND_READY" = "0" ]; then
    echo "服务未在超时内就绪，导出日志排查：" >&2
    docker logs --tail=80 "$CONTAINER" >&2
    exit 1
fi
echo "    embedding 就绪: ${EMB_READY}s, backend 就绪: ${BACKEND_READY}s"

echo "[4/6] 容器内执行真实负载（知识重建 / 记忆向量 / 混合检索，并发 ${MEMTEST_CONCURRENCY:-4}）"
docker exec \
    -e MEMTEST_DOC_VARIANTS="${MEMTEST_DOC_VARIANTS:-1}" \
    -e MEMTEST_MEMORIES="${MEMTEST_MEMORIES:-30}" \
    -e MEMTEST_QUERY_ROUNDS="${MEMTEST_QUERY_ROUNDS:-4}" \
    -e MEMTEST_CONCURRENCY="${MEMTEST_CONCURRENCY:-4}" \
    "$CONTAINER" python /app/scripts/memtest/load_test.py

echo "[5/6] 收集 cgroup 证据"
{
    echo "== memory.max =="; docker exec "$CONTAINER" cat /sys/fs/cgroup/memory.max
    echo "== memory.current =="; docker exec "$CONTAINER" cat /sys/fs/cgroup/memory.current
    echo "== memory.peak =="; docker exec "$CONTAINER" cat /sys/fs/cgroup/memory.peak
    echo "== memory.swap.max =="; docker exec "$CONTAINER" cat /sys/fs/cgroup/memory.swap.max
    echo "== memory.swap.current =="; docker exec "$CONTAINER" cat /sys/fs/cgroup/memory.swap.current
    echo "== memory.events =="; docker exec "$CONTAINER" cat /sys/fs/cgroup/memory.events
    echo "== memory.stat (anon/file) =="; docker exec "$CONTAINER" grep -E '^(anon|file) ' /sys/fs/cgroup/memory.stat
} | tee "$OUT_DIR/cgroup.txt"

echo "[6/6] 导出容器内报告与日志"
# docker cp 的目标必须是 Windows 路径（MSYS_NO_PATHCONV 下不会自动转换）
OUT_DIR_WIN="$(cd "$OUT_DIR" && pwd -W 2>/dev/null || echo "$OUT_DIR")"
docker cp "$CONTAINER:/app/.data/memtest/report.json" "$OUT_DIR_WIN/report.json"
docker exec "$CONTAINER" sh -c 'cat /var/log/embedding.log | tail -20' > "$OUT_DIR/embedding.log" 2>&1 || true
docker exec "$CONTAINER" sh -c 'cat /var/log/backend.log | tail -40' > "$OUT_DIR/backend.log" 2>&1 || true
docker stats --no-stream --format '{{.Name}} MEM={{.MemUsage}} PEAK={{.MemPerc}}' "$CONTAINER" | tee "$OUT_DIR/docker_stats.txt"

echo "证据已写入 $OUT_DIR"
