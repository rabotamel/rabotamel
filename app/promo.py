"""«Автосообщения» и промокоды.

Каждому пользователю через AUTO_DELAY (30 минут) после капчи бот шлёт сообщение, которое владелец пишет на вкладке
«Автосообщения». В тексте работают:
  * оформление, как в описаниях товаров: **жирный**, __подчёркнутый__, *курсив*, ~~зачёркнутый~~, ||спойлер||, `код`,
    [текст](https://адрес), «# Заголовок» (shop.format_about);
  * варианты фраз [привет/здравствуй] (shop.resolve_variants): у одного бота выбор постоянный, у разных — разный;
  * переменная {promo} — личный промокод пользователя: 8 символов A-Z и 0-9, у каждого свой.
Введя промокод в чат с ботом, пользователь получает скидку `percent` % на все товары магазина на 24 часа с момента ввода
(shop.PROMO_DURATION) — на любое число покупок. Пока скидка действует, в кнопке магазина под приветствием дописано
«(🔥 -N% на Hч)», а в меню магазина — сколько осталось до конца.
На введённый промокод бот отвечает сообщением `reply` (тоже с оформлением, вариантами и {promo}).

Настройки лежат одним JSON в kv_settings[AUTO_KEY]: {"text": ..., "percent": 15, "reply": ...}. Тексты хранятся КАК
ВВЕДЕНО, в HTML они переводятся при отправке — так же, как описание товара. Пустой `text` — автосообщение выключено.
"""
import json
import re
import secrets
import string
import time
from dataclasses import dataclass

from . import repo, shop

AUTO_KEY = "auto_message"
AUTO_DELAY = 30 * 60          # через сколько секунд после капчи уходит сообщение
AUTO_STALE = 24 * 3600        # если бот был выключен и сообщение просрочено больше чем на сутки — не отправляем

CODE_LEN = 8
CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_RE = re.compile(rf"[A-Z0-9]{{{CODE_LEN}}}")
VAR_RE = re.compile(r"\{promo\}", re.IGNORECASE)

MAX_TEXT = 4000      # видимых символов в Telegram (после выбора вариантов и без значков оформления); предел сообщения — 4096
MAX_RAW = 8000       # поле на сайте: считается как введено (со скобками вариантов и значками)
MIN_PERCENT, MAX_PERCENT = 1, 99

DEFAULT_REPLY = "✅ Промокод принят! Скидка {percent}% действует на все товары в течение 24 часов."
USED_TEXT = "❌ Этот промокод уже использован."
EXPIRED_TEXT = "❌ Срок действия этого промокода истёк."


# ---------- коды ----------

def generate_code() -> str:
    """8 случайных символов: большие английские буквы и цифры (криптостойкий ГСЧ)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def parse_code(text: str | None) -> str | None:
    """Промокод из сообщения пользователя: только сообщение целиком из 8 букв/цифр (регистр не важен), иначе None."""
    candidate = (text or "").strip().upper()
    return candidate if CODE_RE.fullmatch(candidate) else None


def has_variable(text: str | None) -> bool:
    return bool(VAR_RE.search(text or ""))


# ---------- настройки ----------

@dataclass(frozen=True)
class AutoSettings:
    text: str = ""
    percent: int | None = None
    reply: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.text.strip())

    def to_json(self) -> str:
        return json.dumps({"text": self.text, "percent": self.percent, "reply": self.reply}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "AutoSettings":
        try:
            d = json.loads(raw or "{}")
            if not isinstance(d, dict):
                raise ValueError
            pct = d.get("percent")
            return cls(text=str(d.get("text") or ""), percent=int(pct) if pct else None, reply=str(d.get("reply") or ""))
        except (ValueError, TypeError):
            return cls()


def _clean(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def telegram_length(raw: str | None) -> int:
    """Худший случай длины текста в Telegram (в каждой [а/б] самый длинный вариант; {promo} = 8 символов)."""
    text = VAR_RE.sub("X" * CODE_LEN, _clean(raw))
    return shop.visible_length(shop.format_about(shop.resolve_variants(text, "", 0, longest=True)))


def normalize_percent(value) -> int | None:
    """Пустое значение -> None; иначе целое от 1 до 99 (можно с «%»). ValueError — неверно."""
    raw = str(value if value is not None else "").strip().rstrip("%").strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{1,3}", raw) or not MIN_PERCENT <= int(raw) <= MAX_PERCENT:
        raise ValueError(f"Процент скидки — целое число от {MIN_PERCENT} до {MAX_PERCENT}")
    return int(raw)


def normalize(text: str | None, percent, reply: str | None) -> AutoSettings:
    """Проверка полей с сайта. ValueError — понятное сообщение для панели."""
    text, reply = _clean(text), _clean(reply)
    pct = normalize_percent(percent)
    for title, value in (("Сообщение", text), ("Ответ на промокод", reply)):
        if len(value) > MAX_RAW:
            raise ValueError(f"{title} — не длиннее {MAX_RAW} символов (считая скобки вариантов и значки оформления)")
        shown = telegram_length(value)
        if shown > MAX_TEXT:
            raise ValueError(f"{title}: в Telegram займёт до {shown} символов, а можно не больше {MAX_TEXT}")
    if (has_variable(text) or has_variable(reply)) and pct is None:
        raise ValueError("В тексте есть {promo} — укажите процент скидки")
    if reply and not text:
        raise ValueError("Сначала напишите само сообщение — ответ на промокод без него не нужен")
    return AutoSettings(text=text, percent=pct, reply=reply)


# кэш настроек: бот читает их на каждой капче и на каждом промокоде, а менять их владелец будет редко
_CACHE_TTL = 30.0
_cache: tuple[float, AutoSettings] | None = None


def invalidate() -> None:
    global _cache
    _cache = None


async def load_settings(*, fresh: bool = False) -> AutoSettings:
    global _cache
    now = time.monotonic()
    if not fresh and _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    cfg = AutoSettings.from_json(await repo.kv_get(AUTO_KEY))
    _cache = (now, cfg)
    return cfg


async def save_settings(cfg: AutoSettings) -> None:
    await repo.kv_set(AUTO_KEY, cfg.to_json())
    invalidate()


# ---------- тексты ----------

def render(raw: str, seed: str, code: str | None = None, percent: int | None = None) -> str:
    """Текст «как введено» -> HTML Telegram: варианты [а/б] этого бота (seed), оформление, затем {promo} и {percent}.
    Код состоит только из A-Z и 0-9, поэтому подстановка после оформления разметку сломать не может."""
    html_text = shop.format_about(raw, seed, 0)
    html_text = VAR_RE.sub(code or "", html_text)
    return html_text.replace("{percent}", str(percent) if percent is not None else "")
