"""Общий декоративный маркер бота: эмодзи, обрамляющие описания разделов («🌸 описание 🌸»).

Подбирается один раз для бота (seed = токен) и хранится в БД как отдельный вид сообщений «deco». Сейчас им
пользуется раздел «Галерея»; другие разделы должны брать маркер отсюда (`texts.deco`), чтобы оформление
описаний во всём боте было единым. Хранятся левый и правый эмодзи отдельно — на будущее; сейчас оба равны.
Только добавлять в конец списка EMOJI, не переставлять.
"""
import hashlib
import json
import random
from dataclasses import dataclass

EMOJI: tuple[str, ...] = ("🌸", "✨", "🌷", "🪻", "💫")


@dataclass(frozen=True)
class DecoConfig:
    left: str
    right: str

    def signature(self) -> str:
        return self.left + self.right

    def to_json(self) -> str:
        return json.dumps({"v": 1, "left": self.left, "right": self.right}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "DecoConfig":
        d = json.loads(raw)
        cfg = cls(left=str(d["left"]), right=str(d["right"]))
        if not cfg.left.strip() or not cfg.right.strip() or "🍬" in (cfg.left + cfg.right):
            raise ValueError("некорректный декоративный маркер")
        return cfg

    def wrap(self, text: str) -> str:
        """«🌸 текст 🌸»."""
        return f"{self.left} {text} {self.right}"


def generate_config(seed: str, taken: set[str] | None = None) -> DecoConfig:
    """Детерминированно по seed. `taken` игнорируется: вариантов всего пять, уникальность между ботами
    здесь невозможна (и не нужна — маркер лишь часть общего оформления)."""
    rng = random.Random(hashlib.sha256(("deco:" + seed).encode()).digest())
    emoji = rng.choice(EMOJI)
    return DecoConfig(left=emoji, right=emoji)
