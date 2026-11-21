"""Все операции с базой данных."""
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from . import db
from .cleanup_sql import build_delete_inactive_sql
from . import shop as shop_logic
from .models import (
    AutoMessageQueue,
    UserPromo,
    TASK_APPROVED,
    TASK_PENDING,
    TASK_REJECTED,
    BonusTask,
    Referral,
    UserHistory,
    TaskCompletion,
    TaskCompletionFile,
    TaskReviewMessage,
    UserPremium,
    ReceiptBlockedUser,
    TaskBlockedUser,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_QUEUED,
    SUB_APPROVED,
    SUB_PENDING,
    SUB_REJECTED,
    TOPUP_ACTIVE,
    TOPUP_CANCELED,
    TOPUP_CREATING,
    TOPUP_CREDITED,
    TOPUP_EXPIRED,
    TOPUP_FAILED,
    TOPUP_PAID,
    TOPUP_REJECTED,
    TOPUP_REVIEW,
    BlockedAuthor,
    BotProfile,
    BotUser,
    KeyValue,
    Moderator,
    PushSubscription,
    ShopProduct,
    SubmissionMessage,
    TopupOrder,
    VideoSubmission,
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


async def add_bot(
    token: str | None,
    name: str,
    description: str,
    avatar_url: str | None,
    username: str | None,
    message_image_url: str | None = None,
) -> BotProfile:
    async with db.SessionLocal() as s, s.begin():
        max_pos = (await s.execute(select(func.coalesce(func.max(BotProfile.position), 0)))).scalar_one()
        bot = BotProfile(
            token=token or None,
            name=name,
            description=description,
            avatar_url=avatar_url or None,
            message_image_url=message_image_url or None,
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
            # боты без токена (шаблоны) пропускаются: запустить их нечем
            .where(BotProfile.status == STATUS_QUEUED, BotProfile.token.is_not(None))
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
            select(BotProfile.id)
            .where(BotProfile.status == STATUS_QUEUED, BotProfile.id != exclude_id, BotProfile.token.is_not(None))
            .limit(1)
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
        if target is None or not target.token:  # бот без токена основным быть не может
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


async def set_token(bot_id: int, token: str, username: str | None) -> bool:
    """Добавляет токен боту-шаблону. False — бот не найден или токен у него уже есть (менять токен нельзя)."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(BotProfile)
            .where(BotProfile.id == bot_id, BotProfile.token.is_(None))
            .values(token=token, username=username, last_error=None)
        )
        return res.rowcount > 0


async def update_profile(
    bot_id: int, name: str, description: str, avatar_url: str | None, message_image_url: str | None = None
) -> None:
    """Сохраняет профиль из формы. Если в форме указана ссылка на аватарку — сбрасываем «уже отправлено в Telegram»:
    сохранение формы с ссылкой всегда ставит аватарку заново. Иначе та же ссылка, что уже уходила раньше,
    молча пропускалась бы, даже если фото бота потом поменяли в самом Telegram."""
    values = dict(
        name=name,
        description=description,
        avatar_url=avatar_url or None,
        message_image_url=message_image_url or None,
    )
    if avatar_url:
        values["avatar_applied"] = None
    async with db.SessionLocal() as s, s.begin():
        await s.execute(update(BotProfile).where(BotProfile.id == bot_id).values(**values))


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

async def load_users(bot_id: int) -> dict[int, str | None]:
    """user_id -> username (нижний регистр) всех, кто прошёл капчу у этого бота."""
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotUser.user_id, BotUser.username).where(BotUser.bot_id == bot_id))
        return {uid: name for uid, name in res.all()}


async def save_users(
    bot_id: int, add: dict[int, str | None], rename: dict[int, str | None], remove: set[int]
) -> int:
    """Одна транзакция на пачку: сначала удаления, затем вставки, затем смена юзернеймов.
    Порядок важен: удалённый и снова прошедший капчу пользователь начинает с чистого баланса.
    Вставка в историю (`user_history`, для счётчика «всего было») идёт в той же транзакции и тем же пакетом, отдельных
    обращений к БД не добавляет. Возвращает, сколько из добавленных пользователей прошли капчу у этого бота ВПЕРВЫЕ."""
    new_total = 0
    async with db.SessionLocal() as s, s.begin():
        rem_list = list(remove)
        for i in range(0, len(rem_list), 5000):
            chunk = rem_list[i : i + 5000]
            await s.execute(delete(BotUser).where(BotUser.bot_id == bot_id, BotUser.user_id.in_(chunk)))

        add_list = list(add.items())
        for i in range(0, len(add_list), 5000):
            chunk = add_list[i : i + 5000]
            await s.execute(
                pg_insert(BotUser)
                .values([{"bot_id": bot_id, "user_id": u, "username": n} for u, n in chunk])
                .on_conflict_do_nothing()
            )
            fresh = await s.execute(
                pg_insert(UserHistory)
                .values([{"bot_id": bot_id, "user_id": u} for u, _ in chunk])
                .on_conflict_do_nothing()
                .returning(UserHistory.user_id)
            )
            new_total += len(fresh.all())

        if rename:
            # executemany одной командой; имена параметров не совпадают с колонками (иначе SQLAlchemy ругнётся)
            table = BotUser.__table__
            await s.execute(
                update(table)
                .where(table.c.bot_id == bindparam("b_bot"), table.c.user_id == bindparam("b_uid"))
                .values(username=bindparam("b_name")),
                [{"b_bot": bot_id, "b_uid": u, "b_name": n} for u, n in rename.items()],
            )
    return new_total


async def load_user_totals(bot_id: int) -> tuple[int, int, int]:
    """(всего прошедших капчу у этого бота за всё время, из них приглашённых по реферальной ссылке, всего у всех ботов за
    всё время) — ОДНИМ запросом из трёх count по первичным ключам. Вызывается один раз при запуске бота; дальше счётчики
    ведутся в памяти (UserStats), а сайт читает их оттуда."""
    async with db.SessionLocal() as s:
        row = (await s.execute(select(
            select(func.count()).select_from(UserHistory).where(UserHistory.bot_id == bot_id).scalar_subquery(),
            select(func.count()).select_from(Referral).where(Referral.bot_id == bot_id).scalar_subquery(),
            select(func.count()).select_from(UserHistory).scalar_subquery(),
        ))).one()
    return int(row[0]), int(row[1]), int(row[2])


async def get_candies(bot_id: int, user_id: int) -> int:
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(BotUser.candies).where(BotUser.bot_id == bot_id, BotUser.user_id == user_id)
        )
        return res.scalar_one_or_none() or 0


async def add_candies(bot_id: int, user_id: int, delta: int) -> int | None:
    """Атомарно меняет счётчик 🍬 (delta может быть отрицательной, но ниже нуля не уходит).
    Возвращает новое значение или None, если пользователя нет в базе (не прошёл капчу)."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(BotUser)
            .where(BotUser.bot_id == bot_id, BotUser.user_id == user_id)
            .values(candies=func.greatest(BotUser.candies + delta, 0))
            .returning(BotUser.candies)
        )
        return res.scalar_one_or_none()


async def spend_candies(bot_id: int, user_id: int, amount: int) -> int | None:
    """Атомарно списывает `amount` 🍬, только если хватает. Возвращает новый баланс; None — не хватает
    (или пользователя нет в базе). Проверка и списание — один UPDATE, поэтому два быстрых нажатия подряд
    не потратят одни и те же конфеты дважды."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(BotUser)
            .where(BotUser.bot_id == bot_id, BotUser.user_id == user_id, BotUser.candies >= amount)
            .values(candies=BotUser.candies - amount)
            .returning(BotUser.candies)
        )
        return res.scalar_one_or_none()


async def load_last_active(bot_id: int) -> dict[int, float]:
    """user_id -> unix-время (сек) последней записанной в БД активности. Один запрос при запуске бота: по нему
    заполняется кэш троттлинга (UserStats), чтобы после каждого перезапуска процесса не писать активность заново
    всем подряд."""
    async with db.SessionLocal() as s:
        res = await s.execute(select(BotUser.user_id, BotUser.last_active_at).where(BotUser.bot_id == bot_id))
        return {uid: ts.timestamp() for uid, ts in res.all() if ts is not None}


async def touch_users_active(bot_id: int, user_ids: list[int]) -> None:
    """Пачкой отмечает активность: last_active_at = now() одним UPDATE на каждые 5000 пользователей."""
    async with db.SessionLocal() as s, s.begin():
        for i in range(0, len(user_ids), 5000):
            chunk = user_ids[i : i + 5000]
            await s.execute(
                update(BotUser)
                .where(BotUser.bot_id == bot_id, BotUser.user_id.in_(chunk))
                .values(last_active_at=func.now())
            )


_DELETE_INACTIVE_SQL = build_delete_inactive_sql(
    (TOPUP_CREATING, TOPUP_ACTIVE, TOPUP_REVIEW, TOPUP_PAID), SUB_PENDING
)


async def delete_inactive_users(cutoff: datetime, batch: int) -> list[tuple[int, int]]:
    """Удаляет одну пачку неактивных пользователей (см. cleanup_sql) и возвращает [(bot_id, user_id), ...]."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(text(_DELETE_INACTIVE_SQL), {"cutoff": cutoff, "now": _now(), "batch": batch})
        return [(row[0], row[1]) for row in res.all()]


# ---------- премиум ----------

async def get_premium(user_id: int) -> tuple[datetime | None, bool]:
    """(premium_until, premium_forever); (None, False) — подписки нет."""
    async with db.SessionLocal() as s:
        row = (await s.execute(select(UserPremium).where(UserPremium.user_id == user_id))).scalar_one_or_none()
        return (row.premium_until, bool(row.premium_forever)) if row is not None else (None, False)


async def buy_premium(bot_id: int, user_id: int, price: int, days: int | None) -> tuple[int, datetime | None, bool] | None:
    """Покупка премиума одной транзакцией: списание `price` 🍬 (только если хватает) и выдача подписки. days=None — навсегда.
    Действующая подписка продлевается (дни прибавляются к дате окончания), «навсегда» не понижается. Возвращает
    (новый баланс, до какого, навсегда ли); None — не хватает конфет (или пользователя нет в базе)."""
    async with db.SessionLocal() as s, s.begin():
        paid = await s.execute(
            update(BotUser)
            .where(BotUser.bot_id == bot_id, BotUser.user_id == user_id, BotUser.candies >= price)
            .values(candies=BotUser.candies - price)
            .returning(BotUser.candies)
        )
        balance = paid.scalar_one_or_none()
        if balance is None:
            return None
        row = (await s.execute(
            select(UserPremium).where(UserPremium.user_id == user_id).with_for_update()
        )).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if row is None:
            row = UserPremium(user_id=user_id, premium_until=None, premium_forever=False)
            s.add(row)
        if days is None:
            row.premium_until, row.premium_forever = None, True
        elif not row.premium_forever:
            until = row.premium_until
            if until is not None and until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            base = until if until is not None and until > now else now
            row.premium_until = base + timedelta(days=days)
        await s.flush()
        return int(balance), row.premium_until, bool(row.premium_forever)


# ---------- модераторы предложки ----------

async def list_moderators() -> list[Moderator]:
    async with db.SessionLocal() as s:
        return list((await s.execute(select(Moderator).order_by(Moderator.id))).scalars())


async def add_moderator(user_id: int | None, username: str | None) -> Moderator | None:
    """Добавляет модератора по user_id или по юзернейму (нижний регистр, без «@»). None — такой уже есть.

    Человека может не быть среди пользователей бота: запись всё равно создаётся. Если он уже встречался
    боту (есть в bot_users), недостающие данные (юзернейм или ID) подставляются оттуда; иначе ID у записи
    по юзернейму появится, когда человек впервые напишет боту (bind_moderator)."""
    try:
        async with db.SessionLocal() as s, s.begin():
            if user_id is not None and username is None:
                username = (
                    await s.execute(
                        select(BotUser.username).where(BotUser.user_id == user_id, BotUser.username.is_not(None)).limit(1)
                    )
                ).scalar_one_or_none()
            elif username is not None and user_id is None:
                user_id = (
                    await s.execute(select(BotUser.user_id).where(BotUser.username == username).limit(1))
                ).scalar_one_or_none()

            conds = []
            if user_id is not None:
                conds.append(Moderator.user_id == user_id)
            if username is not None:
                conds.append(Moderator.username == username)
            if not conds:
                return None
            if (await s.execute(select(Moderator.id).where(or_(*conds)).limit(1))).first() is not None:
                return None
            mod = Moderator(user_id=user_id, username=username)
            s.add(mod)
            await s.flush()
            return mod
    except IntegrityError:
        return None  # одновременное добавление того же человека


async def delete_moderator(mod_id: int) -> bool:
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(delete(Moderator).where(Moderator.id == mod_id))
        return res.rowcount > 0


async def bind_moderator(user_id: int, username: str | None) -> None:
    """Пользователь написал боту: привязывает его к записи модератора и актуализирует юзернейм.
    Запись «только юзернейм» получает user_id; если для этого user_id запись уже есть, дубль по юзернейму удаляется."""
    try:
        async with db.SessionLocal() as s, s.begin():
            by_id = (await s.execute(select(Moderator).where(Moderator.user_id == user_id))).scalar_one_or_none()
            by_name = None
            if username:
                by_name = (
                    await s.execute(
                        select(Moderator).where(Moderator.username == username, Moderator.user_id.is_(None))
                    )
                ).scalar_one_or_none()
            if by_id is not None and by_name is not None:
                await s.delete(by_name)
                await s.flush()
                by_id.username = username
            elif by_name is not None:
                by_name.user_id = user_id
            elif by_id is not None and by_id.username != username:
                by_id.username = username
    except IntegrityError:
        pass  # чужая запись уже занимает этот юзернейм — оставляем как есть


# ---------- заявки «Предложки» ----------

@dataclass(frozen=True)
class Submission:
    id: int
    bot_id: int
    user_id: int
    author_username: str | None
    file_id: str
    duration: int
    status: str = SUB_PENDING
    decision: str | None = None
    moderator_id: int | None = None
    moderator_username: str | None = None
    moderator_name: str | None = None


@dataclass(frozen=True)
class Decision:
    sub: Submission
    rewarded: bool                 # +🍬 автору действительно начислены
    auto_rejected: tuple[int, ...]  # заявки автора, закрытые вместе с его блокировкой


def _sub(row: VideoSubmission) -> Submission:
    return Submission(
        id=row.id, bot_id=row.bot_id, user_id=row.user_id, author_username=row.author_username,
        file_id=row.file_id, duration=row.duration, status=row.status, decision=row.decision,
        moderator_id=row.moderator_id, moderator_username=row.moderator_username, moderator_name=row.moderator_name,
    )


async def is_author_blocked(user_id: int) -> bool:
    async with db.SessionLocal() as s:
        return (await s.execute(select(BlockedAuthor.user_id).where(BlockedAuthor.user_id == user_id))).first() is not None


async def _set_block(model, user_id: int, blocked: bool, by: int | None) -> bool:
    """Добавляет/убирает user_id в таблице блокировок. True — список изменился (не было -> есть или наоборот)."""
    async with db.SessionLocal() as s, s.begin():
        if blocked:
            res = await s.execute(
                pg_insert(model).values(user_id=user_id, blocked_by=by).on_conflict_do_nothing().returning(model.user_id)
            )
            return res.first() is not None
        res = await s.execute(delete(model).where(model.user_id == user_id))
        return res.rowcount > 0


async def set_author_block(user_id: int, blocked: bool, by: int | None = None) -> bool:
    """Блокировка/разблокировка отправки в «Предложку» (вручную, из «Песочницы»)."""
    return await _set_block(BlockedAuthor, user_id, blocked, by)


async def is_receipt_blocked(user_id: int) -> bool:
    async with db.SessionLocal() as s:
        return (await s.execute(select(ReceiptBlockedUser.user_id).where(ReceiptBlockedUser.user_id == user_id))).first() is not None


async def set_receipt_block(user_id: int, blocked: bool, by: int | None = None) -> bool:
    """Блокировка/разблокировка отправки чеков об оплате картой."""
    return await _set_block(ReceiptBlockedUser, user_id, blocked, by)


async def is_task_blocked(user_id: int) -> bool:
    async with db.SessionLocal() as s:
        return (await s.execute(select(TaskBlockedUser.user_id).where(TaskBlockedUser.user_id == user_id))).first() is not None


async def set_task_block(user_id: int, blocked: bool, by: int | None = None) -> bool:
    """Блокировка/разблокировка отправки скриншотов заданий (отдельный список: чеки об оплате и «Предложку» не затрагивает)."""
    return await _set_block(TaskBlockedUser, user_id, blocked, by)


async def create_submission(
    bot_id: int, user_id: int, username: str | None, file_id: str, file_unique_id: str, duration: int
) -> Submission:
    async with db.SessionLocal() as s, s.begin():
        row = VideoSubmission(
            bot_id=bot_id, user_id=user_id, author_username=username, file_id=file_id,
            file_unique_id=file_unique_id or "", duration=duration or 0, status=SUB_PENDING,
        )
        s.add(row)
        await s.flush()
        return _sub(row)


async def get_submissions(ids: list[int]) -> list[Submission]:
    if not ids:
        return []
    async with db.SessionLocal() as s:
        rows = (await s.execute(select(VideoSubmission).where(VideoSubmission.id.in_(ids)))).scalars()
        return [_sub(r) for r in rows]


async def save_submission_message(sub_id: int, chat_id: int, message_id: int) -> None:
    async with db.SessionLocal() as s, s.begin():
        stmt = pg_insert(SubmissionMessage).values(submission_id=sub_id, chat_id=chat_id, message_id=message_id)
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["submission_id", "chat_id"], set_={"message_id": message_id}
            )
        )


async def submission_messages(ids: list[int]) -> dict[int, list[tuple[int, int]]]:
    """id заявки -> [(chat_id модератора, message_id), ...]."""
    out: dict[int, list[tuple[int, int]]] = {i: [] for i in ids}
    if not ids:
        return out
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(SubmissionMessage.submission_id, SubmissionMessage.chat_id, SubmissionMessage.message_id)
            .where(SubmissionMessage.submission_id.in_(ids))
        )
        for sid, chat_id, msg_id in res.all():
            out.setdefault(sid, []).append((chat_id, msg_id))
    return out


async def decide_submission(
    bot_id: int, sub_id: int, action: str, moderator_id: int, moderator_username: str | None,
    moderator_name: str | None, reward: int = 0,
) -> Decision | None:
    """Решение модератора по заявке (action: approve | reject | block) — одна транзакция.

    Переход pending -> approved/rejected делает один UPDATE ... WHERE status = 'pending': при двух
    одновременных нажатиях второе не найдёт строку и вернёт None (заявка уже обработана), поэтому конфеты
    автору не начисляются дважды. Начисление идёт в той же транзакции, что и смена статуса.
    block: автор попадает в чёрный список, а его остальные pending-заявки закрываются как отклонённые."""
    new_status = SUB_APPROVED if action == "approve" else SUB_REJECTED
    now = _now()
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(VideoSubmission)
            .where(
                VideoSubmission.id == sub_id,
                VideoSubmission.bot_id == bot_id,
                VideoSubmission.status == SUB_PENDING,
            )
            .values(
                status=new_status, decision=action, moderator_id=moderator_id,
                moderator_username=moderator_username, moderator_name=moderator_name, decided_at=now,
            )
            .returning(
                VideoSubmission.id, VideoSubmission.bot_id, VideoSubmission.user_id,
                VideoSubmission.author_username, VideoSubmission.file_id, VideoSubmission.duration,
            )
        )
        row = res.first()
        if row is None:
            return None
        sub = Submission(
            id=row[0], bot_id=row[1], user_id=row[2], author_username=row[3], file_id=row[4], duration=row[5],
            status=new_status, decision=action, moderator_id=moderator_id,
            moderator_username=moderator_username, moderator_name=moderator_name,
        )

        rewarded = False
        auto: list[int] = []
        if action == "approve" and reward > 0:
            paid = await s.execute(
                update(BotUser)
                .where(BotUser.bot_id == bot_id, BotUser.user_id == sub.user_id)
                .values(candies=BotUser.candies + reward, last_active_at=func.now())
                .returning(BotUser.candies)
            )
            rewarded = paid.scalar_one_or_none() is not None
        elif action == "block":
            await s.execute(
                pg_insert(BlockedAuthor)
                .values(user_id=sub.user_id, blocked_by=moderator_id)
                .on_conflict_do_nothing()
            )
            closed = await s.execute(
                update(VideoSubmission)
                .where(VideoSubmission.user_id == sub.user_id, VideoSubmission.status == SUB_PENDING)
                .values(
                    status=SUB_REJECTED, decision="block_auto", moderator_id=moderator_id,
                    moderator_username=moderator_username, moderator_name=moderator_name, decided_at=now,
                )
                .returning(VideoSubmission.id)
            )
            auto = [r[0] for r in closed.all()]
        return Decision(sub=sub, rewarded=rewarded, auto_rejected=tuple(auto))


async def load_approved_pool(bot_id: int) -> list[tuple[int, str]]:
    """(id заявки, file_id) всех одобренных видео этого бота — пул для кнопки покупки видео."""
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(VideoSubmission.id, VideoSubmission.file_id)
            .where(VideoSubmission.bot_id == bot_id, VideoSubmission.status == SUB_APPROVED)
        )
        return [(i, f) for i, f in res.all()]


async def count_gallery_videos(bot_id: int) -> int:
    """Сколько видео сейчас в галерее бота (одобренные заявки; именно из них выдаёт «Купить видео»)."""
    async with db.SessionLocal() as s:
        res = await s.execute(
            select(func.count()).select_from(VideoSubmission)
            .where(VideoSubmission.bot_id == bot_id, VideoSubmission.status == SUB_APPROVED)
        )
        return int(res.scalar_one() or 0)


async def delete_gallery_videos(bot_id: int | None = None) -> int:
    """Удаляет все видео из галереи бота (одобренные заявки; без bot_id — у всех ботов). Возвращает, сколько удалено.
    Ожидающие решения заявки не трогаются. Записи о сообщениях у модераторов удаляются каскадом."""
    async with db.SessionLocal() as s, s.begin():
        stmt = delete(VideoSubmission).where(VideoSubmission.status == SUB_APPROVED)
        if bot_id is not None:
            stmt = stmt.where(VideoSubmission.bot_id == bot_id)
        res = await s.execute(stmt.returning(VideoSubmission.id))
        return len(res.all())


GALLERY_OWNER_KEY = "gallery_owner_bot"


async def reset_gallery_on_switch(bot_id: int) -> int:
    """Вызывается при запуске бота. Галерея принадлежит тому боту, который работал последним: если теперь запускается
    ДРУГОЙ бот (смена бота), все видео галереи удаляются и она начинается с нуля. Перезапуск того же бота
    (правка профиля, перезапуск приложения) галерею не трогает. Самый первый запуск после обновления (запомненного
    бота ещё нет) только запоминает бота и ничего не удаляет. Возвращает, сколько видео удалено."""
    owner = await kv_get(GALLERY_OWNER_KEY)
    if owner == str(bot_id):
        return 0
    deleted = 0
    if owner is not None:
        deleted = await delete_gallery_videos()  # file_id действует только у бота, получившего видео
    await kv_set(GALLERY_OWNER_KEY, str(bot_id))
    return deleted


# ---------- товары магазина ----------

async def list_shop_products(only_visible: bool = True, limit: int | None = None) -> list[ShopProduct]:
    """Товары в порядке показа. only_visible=True — только выбранные для показа в магазине (запрос меню в боте);
    False — весь каталог (панель). Один запрос по индексу."""
    q = select(ShopProduct).order_by(ShopProduct.position, ShopProduct.id)
    if only_visible:
        q = q.where(ShopProduct.is_visible.is_(True))
    if limit:
        q = q.limit(limit)
    async with db.SessionLocal() as s:
        return list((await s.execute(q)).scalars())


async def get_shop_product(product_id: int, only_visible: bool = True) -> ShopProduct | None:
    q = select(ShopProduct).where(ShopProduct.id == product_id)
    if only_visible:
        q = q.where(ShopProduct.is_visible.is_(True))
    async with db.SessionLocal() as s:
        return (await s.execute(q)).scalar_one_or_none()


async def add_shop_product(name: str, price: int, about: str, color: str, visible: bool = True) -> ShopProduct:
    """Новый товар — в конец списка."""
    async with db.SessionLocal() as s, s.begin():
        last = (await s.execute(select(func.max(ShopProduct.position)))).scalar_one_or_none() or 0
        row = ShopProduct(name=name, price=price, about=about, color=color, position=last + 1, is_visible=visible)
        s.add(row)
        await s.flush()
        return row


async def update_shop_product(product_id: int, **values) -> ShopProduct | None:
    """Меняет переданные поля (name, price, about, color, is_visible). None — товара нет."""
    allowed = {k: v for k, v in values.items() if k in {"name", "price", "about", "color", "is_visible"}}
    async with db.SessionLocal() as s, s.begin():
        row = (
            await s.execute(select(ShopProduct).where(ShopProduct.id == product_id).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            return None
        for key, val in allowed.items():
            setattr(row, key, val)
        return row


async def delete_shop_product(product_id: int) -> bool:
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(delete(ShopProduct).where(ShopProduct.id == product_id))
        return res.rowcount > 0


async def reorder_shop_products(ids: list[int]) -> None:
    """Порядок каталога: перечисленные id — первыми в заданном порядке, остальные следом в прежнем."""
    async with db.SessionLocal() as s, s.begin():
        rows = list((await s.execute(
            select(ShopProduct).order_by(ShopProduct.position, ShopProduct.id).with_for_update()
        )).scalars())
        by_id = {r.id: r for r in rows}
        ordered = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        seen = {r.id for r in ordered}
        for pos, r in enumerate(ordered + [r for r in rows if r.id not in seen], start=1):
            r.position = pos


async def _replace_products_tx(s, items: list) -> None:
    """Тело replace_shop_products внутри уже открытой транзакции."""
    await s.execute(delete(ShopProduct))

    def make(pos, it):
        return ShopProduct(id=it.id, name=it.name, price=it.price, about=it.about, color=it.color,
                           position=pos, is_visible=it.visible)
    s.add_all([make(pos, it) for pos, it in enumerate(items, start=1) if it.id is not None])
    await s.flush()
    # счётчик id — за максимальный сохранённый id, чтобы новые товары не столкнулись с восстановленными
    await s.execute(text(
        "SELECT setval(pg_get_serial_sequence('shop_products', 'id'), GREATEST(COALESCE(MAX(id), 0), 1), "
        "COALESCE(MAX(id), 0) >= 1) FROM shop_products"
    ))
    s.add_all([make(pos, it) for pos, it in enumerate(items, start=1) if it.id is None])
    await s.flush()


async def _sync_tasks_tx(s, items: list) -> None:
    """Задания из файла (shop_io.TaskItem) -> таблица bonus_tasks в порядке файла, внутри открытой транзакции.
    Задания с id, который уже есть в БД, ОБНОВЛЯЮТСЯ на месте (отметки выполнения и заявки на проверку сохраняются);
    id, которого нет в БД, создаются с этим же id; без id — получают новые; задания, которых нет в файле, удаляются
    (вместе с отметками выполнения — каскадом)."""
    existing = {r.id: r for r in (await s.execute(select(BonusTask).with_for_update())).scalars()}
    keep = {it.id for it in items if it.id is not None}
    for tid, row in existing.items():
        if tid not in keep:
            await s.delete(row)
    await s.flush()

    def fill(row, pos, it):
        row.button_text, row.about, row.reward = it.button_text, it.about, it.reward
        row.require_screenshot, row.one_time, row.comment = it.screenshot, it.one_time, it.comment
        row.is_visible, row.position = it.visible, pos
        return row
    for pos, it in enumerate(items, start=1):
        if it.id is not None:
            row = existing.get(it.id)
            if row is not None:
                fill(row, pos, it)
            else:
                s.add(fill(BonusTask(id=it.id), pos, it))
    await s.flush()
    await s.execute(text(
        "SELECT setval(pg_get_serial_sequence('bonus_tasks', 'id'), GREATEST(COALESCE(MAX(id), 0), 1), "
        "COALESCE(MAX(id), 0) >= 1) FROM bonus_tasks"
    ))
    for pos, it in enumerate(items, start=1):
        if it.id is None:
            s.add(fill(BonusTask(), pos, it))
    await s.flush()


async def replace_shop_products(items) -> int:
    """Восстановление каталога из файла: ВСЕ текущие товары заменяются товарами из `items` (shop_io.Item) в их порядке —
    одной транзакцией: при любой ошибке текущий каталог остаётся нетронутым. Товары с id сохраняют его (от id зависят
    выбор вариантов и кнопки), товары без id получают новые. Возвращает число товаров."""
    items = list(items)
    async with db.SessionLocal() as s, s.begin():
        await _replace_products_tx(s, items)
    return len(items)


async def restore_buttons(products: list, task_items: list | None) -> tuple[int, int | None]:
    """Восстановление всей вкладки «Кнопки в боте» из файла экспорта ОДНОЙ транзакцией: товары заменяются, задания
    синхронизируются (см. _sync_tasks_tx). task_items=None (файл старого формата) — задания не затрагиваются.
    При любой ошибке не меняется ничего. Возвращает (число товаров, число заданий или None)."""
    products = list(products)
    async with db.SessionLocal() as s, s.begin():
        await _replace_products_tx(s, products)
        if task_items is not None:
            await _sync_tasks_tx(s, list(task_items))
    return len(products), (len(task_items) if task_items is not None else None)


# ---------- «Бесплатные конфеты»: ежедневный подарок, рефералы, задания ----------

async def get_balance_and_daily(bot_id: int, user_id: int) -> tuple[int, datetime | None]:
    """Баланс 🍬 и момент последнего ежедневного подарка (None — не брал) ОДНИМ запросом по ключу: меню «Бесплатные
    конфеты» показывает и то, и другое. Пользователя нет в базе — (0, None)."""
    async with db.SessionLocal() as s:
        row = (await s.execute(
            select(BotUser.candies, BotUser.daily_at).where(BotUser.bot_id == bot_id, BotUser.user_id == user_id)
        )).one_or_none()
    return (int(row[0] or 0), row[1]) if row is not None else (0, None)


async def claim_daily(bot_id: int, user_id: int, amount: int, cooldown: timedelta) -> tuple[bool, int | None, datetime | None]:
    """Ежедневный подарок: ОДИН условный UPDATE (подарок не брали или с прошлого прошло >= cooldown), поэтому два быстрых
    нажатия подряд не выдадут его дважды. (True, новый баланс, None) — выдан; (False, None, когда брали прошлый) — рано;
    (False, None, None) — пользователя нет в базе."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(BotUser)
            .where(
                BotUser.bot_id == bot_id, BotUser.user_id == user_id,
                or_(BotUser.daily_at.is_(None), BotUser.daily_at <= func.now() - cooldown),
            )
            .values(candies=BotUser.candies + amount, daily_at=func.now(), last_active_at=func.now())
            .returning(BotUser.candies)
        )
        balance = res.scalar_one_or_none()
        if balance is not None:
            return True, balance, None
        last = (await s.execute(
            select(BotUser.daily_at).where(BotUser.bot_id == bot_id, BotUser.user_id == user_id)
        )).scalar_one_or_none()
        return False, None, last


async def register_referral(bot_id: int, invited_id: int, referrer_id: int, reward: int) -> int | None:
    """Новый пользователь (прошёл капчу) пришёл по ссылке `referrer_id`: запись о друге и награда пригласившему — одной
    транзакцией. Новый баланс пригласившего или None (сам себя, пригласившего нет в базе бота, друг уже учтён)."""
    if invited_id == referrer_id:
        return None
    async with db.SessionLocal() as s, s.begin():
        exists = (await s.execute(
            select(BotUser.user_id).where(BotUser.bot_id == bot_id, BotUser.user_id == referrer_id)
        )).scalar_one_or_none()
        if exists is None:
            return None
        ins = await s.execute(
            pg_insert(Referral).values(bot_id=bot_id, invited_id=invited_id, referrer_id=referrer_id)
            .on_conflict_do_nothing(index_elements=["bot_id", "invited_id"]).returning(Referral.invited_id)
        )
        if ins.scalar_one_or_none() is None:
            return None
        res = await s.execute(
            update(BotUser).where(BotUser.bot_id == bot_id, BotUser.user_id == referrer_id)
            .values(candies=BotUser.candies + reward, last_active_at=func.now()).returning(BotUser.candies)
        )
        return res.scalar_one_or_none()


async def count_referrals(bot_id: int, user_id: int) -> int:
    async with db.SessionLocal() as s:
        return (await s.execute(
            select(func.count()).select_from(Referral).where(Referral.bot_id == bot_id, Referral.referrer_id == user_id)
        )).scalar_one()


async def get_reward_boost(bot_id: int, user_id: int) -> datetime | None:
    """Момент окончания «УДВОЕНИЯ наград» пользователя (None — не было; прошедший момент тоже возвращается как есть)."""
    async with db.SessionLocal() as s:
        return (await s.execute(
            select(BotUser.boost_until).where(BotUser.bot_id == bot_id, BotUser.user_id == user_id)
        )).scalar_one_or_none()


async def set_reward_boost(bot_id: int, user_id: int, until: datetime) -> bool:
    """Включает удвоение наград до `until`. False — пользователя нет в БД бота."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(BotUser).where(BotUser.bot_id == bot_id, BotUser.user_id == user_id).values(boost_until=until)
        )
        return res.rowcount > 0


async def returned_after_switch(bot_id: int, user_id: int) -> bool:
    """Пользователь уже проходил капчу у ДРУГОГО бота (запись в user_history), а у этого — ещё нет: он вернулся после смены бота.
    Вызывать до того, как пользователь записан в историю этого бота (то есть до flush после успешной капчи)."""
    async with db.SessionLocal() as s:
        row = (await s.execute(
            select(
                select(func.count()).select_from(UserHistory)
                .where(UserHistory.user_id == user_id, UserHistory.bot_id != bot_id).scalar_subquery(),
                select(func.count()).select_from(UserHistory)
                .where(UserHistory.user_id == user_id, UserHistory.bot_id == bot_id).scalar_subquery(),
            )
        )).one()
    return int(row[0]) > 0 and int(row[1]) == 0


async def list_tasks(only_visible: bool = True, limit: int | None = None) -> list[BonusTask]:
    """Задания в порядке показа. only_visible=True — только показываемые (меню в боте); False — все (панель)."""
    q = select(BonusTask).order_by(BonusTask.position, BonusTask.id)
    if only_visible:
        q = q.where(BonusTask.is_visible.is_(True))
    if limit:
        q = q.limit(limit)
    async with db.SessionLocal() as s:
        return list((await s.execute(q)).scalars())


async def get_task(task_id: int, only_visible: bool = True) -> BonusTask | None:
    q = select(BonusTask).where(BonusTask.id == task_id)
    if only_visible:
        q = q.where(BonusTask.is_visible.is_(True))
    async with db.SessionLocal() as s:
        return (await s.execute(q)).scalar_one_or_none()


async def add_task(
    button_text: str, about: str, reward: int, require_screenshot: bool, visible: bool = True, one_time: bool = True,
    comment: str = "",
) -> BonusTask:
    """Новое задание — в конец списка."""
    async with db.SessionLocal() as s, s.begin():
        last = (await s.execute(select(func.max(BonusTask.position)))).scalar_one_or_none() or 0
        row = BonusTask(
            button_text=button_text, about=about, reward=reward, require_screenshot=require_screenshot,
            one_time=one_time, comment=comment, position=last + 1, is_visible=visible,
        )
        s.add(row)
        await s.flush()
        return row


async def update_task(task_id: int, **values) -> BonusTask | None:
    allowed = {k: v for k, v in values.items() if k in {"button_text", "about", "reward", "require_screenshot", "one_time", "is_visible", "comment"}}
    async with db.SessionLocal() as s, s.begin():
        row = (await s.execute(select(BonusTask).where(BonusTask.id == task_id).with_for_update())).scalar_one_or_none()
        if row is None:
            return None
        for key, val in allowed.items():
            setattr(row, key, val)
        return row


async def delete_task(task_id: int) -> bool:
    """Удаляет задание вместе с отметками о выполнении (каскадом)."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(delete(BonusTask).where(BonusTask.id == task_id))
        return res.rowcount > 0


async def reorder_tasks(ids: list[int]) -> None:
    async with db.SessionLocal() as s, s.begin():
        rows = list((await s.execute(
            select(BonusTask).order_by(BonusTask.position, BonusTask.id).with_for_update()
        )).scalars())
        by_id = {r.id: r for r in rows}
        ordered = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        seen = {r.id for r in ordered}
        for pos, r in enumerate(ordered + [r for r in rows if r.id not in seen], start=1):
            r.position = pos


async def get_completion(bot_id: int, user_id: int, task_id: int) -> TaskCompletion | None:
    async with db.SessionLocal() as s:
        return (await s.execute(
            select(TaskCompletion).where(
                TaskCompletion.bot_id == bot_id, TaskCompletion.user_id == user_id, TaskCompletion.task_id == task_id
            )
        )).scalar_one_or_none()


async def get_task_statuses(bot_id: int, user_id: int) -> dict[int, str]:
    """{id задания: статус выполнения} этого пользователя — одним запросом (для цвета кнопок в меню заданий)."""
    async with db.SessionLocal() as s:
        rows = (await s.execute(
            select(TaskCompletion.task_id, TaskCompletion.status)
            .where(TaskCompletion.bot_id == bot_id, TaskCompletion.user_id == user_id)
        )).all()
        return {int(t): str(st) for t, st in rows}


async def _upsert_completion(
    s, bot_id: int, user_id: int, task_id: int, repeatable: bool = False, **values
) -> int | None:
    """Создаёт запись о выполнении или обновляет ОТКЛОНЁННУЮ (повторная попытка). pending не трогает — тогда None.
    Принятую (approved) обновляет только у неодноразового задания (`repeatable=True`): новая попытка заменяет прежнюю
    запись, а награда за прежнюю уже начислена и не отзывается; у одноразового approved тоже даёт None."""
    stmt = pg_insert(TaskCompletion).values(bot_id=bot_id, user_id=user_id, task_id=task_id, **values)
    redo = [TASK_REJECTED, TASK_APPROVED] if repeatable else [TASK_REJECTED]
    stmt = stmt.on_conflict_do_update(
        index_elements=["bot_id", "user_id", "task_id"],
        set_={**values, "created_at": func.now(), "decided_at": None},
        where=TaskCompletion.status.in_(redo),
    ).returning(TaskCompletion.id)
    return (await s.execute(stmt)).scalar_one_or_none()


class _NoUser(Exception):
    """Пользователя нет в базе: транзакцию откатываем целиком (отметка о задании не должна остаться без награды)."""


async def complete_task_instant(
    bot_id: int, user_id: int, task_id: int, reward: int, username: str | None = None, repeatable: bool = False
) -> int | None:
    """Задание без скриншота: отметка и награда одной транзакцией. Новый баланс; None — уже выполнено (одноразовое)/на
    проверке или пользователя нет в базе (тогда отметка откатывается и конфеты не теряются). `repeatable=True` — у
    неодноразового задания награду можно получать снова."""
    try:
        async with db.SessionLocal() as s, s.begin():
            cid = await _upsert_completion(
                s, bot_id, user_id, task_id, repeatable, status=TASK_APPROVED, reward=reward, username=username,
                file_id=None, file_kind=None,
            )
            if cid is None:
                return None
            res = await s.execute(
                update(BotUser).where(BotUser.bot_id == bot_id, BotUser.user_id == user_id)
                .values(candies=BotUser.candies + reward, last_active_at=func.now()).returning(BotUser.candies)
            )
            balance = res.scalar_one_or_none()
            if balance is None:
                raise _NoUser
            await s.execute(update(TaskCompletion).where(TaskCompletion.id == cid).values(decided_at=func.now()))
            return balance
    except _NoUser:
        return None


async def submit_task_screenshot(
    bot_id: int, user_id: int, task_id: int, reward: int, files: list[tuple[str, str]], username: str | None = None,
    repeatable: bool = False,
) -> int | None:
    """Скриншоты на проверку: `files` — [(file_id, 'photo'|'document')], от 1 штуки. id заявки; None — задание уже
    выполнено (одноразовое) или уже на проверке; `repeatable=True` — у неодноразового задания можно слать снова. Заявка и набор файлов пишутся одной транзакцией (при повторной отправке после
    отказа старые файлы заменяются)."""
    if not files:
        return None
    async with db.SessionLocal() as s, s.begin():
        cid = await _upsert_completion(
            s, bot_id, user_id, task_id, repeatable, status=TASK_PENDING, reward=reward, username=username,
            file_id=files[0][0], file_kind=files[0][1],
        )
        if cid is None:
            return None
        await s.execute(delete(TaskCompletionFile).where(TaskCompletionFile.completion_id == cid))
        s.add_all(
            TaskCompletionFile(completion_id=cid, position=i, file_id=fid, file_kind=kind)
            for i, (fid, kind) in enumerate(files)
        )
        return cid


async def get_task_files(completion_id: int) -> list[tuple[str, str]]:
    """[(file_id, kind)] скриншотов заявки по порядку. У записей, созданных до появления нескольких скринов, —
    единственный файл из самой заявки."""
    async with db.SessionLocal() as s:
        rows = (await s.execute(
            select(TaskCompletionFile.file_id, TaskCompletionFile.file_kind)
            .where(TaskCompletionFile.completion_id == completion_id).order_by(TaskCompletionFile.position)
        )).all()
        if rows:
            return [(f, k) for f, k in rows]
        legacy = (await s.execute(
            select(TaskCompletion.file_id, TaskCompletion.file_kind).where(TaskCompletion.id == completion_id)
        )).first()
        return [(legacy[0], legacy[1] or "photo")] if legacy and legacy[0] else []


@dataclass
class TaskReview:
    id: int
    bot_id: int
    user_id: int
    username: str | None
    task_id: int
    task_name: str
    reward: int
    status: str
    file_id: str | None
    file_kind: str | None
    moderator_id: int | None
    moderator_username: str | None
    moderator_name: str | None
    balance: int | None = None   # баланс пользователя после начисления (только в ответе decide_task)
    comment: str = ""            # комментарий к заданию для админа (bonus_tasks.comment); пусто — показываем название


def _review(c: TaskCompletion, name: str, comment: str | None = "") -> TaskReview:
    return TaskReview(
        id=c.id, bot_id=c.bot_id, user_id=c.user_id, username=c.username, task_id=c.task_id, task_name=name,
        reward=c.reward, status=c.status, file_id=c.file_id, file_kind=c.file_kind, moderator_id=c.moderator_id,
        moderator_username=c.moderator_username, moderator_name=c.moderator_name, comment=comment or "",
    )


async def get_task_reviews(ids: list[int]) -> list[TaskReview]:
    if not ids:
        return []
    async with db.SessionLocal() as s:
        rows = (await s.execute(
            select(TaskCompletion, BonusTask.button_text, BonusTask.comment)
            .join(BonusTask, BonusTask.id == TaskCompletion.task_id).where(TaskCompletion.id.in_(ids))
        )).all()
        return [_review(c, name, comment) for c, name, comment in rows]


async def decide_task(
    completion_id: int, bot_id: int | None, approve: bool, moderator_id: int, moderator_username: str | None,
    moderator_name: str | None,
) -> TaskReview | None:
    """Решение админа: один UPDATE ... WHERE status = 'pending' (выигрывает один) и начисление награды той же
    транзакцией. None — уже решено или заявки нет. `bot_id=None` — любой бот (бот для чеков общий для всех магазинов:
    баланс начисляется у бота, чья это заявка); число — только заявка этого бота."""
    async with db.SessionLocal() as s, s.begin():
        conds = [TaskCompletion.id == completion_id, TaskCompletion.status == TASK_PENDING]
        if bot_id is not None:
            conds.append(TaskCompletion.bot_id == bot_id)
        res = await s.execute(
            update(TaskCompletion)
            .where(*conds)
            .values(
                status=TASK_APPROVED if approve else TASK_REJECTED, decided_at=func.now(), moderator_id=moderator_id,
                moderator_username=moderator_username, moderator_name=moderator_name,
            )
            .returning(TaskCompletion)
        )
        c = res.scalar_one_or_none()
        if c is None:
            return None
        named = (await s.execute(
            select(BonusTask.button_text, BonusTask.comment).where(BonusTask.id == c.task_id)
        )).first()
        review = _review(c, named[0] if named else "", named[1] if named else "")
        if approve:
            r = await s.execute(
                update(BotUser).where(BotUser.bot_id == c.bot_id, BotUser.user_id == c.user_id)
                .values(candies=BotUser.candies + c.reward, last_active_at=func.now()).returning(BotUser.candies)
            )
            review.balance = r.scalar_one_or_none()
        return review


async def list_pending_task_reviews(limit: int = 50) -> list[TaskReview]:
    """Заявки на проверке (старые первыми) по всем ботам — для /pending в боте для чеков."""
    async with db.SessionLocal() as s:
        rows = (await s.execute(
            select(TaskCompletion, BonusTask.button_text, BonusTask.comment)
            .join(BonusTask, BonusTask.id == TaskCompletion.task_id)
            .where(TaskCompletion.status == TASK_PENDING).order_by(TaskCompletion.id).limit(limit)
        )).all()
        return [_review(c, name, comment) for c, name, comment in rows]


async def save_task_review_message(completion_id: int, chat_id: int, message_id: int) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            pg_insert(TaskReviewMessage).values(completion_id=completion_id, chat_id=chat_id, message_id=message_id)
            .on_conflict_do_update(
                index_elements=["completion_id", "chat_id"], set_={"message_id": message_id}
            )
        )


async def task_review_messages(completion_id: int) -> list[tuple[int, int]]:
    async with db.SessionLocal() as s:
        rows = (await s.execute(
            select(TaskReviewMessage.chat_id, TaskReviewMessage.message_id)
            .where(TaskReviewMessage.completion_id == completion_id)
        )).all()
        return [(c, m) for c, m in rows]


# ---------- рандомизируемые сообщения и аватарки ----------

def _read_texts(row: BotProfile) -> dict[str, str]:
    """Сырые JSON-конфиги всех видов сообщений из строки бота (отсутствующие пропускаются)."""
    from . import texts

    try:
        extra = json.loads(row.texts_config) if row.texts_config else {}
        if not isinstance(extra, dict):
            extra = {}
    except ValueError:
        extra = {}
    raw: dict[str, str] = {}
    for kind in texts.KINDS:
        col = texts.LEGACY_COLUMNS.get(kind.key)
        val = getattr(row, col) if col else extra.get(kind.key)
        if val:
            raw[kind.key] = val
    return raw


def _write_texts(row: BotProfile, raw: dict[str, str]) -> None:
    from . import texts

    extra: dict[str, str] = {}
    for kind in texts.KINDS:
        col = texts.LEGACY_COLUMNS.get(kind.key)
        if col:
            if getattr(row, col) != raw[kind.key]:
                setattr(row, col, raw[kind.key])
        else:
            extra[kind.key] = raw[kind.key]
    dumped = json.dumps(extra, ensure_ascii=False)
    if (row.texts_config or "") != dumped:
        row.texts_config = dumped


async def get_or_create_texts(bot_id: int, token: str, reroll: bool = False) -> dict[str, str]:
    """Варианты всех рандомизируемых сообщений бота (kind -> JSON).

    * Обычный вызов: существующие корректные варианты возвращаются как есть (запись в БД не делается),
      недостающие или повреждённые создаются детерминированно по токену бота.
    * reroll=True: все варианты создаются заново со случайным seed и отличаются от текущих.
    В обоих случаях новый вариант подбирается так, чтобы не совпасть с вариантами других ботов."""
    from . import texts

    async with db.SessionLocal() as s, s.begin():
        row = (
            await s.execute(select(BotProfile).where(BotProfile.id == bot_id).with_for_update())
        ).scalar_one()

        current = _read_texts(row)
        valid: dict[str, str] = {}
        for kind in texts.KINDS:
            raw = current.get(kind.key)
            if raw is None:
                continue
            try:
                kind.from_json(raw)
                valid[kind.key] = raw
            except Exception:
                pass  # повреждён или устарел — создадим заново

        todo = [k for k in texts.KINDS if reroll or k.key not in valid]
        if not todo:
            return valid

        # signature чужих вариантов (одним запросом на все виды)
        others = (
            await s.execute(select(BotProfile).where(BotProfile.id != bot_id))
        ).scalars()
        taken: dict[str, set] = {k.key: set() for k in todo}
        for other in others:
            for key, raw in _read_texts(other).items():
                if key in taken:
                    try:
                        kind = next(k for k in todo if k.key == key)
                        taken[key].add(kind.from_json(raw).signature())
                    except Exception:
                        continue

        result = dict(valid)
        nonce = secrets.token_hex(8) if reroll else ""
        for kind in todo:
            if reroll and kind.key in valid:
                taken[kind.key].add(kind.from_json(valid[kind.key]).signature())  # новый вариант ≠ текущему
            seed = f"{token}:{nonce}" if reroll else token
            result[kind.key] = kind.generate(seed, taken[kind.key]).to_json()

        _write_texts(row, result)
        return result


async def set_avatar_applied(bot_id: int, url: str | None) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(update(BotProfile).where(BotProfile.id == bot_id).values(avatar_applied=url))


# ---------- push-подписки ----------

async def get_push_sub(endpoint: str) -> PushSubscription | None:
    async with db.SessionLocal() as s:
        res = await s.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
        return res.scalar_one_or_none()



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


async def add_metrics(key: str, delta: dict) -> dict:
    """Прибавляет дельту к накопленным счётчикам в kv_settings (JSON) в одной транзакции с блокировкой строки и
    возвращает новые итоги. Пустая дельта — просто чтение. Так два процесса не затирают друг друга."""
    async with db.SessionLocal() as s, s.begin():
        await s.execute(pg_insert(KeyValue).values(key=key, value="{}").on_conflict_do_nothing())
        row = (await s.execute(select(KeyValue).where(KeyValue.key == key).with_for_update())).scalar_one()
        try:
            data = json.loads(row.value or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        changed = False
        for k, v in delta.items():
            if v:
                data[k] = int(data.get(k, 0) or 0) + int(v)
                changed = True
        if changed:
            row.value = json.dumps(data)
        return data


async def kv_set(key: str, value: str) -> None:
    async with db.SessionLocal() as s, s.begin():
        stmt = pg_insert(KeyValue).values(key=key, value=value)
        await s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))


# ---------- пополнение («Купить конфеты») ----------

class _NoUser(Exception):
    pass


async def create_topup_order(
    bot_id: int, user_id: int, candies: int, method: str, currency: str, fiat_amount, *,
    status: str = TOPUP_CREATING, screen_chat_id: int | None = None, screen_message_id: int | None = None,
    receipt_file_id: str | None = None, receipt_sender: str | None = None,
) -> int:
    async with db.SessionLocal() as s, s.begin():
        order = TopupOrder(
            bot_id=bot_id, user_id=user_id, candies=candies, method=method, currency=currency,
            fiat_amount=fiat_amount, status=status, screen_chat_id=screen_chat_id,
            screen_message_id=screen_message_id, receipt_file_id=receipt_file_id, receipt_sender=receipt_sender,
        )
        s.add(order)
        await s.flush()
        return int(order.id)


async def get_topup_order(order_id: int) -> TopupOrder | None:
    async with db.SessionLocal() as s:
        return await s.get(TopupOrder, order_id)


async def set_topup_invoice(order_id: int, invoice_id: int | None, url: str | None, expires_at: datetime | None) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            update(TopupOrder).where(TopupOrder.id == order_id)
            .values(invoice_id=invoice_id, invoice_url=url, expires_at=expires_at, status=TOPUP_ACTIVE)
        )


async def set_topup_screen(order_id: int, chat_id: int, message_id: int) -> None:
    async with db.SessionLocal() as s, s.begin():
        await s.execute(
            update(TopupOrder).where(TopupOrder.id == order_id)
            .values(screen_chat_id=chat_id, screen_message_id=message_id)
        )


async def finish_topup_order(order_id: int, status: str) -> bool:
    """Закрывает заказ без начисления (canceled / failed / expired / rejected); только из ещё «живых» статусов."""
    allowed = {
        TOPUP_CANCELED: (TOPUP_CREATING, TOPUP_ACTIVE),
        TOPUP_FAILED: (TOPUP_CREATING,),
        TOPUP_EXPIRED: (TOPUP_ACTIVE,),
        TOPUP_REJECTED: (TOPUP_REVIEW,),
    }[status]
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(TopupOrder).where(TopupOrder.id == order_id, TopupOrder.status.in_(allowed))
            .values(status=status).returning(TopupOrder.id)
        )
        return res.scalar_one_or_none() is not None


async def mark_topup_paid(order_id: int, charge_id: str | None = None) -> bool:
    """Оплата подтверждена (CryptoBot / Stars / админ по карте). True — статус изменился."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            update(TopupOrder)
            .where(TopupOrder.id == order_id, TopupOrder.status.in_((TOPUP_CREATING, TOPUP_ACTIVE, TOPUP_REVIEW)))
            .values(status=TOPUP_PAID, paid_at=_now(), charge_id=charge_id)
            .returning(TopupOrder.id)
        )
        return res.scalar_one_or_none() is not None


async def credit_topup_order(order_id: int) -> TopupOrder | None:
    """Начисляет конфеты за оплаченный заказ ОДНОЙ транзакцией: статус paid -> credited и +🍬 пользователю.
    Возвращает заказ, если начислили сейчас; None — уже начислено раньше или пользователя нет в базе
    (тогда транзакция откатывается и заказ остаётся оплаченным)."""
    try:
        async with db.SessionLocal() as s, s.begin():
            res = await s.execute(
                update(TopupOrder).where(TopupOrder.id == order_id, TopupOrder.status == TOPUP_PAID)
                .values(status=TOPUP_CREDITED, credited_at=_now())
                .returning(TopupOrder.id, TopupOrder.bot_id, TopupOrder.user_id, TopupOrder.candies)
            )
            row = res.first()
            if row is None:
                return None
            added = await s.execute(
                update(BotUser).where(BotUser.bot_id == row.bot_id, BotUser.user_id == row.user_id)
                .values(candies=BotUser.candies + row.candies, last_active_at=func.now())
                .returning(BotUser.candies)
            )
            if added.scalar_one_or_none() is None:
                raise _NoUser  # откат: заказ остаётся оплаченным
    except _NoUser:
        return None
    return await get_topup_order(order_id)


async def list_topup_orders(status: str, method: str | None = None) -> list[TopupOrder]:
    async with db.SessionLocal() as s:
        q = select(TopupOrder).where(TopupOrder.status == status)
        if method:
            q = q.where(TopupOrder.method == method)
        return list((await s.execute(q.order_by(TopupOrder.id).limit(200))).scalars())


# ---------- «Автосообщения» и промокоды (app/promo.py, app/automsg.py) ----------

async def queue_auto_message(bot_id: int, user_id: int, due_at: datetime) -> None:
    """Ставит пользователя в очередь автосообщения (повторно прошёл капчу — срок обновляется)."""
    async with db.SessionLocal() as s, s.begin():
        stmt = pg_insert(AutoMessageQueue).values(bot_id=bot_id, user_id=user_id, due_at=due_at)
        await s.execute(stmt.on_conflict_do_update(index_elements=["bot_id", "user_id"], set_={"due_at": due_at}))


async def load_auto_queue(bot_id: int) -> list[tuple[int, datetime]]:
    """Все, кому автосообщение этого бота ещё не ушло: один запрос при запуске бота."""
    async with db.SessionLocal() as s:
        rows = (await s.execute(
            select(AutoMessageQueue.user_id, AutoMessageQueue.due_at).where(AutoMessageQueue.bot_id == bot_id)
        )).all()
    return [(int(uid), due) for uid, due in rows]


async def claim_auto_message(bot_id: int, user_id: int) -> bool:
    """Забирает запись из очереди ОДНИМ DELETE: True только тому, кто реально удалил (два процесса во время деплоя
    не отправят сообщение дважды)."""
    async with db.SessionLocal() as s, s.begin():
        res = await s.execute(
            delete(AutoMessageQueue)
            .where(AutoMessageQueue.bot_id == bot_id, AutoMessageQueue.user_id == user_id)
            .returning(AutoMessageQueue.user_id)
        )
        return res.scalar_one_or_none() is not None


async def issue_promo(bot_id: int, user_id: int, percent: int) -> str:
    """Выдаёт пользователю новый уникальный промокод (прежний, если был, заменяется; скидка снова не активна)."""
    from .promo import generate_code

    for _ in range(10):
        code = generate_code()
        try:
            async with db.SessionLocal() as s, s.begin():
                stmt = pg_insert(UserPromo).values(
                    bot_id=bot_id, user_id=user_id, code=code, percent=percent, activated=False, used=False
                )
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=["bot_id", "user_id"],
                    set_={"code": code, "percent": percent, "activated": False, "used": False, "activated_at": None,
                          "created_at": func.now()},
                ))
            return code
        except IntegrityError:   # такой код уже есть у другого пользователя — пробуем другой
            continue
    raise RuntimeError("не удалось подобрать уникальный промокод")


def _promo_expires(promo: UserPromo | None) -> datetime | None:
    """Когда истекает скидка по промокоду; None — скидки нет (код не введён, погашен по старым правилам или срок вышел)."""
    if promo is None or not promo.activated or promo.used or promo.activated_at is None:
        return None
    expires = promo.activated_at + shop_logic.PROMO_DURATION
    return expires if expires > datetime.now(timezone.utc) else None


async def get_promo_discount(bot_id: int, user_id: int) -> tuple[int, datetime | None]:
    """(скидка в %, момент окончания) прямо сейчас; (0, None) — скидки нет. Скидка действует PROMO_DURATION (24 ч)
    с ввода промокода — на любое число покупок."""
    async with db.SessionLocal() as s:
        promo = (await s.execute(
            select(UserPromo).where(UserPromo.bot_id == bot_id, UserPromo.user_id == user_id)
        )).scalar_one_or_none()
    expires = _promo_expires(promo)
    return (int(promo.percent), expires) if expires is not None else (0, None)


async def activate_promo(bot_id: int, user_id: int, code: str) -> tuple[str, int | None]:
    """Пользователь ввёл код в чат. ('none', None) — такого кода у этого пользователя нет (чужой тоже «нет»);
    ('used', %) — погашен по прежним правилам (первой покупкой); ('expired', %) — 24 часа с ввода уже прошли;
    ('ok', %) — активирован сейчас (с этого момента идут 24 часа); ('active', %) — уже активирован и ещё действует."""
    async with db.SessionLocal() as s, s.begin():
        row = (await s.execute(
            select(UserPromo).where(UserPromo.code == code, UserPromo.bot_id == bot_id, UserPromo.user_id == user_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            return "none", None
        if row.used:
            return "used", int(row.percent)
        if row.activated:
            if _promo_expires(row) is None:
                return "expired", int(row.percent)
            return "active", int(row.percent)
        row.activated = True
        row.activated_at = datetime.now(timezone.utc)
        return "ok", int(row.percent)


@dataclass(frozen=True)
class Purchase:
    status: str            # ok | gone (товар скрыт/удалён) | funds (не хватает конфет)
    name: str = ""
    price: int = 0         # сколько списано (или сколько нужно, если не хватило) — уже со скидкой
    balance: int = 0
    percent: int = 0       # какая скидка была применена (0 — без скидки)


async def buy_product(bot_id: int, user_id: int, product_id: int) -> Purchase:
    """Покупка товара за 🍬 одной транзакцией: цена со скидкой (пока действует промокод — 24 ч с ввода, на любое число
    покупок) и списание (только если хватает)."""
    async with db.SessionLocal() as s, s.begin():
        prod = await s.get(ShopProduct, product_id)
        if prod is None or not prod.is_visible:
            return Purchase("gone")
        promo = (await s.execute(
            select(UserPromo).where(UserPromo.bot_id == bot_id, UserPromo.user_id == user_id).with_for_update()
        )).scalar_one_or_none()
        percent = int(promo.percent) if _promo_expires(promo) is not None else 0
        price = shop_logic.discounted_price(int(prod.price), percent)
        res = await s.execute(
            update(BotUser)
            .where(BotUser.bot_id == bot_id, BotUser.user_id == user_id, BotUser.candies >= price)
            .values(candies=BotUser.candies - price, last_active_at=func.now())
            .returning(BotUser.candies)
        )
        balance = res.scalar_one_or_none()
        if balance is None:
            return Purchase("funds", prod.name, price, 0, percent)
        return Purchase("ok", prod.name, price, int(balance), percent)
