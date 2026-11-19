"""Раздел «Галерея»: сообщение и inline-кнопки (поштучная покупка случайных видео).

Сообщение (HTML):
    <b>{название кнопки галереи этого бота}</b>

    {маркер} {описание} {маркер}

    {строка баланса из приветствия этого бота}

  * название — текст кнопки «Галерея» из shop_buttons (без эмодзи); подставляется при отправке;
  * маркер — общий декоративный эмодзи бота (app/deco.py);
  * строка баланса — ровно последняя строка шаблона приветствия бота (та же формулировка, эмодзи и 🍬).
Описание, подписи и callback_data кнопок подбираются один раз по seed (токен) и хранятся в БД.
Цена видео фиксирована — 3🍬 (PRICE), входит в тексты кнопки покупки и не меняется.
Кнопка «⬅️ Назад» без рандомизации текста; её callback_data уникален для бота.
Кнопка «Предложка» стоит перед «Назад», её подпись рандомизируется отдельно (suggest_ui.py); её callback_data и callback_data
«Назад» внутри меню предложки не хранятся, а выводятся из back_cb бота (suggest_cb / suggest_back_cb): так они
уникальны для бота и появляются у уже сохранённых в БД конфигов без перезаписи.
"""
import hashlib
import html
import json
import random
from dataclasses import dataclass

PRICE = 3
CANDY = "🍬"
BACK_LABEL = "⬅️ Назад"
SUGGEST_LABEL = "📩 Предложка 🤍"  # эталон; у каждого бота своя подпись из suggest_ui (хранится в БД)

DESCRIPTIONS: tuple[str, ...] = (
    "Здесь вы можете поштучно покупать случайные видео из нашей галереи",
    "В этом разделе можно поштучно открывать случайные ролики из галереи",
    "Поштучный доступ к случайным видео из общей галереи бота",
    "Открывайте случайные видео из галереи по одному",
    "Покупайте случайные видео из галереи по одному",
)

BUY_TEXTS: tuple[str, ...] = (
    f"Купить видео ({PRICE}{CANDY})", f"Открыть видео ({PRICE}{CANDY})",
    f"Случайное видео ({PRICE}{CANDY})", f"Смотреть видео ({PRICE}{CANDY})",
)
BUY_EMOJI: tuple[tuple[str, str], ...] = (("🎥", "🖤"), ("🎬", "✨"), ("🎞️", "💫"), ("📹", "🤍"))

# Подписка: бесплатный просмотр видео + можно скачивать и пересылать контент из бота
PREMIUM_TEXTS: tuple[str, ...] = ("Премиум подписка", "Премиум-доступ", "VIP-подписка")
PREMIUM_EMOJI: tuple[tuple[str, str], ...] = (("💝", "👑"), ("👑", "✨"), ("💎", "👑"), ("🌟", "💎"))

# Подсказка под описанием: как заработать конфеты. {proposal_title} подставляется при отправке — это название
# кнопки предложки ЭТОГО бота (suggest_ui, без эмодзи), поэтому подсказка всегда совпадает с кнопкой на экране.
# Количество конфет не указывается. Только добавлять в конец.
HINT_TITLE = "{proposal_title}"
HINTS: tuple[str, ...] = (
    "🤍 Заработать конфеты можно, нажав «{proposal_title}»",
    "🍬 Заработать конфеты можно через «{proposal_title}»",
    "✨ Нажмите «{proposal_title}», чтобы заработать конфеты",
    "💫 Зарабатывайте конфеты, выбрав «{proposal_title}»",
)
DEFAULT_SUGGEST_NAME = "Предложка"

BUY_PREFIX, PREMIUM_PREFIX, BACK_PREFIX = "gb:", "gp:", "gk:"
SUGGEST_PREFIX, SUGGEST_BACK_PREFIX = "gs:", "gx:"
HASH_LEN = 10


def validate_tables() -> None:
    for words in (DESCRIPTIONS, BUY_TEXTS, PREMIUM_TEXTS, BUY_EMOJI, PREMIUM_EMOJI, HINTS):
        if len(set(words)) != len(words):
            raise AssertionError("повторы в списках")
    for h in HINTS:
        if HINT_TITLE not in h or any(ch.isdigit() for ch in h):
            raise AssertionError("в подсказке должно быть название предложки и не должно быть чисел")
    for t in BUY_TEXTS:
        if f"({PRICE}{CANDY})" not in t:
            raise AssertionError("цена должна быть в каждом тексте кнопки покупки")


validate_tables()


# Тексты, убранные из списков: у уже работающих ботов они лежат в БД готовой подписью и заменяются при чтении.
_LEGACY_PREMIUM = {"Безлимит и скачивание": "Премиум подписка"}


def label(text: str, emoji: tuple[str, str]) -> str:
    """«🎥 Купить видео (3🍬) 🖤» — эмодзи по обе стороны, без разделителей."""
    return f"{emoji[0]} {text} {emoji[1]}"


@dataclass(frozen=True)
class GalleryConfig:
    desc: int
    buy_text: int
    buy_emoji: int
    premium_text: int
    premium_emoji: int
    description: str   # готовая фраза (именно она показывается)
    buy_label: str
    premium_label: str
    buy_cb: str
    premium_cb: str
    back_cb: str
    hint: str = ""     # шаблон подсказки с {proposal_title}; пусто — у старых конфигов (см. hint_template)

    @property
    def hint_template(self) -> str:
        """Сохранённый шаблон подсказки. У конфигов, созданных до появления подсказки, вариант выводится из back_cb:
        он уникален для бота и не меняется, поэтому подсказка стабильна без перезаписи БД."""
        if self.hint:
            return self.hint
        return HINTS[int(hashlib.sha256(f"hint:{self.back_cb}".encode()).hexdigest()[:8], 16) % len(HINTS)]

    @property
    def suggest_cb(self) -> str:
        """Кнопка «Предложка» в меню галереи (выводится из back_cb: уникальна для бота, не хранится)."""
        return SUGGEST_PREFIX + hashlib.sha256(f"suggest:{self.back_cb}".encode()).hexdigest()[:HASH_LEN]

    @property
    def suggest_back_cb(self) -> str:
        """Кнопка «Назад» внутри меню предложки."""
        return SUGGEST_BACK_PREFIX + hashlib.sha256(f"suggest-back:{self.back_cb}".encode()).hexdigest()[:HASH_LEN]

    def signature(self) -> str:
        return "|".join((self.description, self.hint_template, self.buy_label, self.premium_label,
                         self.buy_cb, self.premium_cb, self.back_cb))

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": 1, "desc": self.desc, "buy_text": self.buy_text, "buy_emoji": self.buy_emoji,
                "premium_text": self.premium_text, "premium_emoji": self.premium_emoji,
                "description": self.description, "buy_label": self.buy_label,
                "premium_label": self.premium_label,
                "buy_cb": self.buy_cb, "premium_cb": self.premium_cb, "back_cb": self.back_cb,
                "hint": self.hint_template,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "GalleryConfig":
        d = json.loads(raw)
        for old, new in _LEGACY_PREMIUM.items():
            if old in str(d.get("premium_label", "")):
                d["premium_label"] = str(d["premium_label"]).replace(old, new)
        cfg = cls(
            desc=int(d["desc"]), buy_text=int(d["buy_text"]), buy_emoji=int(d["buy_emoji"]),
            premium_text=int(d["premium_text"]), premium_emoji=int(d["premium_emoji"]),
            description=str(d["description"]), buy_label=str(d["buy_label"]),
            premium_label=str(d["premium_label"]),
            buy_cb=str(d["buy_cb"]), premium_cb=str(d["premium_cb"]), back_cb=str(d["back_cb"]),
            hint=str(d.get("hint") or ""),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.description.strip() or not self.premium_label.strip():
            raise ValueError("пустой текст")
        if HINT_TITLE not in self.hint_template:
            raise ValueError("в подсказке должно быть {proposal_title}")
        if f"{PRICE}{CANDY}" not in self.buy_label:
            raise ValueError("в кнопке покупки должна быть цена 3🍬")
        if not (self.buy_cb.startswith(BUY_PREFIX) and self.premium_cb.startswith(PREMIUM_PREFIX)
                and self.back_cb.startswith(BACK_PREFIX)):
            raise ValueError("неверный префикс callback_data")
        if len({self.buy_cb, self.premium_cb, self.back_cb}) != 3:
            raise ValueError("callback_data совпадают")
        for cb in (self.buy_cb, self.premium_cb, self.back_cb):
            if len(cb.encode()) > 64 or not cb.isascii():
                raise ValueError("callback_data: только ASCII, до 64 байт")
        if any(bad in (self.description + self.buy_label + self.premium_label)
               for bad in ("---", "***", "———", "═", "─", "│")):
            raise ValueError("декоративные разделители запрещены")


def generate_config(seed: str, taken: set[str] | None = None) -> GalleryConfig:
    """Детерминированно по seed; текст и эмодзи каждой кнопки выбираются независимо."""
    rng = random.Random(hashlib.sha256(("gallery:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        desc = rng.randrange(len(DESCRIPTIONS))
        hint = HINTS[rng.randrange(len(HINTS))]
        buy_text, buy_emoji = rng.randrange(len(BUY_TEXTS)), rng.randrange(len(BUY_EMOJI))
        premium_text, premium_emoji = rng.randrange(len(PREMIUM_TEXTS)), rng.randrange(len(PREMIUM_EMOJI))
        digest = hashlib.sha256(f"gcb:{seed}:{rng.getrandbits(64)}".encode()).hexdigest()
        cfg = GalleryConfig(
            desc=desc, buy_text=buy_text, buy_emoji=buy_emoji,
            premium_text=premium_text, premium_emoji=premium_emoji,
            description=DESCRIPTIONS[desc],
            buy_label=label(BUY_TEXTS[buy_text], BUY_EMOJI[buy_emoji]),
            premium_label=label(PREMIUM_TEXTS[premium_text], PREMIUM_EMOJI[premium_emoji]),
            buy_cb=BUY_PREFIX + digest[:HASH_LEN],
            premium_cb=PREMIUM_PREFIX + digest[HASH_LEN:2 * HASH_LEN],
            back_cb=BACK_PREFIX + digest[2 * HASH_LEN:3 * HASH_LEN],
            hint=hint,
        )
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg


def balance_line(greeting_template: str, balance_text: str) -> str:
    """Строка баланса точно как в приветствии бота: последняя строка его шаблона."""
    last = greeting_template.rstrip().splitlines()[-1]
    return last.replace("{balance}", balance_text)


def hint_line(cfg: GalleryConfig, suggest_name: str) -> str:
    """Подсказка «как заработать конфеты» с названием кнопки предложки этого бота (экранируется)."""
    return cfg.hint_template.replace(HINT_TITLE, html.escape(suggest_name, quote=False))


def render(
    cfg: GalleryConfig, deco, title: str, greeting_template: str, balance: int,
    suggest_name: str = DEFAULT_SUGGEST_NAME,
) -> str:
    """Текст раздела (HTML). title — текст кнопки галереи без эмодзи; suggest_name — название кнопки предложки
    этого бота (suggest_ui.SuggestConfig.name); оба экранируются."""
    return (
        f"<b>{html.escape(title, quote=False)}</b>\n\n"
        f"{deco.wrap(cfg.description)}\n\n"
        f"{hint_line(cfg, suggest_name)}\n\n"
        f"{balance_line(greeting_template, str(int(balance)))}"
    )


def layout_rows(cfg: GalleryConfig, suggest_label: str = SUGGEST_LABEL) -> list[list[tuple[str, str]]]:
    """Четыре кнопки по одной в ряд: покупка, премиум, предложка, назад (без aiogram — удобно для тестов)."""
    return [
        [(cfg.buy_label, cfg.buy_cb)],
        [(cfg.premium_label, cfg.premium_cb)],
        [(suggest_label, cfg.suggest_cb)],
        [(BACK_LABEL, cfg.back_cb)],
    ]


# Цвета кнопок галереи по порядку рядов (Bot API 9.4+, нужен aiogram >= 3.25):
# покупка — синяя, премиум — зелёная, «Предложка» — синяя, «Назад» — красная.
ROW_STYLES = ("primary", "success", "primary", "danger")


def _button(text: str, cb: str, style: str | None):
    from aiogram.types import InlineKeyboardButton

    return InlineKeyboardButton(text=text, callback_data=cb, **({"style": style} if style else {}))


def build_keyboard(cfg: GalleryConfig, suggest_label: str = SUGGEST_LABEL):
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(t, cb, style) for t, cb in row]
            for row, style in zip(layout_rows(cfg, suggest_label), ROW_STYLES)
        ]
    )


def video_rows(cfg: GalleryConfig) -> list[list[tuple[str, str]]]:
    """Под купленным видео ровно две кнопки: та же покупка и «Назад» (callback_data те же, что в меню галереи)."""
    return [[(cfg.buy_label, cfg.buy_cb)], [(BACK_LABEL, cfg.back_cb)]]


def build_video_keyboard(cfg: GalleryConfig):
    from aiogram.types import InlineKeyboardMarkup

    styles = (ROW_STYLES[0], ROW_STYLES[3])  # те же цвета, что у этих кнопок в галерее
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(t, cb, style) for t, cb in row] for row, style in zip(video_rows(cfg), styles)]
    )


def build_suggest_keyboard(cfg: GalleryConfig):
    """Меню предложки: одна кнопка «Назад» (возвращает в меню галереи)."""
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[_button(BACK_LABEL, cfg.suggest_back_cb, None)]])


def parse_action(cfg: GalleryConfig, data: str | None) -> str | None:
    """'buy' / 'premium' / 'back' / 'suggest' / 'suggest_back' по callback_data ЭТОГО бота; None — чужие или устаревшие."""
    if not data:
        return None
    return {
        cfg.buy_cb: "buy", cfg.premium_cb: "premium", cfg.back_cb: "back",
        cfg.suggest_cb: "suggest", cfg.suggest_back_cb: "suggest_back",
    }.get(data)
