from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUS_QUEUED = "queued"


class BotProfile(Base):
    __tablename__ = "bot_profiles"
    __table_args__ = (
        # В базе не может быть двух активных ботов одновременно
        Index("uq_bot_profiles_one_active", "is_active", unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_QUEUED, server_default=STATUS_QUEUED)

    # служебные поля
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def masked_token(self) -> str:
        """Показываем только числовой id бота и последние 4 символа."""
        head, _, tail = self.token.partition(":")
        return f"{head}:••••{tail[-4:]}"
