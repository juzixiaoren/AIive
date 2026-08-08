"""离线导出 Agent 完整上下文结构为 Markdown，写入 docs/context_dumps/。

用法（无需任何参数，不连数据库、不绑定线程）：

    cd <repo>/backend
    python ../scripts/dump_agent_context.py

或任意位置设置 PYTHONPATH 指向 backend 后执行本脚本。

输出文件：docs/context_dumps/context_<YYYYMMDD_HHMMSS>.md
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# 让脚本能直接 import aiive 包（backend 为包根）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from aiive.runtime.context_inspector import (  # noqa: E402
    build_context_dump,
    render_markdown,
)


def main() -> None:
    dump = build_context_dump()
    markdown = render_markdown(dump)

    out_dir = _REPO_ROOT / "docs" / "context_dumps"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"context_{timestamp}.md"
    out_path.write_text(markdown, encoding="utf-8")

    print(f"已生成上下文导出：{out_path}")
    print(f"分区数：{len(dump.sections)}  估算 token：{dump.total_token_estimate}")


if __name__ == "__main__":
    main()
