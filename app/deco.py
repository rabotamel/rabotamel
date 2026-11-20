"""Общий декоративный маркер бота: эмодзи, обрамляющие описания разделов («💫 описание 💫»).

Маркер СТАТИЧЕН: у всех ботов это одно и то же `MARKER` (💫), по seed он больше не подбирается. Вид «deco» остаётся в
БД и в `texts.KINDS` ради совместимости; сейчас им пользуется раздел «Галерея», другие разделы должны брать маркер
отсюда (`texts.deco`), чтобы оформление описаний во всём боте было единым. Хранятся левый и правый эмодзи отдельно — на
будущее; сейчас оба равны. Маркер, сохранённый в БД у уже работающих ботов (🌸 ✨ 🌷 🪻 …), приводится к 💫 при чтении
(`DecoConfig.from_json`), без перезаписи БД.
"""
import json
from dataclasses import dataclass

MARKER = "💫"
EMOJI: tuple[str, ...] = (MARKER,)   # единственный допустимый маркер (раньше было пять вариантов по seed)


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
        if not str(d["left"]).strip() or not str(d["right"]).strip() or "🍬" in (str(d["left"]) + str(d["right"])):
            raise ValueError("некорректный декоративный маркер")
        return cls(left=MARKER, right=MARKER)  # старый маркер из БД заменяется статичным

    def wrap(self, text: str) -> str:
        """«💫 текст 💫»."""
        return f"{self.left} {text} {self.right}"


def generate_config(seed: str, taken: set[str] | None = None) -> DecoConfig:
    """Маркер статичен — не зависит от seed. Параметры оставлены ради единого интерфейса `TextKind.generate`."""
    return DecoConfig(left=MARKER, right=MARKER)
