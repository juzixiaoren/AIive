"""
模块功能说明：
- AIive 后端应用的入口模块，负责创建和配置 FastAPI 应用实例
- 注册所有 API 路由、中间件，以及生命周期管理（lifespan）
- 在应用启动时确保数据库 schema 完整性和系统线程存在，并启动后台调度守护进程
"""
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

from aiive.api.routes_health import router as health_router
from aiive.api.routes_chat import router as chat_router
from aiive.api.routes_debug import router as debug_router
from aiive.api.routes_memories import router as memories_router
from aiive.api.routes_personal import router as personal_router
from aiive.api.routes_tools import router as tools_router
from aiive.api.routes_capabilities import router as capabilities_router
from aiive.api.routes_mcp import router as mcp_router
from aiive.api.routes_mcp_install import router as mcp_install_router
from aiive.api.routes_selfdev import router as selfdev_router
from aiive.api.routes_outbox import router as outbox_router
from aiive.api.routes_knowledge import router as knowledge_router
from aiive.api.routes_inspector import router as inspector_router
from aiive.api.routes_tasks import router as tasks_router
from aiive.api.routes_attention import router as attention_router
from aiive.api.routes_notifications import router as notifications_router
from aiive.api.routes_threads import router as threads_router
from aiive.api.routes_epochs import router as epochs_router
from aiive.api.routes_ws import router as ws_router
from aiive.api.routes_forget import router as forget_router
from aiive.api.routes_approval import router as approval_router
from aiive.api.routes_desktop import router as desktop_router
from aiive.api.routes_agent_tasks import router as agent_tasks_router
from aiive.api.routes_skills import router as skills_router
from aiive.config import settings

logger = logging.getLogger(__name__)


def _ensure_system_thread():
    """确保系统线程存在，委托给 ThreadBootstrapService。"""
    from aiive.runtime.thread_bootstrap import ThreadBootstrapService

    try:
        ThreadBootstrapService.ensure_system_thread()
    except Exception:
        logger.error("Failed to ensure system thread", exc_info=True)


def _ensure_retrieval_generation():
    """确保存在唯一 active 的检索索引 generation（Phase 5 查询真相源）。

    无 active generation 时，refresh_source 无的放矢、lexical 路由空转，统一检索失效。
    幂等：仅当库内尚不存在任何 generation（全新部署）时创建 index_version=1 的
    active generation；若已有 generation（如正在进行的 rebuild 持有 building），
    则不干预，交由 rebuild 流程原子激活。
    """
    from aiive.db.base import SessionLocal
    from aiive.db.models import RetrievalIndexGeneration
    from aiive.retrieval.retrieval_index import RetrievalIndexManager

    try:
        db = SessionLocal()
        try:
            mgr = RetrievalIndexManager()
            exists = db.query(RetrievalIndexGeneration).first() is not None
            if not exists:
                mgr.create_generation(db, 1, status="active")
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.error("Failed to ensure retrieval generation", exc_info=True)


def _ensure_schema():
    """确保数据库 schema 完整性：以 Alembic 迁移为唯一真相源。

    启动时执行 `alembic upgrade head`，将数据库 schema 推进到最新版本。
    不再使用 create_all() + 手工 ALTER 的双轨机制（会与 Alembic 迁移历史漂移）。
    schema 的任何变更都必须通过新增 Alembic 迁移脚本完成。
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    # alembic.ini 的位置取决于运行布局：
    #  - 源码运行：仓库根目录（backend 的上一级）
    #  - wheel 安装（Docker）：Dockerfile 将其复制到 /app/alembic.ini
    #  - 也可通过环境变量 ALEMBIC_INI_PATH 显式指定
    import os

    candidates = []
    env_ini = os.environ.get("ALEMBIC_INI_PATH")
    if env_ini:
        candidates.append(Path(env_ini))
    candidates.append(Path("/app/alembic.ini"))
    candidates.append(Path.cwd() / "alembic.ini")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "alembic.ini")

    ini_path = next((p for p in candidates if p.exists()), None)
    if ini_path is None:
        tried = "; ".join(str(p) for p in candidates)
        logger.error("未找到 alembic.ini，尝试过的路径: %s", tried)
        raise RuntimeError(f"alembic.ini not found. tried: {tried}")

    alembic_cfg = Config(str(ini_path))
    # 用运行时配置的数据库 URL 覆盖 ini 中的默认值，保证与应用连接一致
    from aiive.config import settings
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)

    try:
        command.upgrade(alembic_cfg, "head")
    except Exception:
        logger.exception("Alembic 迁移执行失败")
        raise


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    包含 CORS 中间件、所有 API 路由注册，以及应用生命周期管理。
    在启动阶段确保数据库 schema 完整、系统线程存在，并启动调度守护进程。

    返回值:
        FastAPI: 已配置的应用实例
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """应用生命周期管理：启动时执行初始化，关闭时执行清理。"""
        import asyncio
        import os
        import uuid as _uuid
        from aiive.worker.scheduler_daemon import start_daemon
        from aiive.worker.outbox_worker import OutboxWorker
        from aiive.worker.outbox_heartbeat import ActiveClaimRegistry, OutboxHeartbeat
        from aiive.worker.handler_registry import HandlerRegistry
        from aiive.worker.outbox_handlers import register_all
        from aiive.api.ws_manager import ws_manager
        from aiive.desktop.connection_manager import desktop_connection_manager

        # 绑定主事件循环，供后台线程安全推送 WebSocket
        ws_manager.set_main_loop(asyncio.get_running_loop())
        desktop_connection_manager.set_main_loop(asyncio.get_running_loop())
        try:
            from aiive.prompts import validate_prompt_catalog
            validate_prompt_catalog()
        except Exception:
            logger.exception("Prompt 目录校验失败，阻断启动")
            raise
        try:
            _ensure_schema()
        except Exception:
            # 迁移失败必须阻断启动（与 validate_vector_runtime 策略一致）：
            # 带着漂移的 schema 继续运行会产生更难恢复的数据损坏。
            logger.exception("数据库 schema 迁移失败，阻断启动")
            raise
        try:
            from aiive.config import settings
            from aiive.forget.fingerprint import configure_hmac_secret
            configure_hmac_secret(settings.forget_hmac_secret, settings.forget_hmac_key_version)
            logger.info("HMAC 指纹密钥已初始化 (key_version=%d)", settings.forget_hmac_key_version)
        except Exception:
            logger.exception("HMAC 密钥初始化失败")
        try:
            _ensure_system_thread()
        except Exception:
            logger.exception("系统线程初始化失败")
        try:
            _ensure_retrieval_generation()
        except Exception:
            logger.exception("检索索引 generation 初始化失败")
        try:
            from aiive.retrieval.retrieval_bootstrap import ensure_retrieval_backfill
            ensure_retrieval_backfill()
        except Exception:
            logger.exception("检索索引历史 backfill 引导失败")
        try:
            from aiive.runtime.context_budget import ContextBudget
            ContextBudget.from_env()
        except Exception:
            logger.exception("ContextBudget 启动校验失败")
        from aiive.config import settings
        if settings.aiive_memory_vector_enabled:
            from aiive.db.base import SessionLocal
            from aiive.memory.vector_bootstrap import ensure_memory_vector_backfill
            from aiive.memory.vector_projection import validate_vector_runtime

            vector_db = SessionLocal()
            try:
                validate_vector_runtime(vector_db)
            finally:
                vector_db.close()
            ensure_memory_vector_backfill()
        try:
            # 重启后恢复已激活 MCP 能力的工具注册（注册表是进程内状态，DB 才是权威）
            from aiive.db.base import SessionLocal
            from aiive.mcp.builtin import ensure_builtin_mcp_capability
            from aiive.mcp.bootstrap import restore_active_capabilities
            from aiive.tools.registry import get_tool_registry

            builtin_db = SessionLocal()
            try:
                ensure_builtin_mcp_capability(builtin_db)
                builtin_db.commit()
            except Exception:
                builtin_db.rollback()
                raise
            finally:
                builtin_db.close()
            restore_active_capabilities(get_tool_registry())
        except Exception:
            logger.exception("MCP 已激活能力恢复注册失败")
        try:
            start_daemon()
        except Exception:
            logger.exception("调度守护进程启动失败")

        # ── Phase 0.5B: Outbox Worker + Heartbeat ──
        worker_id = f"outbox-{os.getpid()}-{_uuid.uuid4().hex[:8]}"
        handler_registry = HandlerRegistry()
        active_claims = ActiveClaimRegistry()
        register_all(handler_registry)

        outbox_worker = OutboxWorker(
            worker_id=worker_id,
            registry=handler_registry,
            claims=active_claims,
        )
        heartbeat = OutboxHeartbeat(worker_id=worker_id, registry=active_claims)

        heartbeat.start()
        logger.info("OutboxWorker started: worker_id=%s", worker_id)

        # 注册 OutboxWorker 并接入 APScheduler 周期 poll（spec 附录 C 第 7 项）
        try:
            from aiive.worker.scheduler_daemon import (
                schedule_outbox_poll,
                set_outbox_worker,
            )
            set_outbox_worker(outbox_worker)
            schedule_outbox_poll()
        except Exception:
            logger.exception("Outbox poll 调度注册失败")

        yield

        # 关闭：先 wait_active 结束在途 poll/调度任务，再停止租约心跳
        try:
            from aiive.worker.scheduler_daemon import stop_daemon
            stop_daemon()
            logger.info("Scheduler daemon stopped (wait_active)")
        except Exception:
            logger.exception("调度器关闭失败")
        try:
            heartbeat.stop()
            logger.info("OutboxHeartbeat stopped")
        except Exception:
            logger.exception("Heartbeat 关闭失败")

    app = FastAPI(title="AIive", version="0.1.0", lifespan=lifespan)

    # 全局异常处理中间件
    async def catch_unhandled_exceptions(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ):
        try:
            return await call_next(request)
        except Exception:
            logger.exception("未处理的请求异常: %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )

    app.middleware("http")(catch_unhandled_exceptions)

    # 配置 CORS 中间件，允许 Web 开发服务器和 Android Capacitor WebView 访问。
    # 生产域名可通过 AIIVE_CORS_ORIGINS 追加或替换，避免使用通配符来源。
    cors_origins = [
        origin.strip()
        for origin in settings.aiive_cors_origins.split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册所有 API 路由
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(debug_router)
    app.include_router(memories_router)
    app.include_router(personal_router)
    app.include_router(tools_router)
    app.include_router(capabilities_router)
    app.include_router(mcp_router)
    app.include_router(mcp_install_router)
    app.include_router(selfdev_router)
    app.include_router(outbox_router)
    app.include_router(knowledge_router)
    app.include_router(inspector_router)
    app.include_router(tasks_router)
    app.include_router(attention_router)
    app.include_router(notifications_router)
    app.include_router(threads_router)
    app.include_router(epochs_router)
    app.include_router(ws_router)
    app.include_router(forget_router)
    app.include_router(approval_router)
    app.include_router(desktop_router)
    app.include_router(agent_tasks_router)
    app.include_router(skills_router)
    return app


app = create_app()
