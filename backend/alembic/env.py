from logging.config import fileConfig

from sqlalchemy import pool

from alembic import context

from aiive.db.models import Base

# 确保全部 ORM 模型都注册到 Base.metadata：forget_models / retention_models
# 定义在独立模块且未被 models.py 导入，若不在此显式导入，autogenerate 与
# create_all 都会遗漏这些表（Phase 6A Forget Saga、Phase 6B 保留治理）。
import aiive.db.forget_models  # noqa: F401  # pyright: ignore[reportUnusedImport]
import aiive.db.retention_models  # noqa: F401  # pyright: ignore[reportUnusedImport]

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# 程序化调用（应用启动时 command.upgrade）会复用宿主进程的 logger。
# 默认 disable_existing_loggers=True 会静默禁用 uvicorn/aiive 已配置的 logger，
# 表现为迁移日志后"卡住"（实际应用已启动，只是后续日志被吞）。
# 传 disable_existing_loggers=False 保留宿主 logger。
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # 从项目 settings 读取数据库 URL
    from aiive.config import settings
    from sqlalchemy import create_engine
    connectable = create_engine(settings.database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
