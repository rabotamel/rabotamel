"""Капча «выбери нужный фрукт».

Всё, что относится к содержимому капчи, собрано здесь и не зависит от aiogram (кроме build_keyboard):
  * FRUITS     — словарь фруктов: эмодзи + название в именительном и винительном падеже;
  * LEADS / ACTIONS — синонимичные формулировки задания (вступление + действие);
  * CaptchaConfig   — конфигурация одного бота: формулировка, 4 фрукта и целевой фрукт;
  * generate_config — детерминированный выбор конфигурации по seed (токену бота),
                      с попыткой не повторять конфигурации других ботов.

Правила:
  * Конфигурация (формулировка + 4 фрукта) создаётся один раз для бота, хранится в БД и не меняется, пока жив
    этот бот (кроме «Уникальные текста»).
  * Какой именно из этих 4 фруктов нужно выбрать — НЕ часть конфигурации: он меняется при каждом показе капчи
    (каждый /start). Состояние по пользователю хранить не нужно: в callback_data каждой кнопки лежит короткий
    случайный nonce, а целевой фрукт = HMAC(секрет бота, user_id + nonce) -> индекс среди 4 фруктов. Поэтому
    проверка работает и после перезапуска сервиса, а подделать ответ без токена бота нельзя.
  * При новом показе фрукт отличается от прошлого (если прошлый известен), порядок кнопок — тоже.
  * После неверного ответа задание остаётся тем же (тот же nonce), меняется только порядок кнопок.
  * Поле `target` в конфигурации осталось для совместимости: оно нужно только для старых сообщений с капчей,
    у которых в кнопках ещё нет nonce (callback вида «cap:apple»).
"""
import hashlib
import hmac
import json
import random
import re
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Fruit:
    key: str    # короткий ASCII-ключ, он же идёт в callback_data
    emoji: str
    nom: str    # именительный падеж (для справки и логов)
    acc: str    # винительный падеж — «выберите ...»


# Название и эмодзи обязаны совпадать по смыслу (названия эмодзи по Unicode).
FRUITS: dict[str, Fruit] = {f.key: f for f in (
    Fruit("apple", "🍎", "яблоко", "яблоко"),
    Fruit("banana", "🍌", "банан", "банан"),
    Fruit("grapes", "🍇", "виноград", "виноград"),
    Fruit("strawberry", "🍓", "клубника", "клубнику"),
    Fruit("watermelon", "🍉", "арбуз", "арбуз"),
    Fruit("tangerine", "🍊", "мандарин", "мандарин"),
    Fruit("lemon", "🍋", "лимон", "лимон"),
    Fruit("pear", "🍐", "груша", "грушу"),
    Fruit("cherries", "🍒", "вишня", "вишню"),
    Fruit("peach", "🍑", "персик", "персик"),
    Fruit("pineapple", "🍍", "ананас", "ананас"),
    Fruit("mango", "🥭", "манго", "манго"),
    Fruit("kiwi", "🥝", "киви", "киви"),
    Fruit("melon", "🍈", "дыня", "дыню"),
    Fruit("blueberries", "🫐", "черника", "чернику"),
    Fruit("coconut", "🥥", "кокос", "кокос"),
)}

# Вступление: синонимичные спокойные формулировки.
LEADS: tuple[str, ...] = (
    "Небольшая проверка.",
    "Быстрая проверка.",
    "Подтвердите, что вы не бот.",
    "Один шаг до начала.",
    "Нужно убедиться, что вы человек.",
)

# Само задание. {fruit} подставляется в винительном падеже и выделяется жирным.
ACTIONS: tuple[str, ...] = (
    "Выберите {fruit}.",
    "Нажмите на {fruit}.",
    "Отметьте {fruit}.",
    "Найдите {fruit} среди вариантов ниже.",
    "Укажите {fruit}.",
)

OPTIONS_COUNT = 4
CALLBACK_PREFIX = "cap:"
_NONCE_RE = re.compile(r"^[0-9a-f]{4}$")


@dataclass(frozen=True)
class CaptchaConfig:
    lead: int
    action: int
    fruits: tuple[str, ...]  # ровно OPTIONS_COUNT ключей из FRUITS
    target: str              # один из fruits

    def signature(self) -> tuple:
        """По чему сравниваем «одинаковость» ботов: формулировка + набор фруктов (порядок неважен)."""
        return (self.lead, self.action, tuple(sorted(self.fruits)))

    def to_json(self) -> str:
        return json.dumps(
            {"lead": self.lead, "action": self.action, "fruits": list(self.fruits), "target": self.target},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "CaptchaConfig":
        d = json.loads(raw)
        cfg = cls(int(d["lead"]), int(d["action"]), tuple(d["fruits"]), str(d["target"]))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not (0 <= self.lead < len(LEADS) and 0 <= self.action < len(ACTIONS)):
            raise ValueError("неизвестная формулировка")
        if len(self.fruits) != OPTIONS_COUNT or len(set(self.fruits)) != OPTIONS_COUNT:
            raise ValueError("нужно ровно 4 разных фрукта")
        if any(k not in FRUITS for k in self.fruits) or self.target not in self.fruits:
            raise ValueError("неизвестный фрукт")


def generate_config(seed: str, taken: set[tuple] | None = None) -> CaptchaConfig:
    """Детерминированно по seed. Если конфигурация совпала с чужой из `taken`, берём следующую попытку
    (последовательность попыток тоже детерминирована, поэтому результат воспроизводим при тех же `taken`)."""
    rng = random.Random(hashlib.sha256(("captcha:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        keys = tuple(rng.sample(sorted(FRUITS), OPTIONS_COUNT))
        cfg = CaptchaConfig(
            lead=rng.randrange(len(LEADS)),
            action=rng.randrange(len(ACTIONS)),
            fruits=keys,
            target=rng.choice(keys),
        )
        if cfg.signature() not in taken:
            break
    return cfg


def target_for(cfg: CaptchaConfig, secret: str, user_id: int, nonce: str) -> str:
    """Какой фрукт нужно выбрать в этой капче. Зависит только от секрета бота, пользователя и nonce из кнопок."""
    digest = hmac.new(secret.encode(), f"captcha:{user_id}:{nonce}".encode(), hashlib.sha256).digest()
    return cfg.fruits[int.from_bytes(digest[:4], "big") % OPTIONS_COUNT]


def new_nonce(cfg: CaptchaConfig, secret: str, user_id: int, previous_target: str | None = None) -> str:
    """Случайный nonce нового показа; целевой фрукт при этом отличается от прошлого (если он известен)."""
    nonce = secrets.token_hex(2)
    for _ in range(50):
        if previous_target is None or target_for(cfg, secret, user_id, nonce) != previous_target:
            break
        nonce = secrets.token_hex(2)
    return nonce


def render_text(cfg: CaptchaConfig, target: str | None = None) -> str:
    """Текст сообщения (HTML). Название фрукта в задании — в винительном падеже и жирным."""
    fruit = FRUITS[target or cfg.target]
    task = ACTIONS[cfg.action].format(fruit=f"<b>{fruit.acc}</b>")
    return f"{LEADS[cfg.lead]}\n\n{task}"


def shuffled_order(cfg: CaptchaConfig, previous: list[str] | None = None) -> list[str]:
    """Новый порядок кнопок; отличается от предыдущего, если он известен."""
    order = list(cfg.fruits)
    rng = random.SystemRandom()
    for _ in range(10):
        rng.shuffle(order)
        if previous is None or order != previous:
            break
    return order


def build_keyboard(cfg: CaptchaConfig, order: list[str] | None = None, nonce: str | None = None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    order = order or shuffled_order(cfg)
    suffix = f":{nonce}" if nonce else ""
    buttons = [
        InlineKeyboardButton(text=FRUITS[k].emoji, callback_data=CALLBACK_PREFIX + k + suffix) for k in order
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def parse_answer(data: str | None) -> str | None:
    """Достаёт ключ фрукта из callback_data или None, если данные чужие/некорректные."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    key = data[len(CALLBACK_PREFIX):].split(":", 1)[0]
    return key if key in FRUITS else None


def parse_nonce(data: str | None) -> str | None:
    """nonce из callback_data («cap:apple:1a2b» -> «1a2b»); None у старых сообщений без nonce или при мусоре."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    _, _, nonce = data[len(CALLBACK_PREFIX):].partition(":")
    return nonce if _NONCE_RE.match(nonce) else None


def is_correct(cfg: CaptchaConfig, key: str | None, target: str | None = None) -> bool:
    """target — фрукт, заданный в показанной капче (target_for); без него — фрукт из конфигурации (старые сообщения)."""
    return key is not None and key == (target or cfg.target)
