"""测试根目录 conftest：所有测试共享的初始化逻辑。"""

from pathlib import Path


def _load_dotenv():
    """从项目根 .env 加载 LLM 相关环境变量，供集成测试使用。

    注意：只加载 AIIVE_ 前缀的变量，DATABASE_URL 等不影响测试隔离。
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    import os

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            # 仅加载 LLM 配置，避免 DATABASE_URL 等干扰 SQLite 测试
            if key and value and key.startswith("AIIVE_") and key not in os.environ:
                os.environ[key] = value


_load_dotenv()
