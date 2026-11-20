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
вкладка «Магазин» в панели). Описание хранится ТАК, КАК ЕГО ВВЕЛИ (с `**...**` и т. п.), а в HTML переводится при показе
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
MAX_ABOUT = 3000
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
    about = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(about) > MAX_ABOUT:
        raise ValueError(f"Описание товара — не длиннее {MAX_ABOUT} символов")
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


def _pick(options: list[str], seed: str, product_id: int, counts: dict[str, int]) -> str:
    key = "/".join(options)
    n = counts.get(key, 0)
    counts[key] = n + 1
    digest = hashlib.sha256(f"{seed}|{product_id}|{n}|{key}".encode()).digest()
    return options[int.from_bytes(digest[:8], "big") % len(options)]


def _resolve_segment(text: str, seed: str, product_id: int, counts: dict[str, int]) -> str:
    stack: list[list[str]] = [[]]
    for i, ch in enumerate(text):
        if ch == "[":
            stack.append([])
        elif ch == "]" and len(stack) > 1:
            content = "".join(stack.pop())
            is_link = text.startswith(("(http://", "(https://"), i + 1)
            if "/" in content and "://" not in content and not is_link:
                stack[-1].append(_pick([o.strip() for o in content.split("/")], seed, product_id, counts))
            else:  # не вариант: скобки и «/» внутри прячем, чтобы внешняя конструкция их не разбирала
                stack[-1].append(_L + content.replace("/", _SL) + _R)
        else:
            stack[-1].append(ch)
    while len(stack) > 1:  # незакрытая «[» — обычный текст
        content = "".join(stack.pop())
        stack[-1].append(_L + content.replace("/", _SL))
    return "".join(stack[0])


def resolve_variants(raw: str | None, seed: str, product_id: int = 0) -> str:
    """Подставляет по одному варианту вместо каждой конструкции [а/б] (правила выше). Без «[» возвращает текст как есть."""
    text = raw or ""
    if "[" not in text:
        return text
    counts: dict[str, int] = {}
    parts, pos = [], 0
    for m in _CODE_RE.finditer(text):       # код `...` не трогаем
        parts.append(_resolve_segment(text[pos:m.start()], seed, product_id, counts))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_resolve_segment(text[pos:], seed, product_id, counts))
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


# ---------- тексты сообщений ----------

def product_label(p: Product) -> str:
    """Подпись кнопки товара в меню: «Название — Цена🍬» (цена в конце — только на кнопке)."""
    return f"{p.name} — {int(p.price)}{CANDY}"


def buy_label(p: Product) -> str:
    return f"🍭 Купить ({int(p.price)}{CANDY})"


def render_menu(heading: str, balance: int) -> str:
    """Меню магазина (HTML). heading — подпись кнопки магазина этого бота (shop_buttons.shop_heading):
    выводится ЖИРНЫМ шрифтом и экранируется."""
    return (
        f"<b>{html.escape(heading, quote=False)}</b>\n\n"
        f"{MENU_HINT}\n\n"
        f"{BALANCE_EMOJI} Ваш баланс: {int(balance)}{CANDY}"
    )


def render_product(p: Product, seed: str = "") -> str:
    """Карточка товара (HTML): название без « — 6789🍬», цена отдельной строкой, описание из БД с оформлением.
    seed — seed бота: по нему из [а/б] в описании выбираются варианты (у одного бота всегда одни и те же)."""
    text = (
        f"<b>📦 Товар:</b> {html.escape(p.name, quote=False)}\n"
        f"{BALANCE_EMOJI} <b>Цена:</b> {int(p.price)}{CANDY}\n\n"
        f"<b>ℹ️ О товаре:</b>"
    )
    about = format_about(p.about, seed, p.id)
    return f"{text}\n\n{about}" if about else text


# ---------- клавиатуры и разбор нажатий ----------

def _cb(prefix_cb: str, product_id: int) -> str:
    return f"{prefix_cb}:{int(product_id)}"


def menu_rows(cfg: ShopConfig, products: list[Product]) -> list[list[tuple[str, str]]]:
    """Ряды кнопок меню [(подпись, callback_data)] без зависимости от aiogram (удобно для тестов):
    товары в переданном порядке, «⬅️ Назад» — всегда последняя."""
    rows = [[(product_label(p), _cb(cfg.product_cb, p.id))] for p in products]
    rows.append([(BACK_LABEL, cfg.back_cb)])
    return rows


def menu_styles(products: list[Product]) -> tuple[str, ...]:
    """Цвета рядов меню: у каждого товара свой (пустой — без цвета), «Назад» красная."""
    return tuple(p.color for p in products) + (BACK_STYLE,)


def product_rows(cfg: ShopConfig, p: Product) -> list[list[tuple[str, str]]]:
    return [[(buy_label(p), _cb(cfg.buy_cb, p.id))], [(BACK_LABEL, cfg.product_back_cb)]]


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


def build_menu_keyboard(cfg: ShopConfig, products: list[Product]):
    return _keyboard(menu_rows(cfg, products), menu_styles(products))


def build_product_keyboard(cfg: ShopConfig, p: Product):
    return _keyboard(product_rows(cfg, p), PRODUCT_STYLES)


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
