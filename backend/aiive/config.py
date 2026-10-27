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
    # 单次 Embeddings 请求的最大文本条数：本地 TEI cpu 后端的
    # max_batch_requests 通常为 4，批越大只会排队；同时控制回填内存峰值。
    aiive_embedding_batch_size: int = 4

    # 知识库分块向量检索（与记忆共用同一 Embedding 模型与 pgvector）。
    # 关闭时知识检索只走关键词路径；开启后为词法 + 向量混合召回。
    aiive_knowledge_vector_enabled: bool = False
    aiive_knowledge_vector_top_k: int = 16
    # 向量召回的相似度下限：低于该值的分块不进入混合结果，避免无关查询
    # 也被语义近邻"兜底"出噪声。按本地 bge-small-zh 的实测分布标定。
    aiive_knowledge_vector_min_similarity: float = 0.30
    # 单次回填/修复最多处理的分块数（有界批次，避免长事务与内存尖峰）。
    aiive_knowledge_vector_backfill_batch: int = 200

    # 长期目标（Goal）：提案确认与步骤推进的时间阈值。
    # 计划未确认：24 小时后提醒一次，7 天无回复则保留草稿并停止催促。
    aiive_goal_proposal_reminder_hours: int = 24
    aiive_goal_proposal_expire_days: int = 7
    # 步骤等待用户输入：24 小时后最多提醒一次。
    aiive_goal_wait_reminder_hours: int = 24
    # 连续错过同一周期任务达到该次数时，询问是否调整计划。
    aiive_goal_missed_streak_threshold: int = 3
    # 目标触发决策：单个目标 24 小时内最多调用模型的次数（硬上限，防止触发风暴）。
    aiive_goal_trigger_max_decisions_per_day: int = 12

    # 记忆文件投影：输出目录只能来自服务端配置，Outbox payload 不接受路径。
    aiive_memory_file_projection_enabled: bool = True
    aiive_memory_file_projection_dir: str = ".data/memory_projection"

    # 时序知识图谱（派生投影 + 读侧关系扩展）。关闭后不投影、不扩展，
    # 已有图谱行保持原样（可随时重新开启或整库重建）。
    aiive_memory_graph_enabled: bool = True
    # 关系扩展的最大跳数（visited 集合保证不循环）
    aiive_memory_graph_max_hops: int = 2
    # 单次 BFS 每层的查询批大小（有界查询，不是最终关系条数上限）
    aiive_memory_graph_batch_size: int = 200
    # 查询侧参与扩展的种子实体上限
    aiive_memory_graph_max_seed_entities: int = 32
    # 低于该置信度的关系不投影为可用事实、也不注入上下文
    aiive_memory_graph_min_confidence: float = 0.6
    # 图谱在 retrieved_memory 分区中的动态预算占比（随窗口与裁剪轮次变化）
    aiive_memory_graph_budget_ratio: float = 0.35
    # 单次扩展最多收集的关系数（安全阀，防止极端稠密图）
    aiive_memory_graph_max_relations: int = 2000
    # 额外声明为单值的谓词（逗号分隔 canonical 名）。未声明的谓词一律按多值处理，
    # 只有单值谓词才可能因出现不同客体而结束旧关系（仍需记忆层权威取代）。
    aiive_memory_graph_single_valued_predicates: str = ""

    # 长期记忆冲突处理闭环：新提案与既有记忆语义冲突、无法可靠自动裁决时，
    # 持久化待确认案例并通过通知向用户提问，用户自然语言回复后再走正式写入。
    # enabled: 总开关；关闭后退回旧的确定性写入流程（不建案例、不提问）。
    aiive_memory_conflict_confirmation_enabled: bool = True
    # 单次冲突判定最多比较的候选记忆条数（有界，避免稠密记忆下 LLM 上下文失控）。
    aiive_memory_conflict_max_candidates: int = 5
    # 统一检索候选进入语义裁决的最低分；低于该分不送裁决（图谱证据不受此限）。
    aiive_memory_conflict_retrieval_min_score: float = 0.3
    # 待确认案例的有效期（小时）：超时未回复视为放弃新提案，不污染现有记忆。
    aiive_memory_conflict_ttl_hours: int = 72
    # 同一冲突最多追问轮数；超过后保留旧记忆并结束案例（不无限打扰用户）。
    aiive_memory_conflict_max_clarify_rounds: int = 3

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

    # 本机 Bash 执行（run_shell）。
    # shell_bash_path: 显式指定 bash 可执行文件；为空时按 PATH → 常见 Git Bash 路径探测。
    #   探测失败直接返回 shell_unavailable，绝不静默回退到 cmd/powershell。
    # shell_default_timeout_seconds / shell_max_timeout_seconds: 单次命令默认与最大超时。
    # shell_max_output_bytes: stdout / stderr 各自保留的最大字节数，超出截断。
    shell_bash_path: str = ""
    shell_default_timeout_seconds: int = 60
    shell_max_timeout_seconds: int = 300
    shell_max_output_bytes: int = 65536

    # 工具结果回填预算与落盘（防止一次性返回大内容撑爆 LLM 上下文）。
    # tool_output_dir: 本功能专用目录，清理只作用于该目录内的日志文件。
    # tool_output_retention_hours: 日志保留小时数，超期由调度器删除。
    # tool_output_cleanup_interval_hours: 过期日志扫描周期。
    tool_output_dir: str = ".data/tool_outputs"
    tool_output_retention_hours: int = 10
    tool_output_cleanup_interval_hours: int = 5

    # Supervisor 进程托管（自进化 A/B 槽位晋升的真实进程切换）。
    # enabled: 是否启用独立守护进程托管应用进程；关闭时 promote 只切 active_slot 指针
    #   （兼容 docker compose 由容器编排负责重启的部署方式）。
    # host/port: 被托管应用监听的地址与端口（守护进程与被托管进程共用）。
    # app_module: uvicorn 导入串；app_command: 覆盖默认启动命令（模板占位符
    #   {python} {host} {port} {slot} {slot_root} {app_module}），用于测试或自定义启动方式。
    # start_timeout_seconds: 新进程从启动到通过健康检查的最长等待。
    # stop_timeout_seconds: 停止进程时的优雅等待上限，超时后强制结束进程树。
    # poll_seconds: 守护循环轮询间隔；max_restarts/restart_window_seconds: 崩溃重启预算。
    # monitor_seconds: 晋升后的观察窗，窗内异常退出自动回滚。
    supervisor_enabled: bool = False
    supervisor_host: str = "127.0.0.1"
    supervisor_port: int = 8000
    supervisor_health_path: str = "/health"
    supervisor_app_module: str = "aiive.main:app"
    supervisor_app_command: str = ""
    supervisor_start_timeout_seconds: int = 90
    supervisor_stop_timeout_seconds: int = 15
    supervisor_poll_seconds: float = 2.0
    supervisor_max_restarts: int = 3
    supervisor_restart_window_seconds: int = 300
    supervisor_monitor_seconds: int = 600
    supervisor_state_dir: str = ""

    # 公开网页检索与抓取。默认 DuckDuckGo HTML 无需密钥；也可切换 Brave API
    # 或自建 SearXNG。所有返回内容均标记为不可信外部证据。
    aiive_web_search_enabled: bool = True
    aiive_web_search_provider: Literal["duckduckgo", "brave", "searxng"] = "duckduckgo"
    aiive_brave_search_api_key: str = ""
    aiive_searxng_base_url: str = ""
    aiive_web_request_timeout_seconds: int = 15
    aiive_web_max_response_bytes: int = 2_000_000
    aiive_web_user_agent: str = "AIive/0.1 (+local-personal-agent)"
    # 可读取/导入文档的受信根目录，使用 os.pathsep 分隔。留空时默认
    # ~/Documents 与仓库 .data/knowledge。
    aiive_knowledge_roots: str = ""

    # Electron Desktop Node：节点以出站 WebSocket 连接后端，心跳续租。租约过期后
    # 立即停止向新 Turn 注入该节点的本地工具；在途执行由 operation 状态单独收敛。
    aiive_desktop_node_lease_seconds: int = 45
    aiive_desktop_dispatch_timeout_seconds: int = 30
    # Node v2 握手令牌。默认值仅供 loopback 开发；非 loopback 监听必须显式更换。
    aiive_desktop_node_auth_token: str = "aiive-desktop-dev-token"
    aiive_desktop_protocol_version: int = 2
    # 旧 selfdev 直接变更 API 的 Trusted Core 管理令牌；Task 路径仍需 Action 审批。
    aiive_trusted_core_admin_token: str = "aiive-trusted-core-dev-token"

    # 单用户登录（V49）：唯一所有者账号 + 单个长期 access_token。
    # auth_enabled: 全局 HTTP/WebSocket 认证开关。默认开启；关闭仅用于本地
    #   测试与单机调试，非 loopback 部署会在启动校验中直接拒绝关闭。
    # auth_secret: HS256 签名密钥。留空时自动生成一次并写入 auth_secret_file，
    #   重启读取同一文件，保证已签发 Token 继续有效；更换密钥会使全部旧 Token 失效。
    # auth_token_days: Token 有效期（天），默认 365。
    # auth_login_max_attempts / auth_login_window_seconds: 登录失败限流窗口。
    aiive_auth_enabled: bool = True
    aiive_auth_secret: str = ""
    aiive_auth_secret_file: str = ".data/auth_secret"
    aiive_auth_token_days: int = 365
    aiive_auth_login_max_attempts: int = 5
    aiive_auth_login_window_seconds: int = 900

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
        if self.aiive_web_request_timeout_seconds <= 0:
            raise ValueError("AIIVE_WEB_REQUEST_TIMEOUT_SECONDS 必须大于 0")
        if not 1024 <= self.aiive_web_max_response_bytes <= 10_000_000:
            raise ValueError("AIIVE_WEB_MAX_RESPONSE_BYTES 必须在 1KB 到 10MB 之间")
        if self.aiive_web_search_provider == "brave" and not self.aiive_brave_search_api_key.strip():
            raise ValueError("使用 Brave 搜索时必须配置 AIIVE_BRAVE_SEARCH_API_KEY")
        if self.aiive_web_search_provider == "searxng" and not self.aiive_searxng_base_url.strip():
            raise ValueError("使用 SearXNG 搜索时必须配置 AIIVE_SEARXNG_BASE_URL")
        if self.aiive_desktop_node_lease_seconds < 15:
            raise ValueError("AIIVE_DESKTOP_NODE_LEASE_SECONDS 不能小于 15")
        if self.aiive_desktop_dispatch_timeout_seconds <= 0:
            raise ValueError("AIIVE_DESKTOP_DISPATCH_TIMEOUT_SECONDS 必须大于 0")
        if self.aiive_desktop_protocol_version != 2:
            raise ValueError("当前后端只支持 AIIVE_DESKTOP_PROTOCOL_VERSION=2")
        if not self.aiive_desktop_node_auth_token.strip():
            raise ValueError("AIIVE_DESKTOP_NODE_AUTH_TOKEN 不能为空")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and self.aiive_desktop_node_auth_token == "aiive-desktop-dev-token":
            raise ValueError("非 loopback 部署必须更换 Desktop Node 默认令牌")
        if not self.aiive_trusted_core_admin_token.strip():
            raise ValueError("AIIVE_TRUSTED_CORE_ADMIN_TOKEN 不能为空")
        if (
            self.host not in {"127.0.0.1", "localhost", "::1"}
            and self.aiive_trusted_core_admin_token == "aiive-trusted-core-dev-token"
        ):
            raise ValueError("非 loopback 部署必须更换 Trusted Core 默认管理令牌")
        if self.aiive_embedding_batch_size <= 0:
            raise ValueError("AIIVE_EMBEDDING_BATCH_SIZE 必须大于 0")
        if self.aiive_goal_proposal_reminder_hours <= 0:
            raise ValueError("AIIVE_GOAL_PROPOSAL_REMINDER_HOURS 必须大于 0")
        if self.aiive_goal_proposal_expire_days <= 0:
            raise ValueError("AIIVE_GOAL_PROPOSAL_EXPIRE_DAYS 必须大于 0")
        if self.aiive_goal_wait_reminder_hours <= 0:
            raise ValueError("AIIVE_GOAL_WAIT_REMINDER_HOURS 必须大于 0")
        if self.aiive_goal_missed_streak_threshold <= 0:
            raise ValueError("AIIVE_GOAL_MISSED_STREAK_THRESHOLD 必须大于 0")
        if self.aiive_goal_trigger_max_decisions_per_day <= 0:
            raise ValueError("AIIVE_GOAL_TRIGGER_MAX_DECISIONS_PER_DAY 必须大于 0")
        if self.aiive_auth_secret.strip() and len(self.aiive_auth_secret.strip()) < 32:
            # HS256 密钥短于 32 字节会显著降低暴力破解成本。
            raise ValueError("AIIVE_AUTH_SECRET 至少需要 32 个字符")
        if self.aiive_auth_token_days <= 0:
            raise ValueError("AIIVE_AUTH_TOKEN_DAYS 必须大于 0")
        if self.aiive_auth_login_max_attempts <= 0:
            raise ValueError("AIIVE_AUTH_LOGIN_MAX_ATTEMPTS 必须大于 0")
        if self.aiive_auth_login_window_seconds <= 0:
            raise ValueError("AIIVE_AUTH_LOGIN_WINDOW_SECONDS 必须大于 0")
        if self.tool_output_retention_hours <= 0:
            raise ValueError("TOOL_OUTPUT_RETENTION_HOURS 必须大于 0")
        if self.tool_output_cleanup_interval_hours <= 0:
            raise ValueError("TOOL_OUTPUT_CLEANUP_INTERVAL_HOURS 必须大于 0")
        if not self.aiive_auth_enabled and self.host not in {"127.0.0.1", "localhost", "::1"}:
            # 关闭认证会让全部私人数据接口裸奔，只允许在 loopback 调试时使用。
            raise ValueError("非 loopback 部署不允许关闭 AIIVE_AUTH_ENABLED")
        if not (self.aiive_memory_vector_enabled or self.aiive_knowledge_vector_enabled):
            return self
        # 记忆向量是强依赖：开启即要求 PostgreSQL。知识分块向量属于可降级的
        # 派生通道（SQLite / 迁移未执行时自动退回关键词检索），因此不在此阻断启动。
        if self.aiive_memory_vector_enabled and not self.database_url.startswith("postgresql"):
            raise ValueError("记忆向量能力仅支持 PostgreSQL + pgvector")
        if (
            self.aiive_embedding_provider == "openai_compatible"
            and not self.aiive_embedding_api_key.strip()
        ):
            raise ValueError("启用记忆向量能力时必须配置 AIIVE_EMBEDDING_API_KEY")
        if self.aiive_embedding_dimensions != 512:
            raise ValueError("当前向量投影表 schema 要求 512 维 embedding")
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
        if self.aiive_knowledge_vector_top_k <= 0:
            raise ValueError("AIIVE_KNOWLEDGE_VECTOR_TOP_K 必须大于 0")
        if self.aiive_knowledge_vector_backfill_batch <= 0:
            raise ValueError("AIIVE_KNOWLEDGE_VECTOR_BACKFILL_BATCH 必须大于 0")
        if not 0.0 <= self.aiive_knowledge_vector_min_similarity <= 1.0:
            raise ValueError("AIIVE_KNOWLEDGE_VECTOR_MIN_SIMILARITY 必须在 0 到 1 之间")
        return self


settings = Settings()
