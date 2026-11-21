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

from . import captcha, deco, gallery, greeting, shop, shop_buttons, suggest_ui, tasks


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
    TextKind(
        "shop_buttons", "Кнопки «Магазин» и «Галерея»",
        shop_buttons.generate_config, shop_buttons.ShopButtonsConfig.from_json,
    ),
    TextKind("deco", "Декоративный маркер", deco.generate_config, deco.DecoConfig.from_json),
    TextKind("gallery", "Раздел «Галерея»", gallery.generate_config, gallery.GalleryConfig.from_json),
    TextKind(
        "suggest", "Кнопка и меню «Предложка»", suggest_ui.generate_config, suggest_ui.SuggestConfig.from_json,
    ),
    TextKind("shop_menu", "Раздел «Магазин»", shop.generate_config, shop.ShopConfig.from_json),
    TextKind("tasks_menu", "Раздел «Бесплатные конфеты»", tasks.generate_config, tasks.TasksConfig.from_json),
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

    @property
    def shop_buttons(self) -> shop_buttons.ShopButtonsConfig:
        return self.configs["shop_buttons"]

    @property
    def deco(self) -> deco.DecoConfig:
        return self.configs["deco"]

    @property
    def suggest(self) -> suggest_ui.SuggestConfig:
        return self.configs["suggest"]

    @property
    def gallery(self) -> gallery.GalleryConfig:
        return self.configs["gallery"]

    @property
    def shop_menu(self) -> shop.ShopConfig:
        return self.configs["shop_menu"]

    @property
    def tasks_menu(self) -> tasks.TasksConfig:
        return self.configs["tasks_menu"]
