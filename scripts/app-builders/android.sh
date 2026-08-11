#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_DIR="$PROJECT_DIR/apps/android"
OUTPUT_DIR="$PROJECT_DIR/artifacts/apps/android"
GRADLE_CACHE_DIR="$PROJECT_DIR/.gradle-cache"
variant=${1:-debug}

case "$variant" in
    debug)
        gradle_task=assembleDebug
        apk_name=app-debug.apk
        output_name=AIive-debug.apk
        ;;
    release)
        gradle_task=assembleRelease
        apk_name=app-release-unsigned.apk
        output_name=AIive-release-unsigned.apk
        ;;
    *)
        echo "错误：Android variant 仅支持 debug 或 release。" >&2
        exit 2
        ;;
esac

for command_name in node npm python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：未找到 $command_name。" >&2
        exit 1
    fi
done

if ! python3 -c "from PIL import Image" >/dev/null 2>&1; then
    echo "错误：缺少 Pillow。请先在项目根目录执行 pip install -e ." >&2
    exit 1
fi

if [ ! -d "$APP_DIR/node_modules" ]; then
    echo "安装 Android 前端依赖..."
    (cd "$APP_DIR" && npm ci)
fi

# Android Gradle Plugin 需要 JDK 21。可用 AIIVE_JAVA_HOME 显式指定；macOS
# 安装 Android Studio 后优先复用其 JBR，避免继承到系统预设的 Java 17。
studio_jdk=/Applications/Android\ Studio.app/Contents/jbr/Contents/Home
if [ -n "${AIIVE_JAVA_HOME:-}" ]; then
    export JAVA_HOME="$AIIVE_JAVA_HOME"
elif [ -x "$studio_jdk/bin/java" ]; then
    export JAVA_HOME="$studio_jdk"
fi

if [ -z "${JAVA_HOME:-}" ] || [ ! -x "$JAVA_HOME/bin/java" ]; then
    echo "错误：未找到 Java 21，请安装 Android Studio 或设置 JAVA_HOME。" >&2
    exit 1
fi

java_major=$(
    "$JAVA_HOME/bin/java" -version 2>&1 \
        | awk -F'[\".]' '/version/ { print $2; exit }'
)
if [ "${java_major:-0}" -lt 21 ]; then
    echo "错误：Android 构建要求 Java 21+，当前 JAVA_HOME 为 $JAVA_HOME。" >&2
    exit 1
fi

android_sdk=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}
if [ -z "$android_sdk" ] && [ -d "$HOME/Library/Android/sdk" ]; then
    android_sdk="$HOME/Library/Android/sdk"
fi
if [ -z "$android_sdk" ] || [ ! -d "$android_sdk" ]; then
    echo "错误：未找到 Android SDK，请设置 ANDROID_SDK_ROOT。" >&2
    exit 1
fi
export ANDROID_SDK_ROOT="$android_sdk"
export GRADLE_USER_HOME="$GRADLE_CACHE_DIR"

echo "生成品牌资源并同步 Android 工程..."
python3 "$PROJECT_DIR/scripts/prepare_brand_assets.py"
(cd "$APP_DIR" && npm run sync)

echo "构建 Android $variant APK..."
(cd "$APP_DIR/android" && ./gradlew --no-daemon "$gradle_task")

source_apk="$APP_DIR/android/app/build/outputs/apk/$variant/$apk_name"
if [ ! -f "$source_apk" ]; then
    echo "错误：Gradle 已结束，但未找到 APK：$source_apk" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
cp "$source_apk" "$OUTPUT_DIR/$output_name"
echo "APK 已生成：$OUTPUT_DIR/$output_name"
