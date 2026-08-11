#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BUILDERS_DIR="$SCRIPT_DIR/app-builders"

usage() {
    echo "用法: ./scripts/build-app.sh <target> [variant]"
    echo ""
    echo "可用 target:"
    echo "  android [debug|release]  构建 Android APK（默认 debug）"
    echo "  desktop [debug|release]  构建 Electron 桌面应用（默认 debug）"
    echo "  list                     列出已注册的应用构建器"
    echo ""
    echo "其他应用形态可通过新增 scripts/app-builders/<target>.sh 接入。"
}

target=${1:-help}
variant=${2:-debug}

case "$target" in
    help|-h|--help)
        usage
        ;;
    list)
        for builder in "$BUILDERS_DIR"/*.sh; do
            [ -f "$builder" ] || continue
            basename "$builder" .sh
        done
        ;;
    *)
        builder="$BUILDERS_DIR/$target.sh"
        if [ ! -x "$builder" ]; then
            echo "错误：未知或不可执行的应用构建目标：$target" >&2
            usage >&2
            exit 2
        fi
        exec "$builder" "$variant"
        ;;
esac
