from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

engine: AsyncEngine | None = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> AsyncEngine:
    """Приводит обычный URL Neon (postgresql://...?sslmode=require) к виду для asyncpg."""
    url = make_url(database_url)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+asyncpg")

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)  # asyncpg не понимает этот параметр
    # Neon pooler (pgbouncer) не дружит с prepared statements
    query["prepared_statement_cache_size"] = "0"
    url = url.set(query=query)

    connect_args: dict = {"statement_cache_size": 0}
    if sslmode not in (None, "disable") or "neon.tech" in (url.host or ""):
        connect_args["ssl"] = "require"

    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,  # Neon усыпляет compute — соединения могут протухнуть
        pool_recycle=300,
        connect_args=connect_args,
    )


async def init_db(database_url: str) -> None:
    global engine, SessionLocal
    from . import models  # noqa: F401  (регистрирует таблицы в Base.metadata)

    engine = build_engine(database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all не добавляет колонки в уже существующие таблицы — докидываем вручную
        await conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN IF NOT EXISTS position integer NOT NULL DEFAULT 0"))
        await conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN IF NOT EXISTS active_since timestamptz"))
        await conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN IF NOT EXISTS avatar_applied text"))
        await conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN IF NOT EXISTS captcha_config text"))
        await conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN IF NOT EXISTS texts_config text"))
        await conn.execute(text("ALTER TABLE bot_profiles ADD COLUMN IF NOT EXISTS message_image_url text"))
        # боты без токена (шаблоны): токен больше не обязателен
        await conn.execute(text("ALTER TABLE bot_profiles ALTER COLUMN token DROP NOT NULL"))
        await conn.execute(text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS candies bigint NOT NULL DEFAULT 0"))
        await conn.execute(text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS username varchar(64)"))
        await conn.execute(text("UPDATE bot_profiles SET position = id WHERE position = 0"))
        await conn.execute(text("UPDATE bot_profiles SET active_since = now() WHERE is_active AND active_since IS NULL"))


async def close_db() -> None:
    if engine is not None:
        await engine.dispose()
