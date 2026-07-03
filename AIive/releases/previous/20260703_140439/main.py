#!/usr/bin/env python3
"""
AIive - 自进化个人Agent自举内核

第一版自举内核，实现最小可扩展的Agent框架。
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载.env文件
from pathlib import Path
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

from core.agent_loop import AgentLoop, main


if __name__ == "__main__":
    main()
