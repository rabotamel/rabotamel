"""Рандомизируемые тексты «Предложки»: подпись кнопки в меню «Галерея» и текст самого меню предложки.

Названия и описания подбираются один раз для бота (seed = токен), хранятся в БД готовым результатом (вид `suggest` в
`texts_config`) и не меняются при перезапусках; единственный способ сменить — «Перенаписать текста» в «Песочнице».
Схема та же, что у приветствия и галереи (см. NOTES.md, «Рандомизация сообщений»).

ЭМОДЗИ СТАТИЧНЫ и по seed не подбираются:
  * кнопка: «📩 {название} 🤍» (LEFT_EMOJI / RIGHT_EMOJI);
  * меню: заголовок = та же подпись, ниже одно из синонимичных описаний (+2🍬 за каждое принятое видео) в обрамлении
    «💫 … 💫» (DECO_EMOJI). Ключевое условие (награда) есть в каждом варианте.
Подписи, сохранённые у работающих ботов со случайными эмодзи, приводятся к статичным при чтении (`SuggestConfig.from_json`).
Индексы в списках текстов только ДОБАВЛЯЮТСЯ в конец, не переставляются и не удаляются.
"""
import hashlib
import html
import json
import random
from dataclasses import dataclass

from .suggest import CANDY, REWARD

REWARD_TEXT = f"+{REWARD}{CANDY}"

TITLES: tuple[str, ...] = (
    "Предложка", "Предложить видео", "Прислать видео", "Предложить контент", "Поделиться видео",
)
# Статичные эмодзи (индексы left / right / deco в конфиге остаются ради совместимости и всегда 0).
LEFT_EMOJI: tuple[str, ...] = ("📩",)
RIGHT_EMOJI: tuple[str, ...] = ("🤍",)
DECO_EMOJI: tuple[str, ...] = ("💫",)

# Сколько вариантов эмодзи было раньше: при генерации ГСЧ продвигается на столько же, поэтому выбор названия и
# описания по seed остаётся прежним, а меняются только эмодзи.
_LEGACY_LEFT_COUNT, _LEGACY_RIGHT_COUNT, _LEGACY_DECO_COUNT = 4, 7, 6

BODIES: tuple[str, ...] = (
    f"Отправьте видео в предложку и получите {REWARD_TEXT} за каждое принятое видео",
    f"Присылайте свои видео в предложку — за каждый одобренный ролик получите {REWARD_TEXT}",
    f"Загрузите видео сюда, и после проверки вы получите {REWARD_TEXT} за каждое принятое видео",
    f"Поделитесь видео в предложке: каждое одобренное модераторами видео принесёт вам {REWARD_TEXT}",
    f"Отправляйте подходящие видео в этот раздел и получайте {REWARD_TEXT} за каждое принятое видео",
)

_BAD = ("---", "***", "———", "═", "─", "│")


def _check_tables() -> None:
    for words in (TITLES, LEFT_EMOJI, RIGHT_EMOJI, DECO_EMOJI, BODIES):
        if len(set(words)) != len(words):
            raise AssertionError("повторы в списках")
    for b in BODIES:
        if REWARD_TEXT not in b:
            raise AssertionError("награда должна быть в каждом описании")


_check_tables()


@dataclass(frozen=True)
class SuggestConfig:
    title: int
    left: int
    right: int
    body: int
    deco: int
    label: str   # «📩 Предложка 🤍» — подпись кнопки и заголовок меню (эмодзи статичны)
    text: str    # описание без обрамления (с +2🍬)
    mark: str    # декоративный эмодзи вокруг описания (статичный 💫)

    @property
    def name(self) -> str:
        """Название раздела без эмодзи — ровно то, что стоит в подписи кнопки между левым и правым эмодзи
        (выводится из сохранённой подписи, поэтому всегда совпадает с кнопкой на экране)."""
        parts = self.label.split(" ")
        return " ".join(parts[1:-1]) if len(parts) > 2 else self.label

    def signature(self) -> str:
        return "|".join((self.label, self.text, self.mark))

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": 1, "title": self.title, "left": self.left, "right": self.right, "body": self.body,
                "deco": self.deco, "label": self.label, "text": self.text, "mark": self.mark,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "SuggestConfig":
        d = json.loads(raw)
        # эмодзи статичны: у работающих ботов подпись и маркер со случайными эмодзи приводятся к текущим (без записи в БД)
        parts = str(d["label"]).split(" ")
        label = f"{LEFT_EMOJI[0]} {' '.join(parts[1:-1])} {RIGHT_EMOJI[0]}" if len(parts) > 2 else str(d["label"])
        cfg = cls(
            title=int(d["title"]), left=0, right=0, body=int(d["body"]),
            deco=0, label=label, text=str(d["text"]), mark=DECO_EMOJI[0],
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.label.strip() or not self.text.strip() or not self.mark.strip():
            raise ValueError("пустой текст")
        if REWARD_TEXT not in self.text:
            raise ValueError(f"в описании должно быть {REWARD_TEXT}")
        if CANDY in (self.label + self.mark):
            raise ValueError("🍬 — валюта, в оформлении её быть не должно")
        if any(bad in (self.label + self.text) for bad in _BAD):
            raise ValueError("декоративные разделители запрещены")

    def render_menu(self) -> str:
        """Меню предложки (HTML): заголовок = подпись кнопки, ниже описание в обрамлении из выбранного эмодзи."""
        return f"<b>{html.escape(self.label, quote=False)}</b>\n\n{self.mark} {html.escape(self.text, quote=False)} {self.mark}"


def generate_config(seed: str, taken: set[str] | None = None) -> SuggestConfig:
    """Детерминированно по seed; название и описание выбираются независимо, эмодзи статичны."""
    rng = random.Random(hashlib.sha256(("suggest:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        t = rng.randrange(len(TITLES))
        rng.randrange(_LEGACY_LEFT_COUNT)    # эмодзи статичны; шаги ГСЧ сохранены, чтобы тексты по seed не сдвинулись
        rng.randrange(_LEGACY_RIGHT_COUNT)
        b = rng.randrange(len(BODIES))
        rng.randrange(_LEGACY_DECO_COUNT)
        cfg = SuggestConfig(
            title=t, left=0, right=0, body=b, deco=0,
            label=f"{LEFT_EMOJI[0]} {TITLES[t]} {RIGHT_EMOJI[0]}", text=BODIES[b], mark=DECO_EMOJI[0],
        )
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg
