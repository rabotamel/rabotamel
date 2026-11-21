from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUS_QUEUED = "queued"

# статусы заявок предложки
SUB_PENDING = "pending"
SUB_APPROVED = "approved"
SUB_REJECTED = "rejected"


class BotProfile(Base):
    __tablename__ = "bot_profiles"
    __table_args__ = (
        # В базе не может быть двух активных ботов одновременно
        Index("uq_bot_profiles_one_active", "is_active", unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL — бот-шаблон без токена: стоит в очереди «заранее», пока не добавлен токен (см. NOTES.md);
    # запускаться и становиться основным он не может. UNIQUE в PostgreSQL допускает любое число NULL.
    token: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # картинка, которая показывается над текстом во всех сообщениях бота (кроме капчи); см. app/messaging.py
    message_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_QUEUED, server_default=STATUS_QUEUED)

    # порядок в очереди (меньше — раньше)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # с какого момента бот стал основным
    active_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # какая ссылка на аватарку уже отправлена в Telegram (чтобы не загружать одно и то же при каждом старте)
    avatar_applied: Mapped[str | None] = mapped_column(Text, nullable=True)
    # конфигурация капчи этого бота (JSON), создаётся один раз и больше не меняется
    captcha_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    # остальные рандомизируемые сообщения бота (JSON {"greeting": "<json>", ...}); см. app/texts.py и NOTES.md
    texts_config: Mapped[str | None] = mapped_column(Text, nullable=True)

    # служебные поля
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def masked_token(self) -> str:
        """Показываем только числовой id бота и последние 4 символа. У бота без токена — пустая строка."""
        if not self.token:
            return ""
        head, _, tail = self.token.partition(":")
        return f"{head}:••••{tail[-4:]}"


class BotUser(Base):
    """Пользователи, которые заходили в бота (Bot API не умеет отдавать список — считаем сами)."""

    __tablename__ = "bot_users"
    # индекс по last_active_at: выборка и удаление давно неактивных (app/cleanup.py) идут без полного сканирования таблицы
    __table_args__ = (Index("idx_users_last_active_at", "last_active_at"),)

    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # валюта бота 🍬: у каждого прошедшего капчу пользователя свой счётчик
    candies: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # юзернейм в нижнем регистре (нужен, чтобы находить пользователя в «Песочнице» по @username); может отсутствовать
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # последнее взаимодействие с ботом. Пишется не на каждое сообщение, а с троттлингом (см. UserStats.touch_activity)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # когда пользователь в последний раз забрал «Ежедневный подарок» (кулдаун 24 ч, см. tasks.DAILY_COOLDOWN); NULL — ещё не брал
    daily_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserHistory(Base):
    """Все, кто когда-либо прошёл капчу у бота: запись создаётся один раз и НЕ удаляется вместе с `bot_users` (ни при
    удалении в «Песочнице», ни при автоочистке, ни когда человек заблокировал бота). Нужна только для счётчика «сколько
    всего было пользователей» без двойного счёта тех, кто вернулся. Без внешних ключей: история не пропадает и вместе с
    ботом. Читается один раз при запуске бота (count), дальше счётчик ведётся в памяти (app/stats.py)."""

    __tablename__ = "user_history"

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(256))
    auth: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KeyValue(Base):
    __tablename__ = "kv_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Moderator(Base):
    """Модераторы предложки. Не связаны с правами в панели: модератор видит заявки и жмёт кнопки только в боте.

    Общие для всех токенов бота. Добавить можно по user_id или по @username: пока пользователь не написал боту,
    у «юзернейм-записи» user_id пустой (NULL) и подставляется при первом обращении к боту (см. moderation.py)."""

    __tablename__ = "moderators"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    # юзернейм в нижнем регистре, без «@»
    username: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoSubmission(Base):
    """Заявка «Предложки». Хранятся только метаданные: сам файл остаётся у Telegram (file_id).
    file_id действителен только для того бота, который его получил, поэтому заявки и пул одобренных видео
    привязаны к bot_id (как и баланс 🍬)."""

    __tablename__ = "video_submissions"
    __table_args__ = (
        Index("ix_video_submissions_pool", "bot_id", "status"),
        Index("ix_video_submissions_author", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    author_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_id: Mapped[str] = mapped_column(Text)
    file_unique_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    duration: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), default=SUB_PENDING, server_default=SUB_PENDING)
    # что именно сделал модератор: approve | reject | block | block_auto (заявка закрыта блокировкой автора)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    moderator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    moderator_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    moderator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubmissionMessage(Base):
    """Сообщения с заявкой, отправленные модераторам: нужны, чтобы после решения одного модератора
    убрать кнопки и обновить подпись у всех остальных (в том числе после перезапуска бота)."""

    __tablename__ = "submission_messages"

    submission_id: Mapped[int] = mapped_column(
        ForeignKey("video_submissions.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger)


class BlockedAuthor(Base):
    """Заблокированные авторы предложки. Общий список для всех токенов бота (user_id у Telegram один и тот же)."""

    __tablename__ = "blocked_authors"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    blocked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShopProduct(Base):
    """Товар раздела «Магазин». Каталог общий для всех токенов бота (как модераторы): при ротации токена товары остаются.

    `about` хранится так, как его ввели (с `**...**` и другими маркерами) — в HTML Telegram оно переводится при показе
    (shop.format_about). `color` — значение `style` кнопки Telegram: '' (стандартная), 'danger', 'success', 'primary'.
    Показываются только товары с is_visible = true, в порядке (position, id)."""

    __tablename__ = "shop_products"
    __table_args__ = (Index("ix_shop_products_order", "is_visible", "position", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    price: Mapped[int] = mapped_column(BigInteger)
    about: Mapped[str] = mapped_column(Text, default="", server_default="")
    color: Mapped[str] = mapped_column(String(16), default="", server_default="")
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserPremium(Base):
    """Премиум-подписка пользователя («Галерея» -> «Премиум подписка»). Общая для всех токенов бота (user_id у Telegram один).
    Премиум: видео из галереи бесплатно (0🍬) и без защиты от пересылки/сохранения. Либо до premium_until, либо навсегда."""

    __tablename__ = "user_premium"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    premium_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    premium_forever: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ReceiptBlockedUser(Base):
    """Пользователи, которым запрещено отправлять чеки об оплате картой («Купить конфеты»). Общий список для всех
    токенов бота. Блокируют кнопкой «Заблокировать» под чеком в боте для чеков или в «Песочнице» на сайте."""

    __tablename__ = "receipt_blocked_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    blocked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# статусы заказов пополнения («Купить конфеты»)
TOPUP_CREATING = "creating"   # счёт создаётся
TOPUP_ACTIVE = "active"       # счёт выставлен, ждём оплату (CryptoBot / Stars)
TOPUP_REVIEW = "review"       # чек по карте отправлен админам на проверку
TOPUP_PAID = "paid"           # оплата подтверждена, конфеты ещё не начислены
TOPUP_CREDITED = "credited"   # конфеты начислены (финальный статус)
TOPUP_CANCELED = "canceled"
TOPUP_REJECTED = "rejected"
TOPUP_FAILED = "failed"
TOPUP_EXPIRED = "expired"


class TopupOrder(Base):
    """Заказ «Купить конфеты». Привязан к боту (баланс 🍬 у каждого бота свой). Начисление идёт одним
    условным UPDATE по статусу (repo.credit_topup_order), поэтому повторно конфеты за один заказ не начислятся."""

    __tablename__ = "topup_orders"
    __table_args__ = (Index("ix_topup_orders_status", "status", "method"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    candies: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(16))             # cryptobot | stars | card
    currency: Mapped[str] = mapped_column(String(8))            # USD | XTR | RUB
    fiat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(16), default=TOPUP_CREATING, server_default=TOPUP_CREATING)
    invoice_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    charge_id: Mapped[str | None] = mapped_column(String(128), nullable=True)   # Telegram Stars
    receipt_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_sender: Mapped[str | None] = mapped_column(String(128), nullable=True)
    screen_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    screen_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------- «Бесплатные конфеты»: задания, их выполнение, рефералы ----------

# статусы выполнения задания
TASK_PENDING = "pending"     # скриншот отправлен модераторам, ждём решения
TASK_APPROVED = "approved"   # принято, награда начислена
TASK_REJECTED = "rejected"   # отклонено (можно отправить скриншот заново)


class BonusTask(Base):
    """Задание раздела «Бесплатные конфеты». Список общий для всех токенов бота (как товары магазина): при ротации
    токена задания остаются. В коде заданий нет — бот берёт их отсюда, а править их можно во вкладке «Кнопки в боте» на сайте.

    `about` хранится как введено (с `**…**`, `[а/б]` и т. д.; в HTML переводится при показе через shop.format_about).
    `require_screenshot` — нужно ли прикреплять скриншот (тогда награду начисляет модератор после проверки); без скриншота
    награда начисляется сразу по кнопке выполнения. `one_time` (по умолчанию да) — награду можно получить один раз на
    пользователя; если выключено, задание можно выполнять снова и снова. Показываются только is_visible, в порядке (position, id)."""

    __tablename__ = "bonus_tasks"
    __table_args__ = (Index("ix_bonus_tasks_order", "is_visible", "position", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    button_text: Mapped[str] = mapped_column(String(64))
    about: Mapped[str] = mapped_column(Text, default="", server_default="")
    reward: Mapped[int] = mapped_column(BigInteger)
    require_screenshot: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # одноразовое: после награды (отклонение не считается) пользователь не может выполнить задание повторно, кнопка синяя
    one_time: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskCompletion(Base):
    """Выполнение задания пользователем. Баланс 🍬 у каждого бота свой, поэтому и выполнение привязано к bot_id.
    Один пользователь — одна запись на задание: у одноразового задания повторно награду не получить (пока запись
    pending/approved); у неодноразового запись после принятия перезаписывается новой попыткой. После отклонения
    скриншот можно отправить заново. Награда запоминается в момент отправки (`reward`)."""

    __tablename__ = "task_completions"
    __table_args__ = (
        UniqueConstraint("bot_id", "user_id", "task_id", name="uq_task_completions_user_task"),
        Index("ix_task_completions_status", "bot_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("bonus_tasks.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=TASK_PENDING, server_default=TASK_PENDING)
    reward: Mapped[int] = mapped_column(BigInteger)
    file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)   # photo | document
    moderator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    moderator_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    moderator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskCompletionFile(Base):
    """Скриншоты одного выполнения задания (их может быть несколько, до tasks.MAX_SHOTS). Первый дублируется в
    `task_completions.file_id/file_kind` (для старых записей и совместимости); при повторной отправке после отказа
    набор заменяется целиком. Порядок — по `position`."""

    __tablename__ = "task_completion_files"

    completion_id: Mapped[int] = mapped_column(
        ForeignKey("task_completions.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(Text)
    file_kind: Mapped[str] = mapped_column(String(8))   # photo | document


class TaskReviewMessage(Base):
    """Сообщения с кнопками «Подтвердить / Отклонить / Заблокировать», отправленные админам БОТОМ ДЛЯ ЧЕКОВ: нужны, чтобы
    после решения одного админа обновить их у остальных (как submission_messages у «Предложки»)."""

    __tablename__ = "task_review_messages"

    completion_id: Mapped[int] = mapped_column(
        ForeignKey("task_completions.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger)


class Referral(Base):
    """Приглашённый по реферальной ссылке. Один приглашённый — одна запись на бота (PK), поэтому награда за него
    начисляется один раз, сколько бы раз он ни нажимал ссылку."""

    __tablename__ = "referrals"
    __table_args__ = (Index("ix_referrals_referrer", "bot_id", "referrer_id"),)

    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"), primary_key=True)
    invited_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- «Автосообщения» и промокоды ----------

class AutoMessageQueue(Base):
    """Очередь автосообщения: запись создаётся, когда пользователь прошёл капчу, и удаляется в момент отправки
    (спустя promo.AUTO_DELAY). Лежит в БД, чтобы перезапуск приложения (на бесплатном Render они частые) не терял
    тех, кому сообщение ещё не ушло; при запуске бот один раз читает свои записи и ставит таймеры в памяти."""

    __tablename__ = "auto_message_queue"

    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserPromo(Base):
    """Персональный промокод пользователя (8 символов A-Z0-9, уникален во всей таблице). Баланс 🍬 у каждого бота
    свой, поэтому и промокод привязан к bot_id. `percent` запоминается в момент выдачи. Скидка действует, когда
    `activated` (человек ввёл код в чат с ботом) и с `activated_at` не прошло shop.PROMO_DURATION (24 ч): за это время
    скидку можно применять к любому числу покупок. `used` — наследие прежних правил («до первой покупки»): помеченные им
    промокоды остаются погашенными, а новыми покупками флаг больше не ставится."""

    __tablename__ = "user_promos"

    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)
    percent: Mapped[int] = mapped_column(Integer)
    activated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)   # когда ввёл код
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
