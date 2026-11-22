#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
cd "$PROJECT_DIR"

ENV_FILE=.env

if [ ! -f "$ENV_FILE" ]; then
    echo "错误：未找到 .env，请先执行 cp .env.example .env 并完成配置。" >&2
    exit 1
fi

read_env() {
    key=$1
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi
    awk -F= -v wanted="$key" '
        /^[[:space:]]*#/ { next }
        {
            candidate=$1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", candidate)
            if (candidate == wanted) {
                sub(/^[^=]*=/, "")
                gsub(/\r$/, "")
                gsub(/^[[:space:]]+|[[:space:]]+$/, "")
                gsub(/^["'\'']|["'\'']$/, "")
                print
                exit
            }
        }
    ' "$ENV_FILE"
}

is_true() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未找到 docker，请先安装并启动 Docker。" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "错误：当前 Docker 未提供 compose 子命令。" >&2
    exit 1
fi

memory_vector=$(read_env AIIVE_MEMORY_VECTOR_ENABLED)
memory_vector=${AIIVE_MEMORY_VECTOR_ENABLED:-${memory_vector:-false}}
knowledge_vector=$(read_env AIIVE_KNOWLEDGE_VECTOR_ENABLED)
knowledge_vector=${AIIVE_KNOWLEDGE_VECTOR_ENABLED:-${knowledge_vector:-false}}
tool_vector=$(read_env AIIVE_TOOL_DISCOVERY_VECTOR_ENABLED)
tool_vector=${AIIVE_TOOL_DISCOVERY_VECTOR_ENABLED:-${tool_vector:-false}}
embedding_provider=${AIIVE_EMBEDDING_PROVIDER:-$(read_env AIIVE_EMBEDDING_PROVIDER)}
embedding_provider=${embedding_provider:-openai_compatible}

# 任一向量功能开启且使用本地模型时，拉起 TEI 服务。
local_vector=false
required_vector=false
if is_true "$memory_vector" || is_true "$knowledge_vector"; then
    required_vector=true
fi
if [ "$embedding_provider" = "local" ]; then
    if [ "$required_vector" = "true" ] || is_true "$tool_vector"; then
        local_vector=true
    fi
fi

if [ "$local_vector" = "true" ]; then
    # 后端容器必须通过 Compose 服务名访问，不能使用宿主机的 127.0.0.1。
    export AIIVE_DOCKER_EMBEDDING_BASE_URL=http://embedding:80/v1
    export AIIVE_TEI_IMAGE_TAG=$(sh "$SCRIPT_DIR/embedding_model.sh" image-tag)

    embedding_port=${AIIVE_LOCAL_EMBEDDING_PORT:-$(read_env AIIVE_LOCAL_EMBEDDING_PORT)}
    embedding_port=${embedding_port:-8081}
    wait_seconds=${AIIVE_LOCAL_EMBEDDING_STARTUP_TIMEOUT_SECONDS:-$(read_env AIIVE_LOCAL_EMBEDDING_STARTUP_TIMEOUT_SECONDS)}
    wait_seconds=${wait_seconds:-600}

    echo "启动 PostgreSQL..."
    docker compose up -d postgres

    # 检测本地模型缓存：缺失时自动下载（约 92MB），已有缓存直接复用；
    # 随后幂等启动 embedding 容器（下载由该容器完成）。
    wait_for_embedding() {
        command -v curl >/dev/null 2>&1 || return 1
        elapsed=0
        until curl --fail --silent --show-error \
            "http://127.0.0.1:${embedding_port}/health" >/dev/null 2>&1; do
            if [ "$elapsed" -ge "$wait_seconds" ]; then
                return 1
            fi
            sleep 2
            elapsed=$((elapsed + 2))
        done
    }
    if sh "$SCRIPT_DIR/embedding_model.sh" ensure && wait_for_embedding; then
        echo "本地 Embedding 服务已就绪，启动 AIive..."
    elif [ "$required_vector" = "true" ]; then
        echo "错误：本地 Embedding 服务未就绪，记忆或知识库向量启动中止。" >&2
        docker compose --profile local-embedding logs --tail=100 embedding >&2 || true
        exit 1
    else
        echo "本地 Embedding 服务未就绪，启动 AIive 并以关键词提供工具检索。" >&2
    fi
    docker compose --profile local-embedding up -d backend frontend
else
    # 若此前启用过本地模型，关闭可选容器以释放内存；其模型缓存仍保留。
    docker compose --profile local-embedding stop embedding >/dev/null 2>&1 || true
    docker compose up -d
fi
