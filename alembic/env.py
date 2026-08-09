import anyio.from_thread
from alembic import context
from pydantic import BaseModel
from rewire.config import config as config_class
from rewire.dependencies import Dependencies
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel

config = context.config
target_metadata = SQLModel.metadata


@config_class(path="rewire_sqlmodel.alembic.env")
class Config(BaseModel):
    render_as_batch: bool = False


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=Config.render_as_batch,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = Dependencies.ctx.get().resolve(AsyncEngine)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)


if context.is_offline_mode():
    run_migrations_offline()
else:
    anyio.from_thread.run(run_migrations_online)
