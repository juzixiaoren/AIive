#!/usr/bin/env bash
# 单文件隔离运行防线：每个测试文件单独起一个 pytest 进程。
#
# 目的：全量套件一起跑时，某个文件可能依赖前一个文件留下的数据或模块状态，
# 导致「一起跑绿、单独跑红」的顺序依赖被长期掩盖。本脚本对每个测试文件单独
# 建进程（根 conftest 用 PID 命名 SQLite 库，进程间互不干扰），任一文件失败
# 即视为顺序依赖回归。
#
# 用法：
#   scripts/check_single_file_tests.sh
#   JOBS=4 PYTHON_BIN=.venv/Scripts/python.exe scripts/check_single_file_tests.sh
#   PYTEST_TARGETS="tests/unit tests/flow" scripts/check_single_file_tests.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PYTHON_BIN="${PYTHON_BIN:-python}"
JOBS="${JOBS:-2}"
TARGETS="${PYTEST_TARGETS:-tests}"
LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT

mapfile -t FILES < <(find $TARGETS -name 'test_*.py' | sort)
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "未找到测试文件：$TARGETS" >&2
  exit 1
fi

echo "单文件隔离运行：${#FILES[@]} 个文件，并发 $JOBS，目标：$TARGETS"

run_one() {
  local f="$1"
  local name
  name="$(printf '%s' "$f" | tr '/' '_')"
  if "$PYTHON_BIN" -m pytest "$f" -q --no-header -p no:cacheprovider >"$LOG_DIR/$name.log" 2>&1; then
    echo "PASS $f"
  else
    echo "FAIL $f"
    : >"$LOG_DIR/$name.fail"
  fi
}

export -f run_one
export PYTHON_BIN LOG_DIR

printf '%s\n' "${FILES[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}

FAILED=0
for f in "${FILES[@]}"; do
  name="$(printf '%s' "$f" | tr '/' '_')"
  if [ -f "$LOG_DIR/$name.fail" ]; then
    FAILED=$((FAILED + 1))
  fi
done

if [ "$FAILED" -ne 0 ]; then
  echo "=== $FAILED/${#FILES[@]} 个文件单独运行失败（顺序依赖或真实缺陷）==="
  for f in "${FILES[@]}"; do
    name="$(printf '%s' "$f" | tr '/' '_')"
    if [ -f "$LOG_DIR/$name.fail" ]; then
      echo "--- $f ---"
      tail -25 "$LOG_DIR/$name.log"
    fi
  done
  exit 1
fi

echo "全部 ${#FILES[@]} 个文件单独运行通过"
