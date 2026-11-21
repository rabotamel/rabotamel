"""Раздел «Галерея»: сообщение и inline-кнопки (поштучная покупка случайных видео).

Сообщение (HTML):
    <b>{название кнопки галереи этого бота}</b>

    {маркер} {описание} {маркер}

    {подсказка: как заработать конфеты}

    {строка баланса из приветствия этого бота}

  * название — подпись кнопки «Галерея» из shop_buttons («🎥 Галерея 💖»; эмодзи статичны);
  * маркер — статичный 💫 (app/deco.py): «💫 описание 💫»;
  * подсказка — «🤍 <фраза с названием предложки> 🍬» (HINT_LEFT / HINT_RIGHT, статичные);
  * строка баланса — ровно последняя строка шаблона приветствия бота («🍭 Ваш баланс: N🍬»).
ЭМОДЗИ ВО ВСЕХ ЭЛЕМЕНТАХ СТАТИЧНЫ (по seed не подбираются). Тексты описания/подсказки/кнопок и callback_data подбираются
один раз по seed (токен) и хранятся в БД; у уже сохранённых конфигов эмодзи приводятся к статичным при чтении.
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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PRICE = 3
CANDY = "🍬"
BACK_LABEL = "⬅️ Назад"

# Премиум-подписка (по образцу присланного владельцем gallery.py): три тарифа, цена в 🍬; эмодзи заданы владельцем и статичны.
# Премиум: видео из галереи бесплатно (0🍬) и без защиты от пересылки/сохранения.
PREMIUM_OPTIONS: dict[str, dict] = {
    "3": {"days": 3, "price": 650, "label": "💕 3 дня (650🍬) 👑"},
    "7": {"days": 7, "price": 1250, "label": "🔥 Неделя (1250🍬) 👑"},
    "forever": {"days": None, "price": 1750, "label": "💎 Безлимит (1750🍬) 👑"},
}
PREMIUM_KEYS = tuple(PREMIUM_OPTIONS)
PREMIUM_BACK_KEY = "b"   # callback_data «Назад» в меню премиума: «<premium_cb>:b»; тарифы: «<premium_cb>:3|7|forever»
PREMIUM_INFO = "🌸 С премиумом вы сможете <b>бесплатно</b> смотреть, скачивать и пересылать видео из галереи 🌸"
PREMIUM_BOUGHT = {"3": "на 3 дня", "7": "на неделю", "forever": "навсегда"}

# --- Рандомизация меню премиума -------------------------------------------------------------------------------------
# Варианты ТЕКСТОВ экрана премиума (эмодзи статичны, как и во всём проекте). Вариант каждого элемента выводится из back_cb
# бота (он уникален для бота и не меняется): без записи в БД, у уже работающих ботов вариант просто «появляется» и дальше
# стабилен. Первый вариант в каждом списке — прежний текст. Цены, callback_data и расчёт остатка времени НЕ рандомизируются.
PREMIUM_TITLES: tuple[str, ...] = ("Премиум подписка", "Премиум-доступ", "VIP-подписка", "Премиум-статус")
PREMIUM_TITLE_EMOJI: tuple[str, str] = ("💫", "💖")
PREMIUM_INFOS: tuple[str, ...] = (
    "С премиумом вы сможете <b>бесплатно</b> смотреть, скачивать и пересылать видео из галереи",
    "Премиум открывает <b>бесплатный</b> просмотр видео из галереи, а также их скачивание и пересылку",
    "Оформите премиум: видео из галереи — <b>бесплатно</b>, их можно скачивать и пересылать",
    "Премиум даёт <b>бесплатный</b> просмотр роликов из галереи и возможность скачивать и пересылать их",
)
PREMIUM_INFO_EMOJI = "🌸"
STATUS_HEADS: tuple[str, ...] = ("Статус", "Ваш статус", "Статус подписки", "Состояние подписки")
STATUS_HEAD_EMOJI: tuple[str, str] = ("🔥", "🩷")
# Формулировки статуса по трём состояниям; в «временном» обязательно {left} — остаток «Xд Yч Zм» подставляет premium_status.
STATUS_NONE: tuple[str, ...] = ("У вас нет премиума", "Премиум не подключён", "Премиум пока не оформлен", "Подписка не активна")
STATUS_TEMP: tuple[str, ...] = (
    "Ваш премиум истечёт через {left}", "Премиум активен, осталось {left}",
    "Подписка действует ещё {left}", "До конца премиума осталось {left}",
)
STATUS_FOREVER: tuple[str, ...] = (
    "Ваш премиум безлимитный", "У вас безлимитный премиум", "Премиум активен навсегда", "Подписка бессрочная",
)
STATUS_EMOJI = {"none": "🤷🏻‍♀️", "temp": "❤️‍🔥", "forever": "💎"}
# Подписи тарифов: «{эмодзи} {текст} ({цена}🍬) 👑»; цена берётся из PREMIUM_OPTIONS и не рандомизируется.
TARIFF_TEXTS: dict[str, tuple[str, ...]] = {
    "3": ("3 дня", "На 3 дня", "Три дня", "3 суток"),
    "7": ("Неделя", "7 дней", "На неделю", "Семь дней"),
    "forever": ("Безлимит", "Навсегда", "Без ограничений", "Бессрочно"),
}
TARIFF_EMOJI = {"3": "💕", "7": "🔥", "forever": "💎"}
TARIFF_END_EMOJI = "👑"
PREMIUM_BACK_TEXTS: tuple[str, ...] = ("Назад", "Вернуться", "Вернуться назад", "В галерею")
PREMIUM_BACK_EMOJI = "⬅️"


@dataclass(frozen=True)
class PremiumUi:
    """Готовые тексты меню премиума одного бота (см. premium_ui)."""
    title: str          # без эмодзи
    info: str           # без эмодзи, с <b>
    status_head: str    # без эмодзи
    status_none: str
    status_temp: str    # с {left}
    status_forever: str
    tariffs: dict       # ключ тарифа -> текст без эмодзи и цены
    back: str           # без эмодзи


def _variant(cfg: "GalleryConfig", name: str, options: tuple[str, ...]) -> str:
    """Стабильный для бота выбор варианта: каждый элемент выбирается независимо, по хэшу от back_cb бота."""
    h = int(hashlib.sha256(f"premium-ui:{name}:{cfg.back_cb}".encode()).hexdigest()[:8], 16)
    return options[h % len(options)]


def premium_ui(cfg: "GalleryConfig | None") -> PremiumUi:
    """Тексты меню премиума для бота; cfg=None — прежние (первые) варианты."""
    if cfg is None:
        pick = lambda name, options: options[0]  # noqa: E731
    else:
        pick = lambda name, options: _variant(cfg, name, options)  # noqa: E731
    return PremiumUi(
        title=pick("title", PREMIUM_TITLES), info=pick("info", PREMIUM_INFOS),
        status_head=pick("status_head", STATUS_HEADS),
        status_none=pick("status_none", STATUS_NONE), status_temp=pick("status_temp", STATUS_TEMP),
        status_forever=pick("status_forever", STATUS_FOREVER),
        tariffs={k: pick(f"tariff:{k}", TARIFF_TEXTS[k]) for k in PREMIUM_KEYS},
        back=pick("back", PREMIUM_BACK_TEXTS),
    )
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
# Эмодзи кнопок статичны: одна пара на кнопку (индекс buy_emoji / premium_emoji в конфиге остаётся ради совместимости и всегда 0).
BUY_EMOJI: tuple[tuple[str, str], ...] = (("🎥", "💖"),)

# Подписка: бесплатный просмотр видео + можно скачивать и пересылать контент из бота
PREMIUM_TEXTS: tuple[str, ...] = ("Премиум подписка", "Премиум-доступ", "VIP-подписка")
PREMIUM_EMOJI: tuple[tuple[str, str], ...] = (("💫", "💖"),)

# Сколько вариантов эмодзи было раньше: при генерации ГСЧ продвигается на столько же, поэтому выбор текстов
# и callback_data по seed остаётся прежним, а меняются только эмодзи.
_LEGACY_BUY_EMOJI_COUNT = 4
_LEGACY_PREMIUM_EMOJI_COUNT = 4

# Подсказка под описанием: как заработать конфеты. {proposal_title} подставляется при отправке — это название
# кнопки предложки ЭТОГО бота (suggest_ui, без эмодзи), поэтому подсказка всегда совпадает с кнопкой на экране.
# Количество конфет не указывается. Только добавлять в конец. Эмодзи в шаблонах нет: на экране подсказка
# обрамлена статичной парой HINT_LEFT / HINT_RIGHT («🤍 Заработать конфеты можно, нажав «Предложка» 🍬»).
HINT_TITLE = "{proposal_title}"
HINT_LEFT, HINT_RIGHT = "🤍", CANDY
HINTS: tuple[str, ...] = (
    "Заработать конфеты можно, нажав «{proposal_title}»",
    "Заработать конфеты можно через «{proposal_title}»",
    "Нажмите «{proposal_title}», чтобы заработать конфеты",
    "Зарабатывайте конфеты, выбрав «{proposal_title}»",
)
DEFAULT_SUGGEST_NAME = "Предложка"

BUY_PREFIX, PREMIUM_PREFIX, BACK_PREFIX = "gb:", "gp:", "gk:"
SUGGEST_PREFIX, SUGGEST_BACK_PREFIX = "gs:", "gx:"
HASH_LEN = 10


_LEAD_NOISE = re.compile(r"^[^\w«{]+")


def _strip_lead(text: str) -> str:
    """Убирает эмодзи и пробелы в начале шаблона подсказки («🍬 Заработать…» -> «Заработать…»)."""
    return _LEAD_NOISE.sub("", text)


def validate_tables() -> None:
    for words in (DESCRIPTIONS, BUY_TEXTS, PREMIUM_TEXTS, BUY_EMOJI, PREMIUM_EMOJI, HINTS):
        if len(set(words)) != len(words):
            raise AssertionError("повторы в списках")
    for h in HINTS:
        if HINT_TITLE not in h or any(ch.isdigit() for ch in h):
            raise AssertionError("в подсказке должно быть название предложки и не должно быть чисел")
        if _strip_lead(h) != h:
            raise AssertionError("эмодзи в шаблон подсказки не входят: их ставит hint_line (HINT_LEFT / HINT_RIGHT)")
    for t in BUY_TEXTS:
        if f"({PRICE}{CANDY})" not in t:
            raise AssertionError("цена должна быть в каждом тексте кнопки покупки")
    for words in (PREMIUM_TITLES, PREMIUM_INFOS, STATUS_HEADS, STATUS_NONE, STATUS_TEMP, STATUS_FOREVER, PREMIUM_BACK_TEXTS,
                  *TARIFF_TEXTS.values()):
        if len(set(words)) != len(words):
            raise AssertionError("повторы в вариантах меню премиума")
    if any("{left}" not in t for t in STATUS_TEMP) or any("{left}" in t for t in STATUS_NONE + STATUS_FOREVER):
        raise AssertionError("{left} (остаток времени) нужен ровно во «временном» статусе")
    if any("<b>бесплатн" not in t.lower() for t in PREMIUM_INFOS):
        raise AssertionError("в описании премиума «бесплатно» выделяется <b>")
    if set(TARIFF_TEXTS) != set(PREMIUM_KEYS) or any(f"{o['price']}" in t for k, o in PREMIUM_OPTIONS.items() for t in TARIFF_TEXTS[k]):
        raise AssertionError("тарифы: ключи как в PREMIUM_OPTIONS, цена в текст не входит (её подставляет premium_label)")


validate_tables()


# Тексты, убранные из списков: у уже работающих ботов они лежат в БД готовой подписью и заменяются при чтении.
_LEGACY_PREMIUM = {"Безлимит и скачивание": "Премиум подписка"}


def label(text: str, emoji: tuple[str, str]) -> str:
    """«🎥 Купить видео (3🍬) 💖» — эмодзи по обе стороны, без разделителей."""
    return f"{emoji[0]} {text} {emoji[1]}"


def _static_label(lbl: str, emoji: tuple[str, str]) -> str:
    """Подпись из БД со статичной парой эмодзи: текст между эмодзи (в том числе «(3🍬)») сохраняется как есть."""
    parts = lbl.split(" ")
    if len(parts) < 3:
        return lbl
    return label(" ".join(parts[1:-1]), emoji)


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
        # эмодзи статичны: подписи, сохранённые у работающих ботов со случайной парой, приводятся к текущей (без записи в БД)
        cfg = cls(
            desc=int(d["desc"]), buy_text=int(d["buy_text"]), buy_emoji=0,
            premium_text=int(d["premium_text"]), premium_emoji=0,
            description=str(d["description"]),
            buy_label=_static_label(str(d["buy_label"]), BUY_EMOJI[0]),
            premium_label=_static_label(str(d["premium_label"]), PREMIUM_EMOJI[0]),
            buy_cb=str(d["buy_cb"]), premium_cb=str(d["premium_cb"]), back_cb=str(d["back_cb"]),
            hint=_strip_lead(str(d.get("hint") or "")),
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
    """Детерминированно по seed; тексты (описание, подсказка, кнопки) выбираются независимо, эмодзи статичны."""
    rng = random.Random(hashlib.sha256(("gallery:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        desc = rng.randrange(len(DESCRIPTIONS))
        hint = HINTS[rng.randrange(len(HINTS))]
        buy_text = rng.randrange(len(BUY_TEXTS))
        rng.randrange(_LEGACY_BUY_EMOJI_COUNT)        # эмодзи статичны; шаг ГСЧ сохранён, чтобы тексты по seed не сдвинулись
        premium_text = rng.randrange(len(PREMIUM_TEXTS))
        rng.randrange(_LEGACY_PREMIUM_EMOJI_COUNT)
        buy_emoji = premium_emoji = 0
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
    """Подсказка «как заработать конфеты» с названием кнопки предложки этого бота (экранируется), в статичной
    обрамляющей паре: «🤍 Заработать конфеты можно, нажав «Предложка» 🍬»."""
    text = cfg.hint_template.replace(HINT_TITLE, html.escape(suggest_name, quote=False))
    return f"{HINT_LEFT} {text} {HINT_RIGHT}"


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


def premium_active(until: datetime | None, forever: bool, now: datetime | None = None) -> bool:
    """Действует ли премиум: «навсегда» или дата окончания ещё впереди."""
    if forever:
        return True
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > (now or datetime.now(timezone.utc))


def format_left(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total <= 0:
        return "0д 0ч 0м"
    return f"{total // 86400}д {total % 86400 // 3600}ч {total % 3600 // 60}м"


def premium_status(
    until: datetime | None, forever: bool, now: datetime | None = None, ui: PremiumUi | None = None,
) -> str:
    """Статус подписки: безлимит / временный (остаток «Xд Yч Zм» считается здесь) / нет премиума; формулировки — из ui."""
    ui = ui or premium_ui(None)
    now = now or datetime.now(timezone.utc)
    if forever:
        return f"{STATUS_EMOJI['forever']} {ui.status_forever}"
    if premium_active(until, False, now):
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return f"{STATUS_EMOJI['temp']} {ui.status_temp.format(left=format_left(until - now))}"
    return f"{STATUS_EMOJI['none']} {ui.status_none}"


def premium_title(ui: PremiumUi) -> str:
    return label(ui.title, PREMIUM_TITLE_EMOJI)


def premium_info(ui: PremiumUi) -> str:
    return f"{PREMIUM_INFO_EMOJI} {ui.info} {PREMIUM_INFO_EMOJI}"


def tariff_label(ui: PremiumUi, key: str) -> str:
    """«💕 3 дня (650🍬) 👑»: цена из PREMIUM_OPTIONS."""
    return f"{TARIFF_EMOJI[key]} {ui.tariffs[key]} ({PREMIUM_OPTIONS[key]['price']}{CANDY}) {TARIFF_END_EMOJI}"


def premium_back_label(ui: PremiumUi) -> str:
    return f"{PREMIUM_BACK_EMOJI} {ui.back}"


def render_premium(
    cfg: GalleryConfig, greeting_template: str, balance: int, until: datetime | None, forever: bool,
    now: datetime | None = None,
) -> str:
    """Экран премиума (HTML): заголовок, описание преимуществ, блок статуса и баланс. Тексты заголовка, описания, блока
    статуса и самого статуса (нет / временный / безлимит) — свои у каждого бота (premium_ui); баланс не рандомизируется."""
    ui = premium_ui(cfg)
    return (
        f"<b>{html.escape(premium_title(ui), quote=False)}</b>\n\n"
        f"{premium_info(ui)}\n\n"
        f"<b>{STATUS_HEAD_EMOJI[0]} {html.escape(ui.status_head, quote=False)} {STATUS_HEAD_EMOJI[1]}</b>\n"
        f"{premium_status(until, forever, now, ui)}\n\n"
        f"{balance_line(greeting_template, str(int(balance)))}"
    )


def premium_cb(cfg: GalleryConfig, key: str) -> str:
    return f"{cfg.premium_cb}:{key}"


def parse_premium_key(cfg: GalleryConfig, data: str | None) -> str | None:
    """Тариф ('3' | '7' | 'forever') или PREMIUM_BACK_KEY по callback_data меню премиума ЭТОГО бота; иначе None."""
    prefix = cfg.premium_cb + ":"
    if not data or not data.startswith(prefix):
        return None
    key = data[len(prefix):]
    return key if key in PREMIUM_KEYS or key == PREMIUM_BACK_KEY else None


def premium_rows(cfg: GalleryConfig) -> list[list[tuple[str, str]]]:
    """Меню премиума: три тарифа по одной кнопке в ряд и «Назад» (в галерею). Тексты — из premium_ui, callback_data прежние."""
    ui = premium_ui(cfg)
    rows = [[(tariff_label(ui, k), premium_cb(cfg, k))] for k in PREMIUM_KEYS]
    rows.append([(premium_back_label(ui), premium_cb(cfg, PREMIUM_BACK_KEY))])
    return rows


def build_premium_keyboard(cfg: GalleryConfig):
    from aiogram.types import InlineKeyboardMarkup

    styles = ("success",) * len(PREMIUM_KEYS) + ("danger",)
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(t, cb, style) for t, cb in row] for row, style in zip(premium_rows(cfg), styles)]
    )


def free_label(label: str) -> str:
    """Подпись покупки видео для премиума: цена 0🍬 вместо 3🍬."""
    return label.replace(f"({PRICE}{CANDY})", f"(0{CANDY})")


def layout_rows(cfg: GalleryConfig, suggest_label: str = SUGGEST_LABEL, premium: bool = False) -> list[list[tuple[str, str]]]:
    """Четыре кнопки по одной в ряд: покупка, премиум, предложка, назад (без aiogram — удобно для тестов).
    У пользователя с премиумом в подписи покупки стоит (0🍬)."""
    return [
        [(free_label(cfg.buy_label) if premium else cfg.buy_label, cfg.buy_cb)],
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


def build_keyboard(cfg: GalleryConfig, suggest_label: str = SUGGEST_LABEL, premium: bool = False):
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(t, cb, style) for t, cb in row]
            for row, style in zip(layout_rows(cfg, suggest_label, premium), ROW_STYLES)
        ]
    )


def video_rows(cfg: GalleryConfig, premium: bool = False) -> list[list[tuple[str, str]]]:
    """Под купленным видео ровно две кнопки: та же покупка и «Назад» (callback_data те же, что в меню галереи)."""
    return [[(free_label(cfg.buy_label) if premium else cfg.buy_label, cfg.buy_cb)], [(BACK_LABEL, cfg.back_cb)]]


def build_video_keyboard(cfg: GalleryConfig, premium: bool = False):
    from aiogram.types import InlineKeyboardMarkup

    styles = (ROW_STYLES[0], ROW_STYLES[3])  # те же цвета, что у этих кнопок в галерее
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(t, cb, style) for t, cb in row] for row, style in zip(video_rows(cfg, premium), styles)]
    )


def build_suggest_keyboard(cfg: GalleryConfig):
    """Меню предложки: одна красная кнопка «Назад» (возвращает в меню галереи)."""
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[_button(BACK_LABEL, cfg.suggest_back_cb, "danger")]])


def parse_action(cfg: GalleryConfig, data: str | None) -> str | None:
    """'buy' / 'premium' / 'premium_buy' / 'premium_back' / 'back' / 'suggest' / 'suggest_back' по callback_data ЭТОГО бота; None — чужие или устаревшие."""
    if not data:
        return None
    action = {
        cfg.buy_cb: "buy", cfg.premium_cb: "premium", cfg.back_cb: "back",
        cfg.suggest_cb: "suggest", cfg.suggest_back_cb: "suggest_back",
    }.get(data)
    if action is None and (key := parse_premium_key(cfg, data)) is not None:
        action = "premium_back" if key == PREMIUM_BACK_KEY else "premium_buy"   # какой тариф — parse_premium_key
    return action
