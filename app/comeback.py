"""Уведомление «Вы вернулись» (вкладка «Автосообщения» на сайте) и «УДВОЕНИЕ наград» на 24 часа.

Кому: пользователь, который уже проходил капчу у ДРУГОГО (предыдущего) бота — запись в `user_history` с другим bot_id — и
теперь, при втором и последующем боте, прошёл капчу у текущего (`repo.returned_after_switch`). После приветствия ему уходит
это уведомление (текст владелец пишет на сайте), и с момента его отправки 24 часа (BOOST_DURATION) награды за ЗАДАНИЯ
удваиваются (BOOST_FACTOR; ежедневный подарок и реферальная награда не меняются). Окончание хранится в `bot_users.boost_until`.

Текст — «как введено», как у описаний товаров: оформление, варианты [а/б] (выбор постоянный у бота). Пустой текст — уведомление
выключено: ни сообщения, ни удвоения. Настройки — один JSON в kv_settings[KEY]: {"text": ...}.
"""
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import repo, shop, tasks

KEY = "return_notice"
BOOST_DURATION = timedelta(hours=24)
BOOST_FACTOR = tasks.BOOST_FACTOR

MAX_TEXT = 4000      # видимых символов в Telegram (после выбора вариантов и без значков оформления); предел сообщения — 4096
MAX_RAW = 8000       # поле на сайте: считается как введено


@dataclass(frozen=True)
class Settings:
    text: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.text.strip())

    def to_json(self) -> str:
        return json.dumps({"text": self.text}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "Settings":
        try:
            d = json.loads(raw or "{}")
            return cls(text=str(d.get("text") or "")) if isinstance(d, dict) else cls()
        except (ValueError, TypeError):
            return cls()


def _clean(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def telegram_length(raw: str | None) -> int:
    """Худший случай длины текста в Telegram (в каждой [а/б] самый длинный вариант)."""
    return shop.visible_length(shop.format_about(shop.resolve_variants(_clean(raw), "", 0, longest=True)))


def normalize(text: str | None) -> Settings:
    """Проверка поля с сайта. ValueError — понятное сообщение для панели."""
    text = _clean(text)
    if len(text) > MAX_RAW:
        raise ValueError(f"Уведомление — не длиннее {MAX_RAW} символов (считая скобки вариантов и значки оформления)")
    shown = telegram_length(text)
    if shown > MAX_TEXT:
        raise ValueError(f"Уведомление: в Telegram займёт до {shown} символов, а можно не больше {MAX_TEXT}")
    return Settings(text=text)


_CACHE_TTL = 30.0
_cache: tuple[float, Settings] | None = None


def invalidate() -> None:
    global _cache
    _cache = None


async def load_settings(*, fresh: bool = False) -> Settings:
    global _cache
    now = time.monotonic()
    if not fresh and _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    cfg = Settings.from_json(await repo.kv_get(KEY))
    _cache = (now, cfg)
    return cfg


async def save_settings(cfg: Settings) -> None:
    await repo.kv_set(KEY, cfg.to_json())
    invalidate()


def render(raw: str, seed: str) -> str:
    """Текст «как введено» -> HTML Telegram (варианты [а/б] этого бота и оформление)."""
    return shop.format_about(raw, seed, 0)


def boost_left(until: datetime | None, now: datetime | None = None) -> float | None:
    """Сколько секунд осталось до конца удвоения; None — оно не действует."""
    if until is None:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    left = (until - (now or datetime.now(timezone.utc))).total_seconds()
    return left if left > 0 else None
