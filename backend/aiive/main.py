from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from aiive.api.routes_health import router as health_router
from aiive.api.routes_chat import router as chat_router
from aiive.api.routes_debug import router as debug_router
from aiive.api.routes_memories import router as memories_router
from aiive.api.routes_personal import router as personal_router
from aiive.api.routes_tools import router as tools_router
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


def _ensure_system_thread():
    """Ensure the system thread exists so that system-level events can be logged."""
    from aiive.db.base import SessionLocal
    from aiive.db.models import Thread as ThreadModel
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        existing = db.get(ThreadModel, "system")
        if existing is None:
            system_thread = ThreadModel(
                id="system",
                title="System Events",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(system_thread)
            db.commit()
    finally:
        db.close()


def _ensure_schema():
    """Apply schema changes that may be missing on existing databases."""
    from aiive.db.base import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        # Add thread_id column to tasks (for reminder context)
        try:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS thread_id VARCHAR(36)"))
            conn.commit()
        except Exception:
            conn.rollback()


def create_app() -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from aiive.worker.scheduler_daemon import start_daemon
        _ensure_schema()
        _ensure_system_thread()
        start_daemon()
        yield

    app = FastAPI(title="AIive", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(debug_router)
    app.include_router(memories_router)
    app.include_router(personal_router)
    app.include_router(tools_router)
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
    return app


app = create_app()
