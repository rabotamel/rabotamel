"""Реестр рандомизируемых сообщений бота.

Каждое сообщение бота, у которого есть «формула случайного подбора» (слова, фразы, эмодзи, компоновка),
описывается одной записью TextKind. Реестр нужен, чтобы:
  * при первом запуске бота создать вариант каждого сообщения (repo.get_or_create_texts);
  * «Уникальные текста» в «Песочнице» заменила ВСЕ варианты разом (repo.get_or_create_texts(reroll=True));
  * варианты не повторялись у разных ботов (сравнение по signature()).

Как добавить новое рандомизируемое сообщение — см. NOTES.md, раздел «Рандомизация сообщений».
"""
from dataclasses import dataclass
from typing import Any, Callable

from . import captcha, greeting


@dataclass(frozen=True)
class TextKind:
    key: str                                   # ключ в БД и в BotTexts.configs
    title: str                                 # название для панели
    generate: Callable[[str, set], Any]        # (seed, занятые signature других ботов) -> конфиг
    from_json: Callable[[str], Any]            # JSON из БД -> конфиг (с проверкой, бросает исключение)
    # конфиг обязан иметь to_json() и signature()


KINDS: tuple[TextKind, ...] = (
    TextKind("captcha", "Капча", captcha.generate_config, captcha.CaptchaConfig.from_json),
    TextKind("greeting", "Приветствие", greeting.generate_config, greeting.GreetingConfig.from_json),
)

# Где лежит JSON вида в БД. captcha — историческая отдельная колонка; всё новое хранится в texts_config
# (один JSON {ключ: JSON-конфиг}), чтобы новое сообщение не требовало новой колонки.
LEGACY_COLUMNS: dict[str, str] = {"captcha": "captcha_config"}


class BotTexts:
    """Актуальные варианты сообщений работающего бота. Объект изменяемый: после перерандомизации
    обработчики сразу видят новые варианты без перезапуска бота."""

    def __init__(self, raw: dict[str, str]) -> None:
        self.version = 0  # растёт при каждой замене (хендлеры по нему понимают, что старая капча устарела)
        self.configs: dict[str, Any] = {}
        self.replace(raw)

    def replace(self, raw: dict[str, str]) -> None:
        new = {k.key: k.from_json(raw[k.key]) for k in KINDS}
        self.configs = new
        self.version += 1

    @property
    def captcha(self) -> captcha.CaptchaConfig:
        return self.configs["captcha"]

    @property
    def greeting(self) -> greeting.GreetingConfig:
        return self.configs["greeting"]
