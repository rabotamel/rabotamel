"""Раздел «Магазин»: меню с товарами из БД и карточка товара за конфеты (🍬).

Меню (HTML), показывается по кнопке «Магазин» под приветствием:
    <b>{подпись кнопки магазина этого бота}</b>          <- заголовок ЖИРНЫМ шрифтом

    🌸 Чтобы ознакомиться с товаром, выберите необходимый, нажав на соответствующую кнопку 🌸

    🍭 Ваш баланс: {баланс}🍬
  кнопки: по одной на каждый видимый товар из БД «{Название} — {Цена}🍬» (цвет — у каждого товара свой)
  и в самом низу «⬅️ Назад» (красная; в главное меню). Товаров нет — меню открывается без кнопок товаров.

Карточка товара — то же сообщение, обновлённое на месте:
    <b>📦 Товар:</b> {название}
    🍭 <b>Цена:</b> {цена}🍬

    <b>ℹ️ О товаре:</b>

    {описание из БД, оформительские маркеры переведены в HTML (format_about)}
  кнопки: «🍭 Купить ({цена}🍬)» (зелёная, пока заглушка) и «⬅️ Назад» (красная; обратно в меню магазина).

Товары лежат в таблице `shop_products` (общий каталог для всех токенов бота; см. models.ShopProduct, repo.*_shop_product,
вкладка «Кнопки в боте» в панели). Описание хранится ТАК, КАК ЕГО ВВЕЛИ (с `**...**` и т. п.), а в HTML переводится при показе
(`format_about`): так правка правил оформления действует сразу на все товары, а в БД не лежит готовый HTML.

Рандомизация — по схеме проекта (NOTES.md): по seed (токен бота) для каждого бота один раз подбираются `callback_data`
(префикс действия + хэш; к товару добавляется его id: `sp:<хэш>:<id>`). ЭМОДЗИ СТАТИЧНЫ и по seed не подбираются
(🌸 🍭 🍬 📦 ℹ️ ⬅️). Поля `about`/`intro`/… в ShopConfig и константы PRODUCT_NAME/PRICE — наследие версии с одним зашитым
товаром: они остаются, чтобы конфиги, уже лежащие в БД, продолжали читаться, но на экран больше не выводятся.
Индексы в списках текстов только ДОБАВЛЯЮТСЯ в конец, не переставляются и не удаляются.
"""
import hashlib
import html
import json
import random
import re
from dataclasses import dataclass
from datetime import timedelta

CANDY = "🍬"
PRODUCT_NAME = "Абсолют | MAXIMUM POWER 🚀"
PRODUCT_EMOJI = "👑"
PRICE = 6789

BALANCE_EMOJI = "🍭"
MENU_HINT = "🌸 Чтобы ознакомиться с товаром, выберите необходимый, нажав на соответствующую кнопку 🌸"
BACK_LABEL = "⬅️ Назад"
BACK_STYLE = "danger"       # «⬅️ Назад» всегда красная
BUY_STYLE = "success"       # «Купить» всегда зелёная

# Цвет кнопки товара (Bot API 9.4+, aiogram >= 3.25). В БД хранится значение Telegram (`style`); пустая строка —
# стандартная кнопка без цвета. Через API/панель можно передать и понятные имена (COLOR_ALIASES).
COLOR_DEFAULT = ""
COLORS: tuple[str, ...] = (COLOR_DEFAULT, "danger", "success", "primary")
COLOR_ALIASES = {
    "": COLOR_DEFAULT, "default": COLOR_DEFAULT, "standard": COLOR_DEFAULT, "стандартный": COLOR_DEFAULT,
    "red": "danger", "danger": "danger", "красный": "danger",
    "green": "success", "success": "success", "зелёный": "success", "зеленый": "success",
    "blue": "primary", "primary": "primary", "синий": "primary",
}

MAX_NAME = 64
MAX_ABOUT = 5000       # лимит для Telegram: сколько символов описания пользователь реально увидит (после выбора вариантов
                       # [а/б] и без значков оформления). Длиннее 4096 в одно сообщение не влезает — карточка уходит частями
MAX_ABOUT_RAW = 20000  # лимит поля на сайте: считается как введено — вместе со скобками вариантов [а/б] и значками оформления
PART_LIMIT = 4000      # сколько видимых символов кладём в одно сообщение (у Telegram предел 4096, берём с запасом)
MAX_PRICE = 1_000_000_000
MAX_PRODUCTS = 90   # у Telegram не больше 100 кнопок под сообщением; одна занята «Назад»

# ---------- варианты текста (по два на каждое место; выбор по seed) ----------
INTROS: tuple[str, ...] = (
    "расширенный бонусный статус и набор стикеров",
    "особый статус участника и набор стикеров",
)
COMPOSITION_TITLES: tuple[str, ...] = ("Что входит", "Состав")
ITEMS_1: tuple[str, ...] = ("Особый значок профиля и набор стикеров", "Статусная отметка и стикерпак")
ITEMS_2: tuple[str, ...] = ("Доступ к дополнительным темам оформления", "Расширенные темы оформления")
ITEMS_3: tuple[str, ...] = ("Быстрый переход между разделами меню", "Удобная навигация по разделам")
OUTROS: tuple[str, ...] = ("Выберите действие с помощью кнопок ниже", "Воспользуйтесь кнопками ниже")
BULLET = "·"

PRODUCT_PREFIX, BACK_PREFIX, BUY_PREFIX, PRODUCT_BACK_PREFIX = "sp:", "sk:", "sb:", "sx:"
PREFIXES = (PRODUCT_PREFIX, BACK_PREFIX, BUY_PREFIX, PRODUCT_BACK_PREFIX)
HASH_LEN = 10

_BAD = ("---", "***", "———", "═", "─", "│")


def _check_tables() -> None:
    for words in (INTROS, COMPOSITION_TITLES, ITEMS_1, ITEMS_2, ITEMS_3, OUTROS):
        if len(set(words)) != len(words):
            raise AssertionError("повторы в списках")
        if any(CANDY in w or any(bad in w for bad in _BAD) for w in words):
            raise AssertionError("в вариантах текста нет ни 🍬, ни декоративных разделителей")
    if len(set(PREFIXES)) != len(PREFIXES):
        raise AssertionError("префиксы callback_data совпадают")


_check_tables()


def build_about(intro: int, title: int, item1: int, item2: int, item3: int, outro: int) -> str:
    """Готовое описание товара (HTML) из выбранных вариантов: именно оно хранится в БД."""
    e = lambda s: html.escape(s, quote=False)  # noqa: E731
    return (
        f"<b>{PRODUCT_EMOJI} {e(PRODUCT_NAME)} — {e(INTROS[intro])}.</b>\n\n"
        f"<b>{e(COMPOSITION_TITLES[title])}:</b>\n"
        f"{BULLET} {e(ITEMS_1[item1])}\n"
        f"{BULLET} {e(ITEMS_2[item2])}\n"
        f"{BULLET} {e(ITEMS_3[item3])}\n\n"
        f"{e(OUTROS[outro])}."
    )


@dataclass(frozen=True)
class ShopConfig:
    intro: int
    title: int
    item1: int
    item2: int
    item3: int
    outro: int
    about: str            # готовое описание товара (HTML): именно оно показывается пользователям
    product_cb: str       # открыть карточку товара
    back_cb: str          # из меню магазина — в главное меню
    buy_cb: str           # «Купить» в карточке (заглушка)
    product_back_cb: str  # из карточки — обратно в меню магазина

    def signature(self) -> str:
        """Чем отличаем ботов друг от друга: текст описания (callback_data уникальны в любом случае)."""
        return self.about

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": 1, "intro": self.intro, "title": self.title,
                "item1": self.item1, "item2": self.item2, "item3": self.item3, "outro": self.outro,
                "about": self.about, "product_cb": self.product_cb, "back_cb": self.back_cb,
                "buy_cb": self.buy_cb, "product_back_cb": self.product_back_cb,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "ShopConfig":
        d = json.loads(raw)
        cfg = cls(
            intro=int(d["intro"]), title=int(d["title"]),
            item1=int(d["item1"]), item2=int(d["item2"]), item3=int(d["item3"]), outro=int(d["outro"]),
            about=str(d["about"]),
            product_cb=str(d["product_cb"]), back_cb=str(d["back_cb"]),
            buy_cb=str(d["buy_cb"]), product_back_cb=str(d["product_back_cb"]),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.about.strip():
            raise ValueError("пустое описание товара")
        if PRODUCT_NAME not in html.unescape(self.about):
            raise ValueError("в описании должно быть название товара")
        if any(bad in self.about for bad in _BAD):
            raise ValueError("декоративные разделители запрещены")
        pairs = ((self.product_cb, PRODUCT_PREFIX), (self.back_cb, BACK_PREFIX),
                 (self.buy_cb, BUY_PREFIX), (self.product_back_cb, PRODUCT_BACK_PREFIX))
        if not all(cb.startswith(prefix) for cb, prefix in pairs):
            raise ValueError("неверный префикс callback_data")
        if len({cb for cb, _ in pairs}) != len(pairs):
            raise ValueError("callback_data совпадают")
        for cb, _ in pairs:
            if len(cb.encode()) > 64 or not cb.isascii():
                raise ValueError("callback_data: только ASCII, до 64 байт")


def generate_config(seed: str, taken: set[str] | None = None) -> ShopConfig:
    """Детерминированно по seed: каждое место описания выбирается независимо. Если итог совпал с чужим из `taken`,
    берётся следующая попытка (последовательность попыток тоже детерминирована); при исчерпании комбинаций
    (их 2^6 = 64) повтор допустим."""
    rng = random.Random(hashlib.sha256(("shop_menu:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        intro, title = rng.randrange(len(INTROS)), rng.randrange(len(COMPOSITION_TITLES))
        item1, item2, item3 = rng.randrange(len(ITEMS_1)), rng.randrange(len(ITEMS_2)), rng.randrange(len(ITEMS_3))
        outro = rng.randrange(len(OUTROS))
        digest = hashlib.sha256(f"shcb:{seed}:{rng.getrandbits(64)}".encode()).hexdigest()
        cfg = ShopConfig(
            intro=intro, title=title, item1=item1, item2=item2, item3=item3, outro=outro,
            about=build_about(intro, title, item1, item2, item3, outro),
            product_cb=PRODUCT_PREFIX + digest[:HASH_LEN],
            back_cb=BACK_PREFIX + digest[HASH_LEN:2 * HASH_LEN],
            buy_cb=BUY_PREFIX + digest[2 * HASH_LEN:3 * HASH_LEN],
            product_back_cb=PRODUCT_BACK_PREFIX + digest[3 * HASH_LEN:4 * HASH_LEN],
        )
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg


# ---------- товар из БД ----------

@dataclass(frozen=True)
class Product:
    """Товар каталога в виде, удобном для сообщений (без привязки к ORM и сессии БД)."""
    id: int
    name: str
    price: int
    about: str = ""        # как введено в БД (с оформительскими маркерами)
    color: str = COLOR_DEFAULT   # значение `style` Telegram: "" / "danger" / "success" / "primary"


def product_from_row(row) -> Product:
    """Строка таблицы shop_products (или любой объект с теми же полями) -> Product. Цвет из БД, не входящий в
    допустимые, показывается как стандартная кнопка (лишнее значение не должно ронять меню)."""
    color = str(row.color or "")
    return Product(
        id=int(row.id), name=str(row.name), price=int(row.price), about=str(row.about or ""),
        color=color if color in COLORS else COLOR_DEFAULT,
    )


def normalize_color(value: str | None) -> str:
    """Цвет кнопки товара -> значение `style` Telegram. ValueError — неизвестный цвет."""
    key = (value or "").strip().lower()
    if key not in COLOR_ALIASES:
        raise ValueError("Цвет кнопки: стандартный, красный, зелёный или синий")
    return COLOR_ALIASES[key]


def normalize_name(value: str | None) -> str:
    name = " ".join((value or "").split())
    if not name:
        raise ValueError("Укажите название товара")
    if len(name) > MAX_NAME:
        raise ValueError(f"Название товара — не длиннее {MAX_NAME} символов")
    return name


def normalize_price(value) -> int:
    try:
        text = str(value).strip().replace(" ", "")
        if not re.fullmatch(r"\d{1,12}", text):
            raise ValueError
        price = int(text)
    except (ValueError, TypeError):
        raise ValueError("Цена — целое число конфет") from None
    if not 1 <= price <= MAX_PRICE:
        raise ValueError(f"Цена — от 1 до {MAX_PRICE}")
    return price


def normalize_about(value: str | None) -> str:
    """Описание товара «как введено». Два лимита: на сайте текст считается целиком (MAX_ABOUT_RAW, со скобками вариантов
    [а/б] и значками оформления), а Telegram видит только выбранный вариант без значков — для него считаем худший случай
    (в каждой скобке самый длинный вариант) и сравниваем с MAX_ABOUT."""
    about = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(about) > MAX_ABOUT_RAW:
        raise ValueError(f"Описание товара — не длиннее {MAX_ABOUT_RAW} символов (считая скобки вариантов и значки оформления)")
    shown = telegram_about_length(about)
    if shown > MAX_ABOUT:
        raise ValueError(
            f"В Telegram описание займёт до {shown} символов, а можно не больше {MAX_ABOUT} "
            "(считается уже после выбора вариантов [а/б], без значков оформления)"
        )
    return about


# ---------- оформительские символы -> HTML Telegram ----------
# Поддерживаются (разметка бота — parse_mode=HTML):
#   **текст**  -> <b>        __текст__ -> <u>        *текст* -> <i>       ~~текст~~ -> <s>
#   ||текст||  -> <tg-spoiler>    `код` -> <code>    [текст](https://адрес) -> <a href>
#   «# Заголовок» в начале строки -> строка жирным.
# Всё остальное экранируется (&, <, >), поэтому сырой HTML из описания не попадает в сообщение и ломать разметку
# не может. Маркеры без пары остаются как есть. Если теги всё же получились перекрёстными
# (например, **а *б** в*), оформление отключается целиком и текст выводится как введён.
_HOLD = "\x00{}\x00"
_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()\"<>]+)\)")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,3}[ \t]+(\S.*?)[ \t]*#*[ \t]*$", re.M)
_INLINE: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S), "<b>", "</b>"),
    (re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.S), "<u>", "</u>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S), "<s>", "</s>"),
    (re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", re.S), "<tg-spoiler>", "</tg-spoiler>"),
    (re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])"), "<i>", "</i>"),
)
_TAG_RE = re.compile(r"<(/?)(b|u|s|i|code|a|tg-spoiler)(?:\s[^>]*)?>")


def _balanced(text: str) -> bool:
    stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        closing, name = m.group(1) == "/", m.group(2)
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


# ---------- варианты фраз [вариант1/вариант2] ----------
# Синтаксис: в квадратных скобках хотя бы один «/» — выбирается ОДИН вариант: «Привет [товарищ/друг]!».
# Варианты обрезаются по краям от пробелов, пустой вариант допустим («[очень/]» — слово можно не писать); несколько
# конструкций и вложенность («[а/б [в/г]]») поддерживаются. Не трогаются: скобки без «/», незакрытые/лишние скобки,
# ссылки `[текст](https://…)`, скобки с «://» внутри и всё внутри `кода`.
# Выбор детерминирован: sha256(seed бота | id товара | какой раз встречается тот же набор вариантов | сам набор) —
# у одного бота текст всегда один и тот же, у разных ботов (разные seed) выбор различается, а одинаковые «слоты»
# внутри описания выбирают независимо. Добавление новой конструкции не меняет выбор в остальных.
_L, _R, _SL = "\x01", "\x02", "\x03"   # маски для скобок/слэшей, которые остаются текстом


def _pick(options: list[str], seed: str, product_id: int, counts: dict[str, int], longest: bool = False) -> str:
    if longest:   # режим проверки длины: берём самый длинный вариант (худший случай для лимита Telegram)
        return max(options, key=len)
    key = "/".join(options)
    n = counts.get(key, 0)
    counts[key] = n + 1
    digest = hashlib.sha256(f"{seed}|{product_id}|{n}|{key}".encode()).digest()
    return options[int.from_bytes(digest[:8], "big") % len(options)]


def _resolve_segment(text: str, seed: str, product_id: int, counts: dict[str, int], longest: bool = False) -> str:
    stack: list[list[str]] = [[]]
    for i, ch in enumerate(text):
        if ch == "[":
            stack.append([])
        elif ch == "]" and len(stack) > 1:
            content = "".join(stack.pop())
            is_link = text.startswith(("(http://", "(https://"), i + 1)
            if "/" in content and "://" not in content and not is_link:
                stack[-1].append(_pick([o.strip() for o in content.split("/")], seed, product_id, counts, longest))
            else:  # не вариант: скобки и «/» внутри прячем, чтобы внешняя конструкция их не разбирала
                stack[-1].append(_L + content.replace("/", _SL) + _R)
        else:
            stack[-1].append(ch)
    while len(stack) > 1:  # незакрытая «[» — обычный текст
        content = "".join(stack.pop())
        stack[-1].append(_L + content.replace("/", _SL))
    return "".join(stack[0])


def resolve_variants(raw: str | None, seed: str, product_id: int = 0, *, longest: bool = False) -> str:
    """Подставляет по одному варианту вместо каждой конструкции [а/б] (правила выше). Без «[» возвращает текст как есть."""
    text = raw or ""
    if "[" not in text:
        return text
    counts: dict[str, int] = {}
    parts, pos = [], 0
    for m in _CODE_RE.finditer(text):       # код `...` не трогаем
        parts.append(_resolve_segment(text[pos:m.start()], seed, product_id, counts, longest))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_resolve_segment(text[pos:], seed, product_id, counts, longest))
    return "".join(parts).replace(_L, "[").replace(_R, "]").replace(_SL, "/")


def format_about(raw: str | None, seed: str | None = None, product_id: int = 0) -> str:
    """Описание товара «как введено» -> безопасный HTML для Telegram (см. правила выше). Пустое описание — пустая строка.
    seed (seed бота) задан — сначала из [а/б] выбираются варианты этого бота, затем идут оформление и экранирование;
    seed=None — варианты не разворачиваются (текст как введён)."""
    if seed is not None:
        raw = resolve_variants(raw, seed, product_id)
    text = html.escape((raw or "").replace("\r\n", "\n").replace("\r", "\n").strip(), quote=False)
    if not text:
        return ""
    held: list[str] = []

    def hold(markup: str) -> str:
        held.append(markup)
        return _HOLD.format(len(held) - 1)

    text = _CODE_RE.sub(lambda m: hold(f"<code>{m.group(1)}</code>"), text)
    text = _LINK_RE.sub(lambda m: hold(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text)
    text = _HEADING_RE.sub(lambda m: f"**{m.group(1)}**", text)
    for pattern, open_tag, close_tag in _INLINE:
        text = pattern.sub(lambda m, o=open_tag, c=close_tag: f"{o}{m.group(1)}{c}", text)
    for i, markup in enumerate(held):
        text = text.replace(_HOLD.format(i), markup)
    if not _balanced(text):
        return html.escape(raw.replace("\r\n", "\n").replace("\r", "\n").strip(), quote=False)
    return text


# ---------- длина текста так, как её считает Telegram ----------
_ANY_TAG_RE = re.compile(r"<[^>]*>")


def visible_length(markup: str) -> int:
    """Сколько символов Telegram посчитает в HTML-тексте: теги не в счёт, &lt; &amp; … — по одному символу."""
    return len(html.unescape(_ANY_TAG_RE.sub("", markup)))


def telegram_about_length(raw: str | None) -> int:
    """Худший случай длины описания в Telegram: в каждой [скобке/вариантов] берётся самый длинный вариант, затем
    описание оформляется как при показе (значки ** __ и т. п. и адреса ссылок в длину не входят)."""
    return visible_length(format_about(resolve_variants(raw, "", 0, longest=True)))


# ---------- длинная карточка товара: разбиение на сообщения ----------
_SPLIT_TOKEN_RE = re.compile(r"(<[^>]+>)")
_ENTITY_OR_CHAR_RE = re.compile(r"&#?\w+;|.", re.S)


def _text_pieces(text: str, limit: int):
    """Куски текста (без тегов) не длиннее limit видимых символов: по строкам; слишком длинную строку — по словам;
    слишком длинное слово — по символам (HTML-сущности вроде &amp; не рвутся)."""
    for line in text.splitlines(keepends=True):
        if visible_length(line) <= limit:
            yield line
            continue
        for word in re.findall(r"\S+\s*|\s+", line):
            if visible_length(word) <= limit:
                yield word
                continue
            buf: list[str] = []
            size = 0
            for unit in _ENTITY_OR_CHAR_RE.findall(word):
                w = len(html.unescape(unit))
                if size + w > limit:
                    yield "".join(buf)
                    buf, size = [], 0
                buf.append(unit)
                size += w
            if buf:
                yield "".join(buf)


def split_html(text: str, limit: int = PART_LIMIT) -> list[str]:
    """Делит HTML-текст сообщения на части не длиннее limit видимых символов. Режет по строкам (если строка длиннее —
    по словам); теги, открытые на границе, закрываются в конце части и открываются снова в начале следующей, поэтому
    каждая часть — корректная разметка. Короткий текст возвращается одной частью без изменений."""
    if visible_length(text) <= limit:
        return [text]
    parts: list[str] = []
    cur: list[str] = []
    size = 0
    stack: list[tuple[str, str]] = []   # (имя тега, тег целиком) — открытые на данный момент

    def flush() -> None:
        nonlocal cur, size
        parts.append("".join(cur).rstrip() + "".join(f"</{name}>" for name, _ in reversed(stack)))
        cur, size = ["".join(tag for _, tag in stack)], 0

    for tok in _SPLIT_TOKEN_RE.split(text):
        if not tok:
            continue
        m = re.match(r"<(/?)([\w-]+)", tok) if _SPLIT_TOKEN_RE.fullmatch(tok) else None
        if m:
            if m.group(1):
                if stack and stack[-1][0] == m.group(2):
                    stack.pop()
            else:
                stack.append((m.group(2), tok))
            cur.append(tok)
            continue
        for piece in _text_pieces(tok, limit):
            n = visible_length(piece)
            if size + n > limit and size > 0:
                flush()
            if size == 0 and not piece.strip():
                continue   # пустые строки в начале части не нужны
            cur.append(piece)
            size += n
    body = "".join(cur)
    if html.unescape(_ANY_TAG_RE.sub("", body)).strip():   # хвост из одних пробелов/тегов частью не считается
        parts.append(body.rstrip() + "".join(f"</{name}>" for name, _ in reversed(stack)))
    return parts


# ---------- скидка по промокоду ----------
# percent = 0 — скидки нет, все функции ниже работают как раньше. Промокоды: app/promo.py, хранение — repo (user_promos).
PROMO_DURATION = timedelta(hours=24)   # сколько действует скидка с момента ввода промокода (на любое число покупок)
STRIKE = "\u0336"   # «combining long stroke overlay»: зачёркивает символ, к которому приписан


def discounted_price(price: int, percent: int) -> int:
    """Цена со скидкой, в целых конфетах: округление половины вверх, но не меньше 1🍬 (100 при 15% -> 85)."""
    pct = max(0, min(99, int(percent or 0)))
    if not pct:
        return int(price)
    return max(1, (int(price) * (100 - pct) + 50) // 100)


def strike(text: str) -> str:
    """Зачёркнутый текст для КНОПОК: в подписях inline-кнопок Telegram не поддерживает форматирование (<s>), поэтому
    зачёркивание делается юникодом — после каждого символа (кроме пробелов) идёт U+0336."""
    return "".join(ch if ch.isspace() else ch + STRIKE for ch in text)


def price_text(price: int, percent: int = 0) -> str:
    """Цена для кнопки: «100🍬» или со скидкой «1̶0̶0̶ 85🍬» (зачёркнуто только число старой цены, без 🍬; рядом новая цена)."""
    if not percent:
        return f"{int(price)}{CANDY}"
    return f"{strike(str(int(price)))} {discounted_price(price, percent)}{CANDY}"


def left_hm(seconds: float) -> str:
    """«23ч 40м» — сколько осталось: всегда часы и минуты, минуты округляются вверх, «0ч 0м» не показываем."""
    total = max(int(-(-float(seconds) // 60)), 1)
    hours, minutes = divmod(total, 60)
    return f"{hours}ч {minutes}м"


def left_short(seconds: float) -> str:
    """Для кнопки: от часа — целые часы вниз («23ч»), меньше часа — минуты вверх («40м», не меньше «1м»)."""
    seconds = max(float(seconds), 0.0)
    if seconds >= 3600:
        return f"{int(seconds // 3600)}ч"
    minutes = max(int(-(-seconds // 60)), 1)
    return "1ч" if minutes >= 60 else f"{minutes}м"


def promo_button_suffix(percent: int, left: float | None) -> str:
    """Приписка к кнопке магазина под приветствием: «(🔥 -15% на 23ч)». Пусто, если скидки нет."""
    if not percent or left is None or left <= 0:
        return ""
    return f"(🔥 -{int(percent)}% на {left_short(left)})"


def promo_line(percent: int, left: float | None = None) -> str:
    """Строка внизу меню магазина, пока действует промокод (HTML, жирным); left — секунд до конца скидки."""
    text = f"🔥 Действует промокод на {int(percent)}%!"
    if left is not None and left > 0:
        text += f" Скидка истечёт через {left_hm(left)}"
    return f"<b>{text}</b>"


# ---------- тексты сообщений ----------

def product_label(p: Product, percent: int = 0) -> str:
    """Подпись кнопки товара в меню: «Название — Цена🍬» (цена в конце — только на кнопке); со скидкой старая
    цена зачёркнута, рядом новая."""
    return f"{p.name} — {price_text(p.price, percent)}"


def buy_label(p: Product, percent: int = 0) -> str:
    return f"🍭 Купить ({discounted_price(p.price, percent)}{CANDY})"


def render_menu(heading: str, balance: int, percent: int = 0, left: float | None = None) -> str:
    """Меню магазина (HTML). heading — подпись кнопки магазина этого бота (shop_buttons.shop_heading):
    выводится ЖИРНЫМ шрифтом и экранируется. percent > 0 — внизу строка «🔥 Действует промокод на N%! Скидка истечёт
    через Hч Mм» (left — секунд до конца скидки; без него — без второй фразы)."""
    text = (
        f"<b>{html.escape(heading, quote=False)}</b>\n\n"
        f"{MENU_HINT}\n\n"
        f"{BALANCE_EMOJI} Ваш баланс: {int(balance)}{CANDY}"
    )
    return f"{text}\n\n{promo_line(percent, left)}" if percent else text


def render_product(p: Product, seed: str = "", percent: int = 0) -> str:
    """Карточка товара (HTML): название без « — 6789🍬», цена отдельной строкой, описание из БД с оформлением.
    seed — seed бота: по нему из [а/б] в описании выбираются варианты (у одного бота всегда одни и те же).
    percent > 0 — цена со скидкой (старая зачёркнута) и внизу строка про промокод."""
    price = f"{int(p.price)}{CANDY}" if not percent else f"<s>{int(p.price)}</s> {discounted_price(p.price, percent)}{CANDY}"
    text = (
        f"<b>📦 Товар:</b> {html.escape(p.name, quote=False)}\n"
        f"{BALANCE_EMOJI} <b>Цена:</b> {price}\n\n"
        f"<b>ℹ️ О товаре:</b>"
    )
    about = format_about(p.about, seed, p.id)
    text = f"{text}\n\n{about}" if about else text
    return f"{text}\n\n{promo_line(percent)}" if percent else text


def render_product_parts(p: Product, seed: str = "", percent: int = 0) -> list[str]:
    """Карточка товара одним или несколькими сообщениями: до MAX_ABOUT символов описания в одно сообщение
    (4096) не влезают, поэтому длинная карточка делится по строкам (split_html). Кнопки крепятся к последней части."""
    return split_html(render_product(p, seed, percent))


# ---------- клавиатуры и разбор нажатий ----------

def _cb(prefix_cb: str, product_id: int) -> str:
    return f"{prefix_cb}:{int(product_id)}"


def menu_rows(cfg: ShopConfig, products: list[Product], percent: int = 0) -> list[list[tuple[str, str]]]:
    """Ряды кнопок меню [(подпись, callback_data)] без зависимости от aiogram (удобно для тестов):
    товары в переданном порядке, «⬅️ Назад» — всегда последняя."""
    rows = [[(product_label(p, percent), _cb(cfg.product_cb, p.id))] for p in products]
    rows.append([(BACK_LABEL, cfg.back_cb)])
    return rows


def menu_styles(products: list[Product]) -> tuple[str, ...]:
    """Цвета рядов меню: у каждого товара свой (пустой — без цвета), «Назад» красная."""
    return tuple(p.color for p in products) + (BACK_STYLE,)


def product_rows(cfg: ShopConfig, p: Product, percent: int = 0) -> list[list[tuple[str, str]]]:
    return [[(buy_label(p, percent), _cb(cfg.buy_cb, p.id))], [(BACK_LABEL, cfg.product_back_cb)]]


PRODUCT_STYLES = (BUY_STYLE, BACK_STYLE)


def _keyboard(rows: list[list[tuple[str, str]]], styles: tuple[str, ...]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            # пустой цвет -> поле style не передаём вовсе (стандартная кнопка)
            [InlineKeyboardButton(text=t, callback_data=cb, **({"style": style} if style else {})) for t, cb in row]
            for row, style in zip(rows, styles)
        ]
    )


def build_menu_keyboard(cfg: ShopConfig, products: list[Product], percent: int = 0):
    return _keyboard(menu_rows(cfg, products, percent), menu_styles(products))


def build_product_keyboard(cfg: ShopConfig, p: Product, percent: int = 0):
    return _keyboard(product_rows(cfg, p, percent), PRODUCT_STYLES)


def parse_callback(cfg: ShopConfig, data: str | None) -> tuple[str, int | None] | None:
    """(действие, id товара) по callback_data ЭТОГО бота; None — чужие или устаревшие данные.
    Действия: 'product' / 'buy' (id товара из кнопки; у старых кнопок без id — None), 'back', 'product_back'."""
    if not data:
        return None
    if data == cfg.back_cb:
        return "back", None
    if data == cfg.product_back_cb:
        return "product_back", None
    for action, key in (("product", cfg.product_cb), ("buy", cfg.buy_cb)):
        if data == key:
            return action, None
        head, sep, tail = data.rpartition(":")
        if sep and head == key and tail.isascii() and tail.isdigit() and len(tail) <= 18:
            return action, int(tail)
    return None


def parse_action(cfg: ShopConfig, data: str | None) -> str | None:
    """'product' / 'back' / 'buy' / 'product_back' по callback_data ЭТОГО бота; None — чужие или устаревшие данные."""
    parsed = parse_callback(cfg, data)
    return parsed[0] if parsed else None
