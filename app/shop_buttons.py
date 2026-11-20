"""Рандомизированные inline-кнопки «Магазин» и «Галерея» под приветственным сообщением.

Отдельного «главного меню» нет: приветствие и есть главное меню, кнопки крепятся прямо под ним.

Что подбирается для бота ОДИН раз (детерминированно по seed = токен бота) и хранится в БД
(`bot_profiles.texts_config`, вид «shop_buttons») до перерандомизации или бана/создания нового бота:
  * текст каждой кнопки — из отдельного списка синонимов (эмодзи СТАТИЧНЫ и не подбираются: «🍫 Магазин 💫»,
    «🎥 Галерея 💖», у всех ботов одни и те же — см. SHOP_EMOJI / GALLERY_EMOJI);
  * сторона эмодзи: всегда с обеих сторон;
  * взаимный порядок кнопок (магазин → галерея или наоборот);
  * раскладка: обе кнопки в одном ряду или в двух рядах;
  * уникальные callback_data (префикс действия + хэш бота), не совпадающие у разных ботов.

Хранится ГОТОВЫЙ результат (итоговые подписи и callback_data), поэтому правки списков текстов в коде не меняют
кнопки уже работающих ботов. Индексы в списках только добавляются в конец. Исключение — эмодзи: у уже сохранённых
подписей они приводятся к статичным при чтении (`_migrate_legacy`), без перезаписи БД.
"""
import hashlib
import json
import random
from dataclasses import dataclass

# ---------- списки ----------
# Эмодзи заданы парой «левый ... правый»: подпись собирается как «<левый> <текст> <правый>».
# Пара ОДНА на кнопку (статичная, по seed не подбирается): допустимый набор оформления бота —
# 🎥 💖 💫 🍫 🍬 📩 🤍 ✅ ❌ 🚫 ⬅️. Индекс эмодзи (shop_emoji / gallery_emoji) в конфиге остаётся ради совместимости и всегда 0.
SHOP_TEXTS: tuple[str, ...] = ("Магазин", "Каталог", "Наш ассортимент")
SHOP_EMOJI: tuple[tuple[str, str], ...] = (("🍫", "💫"),)

GALLERY_TEXTS: tuple[str, ...] = ("Галерея", "Случайное видео", "Видео-рулетка", "Смотреть видео")
GALLERY_EMOJI: tuple[tuple[str, str], ...] = (("🎥", "💖"),)

# Сколько вариантов эмодзи было раньше: при генерации генератор случайных чисел продвигается на столько же,
# поэтому выбор ТЕКСТОВ (и callback_data) по seed остаётся прежним, а меняются только эмодзи.
_LEGACY_SHOP_EMOJI_COUNT = 3
_LEGACY_GALLERY_EMOJI_COUNT = 4

# Как показываем эмодзи пары. Одиночный эмодзи (только слева или только справа) не допускается: всегда с обеих сторон.
ACCENTS: tuple[str, ...] = ("both",)
LAYOUTS: tuple[str, ...] = ("row", "column")      # row — в один ряд, column — по кнопке в ряд
ORDERS: tuple[str, ...] = ("shop_first",)   # «Магазин» всегда идёт раньше «Галереи»

# Telegram: callback_data — до 64 байт. Префиксы короткие, ASCII.
SHOP_PREFIX = "sh:"
GALLERY_PREFIX = "gl:"
HASH_LEN = 10
MAX_LABEL = 64  # запас: лимит текста кнопки у Telegram существенно больше, но длинные подписи в ряду обрезаются


def validate_tables() -> None:
    for words in (SHOP_TEXTS, GALLERY_TEXTS):
        if len(set(words)) != len(words):
            raise AssertionError("повторы в списке текстов")
    for pairs in (SHOP_EMOJI, GALLERY_EMOJI):
        if len(set(pairs)) != len(pairs) or any(len(p) != 2 for p in pairs):
            raise AssertionError("эмодзи — это пары (левый, правый) без повторов")


validate_tables()


def label(text: str, emoji: tuple[str, str], accent: str) -> str:
    """Итоговая подпись кнопки без разделителей: «🍫 Магазин 💫», «🍫 Магазин» или «Магазин 💫»."""
    left, right = emoji
    parts = [text]
    if accent in ("both", "left"):
        parts.insert(0, left)
    if accent in ("both", "right"):
        parts.append(right)
    return " ".join(parts)


_LEGACY_TEXTS = {"Ассортимент": "Наш ассортимент"}


def _static_label(lbl: str, emoji: tuple[str, str]) -> str:
    """Подпись «<левый> <текст> <правый>» со статичной парой эмодзи; текст между эмодзи сохраняется как есть."""
    parts = lbl.split(" ")
    if len(parts) < 3:
        return lbl
    return f"{emoji[0]} {' '.join(parts[1:-1])} {emoji[1]}"


def _migrate_legacy(d: dict) -> dict:
    """Приводит конфиги, уже сохранённые в БД, к текущим правилам (без перезаписи в БД):
    только эмодзи с обеих сторон, «Магазин» первым, «Наш ассортимент» вместо «Ассортимент»,
    статичные эмодзи вместо случайно подобранных раньше."""
    d = dict(d)
    for key, texts, emojis in (("shop", SHOP_TEXTS, SHOP_EMOJI), ("gallery", GALLERY_TEXTS, GALLERY_EMOJI)):
        lbl = str(d[f"{key}_label"])
        for old, new in _LEGACY_TEXTS.items():
            if old in lbl and new not in lbl:
                lbl = lbl.replace(old, new)
        if d.get(f"{key}_accent") != "both":
            idx_t = int(d[f"{key}_text"])
            if idx_t < len(texts):
                lbl = label(texts[idx_t], emojis[0], "both")
            d[f"{key}_accent"] = "both"
        d[f"{key}_label"] = _static_label(lbl, emojis[0])
        d[f"{key}_emoji"] = 0
    d["order"] = "shop_first"
    return d


@dataclass(frozen=True)
class ShopButtonsConfig:
    shop_text: int
    shop_emoji: int
    shop_accent: str
    gallery_text: int
    gallery_emoji: int
    gallery_accent: str
    order: str
    layout: str
    shop_label: str       # готовая подпись (именно она показывается пользователям)
    gallery_label: str
    shop_cb: str          # уникальные callback_data этого бота
    gallery_cb: str

    @property
    def gallery_title(self) -> str:
        """Название кнопки галереи без эмодзи."""
        return GALLERY_TEXTS[self.gallery_text]

    @property
    def gallery_heading(self) -> str:
        """Заголовок раздела «Галерея»: ровно подпись кнопки, по которой пользователь сюда перешёл (с её эмодзи)."""
        return self.gallery_label

    @property
    def shop_heading(self) -> str:
        """Заголовок раздела «Магазин»: ровно подпись кнопки, по которой пользователь сюда перешёл (с её эмодзи)."""
        return self.shop_label

    def signature(self) -> str:
        """По чему сравниваем «одинаковость» ботов: подписи, порядок, раскладка и callback_data."""
        return "|".join((self.shop_label, self.gallery_label, self.order, self.layout, self.shop_cb, self.gallery_cb))

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": 1,
                "shop_text": self.shop_text, "shop_emoji": self.shop_emoji, "shop_accent": self.shop_accent,
                "gallery_text": self.gallery_text, "gallery_emoji": self.gallery_emoji,
                "gallery_accent": self.gallery_accent,
                "order": self.order, "layout": self.layout,
                "shop_label": self.shop_label, "gallery_label": self.gallery_label,
                "shop_cb": self.shop_cb, "gallery_cb": self.gallery_cb,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "ShopButtonsConfig":
        d = json.loads(raw)
        d = _migrate_legacy(d)
        cfg = cls(
            shop_text=int(d["shop_text"]), shop_emoji=int(d["shop_emoji"]), shop_accent=str(d["shop_accent"]),
            gallery_text=int(d["gallery_text"]), gallery_emoji=int(d["gallery_emoji"]),
            gallery_accent=str(d["gallery_accent"]),
            order=str(d["order"]), layout=str(d["layout"]),
            shop_label=str(d["shop_label"]), gallery_label=str(d["gallery_label"]),
            shop_cb=str(d["shop_cb"]), gallery_cb=str(d["gallery_cb"]),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.order not in ORDERS or self.layout not in LAYOUTS:
            raise ValueError("неизвестный порядок или раскладка")
        if not self.shop_label.strip() or not self.gallery_label.strip():
            raise ValueError("пустая подпись кнопки")
        if self.shop_label == self.gallery_label:
            raise ValueError("подписи кнопок совпадают")
        if not self.shop_cb.startswith(SHOP_PREFIX) or not self.gallery_cb.startswith(GALLERY_PREFIX):
            raise ValueError("неверный префикс callback_data")
        if self.shop_cb == self.gallery_cb:
            raise ValueError("callback_data совпадают")
        for cb in (self.shop_cb, self.gallery_cb):
            if len(cb.encode()) > 64 or not cb.isascii():
                raise ValueError("callback_data: только ASCII, до 64 байт")
        if any(bad in (self.shop_label + self.gallery_label) for bad in ("---", "***", "———", "═", "─", "│", "|")):
            raise ValueError("декоративные разделители запрещены")


def generate_config(seed: str, taken: set[str] | None = None) -> ShopButtonsConfig:
    """Детерминированно по seed. Если итог совпал с чужим из `taken`, берётся следующая попытка
    (последовательность попыток тоже детерминирована). Текст кнопки выбирается по seed, эмодзи статичны."""
    rng = random.Random(hashlib.sha256(("shop_buttons:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        shop_text = rng.randrange(len(SHOP_TEXTS))
        rng.randrange(_LEGACY_SHOP_EMOJI_COUNT)      # эмодзи статичны; шаг ГСЧ сохранён, чтобы тексты по seed не сдвинулись
        shop_emoji = 0
        shop_accent = rng.choice(ACCENTS)
        gallery_text = rng.randrange(len(GALLERY_TEXTS))
        rng.randrange(_LEGACY_GALLERY_EMOJI_COUNT)
        gallery_emoji = 0
        gallery_accent = rng.choice(ACCENTS)
        order = rng.choice(ORDERS)
        layout = rng.choice(LAYOUTS)
        # callback_data: действие + хэш бота (из seed) + случайная соль попытки — у разных ботов не совпадают
        digest = hashlib.sha256(f"cb:{seed}:{rng.getrandbits(64)}".encode()).hexdigest()
        cfg = ShopButtonsConfig(
            shop_text=shop_text, shop_emoji=shop_emoji, shop_accent=shop_accent,
            gallery_text=gallery_text, gallery_emoji=gallery_emoji, gallery_accent=gallery_accent,
            order=order, layout=layout,
            shop_label=label(SHOP_TEXTS[shop_text], SHOP_EMOJI[shop_emoji], shop_accent),
            gallery_label=label(GALLERY_TEXTS[gallery_text], GALLERY_EMOJI[gallery_emoji], gallery_accent),
            shop_cb=SHOP_PREFIX + digest[:HASH_LEN],
            gallery_cb=GALLERY_PREFIX + digest[HASH_LEN:2 * HASH_LEN],
        )
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg


# ---------- клавиатура и разбор нажатий ----------

def layout_rows(cfg: ShopButtonsConfig) -> list[list[tuple[str, str]]]:
    """Ряды кнопок [(подпись, callback_data), ...] без зависимости от aiogram (удобно для тестов)."""
    shop, gallery = (cfg.shop_label, cfg.shop_cb), (cfg.gallery_label, cfg.gallery_cb)
    pair = [shop, gallery] if cfg.order == "shop_first" else [gallery, shop]
    return [pair] if cfg.layout == "row" else [[b] for b in pair]


BUTTON_STYLE = "primary"  # цвет кнопок под приветствием: синий (Bot API 9.4+, нужен aiogram >= 3.25)


def build_keyboard(cfg: ShopButtonsConfig, extra_rows=()):
    """Inline-клавиатура под приветствием: все кнопки синие. extra_rows — дополнительные ряды ниже
    [(подпись, callback_data, стиль), ...] (например, «Купить конфеты»); основные кнопки не меняются."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [[InlineKeyboardButton(text=t, callback_data=cb, style=BUTTON_STYLE) for t, cb in row] for row in layout_rows(cfg)]
    for t, cb, st in extra_rows:
        rows.append([InlineKeyboardButton(text=t, callback_data=cb, style=st)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_action(cfg: ShopButtonsConfig, data: str | None) -> str | None:
    """'shop' / 'gallery' по callback_data ЭТОГО бота; None — чужие или устаревшие данные.
    Сравнение именно с сохранённым ключом: callback_data от кнопок другого бота не сработают."""
    if data == cfg.shop_cb:
        return "shop"
    if data == cfg.gallery_cb:
        return "gallery"
    return None
