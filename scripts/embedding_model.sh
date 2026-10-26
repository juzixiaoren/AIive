#!/usr/bin/env sh
# 本地 Embedding 模型缓存管理：检测模型是否已下载，缺失时自动下载并启动服务。
#
# 用法：
#   scripts/embedding_model.sh status     # 输出 cached / missing（missing 时退出码 1）
#   scripts/embedding_model.sh path       # 输出缓存目录
#   scripts/embedding_model.sh image-tag  # 输出 TEI 镜像 tag（按 CPU 架构选择）
#   scripts/embedding_model.sh ensure     # 模型缺失则下载，并确保 embedding 服务已启动
#
# 配置与后端保持一致（环境变量优先，其次 .env）：
#   AIIVE_EMBEDDING_MODEL / AIIVE_EMBEDDING_MODEL_REVISION
#   AIIVE_LOCAL_EMBEDDING_CACHE_DIR（默认 .data/models/embeddings）
#   AIIVE_HF_ENDPOINT（单一下载源，设置后不再尝试候选列表）
#   AIIVE_HF_ENDPOINT_CANDIDATES（逗号分隔，按顺序尝试，默认官方源 + hf-mirror）
#   AIIVE_LOCAL_EMBEDDING_STARTUP_TIMEOUT_SECONDS（单个下载源的等待上限，默认 600）
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
cd "$PROJECT_DIR"

ENV_FILE=.env

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

config_value() {
    key=$1
    default=$2
    value=$(printenv "$key" 2>/dev/null || true)
    if [ -z "$value" ]; then
        value=$(read_env "$key")
    fi
    if [ -z "$value" ]; then
        value=$default
    fi
    printf '%s' "$value"
}

model_id=$(config_value AIIVE_EMBEDDING_MODEL "BAAI/bge-small-zh-v1.5")
revision=$(config_value AIIVE_EMBEDDING_MODEL_REVISION "")
cache_dir=$(config_value AIIVE_LOCAL_EMBEDDING_CACHE_DIR ".data/models/embeddings")
timeout_seconds=$(config_value AIIVE_LOCAL_EMBEDDING_STARTUP_TIMEOUT_SECONDS "600")

# Hugging Face 缓存布局：<cache>/models--<org>--<name>/snapshots/<commit>/...
repo_dir="$cache_dir/models--$(printf '%s' "$model_id" | sed 's|/|--|g')"

endpoint_candidates() {
    single=$(config_value AIIVE_HF_ENDPOINT "")
    if [ -n "$single" ]; then
        printf '%s' "$single"
        return 0
    fi
    printf '%s' "$(config_value AIIVE_HF_ENDPOINT_CANDIDATES "https://huggingface.co,https://hf-mirror.com")" | tr ',' ' '
}

snapshot_dir() {
    if [ -n "$revision" ]; then
        ref_file="$repo_dir/refs/$revision"
        if [ -s "$ref_file" ]; then
            printf '%s' "$repo_dir/snapshots/$(cat "$ref_file")"
            return 0
        fi
        printf '%s' "$repo_dir/snapshots/$revision"
        return 0
    fi
    for candidate in "$repo_dir"/snapshots/*; do
        if [ -d "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    printf '%s' "$repo_dir/snapshots"
}

# 只有配置文件、分词器和权重都落地才算下载完成：hf-hub 先写 blobs，
# 全部完成才在 snapshots 下建软链，因此软链可读即代表可用。
snapshot_complete() {
    dir=$1
    [ -d "$dir" ] || return 1
    [ -s "$dir/config.json" ] || return 1
    if [ ! -s "$dir/tokenizer.json" ] && [ ! -s "$dir/vocab.txt" ]; then
        return 1
    fi
    if [ ! -s "$dir/model.safetensors" ] && [ ! -s "$dir/pytorch_model.bin" ] && [ ! -s "$dir/onnx/model.onnx" ]; then
        return 1
    fi
    return 0
}

cache_size() {
    if [ -d "$cache_dir" ]; then
        du -sh "$cache_dir" 2>/dev/null | awk '{print $1}'
    else
        printf '0'
    fi
}

resolve_image_tag() {
    configured=$(config_value AIIVE_TEI_IMAGE_TAG "")
    if [ -n "$configured" ]; then
        printf '%s' "$configured"
        return 0
    fi
    case "$(uname -m)" in
        arm64|aarch64) printf 'cpu-arm64-1.9' ;;
        *) printf 'cpu-1.9' ;;
    esac
}

embedding_running() {
    docker compose --profile local-embedding ps --status running --services 2>/dev/null | grep -qx embedding
}

# 在单个下载源上等待模型落地；容器退出（网络/鉴权失败）或超时即判定失败。
wait_for_model() {
    elapsed=0
    while :; do
        if snapshot_complete "$(snapshot_dir)"; then
            return 0
        fi
        if ! embedding_running; then
            echo "  下载源不可用（embedding 容器已退出），尝试下一个源。"
            return 1
        fi
        if [ "$elapsed" -ge "$timeout_seconds" ]; then
            echo "  下载超时（${timeout_seconds}s），尝试下一个源。"
            return 1
        fi
        if [ $((elapsed % 10)) -eq 0 ]; then
            echo "  下载中（${elapsed}s，缓存 $(cache_size)）..."
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
}

start_embedding() {
    AIIVE_TEI_IMAGE_TAG=$(resolve_image_tag)
    export AIIVE_TEI_IMAGE_TAG
    docker compose --profile local-embedding up -d "$@" embedding
}

ensure_model() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "错误：未找到 docker，无法启动本地 Embedding 服务。" >&2
        return 1
    fi

    if snapshot_complete "$(snapshot_dir)"; then
        echo "检测到本地 Embedding 模型缓存，跳过下载：$(snapshot_dir)"
        start_embedding
        return 0
    fi

    mkdir -p "$cache_dir"
    echo "未检测到本地 Embedding 模型，开始自动下载..."
    echo "  模型：$model_id$revision_suffix"
    echo "  缓存：$cache_dir"

    for endpoint in $(endpoint_candidates); do
        echo "  下载源：$endpoint"
        AIIVE_HF_ENDPOINT=$endpoint
        export AIIVE_HF_ENDPOINT
        start_embedding --force-recreate
        if wait_for_model; then
            echo "模型下载完成（缓存大小 $(cache_size)）。"
            return 0
        fi
    done

    echo "错误：所有下载源均未能完成模型下载。可手动预置缓存，或检查网络后重试。" >&2
    docker compose --profile local-embedding logs --tail=60 embedding >&2 || true
    return 1
}

revision_suffix=
if [ -n "$revision" ]; then
    revision_suffix="@$revision"
fi

action=${1:-status}
case "$action" in
    path) printf '%s\n' "$cache_dir" ;;
    image-tag) printf '%s\n' "$(resolve_image_tag)" ;;
    status)
        if snapshot_complete "$(snapshot_dir)"; then
            echo "cached $(snapshot_dir)"
        else
            echo "missing $(snapshot_dir)"
            exit 1
        fi
        ;;
    ensure) ensure_model ;;
    *)
        echo "用法：$0 [status|path|image-tag|ensure]" >&2
        exit 2
        ;;
esac
