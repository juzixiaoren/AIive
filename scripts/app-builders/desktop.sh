#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_DIR="$PROJECT_DIR/apps/desktop"
OUTPUT_DIR="$PROJECT_DIR/artifacts/apps/desktop"
variant=${1:-debug}

case "$variant" in
    debug)
        forge_command=package
        ;;
    release)
        forge_command=make
        ;;
    *)
        echo "错误：Desktop variant 仅支持 debug 或 release。" >&2
        exit 2
        ;;
esac

for command_name in node npm python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：未找到 $command_name。" >&2
        exit 1
    fi
done

echo "生成桌面端品牌资源..."
python3 "$PROJECT_DIR/scripts/prepare_brand_assets.py"

if [ ! -d "$APP_DIR/node_modules" ]; then
    echo "安装 Electron 构建依赖..."
    (cd "$APP_DIR" && npm ci)
fi

echo "构建 Electron Desktop $variant..."
(cd "$APP_DIR" && npm run "$forge_command")

mkdir -p "$OUTPUT_DIR"
if [ "$variant" = "release" ]; then
    cp -R "$APP_DIR/out/make/." "$OUTPUT_DIR/"
else
    cp -R "$APP_DIR/out/." "$OUTPUT_DIR/"
fi
echo "Electron Desktop 产物已生成：$OUTPUT_DIR"
