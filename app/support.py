"""Кнопка «Поддержка» под приветствием и экран с контактом поддержки.

Контакт (юзернейм или числовой ID) владелец вводит на сайте: вкладка «Кнопки в боте» -> «Поддержка» (самая верхняя карточка);
хранится одной записью kv_settings[KEY] = {"contact": "@username" | "123456789" | ""} и входит в экспорт/импорт .txt той же
вкладки (строка `support:` в заголовке файла). Пока контакт не задан, кнопки в боте нет: подставлять чужой юзернейм нельзя.

Тексты кнопки и сообщения рандомизируются ПОСЛОВНО и постоянно для бота (как у остального меню: выбор по хэшу от ключа бота,
без записи в БД; «Уникальные текста» меняют ключ — меняются и варианты). Сам контакт не рандомизируется и не искажается;
кнопка «⬅️ Назад» фиксирована и красная.
"""
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass

KEY = "support_contact"
OPEN_PREFIX, BACK_PREFIX = "su:", "sv:"
PREFIXES = (OPEN_PREFIX, BACK_PREFIX)
HASH_LEN = 10
BUTTON_STYLE = "primary"      # как у соседних кнопок под приветствием
BACK_LABEL = "⬅️ Назад"        # не рандомизируется
BACK_STYLE = "danger"         # красная, как у остальных «Назад» в боте

# ---------- варианты (каждый блок выбирается отдельно) ----------
BUTTON_LEFT: tuple[str, ...] = ("📲", "💬")
BUTTON_WORD: tuple[str, ...] = ("Поддержка", "Связь")
BUTTON_RIGHT: tuple[str, ...] = ("👩🏻‍💻", "🛟")
MSG_LEAD: tuple[str, ...] = ("Связь", "Контакт")
MSG_WHO: tuple[str, ...] = ("администрацией", "поддержкой")
MSG_GOT: tuple[str, ...] = ("выдалась", "пришла")
MSG_HAVE: tuple[str, ...] = ("есть", "появились")
MSG_POINTER: tuple[str, ...] = ("👉", "📩")

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
MAX_ID = 10 ** 15


def normalize(raw: str | None) -> str:
    """Ввод с сайта -> «@username» или «123456789»; пусто — контакт не задан. ValueError — понятное сообщение."""
    value = (raw or "").strip()
    if not value:
        return ""
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    for prefix in ("t.me/", "telegram.me/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    value = value.split("?")[0].strip("/").lstrip("@")
    if value.isascii() and value.isdigit():
        if not 0 < int(value) <= MAX_ID:
            raise ValueError("ID поддержки — положительное число")
        return str(int(value))
    if not _USERNAME_RE.match(value):
        raise ValueError("Укажите юзернейм вида @username (5–32 символа: латинские буквы, цифры, _) или числовой ID")
    return "@" + value


@dataclass(frozen=True)
class Settings:
    contact: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.contact)

    def to_json(self) -> str:
        return json.dumps({"contact": self.contact}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "Settings":
        try:
            d = json.loads(raw or "{}")
            return cls(contact=normalize(str(d.get("contact") or ""))) if isinstance(d, dict) else cls()
        except (ValueError, TypeError):
            return cls()


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
    from . import repo   # лениво: разбор контакта и тексты работают без БД (shop_io, тесты)

    cfg = Settings.from_json(await repo.kv_get(KEY))
    _cache = (now, cfg)
    return cfg


async def save_settings(cfg: Settings) -> None:
    from . import repo

    await repo.kv_set(KEY, cfg.to_json())
    invalidate()


# ---------- тексты и кнопки ----------
def _variant(key: str, name: str, options: tuple[str, ...]) -> str:
    """Постоянный для бота выбор варианта: каждый блок независимо, по хэшу от ключа бота."""
    h = int(hashlib.sha256(f"support:{name}:{key}".encode()).hexdigest()[:8], 16)
    return options[h % len(options)]


def button_label(key: str | None) -> str:
    """«📲 Поддержка 👩🏻‍💻» (key=None — базовый вид)."""
    if key is None:
        return f"{BUTTON_LEFT[0]} {BUTTON_WORD[0]} {BUTTON_RIGHT[0]}"
    return (f"{_variant(key, 'bl', BUTTON_LEFT)} {_variant(key, 'bw', BUTTON_WORD)} "
            f"{_variant(key, 'br', BUTTON_RIGHT)}")


def contact_html(contact: str) -> str:
    """Контакт как есть: «@username» (Telegram сам делает его ссылкой) или ссылка на профиль по ID."""
    if contact.isdigit():
        return f'<a href="tg://user?id={int(contact)}">ID {int(contact)}</a>'
    return html.escape(contact, quote=False)


def message_text(key: str | None, contact: str) -> str:
    """Сообщение со ссылкой на поддержку; блоки вариантов — свои у каждого бота, контакт не меняется."""
    pick = (lambda name, options: options[0]) if key is None else (lambda name, options: _variant(key, name, options))
    return (
        f"{pick('lead', MSG_LEAD)} с {pick('who', MSG_WHO)} (если не {pick('got', MSG_GOT)} ссылка или "
        f"{pick('have', MSG_HAVE)} вопросы):\n{pick('ptr', MSG_POINTER)} {contact_html(contact)}"
    )


def cb_open(key: str) -> str:
    return OPEN_PREFIX + hashlib.sha256(f"support-open|{key}".encode()).hexdigest()[:HASH_LEN]


def cb_back(key: str) -> str:
    return BACK_PREFIX + hashlib.sha256(f"support-back|{key}".encode()).hexdigest()[:HASH_LEN]


def parse_action(key: str, data: str | None) -> str | None:
    """'open' / 'back' по callback_data ЭТОГО бота; None — чужие или устаревшие данные."""
    if data == cb_open(key):
        return "open"
    if data == cb_back(key):
        return "back"
    return None


def menu_row(key: str) -> tuple[str, str, str]:
    """Кнопка под приветствием: (подпись, callback_data, стиль) — для shop_buttons.build_keyboard(extra_rows=…)."""
    return (button_label(key), cb_open(key), BUTTON_STYLE)


def back_keyboard(key: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BACK_LABEL, callback_data=cb_back(key), style=BACK_STYLE)
    ]])
