"""Все операции с таблицей bot_profiles."""
from sqlalchemy import delete, func, select, update

from . import db
from .models import STATUS_ACTIVE, STATUS_BLOCKED, STATUS_QUEUED, BotProfile


async def list_bots() -> list[BotProfile]:
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotProfile).order_by(BotProfile.id))
        return list(res.scalars())


async def count_bots() -> int:
    async with db.SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(BotProfile))).scalar_one()


async def token_exists(token: str) -> bool:
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotProfile.id).where(BotProfile.token == token))
        return res.first() is not None


async def add_bot(token: str, name: str, description: str, avatar_url: str | None, username: str | None) -> BotProfile:
    bot = BotProfile(
        token=token,
        name=name,
        description=description,
        avatar_url=avatar_url or None,
        username=username,
        status=STATUS_QUEUED,
        is_active=False,
    )
    async with db.SessionLocal() as s, s.begin():
        s.add(bot)
    return bot


async def claim_next() -> BotProfile | None:
    """Возвращает активного бота. Если активного нет — берёт следующий из очереди (по id)
    и делает его активным. Если очередь пуста — None."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            select(BotProfile).where(BotProfile.is_active.is_(True), BotProfile.status == STATUS_ACTIVE)
        )
        current = res.scalar_one_or_none()
        if current:
            return current

        # на случай рассинхрона (is_active=true, но статус другой) — сбрасываем флаг
        await s.execute(update(BotProfile).where(BotProfile.is_active.is_(True)).values(is_active=False))

        res = await s.execute(
            select(BotProfile)
            .where(BotProfile.status == STATUS_QUEUED)
            .order_by(BotProfile.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        nxt = res.scalar_one_or_none()
        if nxt is None:
            return None
        nxt.is_active = True
        nxt.status = STATUS_ACTIVE
        nxt.last_error = None
        return nxt


async def mark_blocked(bot_id: int, reason: str) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            update(BotProfile)
            .where(BotProfile.id == bot_id)
            .values(is_active=False, status=STATUS_BLOCKED, last_error=reason[:1000])
        )


async def activate(bot_id: int) -> bool:
    """Ручное переключение из панели: текущий активный уходит обратно в очередь."""
    async with db.SessionLocal() as s, s.begin():
        target = await s.get(BotProfile, bot_id)
        if target is None:
            return False
        await s.execute(
            update(BotProfile)
            .where(BotProfile.is_active.is_(True), BotProfile.id != bot_id)
            .values(is_active=False, status=STATUS_QUEUED)
        )
        target.is_active = True
        target.status = STATUS_ACTIVE
        target.last_error = None
        return True


async def requeue(bot_id: int) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            update(BotProfile)
            .where(BotProfile.id == bot_id, BotProfile.status == STATUS_BLOCKED)
            .values(status=STATUS_QUEUED, last_error=None)
        )


async def delete_bot(bot_id: int) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(delete(BotProfile).where(BotProfile.id == bot_id))


async def update_profile(bot_id: int, name: str, description: str, avatar_url: str | None) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            update(BotProfile)
            .where(BotProfile.id == bot_id)
            .values(name=name, description=description, avatar_url=avatar_url or None)
        )


async def is_active(bot_id: int) -> bool:
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(BotProfile.id).where(BotProfile.id == bot_id, BotProfile.is_active.is_(True))
        )
        return res.first() is not None


async def set_username(bot_id: int, username: str | None) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(update(BotProfile).where(BotProfile.id == bot_id).values(username=username))
