"""Все операции с базой данных."""
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import bindparam, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from . import db
from .models import (
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_QUEUED,
    SUB_APPROVED,
    SUB_PENDING,
    SUB_REJECTED,
    BlockedAuthor,
    BotProfile,
    BotUser,
    KeyValue,
    Moderator,
    PushSubscription,
    ShopProduct,
    SubmissionMessage,
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
) -> None:
    """Одна транзакция на пачку: сначала удаления, затем вставки, затем смена юзернеймов.
    Порядок важен: удалённый и снова прошедший капчу пользователь начинает с чистого баланса."""
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

        if rename:
            # executemany одной командой; имена параметров не совпадают с колонками (иначе SQLAlchemy ругнётся)
            table = BotUser.__table__
            await s.execute(
                update(table)
                .where(table.c.bot_id == bindparam("b_bot"), table.c.user_id == bindparam("b_uid"))
                .values(username=bindparam("b_name")),
                [{"b_bot": bot_id, "b_uid": u, "b_name": n} for u, n in rename.items()],
            )


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
                .values(candies=BotUser.candies + reward)
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
