from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
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

    bot_id: Mapped[int] = mapped_column(ForeignKey("bot_profiles.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # валюта бота 🍬: у каждого прошедшего капчу пользователя свой счётчик
    candies: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # юзернейм в нижнем регистре (нужен, чтобы находить пользователя в «Песочнице» по @username); может отсутствовать
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
