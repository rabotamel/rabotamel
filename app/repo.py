"""Все операции с базой данных."""
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import db
from .models import (
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_QUEUED,
    BotProfile,
    BotUser,
    KeyValue,
    PushSubscription,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- боты ----------

async def list_ordered() -> list[BotProfile]:
    """Сначала активный, затем очередь по position, затем заблокированные."""
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotProfile))
        bots = list(res.scalars())
    rank = {STATUS_ACTIVE: 0, STATUS_QUEUED: 1, STATUS_BLOCKED: 2}
    bots.sort(key=lambda b: (rank.get(b.status, 3), b.position, b.id))
    return bots


async def get_bot(bot_id: int) -> BotProfile | None:
    async with db.SessionLocal() as s:
        return await s.get(BotProfile, bot_id)


async def token_exists(token: str) -> bool:
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotProfile.id).where(BotProfile.token == token))
        return res.first() is not None


async def add_bot(token: str, name: str, description: str, avatar_url: str | None, username: str | None) -> BotProfile:
    async with db.SessionLocal() as s, s.begin():
        max_pos = (await s.execute(select(func.coalesce(func.max(BotProfile.position), 0)))).scalar_one()
        bot = BotProfile(
            token=token,
            name=name,
            description=description,
            avatar_url=avatar_url or None,
            username=username,
            status=STATUS_QUEUED,
            is_active=False,
            position=max_pos + 1,
        )
        s.add(bot)
    return bot


async def claim_next() -> BotProfile | None:
    """Возвращает активного бота. Если активного нет — берёт следующий из очереди
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
            .order_by(BotProfile.position, BotProfile.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        nxt = res.scalar_one_or_none()
        if nxt is None:
            return None
        nxt.is_active = True
        nxt.status = STATUS_ACTIVE
        nxt.last_error = None
        nxt.active_since = _now()
        return nxt


async def has_spare(exclude_id: int) -> bool:
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(BotProfile.id).where(BotProfile.status == STATUS_QUEUED, BotProfile.id != exclude_id).limit(1)
        )
        return res.first() is not None


async def mark_blocked(bot_id: int, reason: str) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            update(BotProfile)
            .where(BotProfile.id == bot_id)
            .values(is_active=False, status=STATUS_BLOCKED, last_error=reason[:1000], active_since=None)
        )


async def activate(bot_id: int) -> bool:
    """Ручное переключение: текущий активный уходит обратно в очередь."""
    async with db.SessionLocal() as s, s.begin():
        target = await s.get(BotProfile, bot_id)
        if target is None:
            return False
        await s.execute(
            update(BotProfile)
            .where(BotProfile.is_active.is_(True), BotProfile.id != bot_id)
            .values(is_active=False, status=STATUS_QUEUED, active_since=None)
        )
        target.is_active = True
        target.status = STATUS_ACTIVE
        target.last_error = None
        target.active_since = _now()
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


async def reorder(ids: list[int]) -> None:
    """Задаёт порядок очереди. Основной бот всегда первый и здесь не участвует."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            select(BotProfile).where(BotProfile.status == STATUS_QUEUED).order_by(BotProfile.position, BotProfile.id)
        )
        queued = list(res.scalars())
        by_id = {b.id: b for b in queued}
        ordered = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        seen = {b.id for b in ordered}
        rest = [b for b in queued if b.id not in seen]
        for pos, b in enumerate(ordered + rest, start=1):
            b.position = pos


async def is_active(bot_id: int) -> bool:
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(BotProfile.id).where(BotProfile.id == bot_id, BotProfile.is_active.is_(True))
        )
        return res.first() is not None


async def set_username(bot_id: int, username: str | None) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(update(BotProfile).where(BotProfile.id == bot_id).values(username=username))


# ---------- пользователи бота ----------

async def load_user_ids(bot_id: int) -> set[int]:
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotUser.user_id).where(BotUser.bot_id == bot_id))
        return set(res.scalars())


async def save_users(bot_id: int, add: set[int], remove: set[int]) -> None:
    async with db.SessionLocal() as s, s.begin():
        add_list = list(add)
        for i in range(0, len(add_list), 5000):
            chunk = add_list[i : i + 5000]
            await s.execute(
                pg_insert(BotUser)
                .values([{"bot_id": bot_id, "user_id": u} for u in chunk])
                .on_conflict_do_nothing()
            )
        rem_list = list(remove)
        for i in range(0, len(rem_list), 5000):
            chunk = rem_list[i : i + 5000]
            await s.execute(delete(BotUser).where(BotUser.bot_id == bot_id, BotUser.user_id.in_(chunk)))


# ---------- push-подписки ----------

async def list_push_subs() -> list[PushSubscription]:
    async with db.SessionLocal() as s:
        return list((await s.execute(select(PushSubscription))).scalars())


async def add_push_sub(endpoint: str, p256dh: str, auth: str) -> None:
    async with db.SessionLocal() as s, s.begin():
        stmt = pg_insert(PushSubscription).values(endpoint=endpoint, p256dh=p256dh, auth=auth)
        await s.execute(
            stmt.on_conflict_do_update(index_elements=["endpoint"], set_={"p256dh": p256dh, "auth": auth})
        )


async def delete_push_sub(endpoint: str) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))


# ---------- key-value ----------

async def kv_get(key: str) -> str | None:
    async with db.SessionLocal() as s:
        row = await s.get(KeyValue, key)
        return row.value if row else None


async def kv_set(key: str, value: str) -> None:
    async with db.SessionLocal() as s, s.begin():
        stmt = pg_insert(KeyValue).values(key=key, value=value)
        await s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
