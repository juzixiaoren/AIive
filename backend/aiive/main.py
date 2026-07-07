from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aiive.api.routes_health import router as health_router
from aiive.api.routes_chat import router as chat_router
from aiive.api.routes_debug import router as debug_router
from aiive.api.routes_memories import router as memories_router
from aiive.api.routes_personal import router as personal_router
from aiive.api.routes_tools import router as tools_router
from aiive.api.routes_mcp import router as mcp_router
from aiive.api.routes_mcp_install import router as mcp_install_router
from aiive.api.routes_selfdev import router as selfdev_router


def create_app() -> FastAPI:
    app = FastAPI(title="AIive", version="0.1.0")

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
    return app


app = create_app()
