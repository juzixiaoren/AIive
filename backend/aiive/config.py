"""
模块功能说明：
- 应用配置管理模块，基于 pydantic-settings 从环境变量和 .env 文件加载配置
- 提供统一的 Settings 单例，供整个后端项目引用
- 涵盖 LLM、数据库、服务器和应用元信息等配置项
"""
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置类：从 .env 文件和环境变量加载所有配置项，提供类型校验和默认值。"""
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM 大模型配置（兼容 OpenAI 接口）
    aiive_llm_api_key: str = ""
    aiive_llm_base_url: str = "https://api.deepseek.com/v1"
    aiive_llm_model: str = "deepseek-chat"
    aiive_llm_timeout_seconds: int = 30

    # 数据库连接配置
    database_url: str = "postgresql+psycopg://aiive:aiive_dev@localhost:5432/aiive"

    # 服务器监听配置
    host: str = "127.0.0.1"
    port: int = 8000

    # 应用元信息
    app_name: str = "AIive"
    app_version: str = "0.1.0"


settings = Settings()
