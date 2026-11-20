"""Рандомизированное приветствие бота.

Базовый смысл (обязательные элементы, меняется только форма):

    **💖 Добро пожаловать в <название бота>**

    🤍 Анонимность — оплата без привязок и без риска
    💫 Никаких следов — чаты не хранятся, историю никто не увидит.

    🍫 Ваш баланс: 0🍬

Что здесь устроено:
  * HELLO / ANON_KEY / … — словари синонимов (расширяются добавлением в конец);
  * PALETTES  — эмодзи сообщения. СТАТИЧНЫ: одна палитра (💖 🤍 💫 🍫), по seed больше не подбираются;
  * LAYOUTS   — структурные шаблоны (компоновка блоков), без разделителей и рамок;
  * GreetingConfig / generate_config — детерминированный выбор варианта по seed (токен бота);
  * GreetingConfig.template — ГОТОВЫЙ текст-шаблон с плейсхолдерами {name} и {balance}. Именно он хранится в БД
    и используется при работе бота, поэтому правки словарей в коде не меняют уже созданных ботов.

Правила:
  * Вариант создаётся один раз для бота и не меняется, пока жив бот (меняется только кнопкой «Песочница»).
  * 🍬 в конце строки баланса — валюта бота, ни при каких условиях не заменяется (проверяется в validate()).
  * Формат разметки — Telegram HTML (parse_mode="HTML"): <b> для жирного, <blockquote> для цитаты (только у пунктов).
    Цитата всегда отделена пустой строкой от заголовка сверху и от баланса снизу (см. _space_quote()).
  * Индексы в словарях текстов только добавляются в конец, не переставляются и не удаляются.
  * Шаблоны, сохранённые в БД у работающих ботов со случайными эмодзи, приводятся к статичным при чтении
    (`_static_emoji`, без перезаписи БД).
"""
import hashlib
import html
import json
import random
import re
from dataclasses import dataclass

CANDY = "🍬"  # валюта бота: НЕ менять и не подбирать синонимы

# ---------- синонимы ----------
# Каждый вариант сохраняет смысл целиком. Регистр: все фразы, кроме HELLO и KEYS, начинаются со строчной буквы.

# Приветствие; название бота подставляется в конец через пробел. Нейтрально, без фамильярности.
HELLO: tuple[str, ...] = (
    "Добро пожаловать в",
    "Приветствуем в",
    "Рады видеть вас в",
    "Приветствуем вас в",
    "Рады приветствовать вас в",
)

# «Анонимность»
ANON_KEY: tuple[str, ...] = (
    "Анонимность",
    "Полная анонимность",
    "Приватность",
    "Конфиденциальность",
)

# «оплата без привязок и без риска»
PAY: tuple[str, ...] = (
    "оплата без привязок и без риска",
    "оплата без привязок и рисков",
    "платежи без привязок и без риска",
    "платите без привязок и без риска",
)

# «Никаких следов»
NOLOG_KEY: tuple[str, ...] = (
    "Никаких следов",
    "Без следов",
    "Ни единого следа",
    "Следов не остаётся",
)

# «чаты не хранятся»
CHATS: tuple[str, ...] = (
    "чаты не хранятся",
    "чаты не сохраняются",
    "переписки не хранятся",
    "переписки не сохраняются",
    "диалоги не хранятся",
)

# «историю никто не увидит»
HISTORY: tuple[str, ...] = (
    "историю никто не увидит",
    "историю не увидит никто",
    "никто не увидит историю",
    "историю никто не сможет посмотреть",
)

# «Ваш баланс»
BALANCE_LABEL: tuple[str, ...] = (
    "Ваш баланс",
    "Баланс",
    "На вашем балансе",
    "Ваш текущий баланс",
)

# ---------- эмодзи (статичные) ----------
# По одному эмодзи на позицию: в приветствии у всех ботов одни и те же 💖 🤍 💫 🍫 (по seed не подбираются).
EMOJI_SETS: dict[str, tuple[str, ...]] = {
    "hello": ("💖",),
    "anon": ("🤍",),
    "nolog": ("💫",),
    "balance": ("🍫",),
}

# Единственная палитра: (hello, anon, nolog, balance). Индекс палитры в конфиге остаётся ради совместимости и всегда 0.
PALETTES: tuple[tuple[str, str, str, str], ...] = (
    ("💖", "🤍", "💫", "🍫"),
)

# Сколько палитр было раньше: при генерации ГСЧ продвигается на столько же, поэтому выбор текстовых вариантов по seed
# остаётся прежним, а меняются только эмодзи.
_LEGACY_PALETTE_COUNT = 6

# Эмодзи, которые подбирались раньше (по позициям): в шаблонах, уже сохранённых в БД, они заменяются на статичные.
_LEGACY_EMOJI: dict[str, tuple[str, ...]] = {
    "hello": ("🌸", "✨", "🌷", "🪻"),
    "anon": ("🛜", "🛡️", "🕶️", "🔐"),
    "nolog": ("📘", "📓", "🙈", "🤐"),
    "balance": ("🍭", "🧁", "🍨"),   # 🍫 уже статичный
}

# ---------- компоновка ----------
# order: "anon_first" | "nolog_first" | "any" (выбирается по seed)
# variants: подвид оформления внутри компоновки (выбирается по seed)
#   plain        — эмодзи + «Ключ — текст»
#   bold         — ключевое слово пункта выделено жирным
#   bullet_dot   — маркер «•» в начале строки
#   bullet_dash  — маркер «—» в начале строки (разделитель внутри строки тогда «:»)
#   quote_points — оба пункта в цитате, остальное обычным текстом
# Баланс никогда не оформляется цитатой (владелец проекта убрал такой вариант: выглядит криво).
@dataclass(frozen=True)
class Layout:
    key: str
    title: str
    order: str
    variants: tuple[str, ...]


LAYOUTS: tuple[Layout, ...] = (
    Layout("classic", "Классический вертикальный вид", "anon_first", ("plain",)),
    Layout("swapped", "Пункты поменяны местами", "nolog_first", ("plain",)),
    Layout("emphasis", "Маркеры или жирные ключевые слова", "any", ("bold", "bullet_dot", "bullet_dash")),
    Layout("calm", "Цитата у пунктов", "any", ("quote_points",)),
)

ORDERS = ("anon_first", "nolog_first")
ALL_VARIANTS = frozenset(v for lay in LAYOUTS for v in lay.variants)


def validate_tables() -> None:
    """Проверка словарей при импорте/в тестах: палитры берутся только из наборов, 🍬 нигде не затронута."""
    slots = ("hello", "anon", "nolog", "balance")
    for pal in PALETTES:
        for slot, emo in zip(slots, pal):
            if emo not in EMOJI_SETS[slot]:
                raise AssertionError(f"эмодзи {emo!r} не входит в набор {slot}")
    for words in (HELLO, ANON_KEY, PAY, NOLOG_KEY, CHATS, HISTORY, BALANCE_LABEL):
        if len(words) < 3 or len(set(words)) != len(words):
            raise AssertionError("в каждом словаре нужно минимум 3 разных варианта")
    for slot_set in EMOJI_SETS.values():
        if CANDY in slot_set:
            raise AssertionError("🍬 нельзя включать в наборы эмодзи")
    legacy = [e for slot in _LEGACY_EMOJI.values() for e in slot]
    if len(set(legacy)) != len(legacy) or any(e in legacy for slot in EMOJI_SETS.values() for e in slot):
        raise AssertionError("старые и новые эмодзи не должны пересекаться (иначе замена в шаблонах неоднозначна)")


validate_tables()


# ---------- конфигурация одного бота ----------

_QUOTE_BEFORE = re.compile(r"(?<=\S)\n*(?=<blockquote>)")
_QUOTE_AFTER = re.compile(r"(?<=</blockquote>)\n*(?=\S)")


def _space_quote(template: str) -> str:
    """Гарантирует пустую строку между цитатой и соседним текстом. Нужна для шаблонов, которые уже сохранены в БД
    у работающих ботов (раньше цитата стояла вплотную к тексту): они исправляются при чтении, без перезаписи в БД."""
    if "<blockquote>" not in template:
        return template
    return _QUOTE_AFTER.sub("\n\n", _QUOTE_BEFORE.sub("\n\n", template))


def _tighten_points(template: str, variant: str) -> str:
    """Убирает пустую строку между двумя пунктами (бывший вариант «airy») у шаблонов, уже сохранённых в БД."""
    if variant != "airy":
        return template
    blocks = template.split("\n\n")
    if len(blocks) == 4:   # заголовок, пункт, пункт, баланс
        blocks = [blocks[0], blocks[1] + "\n" + blocks[2], blocks[3]]
    return "\n\n".join(blocks)


def _static_emoji(template: str) -> str:
    """Заменяет эмодзи, подобранные раньше случайно, на статичные (по позициям). У шаблонов, уже собранных
    с текущей палитрой, ничего не меняется, поэтому повторный вызов безопасен. Без перезаписи в БД."""
    for slot, olds in _LEGACY_EMOJI.items():
        new = EMOJI_SETS[slot][0]
        for old in olds:
            template = template.replace(old, new)
    return template


@dataclass(frozen=True)
class GreetingConfig:
    layout: int
    variant: str
    order: str
    hello: int
    anon_key: int
    pay: int
    nolog_key: int
    chats: int
    history: int
    balance_label: int
    palette: int
    template: str  # готовый текст с {name} и {balance}: именно он показывается пользователям

    def signature(self) -> str:
        """По чему сравниваем «одинаковость» ботов: итоговый шаблон."""
        return self.template

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": 1,
                "layout": self.layout, "variant": self.variant, "order": self.order,
                "hello": self.hello, "anon_key": self.anon_key, "pay": self.pay,
                "nolog_key": self.nolog_key, "chats": self.chats, "history": self.history,
                "balance_label": self.balance_label, "palette": self.palette,
                "template": self.template,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "GreetingConfig":
        d = json.loads(raw)
        if d.get("variant") == "airy":
            d["template"] = _tighten_points(str(d["template"]), "airy")
            d["variant"] = "plain"
        cfg = cls(
            layout=int(d["layout"]), variant=str(d["variant"]), order=str(d["order"]),
            hello=int(d["hello"]), anon_key=int(d["anon_key"]), pay=int(d["pay"]),
            nolog_key=int(d["nolog_key"]), chats=int(d["chats"]), history=int(d["history"]),
            balance_label=int(d["balance_label"]), palette=0,
            template=_static_emoji(_space_quote(str(d["template"]))),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        # Шаблон — единственный источник правды; индексы нужны для отладки, поэтому их проверяем мягко.
        t = self.template
        if "{name}" not in t or "{balance}" not in t:
            raise ValueError("в шаблоне нет плейсхолдеров {name} / {balance}")
        if f"{{balance}}{CANDY}" not in t:
            raise ValueError("после баланса должна стоять 🍬")
        if any(bad in t for bad in ("---", "***", "———", "═", "─")):
            raise ValueError("декоративные разделители запрещены")


# ---------- сборка ----------

def _points(layout: Layout, variant: str, order: str, parts: dict) -> tuple[str, str]:
    """Возвращает (блок пунктов, блок баланса) уже с разметкой."""
    e_anon, e_nolog = parts["e_anon"], parts["e_nolog"]
    sep = ": " if variant == "bullet_dash" else " — "
    marker = {"bullet_dot": "• ", "bullet_dash": "— "}.get(variant, "")

    def key(text: str) -> str:
        return f"<b>{text}</b>" if variant == "bold" else text

    anon = f"{marker}{e_anon} {key(parts['anon_key'])}{sep}{parts['pay']}"
    nolog = f"{marker}{e_nolog} {key(parts['nolog_key'])}{sep}{parts['chats']}, {parts['history']}."
    lines = [anon, nolog] if order == "anon_first" else [nolog, anon]
    balance = f"{parts['e_balance']} {parts['balance_label']}: {{balance}}{CANDY}"
    # Пункты всегда идут подряд, без пустой строки между ними — иначе сообщение выглядит неаккуратно.
    return "\n".join(lines), balance


def build_template(
    *, layout: int, variant: str, order: str, hello: int, anon_key: int, pay: int,
    nolog_key: int, chats: int, history: int, balance_label: int, palette: int,
) -> str:
    """Собирает текст с плейсхолдерами {name} и {balance} (HTML для Telegram)."""
    lay = LAYOUTS[layout]
    e_hello, e_anon, e_nolog, e_balance = PALETTES[palette]
    parts = {
        "e_anon": e_anon, "e_nolog": e_nolog, "e_balance": e_balance,
        "anon_key": ANON_KEY[anon_key], "pay": PAY[pay],
        "nolog_key": NOLOG_KEY[nolog_key], "chats": CHATS[chats], "history": HISTORY[history],
        "balance_label": BALANCE_LABEL[balance_label],
    }
    title = f"<b>{e_hello} {HELLO[hello]} {{name}}</b>"
    points, balance = _points(lay, variant, order, parts)

    if variant == "quote_points":
        # цитата отделена пустой строкой и от заголовка, и от баланса — иначе сообщение выглядит слитным
        return f"{title}\n\n<blockquote>{points}</blockquote>\n\n{balance}"
    return f"{title}\n\n{points}\n\n{balance}"


def _pick_palette(rng: random.Random) -> int:
    """Палитра статична (всегда 0); шаг ГСЧ сохранён, чтобы текстовые варианты по seed остались прежними."""
    rng.randrange(_LEGACY_PALETTE_COUNT)
    return 0


def generate_config(seed: str, taken: set[str] | None = None) -> GreetingConfig:
    """Детерминированно по seed. Если итоговый текст совпал с чужим из `taken`, берётся следующая попытка
    (последовательность попыток тоже детерминирована). Тексты выбираются по seed, эмодзи статичны."""
    rng = random.Random(hashlib.sha256(("greeting:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        layout = rng.randrange(len(LAYOUTS))
        lay = LAYOUTS[layout]
        variant = rng.choice(lay.variants)
        order = rng.choice(ORDERS) if lay.order == "any" else lay.order
        choice = dict(
            layout=layout, variant=variant, order=order,
            hello=rng.randrange(len(HELLO)),
            anon_key=rng.randrange(len(ANON_KEY)),
            pay=rng.randrange(len(PAY)),
            nolog_key=rng.randrange(len(NOLOG_KEY)),
            chats=rng.randrange(len(CHATS)),
            history=rng.randrange(len(HISTORY)),
            balance_label=rng.randrange(len(BALANCE_LABEL)),
            palette=_pick_palette(rng),
        )
        cfg = GreetingConfig(**choice, template=build_template(**choice))
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg


# ---------- вывод пользователю ----------

def format_number(n: int) -> str:
    """Число для текста пользователю — без разделителей разрядов: 1000, 12345 (не «1 000»)."""
    return str(int(n))


def render(cfg: GreetingConfig, bot_name: str, balance: int) -> str:
    """Готовый текст (HTML) для конкретного пользователя: название бота экранируется, число форматируется."""
    # сначала баланс, потом имя: если в имени бота окажется «{balance}», оно не должно подмениться
    return (
        cfg.template
        .replace("{balance}", format_number(balance))
        .replace("{name}", html.escape(bot_name or "", quote=False))
    )
