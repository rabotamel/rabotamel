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
    from sqlalchemy import event

    from .metrics import metrics

    # счётчик операций БД для подвала сайта: одна выполненная SQL-команда = одна операция (в памяти, без записи в БД)
    event.listen(engine.sync_engine, "after_cursor_execute", lambda *a, **k: metrics.db_op())
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
        # «Ежедневный подарок»: момент последнего получения (NULL — ещё не брал)
        await conn.execute(text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS daily_at timestamptz"))
        # удвоение наград за задания после возврата в бота (NULL — не действует)
        await conn.execute(text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS boost_until timestamptz"))
        # «Одноразовое» задание: у уже существующих заданий включено (так они и работали)
        await conn.execute(text("ALTER TABLE bonus_tasks ADD COLUMN IF NOT EXISTS one_time boolean NOT NULL DEFAULT true"))
        # комментарий к заданию для админа (у существующих заданий пустой — в заявке остаётся название задания)
        await conn.execute(text("ALTER TABLE bonus_tasks ADD COLUMN IF NOT EXISTS comment text NOT NULL DEFAULT ''"))
        # промокод: скидка действует 24 ч с момента ввода кода. Уже введённые, но ещё не потраченные коды получают
        # полные сутки от момента обновления (условие IS NULL — после первого раза строка не меняется)
        await conn.execute(text("ALTER TABLE user_promos ADD COLUMN IF NOT EXISTS activated_at timestamptz"))
        await conn.execute(text(
            "UPDATE user_promos SET activated_at = now() WHERE activated AND NOT used AND activated_at IS NULL"
        ))
        # last_active_at: у уже существующих строк значением станет момент применения миграции (DEFAULT now() для
        # добавляемой колонки в PostgreSQL проставляется всем строкам сразу и без перезаписи таблицы), поэтому
        # текущие пользователи не попадут под очистку в первый же день
        await conn.execute(
            text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_active_at timestamptz NOT NULL DEFAULT now()")
        )
        # create_all не создаёт индексы у уже существующих таблиц — индекс создаём отдельно (после колонки)
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_users_last_active_at ON bot_users (last_active_at)")
        )
        # история пользователей («всего было»): таблицу создаёт create_all; пока она пуста (первый запуск после обновления),
        # заполняем её теми, кто есть сейчас, и теми, кого уже приглашали по ссылке (они могли выйти). Дальше в неё только
        # добавляют. Условие «таблица пуста» не зависит от строк внешнего запроса — Postgres проверяет его один раз по
        # первичному ключу и на следующих запусках bot_users и referrals не читает.
        await conn.execute(text(
            "INSERT INTO user_history (bot_id, user_id) "
            "SELECT u.bot_id, u.user_id FROM ("
            "SELECT bot_id, user_id FROM bot_users UNION SELECT bot_id, invited_id FROM referrals"
            ") u WHERE NOT EXISTS (SELECT 1 FROM user_history) ON CONFLICT DO NOTHING"
        ))
        await conn.execute(text("UPDATE bot_profiles SET position = id WHERE position = 0"))
        await conn.execute(text("UPDATE bot_profiles SET active_since = now() WHERE is_active AND active_since IS NULL"))


async def close_db() -> None:
    if engine is not None:
        await engine.dispose()
