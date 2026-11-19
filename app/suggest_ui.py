"""Рандомизируемые тексты «Предложки»: подпись кнопки в меню «Галерея» и текст самого меню предложки.

Подбираются один раз для бота (seed = токен), хранятся в БД готовым результатом (вид `suggest` в `texts_config`) и
не меняются при перезапусках; единственный способ сменить — «Перенаписать текста» в «Песочнице». Схема та же, что у
приветствия и галереи (см. NOTES.md, «Рандомизация сообщений»).

Кнопка: «{левый эмодзи} {название} {правый эмодзи}» — название и каждый из эмодзи выбираются независимо.
Меню: заголовок = та же подпись, ниже одно из синонимичных описаний (+2🍬 за каждое принятое видео), обрамлённое
выбранным декоративным эмодзи с обеих сторон. Ключевое условие (награда) есть в каждом варианте.
Индексы в списках только ДОБАВЛЯЮТСЯ в конец, не переставляются и не удаляются.
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
LEFT_EMOJI: tuple[str, ...] = ("📩", "📥", "💌", "📤")
RIGHT_EMOJI: tuple[str, ...] = ("🤍", "✨", "💫", "🫧", "🌸", "💎", "🕊️")
DECO_EMOJI: tuple[str, ...] = ("💫", "✨", "🌟", "💎", "🤍", "🫧")

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
    label: str   # «📩 Предложка 🤍» — подпись кнопки и заголовок меню
    text: str    # описание без обрамления (с +2🍬)
    mark: str    # декоративный эмодзи вокруг описания

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
        cfg = cls(
            title=int(d["title"]), left=int(d["left"]), right=int(d["right"]), body=int(d["body"]),
            deco=int(d["deco"]), label=str(d["label"]), text=str(d["text"]), mark=str(d["mark"]),
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
    """Детерминированно по seed; название, левый и правый эмодзи, описание и обрамление выбираются независимо."""
    rng = random.Random(hashlib.sha256(("suggest:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        t, l, r = rng.randrange(len(TITLES)), rng.randrange(len(LEFT_EMOJI)), rng.randrange(len(RIGHT_EMOJI))
        b, dc = rng.randrange(len(BODIES)), rng.randrange(len(DECO_EMOJI))
        cfg = SuggestConfig(
            title=t, left=l, right=r, body=b, deco=dc,
            label=f"{LEFT_EMOJI[l]} {TITLES[t]} {RIGHT_EMOJI[r]}", text=BODIES[b], mark=DECO_EMOJI[dc],
        )
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg
