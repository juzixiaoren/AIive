"""
模块功能说明：
- 应用配置管理模块，基于 pydantic-settings 从环境变量和 .env 文件加载配置
- 提供统一的 Settings 单例，供整个后端项目引用
- 涵盖 LLM、数据库、服务器和应用元信息等配置项
"""
from typing import ClassVar, Literal

from pydantic import model_validator
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
    aiive_llm_model: str = "deepseek-v4-flash"
    aiive_llm_timeout_seconds: int = 30
    # 模型上下文窗口（token）：按所用模型的真实能力配置。
    # 上下文预算（ContextBudget）与 token 安全校验（ModelProfile）均以此为基准，
    # recent_messages 分区自动吸收扣除固定分区后的剩余空间，改此值即整体自适应。
    aiive_llm_context_window: int = 256000
    # 模型单次生成的最大输出 token（从上下文窗口中预留）。
    aiive_llm_max_output_tokens: int = 4096
    # 内部分类/提取调用的 JSON mode。auto 遇到明确的不支持错误时回退为
    # prompt-only JSON；主 Agent 自然语言/工具调用路径不使用该选项。
    aiive_llm_json_mode: Literal["auto", "on", "off"] = "auto"
    # 流式主 Agent 请求是否要求返回 token usage。OpenAI、DeepSeek、Qwen
    # 兼容接口通常支持 stream_options.include_usage；不兼容端点可关闭。
    aiive_llm_stream_usage: bool = True

    # 记忆向量召回（本地/远端 OpenAI-compatible Embeddings + pgvector）
    aiive_memory_vector_enabled: bool = False
    aiive_embedding_provider: Literal["local", "openai_compatible"] = "openai_compatible"
    aiive_embedding_api_key: str = ""
    aiive_embedding_base_url: str = "https://api.openai.com/v1"
    aiive_embedding_model: str = "text-embedding-3-small"
    aiive_embedding_model_revision: str = ""
    aiive_embedding_dimensions: int = 512
    # 本地 BGE 可配置 query 指令；远端通用模型通常留空。
    aiive_embedding_query_prefix: str = ""
    aiive_embedding_timeout_seconds: int = 30
    aiive_memory_vector_top_k: int = 16

    # 记忆文件投影：输出目录只能来自服务端配置，Outbox payload 不接受路径。
    aiive_memory_file_projection_enabled: bool = True
    aiive_memory_file_projection_dir: str = ".data/memory_projection"

    # 数据库连接配置
    database_url: str = "postgresql+psycopg://aiive:aiive_dev@localhost:5432/aiive"
    # 连接池配置（PostgreSQL）。单个聊天回合峰值可能同时占用约 8 个会话
    #   （resolve 3~4 + recover + load_context + graph + finalize + sync_extract 等），
    #   默认 pool_size=5 在并发下会连接饥饿。此处显式放大并配置化。
    #   总连接数上限 = 进程数 × (db_pool_size + db_max_overflow)，需不超过
    #   PostgreSQL 的 max_connections。
    # db_pool_size: 常驻连接数。
    # db_max_overflow: 峰值时允许临时超出的连接数。
    # db_pool_timeout_seconds: 池满时等待可用连接的最长秒数。
    # db_pool_recycle_seconds: 连接最大存活秒数，超时后回收以避免使用失效连接。
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    # 工具执行配置
    # tool_executor_max_workers: 工具执行线程池大小。所有工具调用共享此池；
    #   若工具因阻塞 I/O 超时后子线程无法及时退出，池被占满会拖累后续调用，
    #   可按部署环境调大。
    # tool_default_timeout_seconds: 未显式声明 timeout 的工具的默认执行超时（秒）。
    tool_executor_max_workers: int = 4
    tool_default_timeout_seconds: int = 30
    # 工具参数首次校验失败后，最多允许模型纠正并重试的次数。
    tool_argument_max_retries: int = 2

    # 服务器监听配置
    host: str = "127.0.0.1"
    port: int = 8000
    # 允许访问 API 的浏览器/WebView 来源，逗号分隔。Android Capacitor
    # WebView 默认使用 https://localhost；本地浏览器开发端口一并列出。
    aiive_cors_origins: str = (
        "http://localhost:5173,http://localhost:5174,"
        "http://localhost,https://localhost,capacitor://localhost"
    )

    # 本地开发者诊断接口默认关闭；启用后仍仅允许 loopback 请求。
    aiive_developer_diagnostics_enabled: bool = False

    # Phase 6A Forget Saga HMAC 密钥
    forget_hmac_secret: str = "aiive-dev-hmac-key-change-me-in-production"
    forget_hmac_key_version: int = 1

    # 应用元信息
    app_name: str = "AIive"
    app_version: str = "0.1.0"

    @model_validator(mode="after")
    def validate_memory_features(self) -> "Settings":
        """显式启用时拒绝不完整的记忆派生能力配置。"""
        if self.aiive_memory_file_projection_enabled:
            projection_dir = self.aiive_memory_file_projection_dir.strip()
            if not projection_dir:
                raise ValueError("启用记忆文件投影时必须配置 AIIVE_MEMORY_FILE_PROJECTION_DIR")
            if ".." in projection_dir.replace("\\", "/").split("/"):
                raise ValueError("AIIVE_MEMORY_FILE_PROJECTION_DIR 不允许包含父目录跳转")
        if self.tool_argument_max_retries < 0:
            raise ValueError("TOOL_ARGUMENT_MAX_RETRIES 不能小于 0")
        if not self.aiive_memory_vector_enabled:
            return self
        if not self.database_url.startswith("postgresql"):
            raise ValueError("记忆向量能力仅支持 PostgreSQL + pgvector")
        if (
            self.aiive_embedding_provider == "openai_compatible"
            and not self.aiive_embedding_api_key.strip()
        ):
            raise ValueError("启用记忆向量能力时必须配置 AIIVE_EMBEDDING_API_KEY")
        if self.aiive_embedding_dimensions != 512:
            raise ValueError("当前 memory_vector_projections schema 要求 512 维 embedding")
        if not self.aiive_embedding_model.strip():
            raise ValueError("启用记忆向量能力时必须配置 AIIVE_EMBEDDING_MODEL")
        identity = self.aiive_embedding_model.strip()
        revision = self.aiive_embedding_model_revision.strip()
        if revision:
            identity = f"{identity}@{revision}"
        if len(identity) > 128:
            raise ValueError("Embedding 模型名与 revision 组合后不能超过 128 字符")
        if self.aiive_memory_vector_top_k <= 0:
            raise ValueError("AIIVE_MEMORY_VECTOR_TOP_K 必须大于 0")
        return self


settings = Settings()
