#!/usr/bin/env bash
# 为已存在的 PGDATA 补齐 shared_preload_libraries = 'pg_search'。
#
# 背景：pg_search 必须通过 shared_preload_libraries 预加载，否则
# CREATE EXTENSION pg_search 直接失败。官方 postgres 镜像只在 initdb 时使用
# postgresql.conf.sample 生成配置，已有数据卷升级镜像不会重跑 initdb，配置里
# 因此没有这一行。本脚本在 PostgreSQL 启动前幂等地补齐配置：
#
#   * 不删除、不重建 PGDATA，只向 postgresql.conf 追加配置行；
#   * 已包含 pg_search 时不做任何改动（重复执行安全）；
#   * 已有其它预加载库时保留原值并追加 pg_search（后出现的赋值生效）。
#
# 用法: ensure-preload.sh <postgresql.conf 路径>
set -euo pipefail

LIB="pg_search"
CONF="${1:-}"

if [ -z "$CONF" ]; then
    echo "用法: ensure-preload.sh <postgresql.conf 路径>" >&2
    exit 2
fi

# 数据卷为空（全新部署）时 postgresql.conf 还不存在：initdb 会用镜像里已经
# 追加过 pg_search 的 postgresql.conf.sample 生成配置，无需处理。
[ -s "$CONF" ] || exit 0

current=""
if grep -Eq "^[[:space:]]*shared_preload_libraries[[:space:]]*=" "$CONF"; then
    current="$(
        grep -E "^[[:space:]]*shared_preload_libraries[[:space:]]*=" "$CONF" \
            | tail -n 1 \
            | sed -E "s/^[^=]*=[[:space:]]*//; s/#.*$//; s/[[:space:]]+$//; s/^['\"]//; s/['\"]$//"
    )"
    case ",${current}," in
        *",${LIB},"*) exit 0 ;;
    esac
fi

if [ -z "$current" ]; then
    value="$LIB"
else
    value="${current},${LIB}"
fi

{
    echo ""
    echo "# aiive: ParadeDB pg_search 必须预加载（旧 PGDATA 升级时自动补齐）"
    echo "shared_preload_libraries = '${value}'"
} >> "$CONF"

echo "aiive: 已更新 ${CONF} 的 shared_preload_libraries = '${value}'"
