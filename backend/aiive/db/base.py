"""
模块功能说明：
- 数据库基础配置模块，负责创建 SQLAlchemy 引擎和会话工厂
- 提供 get_db() 依赖注入生成器，供 FastAPI 路由使用
- 提供 create_all() 辅助函数，用于创建所有数据表（迁移和测试场景）
"""
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from collections.abc import Generator

from aiive.config import settings

logger = logging.getLogger(__name__)

# 创建数据库引擎，连接 URL 来自项目配置
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout_seconds,
    pool_recycle=settings.db_pool_recycle_seconds,
)
# 创建线程安全的会话工厂，绑定到引擎
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入生成器：为每个请求创建数据库会话并确保请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        logger.exception("数据库会话异常，执行回滚")
        db.rollback()
        raise
    finally:
        db.close()


def create_all():
    """创建所有数据表。供迁移初始化和测试场景使用。

    必须显式导入全部模型模块：SQLAlchemy 只有在其模型类被导入后才把表登记到
    Base.metadata，仅导入 models 会漏掉 retention / forget 两张业务表
    （历史上测试因此报 no such table: forget_tombstones）。
    """
    from aiive.db import forget_models, retention_models
    from aiive.db.models import Base  # noqa: F811

    # 显式引用一次：导入本身用于把表登记进 Base.metadata，这里同时让静态检查
    # 明确看到这两个模块被使用，避免误报 unused import。
    logger.debug(
        "create_all: 模型模块已注册 %s / %s",
        forget_models.__name__, retention_models.__name__,
    )

    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        logger.exception("数据表创建失败")
        raise
