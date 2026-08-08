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

vector_enabled=${AIIVE_MEMORY_VECTOR_ENABLED:-$(read_env AIIVE_MEMORY_VECTOR_ENABLED)}
embedding_provider=${AIIVE_EMBEDDING_PROVIDER:-$(read_env AIIVE_EMBEDDING_PROVIDER)}
embedding_provider=${embedding_provider:-openai_compatible}

if is_true "${vector_enabled:-false}" && [ "$embedding_provider" = "local" ]; then
    mkdir -p .data/models/embeddings
    # 后端容器必须通过 Compose 服务名访问，不能使用宿主机的 127.0.0.1。
    export AIIVE_DOCKER_EMBEDDING_BASE_URL=http://embedding:80/v1

    configured_tag=${AIIVE_TEI_IMAGE_TAG:-$(read_env AIIVE_TEI_IMAGE_TAG)}
    if [ -z "$configured_tag" ]; then
        case "$(uname -m)" in
            arm64|aarch64) configured_tag=cpu-arm64-1.9 ;;
            *) configured_tag=cpu-1.9 ;;
        esac
    fi
    export AIIVE_TEI_IMAGE_TAG=$configured_tag

    embedding_port=${AIIVE_LOCAL_EMBEDDING_PORT:-$(read_env AIIVE_LOCAL_EMBEDDING_PORT)}
    embedding_port=${embedding_port:-8081}
    wait_seconds=${AIIVE_LOCAL_EMBEDDING_STARTUP_TIMEOUT_SECONDS:-$(read_env AIIVE_LOCAL_EMBEDDING_STARTUP_TIMEOUT_SECONDS)}
    wait_seconds=${wait_seconds:-600}

    echo "启动 PostgreSQL 和本地 Embedding 服务（首次运行会自动下载模型）..."
    docker compose --profile local-embedding up -d postgres embedding

    if ! command -v curl >/dev/null 2>&1; then
        echo "错误：启动检查需要 curl。" >&2
        exit 1
    fi

    elapsed=0
    until curl --fail --silent --show-error \
        "http://127.0.0.1:${embedding_port}/health" >/dev/null 2>&1; do
        if [ "$elapsed" -ge "$wait_seconds" ]; then
            echo "错误：本地 Embedding 服务在 ${wait_seconds}s 内未就绪。" >&2
            docker compose --profile local-embedding logs --tail=100 embedding >&2
            exit 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "本地 Embedding 服务已就绪，启动 AIive..."
    docker compose --profile local-embedding up -d backend frontend
else
    # 若此前启用过本地模型，关闭可选容器以释放内存；其模型缓存仍保留。
    docker compose --profile local-embedding stop embedding >/dev/null 2>&1 || true
    docker compose up -d
fi
