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
from aiive.api.routes_ws import router as ws_router

logger = logging.getLogger(__name__)


def _ensure_system_thread():
    """确保系统线程存在，委托给 ThreadBootstrapService。"""
    from aiive.runtime.thread_bootstrap import ThreadBootstrapService

    try:
        ThreadBootstrapService.ensure_system_thread()
    except Exception:
        logger.error("Failed to ensure system thread", exc_info=True)


def _ensure_schema():
    """确保数据库 schema 完整性：创建缺失的表，执行必要的增量变更。"""
    from aiive.db.base import create_all

    try:
        create_all()
    except Exception:
        logger.exception("数据库表初始化失败")
        raise

    # 增量迁移：为已有数据库补齐缺失列
    from aiive.db.base import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS thread_id VARCHAR(36)"))
            conn.commit()
        except Exception:
            logger.warning("执行 schema 变更失败，将跳过（可能已存在或为非致命错误）", exc_info=True)
            conn.rollback()


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

        # 绑定主事件循环，供后台线程安全推送 WebSocket
        ws_manager.set_main_loop(asyncio.get_running_loop())
        try:
            _ensure_schema()
        except Exception:
            logger.exception("数据库 schema 检查失败")
        try:
            _ensure_system_thread()
        except Exception:
            logger.exception("系统线程初始化失败")
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

    # 配置 CORS 中间件，允许前端开发服务器跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
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
    app.include_router(ws_router)
    return app


app = create_app()
