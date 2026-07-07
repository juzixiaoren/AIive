from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM (OpenAI-compatible)
    aiive_llm_api_key: str = ""
    aiive_llm_base_url: str = "https://api.deepseek.com/v1"
    aiive_llm_model: str = "deepseek-chat"
    aiive_llm_timeout_seconds: int = 30

    # Database
    database_url: str = "postgresql+psycopg://aiive:aiive_dev@localhost:5432/aiive"

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # App
    app_name: str = "AIive"
    app_version: str = "0.1.0"


settings = Settings()
