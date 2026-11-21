"""Раздел «Бесплатные конфеты»: ежедневный подарок, реферальная ссылка и задания из БД.

Кнопка раздела стоит под приветствием (ниже «Купить конфеты»). Меню (HTML), каждый переход — НОВОЕ сообщение, прежнее
удаляется (как во всём боте); исключение — нажатие «Ежедневного подарка»: меню РЕДАКТИРУЕТСЯ на месте (handlers.claim_daily_gift):
    <b>{подпись кнопки раздела этого бота}</b>

    💫 {подсказка} 💫

    🍭 Ваш баланс: N🍬
  кнопки: «Ежедневный подарок» (зелёная; на время кулдауна — синяя с остатком « (5ч 12м)» после названия; остаток
  считается при показе меню и сам не обновляется), зелёная «Реферальная ссылка», затем по кнопке на каждое ВИДИМОЕ
  задание из БД (текст кнопки задан на сайте) и красная «⬅️ Назад» (в приветствие).

ЗАДАНИЙ В КОДЕ НЕТ. Они лежат в таблице `bonus_tasks` (models.BonusTask), общей для всех токенов бота, и редактируются во
вкладке «Кнопки в боте» на сайте: текст кнопки, описание, награда в 🍬, «требовать скриншот» и «одноразовое».
  * без скриншота — кнопка «Выполнено» сразу начисляет награду;
  * со скриншотом — пользователь присылает один или несколько скриншотов (до MAX_SHOTS) прямо в ответ боту, видит
    счётчик и жмёт «Отправить на проверку»; скрины уходят БОТОМ ДЛЯ ЧЕКОВ админам (вкладка «Модераторы») с кнопками
    «Подтвердить / Отклонить / Заблокировать» (app/task_review.py); награду начисляет решение админа.

«Одноразовое» (по умолчанию включено): награду за задание можно получить один раз на пользователя (отклонённая заявка не
считается). После награды кнопка задания в меню синяя (до неё — зелёная), в карточке написано, что задание выполнено и повторно его
выполнить нельзя, а кнопка действия серая и не работает (нажатие — только всплывающее окно). Если «Одноразовое» выключено, задание
можно выполнять снова и снова: кнопка всегда зелёная, награда начисляется каждый раз.

Ежедневный подарок: DAILY_REWARD конфет раз в 24 часа (атомарно, repo.claim_daily). Реферальная ссылка:
`https://t.me/<бот>?start=ref_<id>`; за каждого приглашённого, который прошёл капчу, пригласивший получает
REFERRAL_REWARD конфет (один раз за друга, repo.register_referral). Размеры наград — переменные окружения
DAILY_GIFT_CANDIES и REFERRAL_REWARD_CANDIES.

Рандомизация — по схеме проекта (NOTES.md): по seed (токен бота) ОДИН раз подбираются тексты кнопок и сообщений и
`callback_data`; хранится готовый результат (вид `tasks_menu` в `texts_config`), меняется только «Перенаписать текста».
НЕ рандомизируются: ссылка, названия и описания заданий (ввод владельца), числа и сами эмодзи (статичны: 🍭 🍬 🤍 💫 ✅ ⬅️).
Индексы в списках текстов только ДОБАВЛЯЮТСЯ в конец.
"""
import hashlib
import html
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import timedelta

from . import shop

CANDY = "🍬"
BALANCE_EMOJI = "🍭"
BACK_LABEL = "⬅️ Назад"
BACK_STYLE = "danger"       # «⬅️ Назад» всегда красная
ACTION_STYLE = "success"    # «Выполнено» / «Отправить скриншот» — зелёная
DAILY_READY_STYLE = "success"   # «Ежедневный подарок» можно забрать — зелёная
DAILY_WAIT_STYLE = "primary"    # идёт кулдаун подарка — синяя (с остатком времени в подписи)
REF_STYLE = "success"           # «Реферальная ссылка» — зелёная
TASK_STYLE = "success"          # кнопка задания в меню — зелёная
TASK_DONE_STYLE = "primary"     # одноразовое задание, за которое уже получена награда, — синяя
ACTION_DONE_STYLE = ""          # кнопка действия в карточке такого задания — серая (обычная, без цвета) и неактивная
MENU_STYLE = "primary"      # кнопка под приветствием — синяя, как соседние


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


DAILY_COOLDOWN = timedelta(hours=24)
DAILY_REWARD = _env_int("DAILY_GIFT_CANDIES", 10)
REFERRAL_REWARD = _env_int("REFERRAL_REWARD_CANDIES", 5)

MAX_SHOTS = 10          # скриншотов за одну отправку (столько же помещается в один альбом Telegram)
COLLECT_REFRESH_DELAY = 0.8   # пауза перед обновлением экрана-счётчика после последнего скриншота, сек
MAX_BUTTON = 64
MAX_REWARD = 1_000_000_000
MAX_TASKS = 80          # у Telegram не больше 100 кнопок под сообщением; две заняты подарком и ссылкой, одна — «Назад»
MAX_DESC = 3000         # сколько символов описания пользователь реально увидит (после выбора вариантов [а/б], без значков)
MAX_DESC_RAW = 8000     # лимит поля на сайте «как введено»
MAX_COMMENT = 500       # комментарий к заданию для админа (обычный текст, пользователю не показывается)

# ---------- списки текстов (индексы только добавляются в конец) ----------
# Подписи кнопок: пара эмодзи «левый ... правый» статична, по seed подбирается только текст.
LABEL_EMOJI = ("🍭", "🍬")
DAILY_EMOJI = ("🤍", "🤍")
REF_EMOJI = ("💫", "💫")
ACTION_EMOJI = ("✅", "✅")

LABELS = ("Бесплатные конфеты", "Конфеты бесплатно", "Получить конфеты", "Заработать конфеты")
DAILY_LABELS = ("Ежедневный подарок", "Подарок дня", "Подарок на сегодня", "Ежедневная награда")
REF_LABELS = ("Реферальная ссылка", "Пригласить друзей", "Ссылка для друзей", "Моя реферальная ссылка")
SEND_LABELS = ("Отправить скриншот", "Прикрепить скриншот", "Загрузить скриншот")
DONE_LABELS = ("Выполнено", "Задание выполнено", "Я выполнил задание")

HINTS = (
    "Выберите, как получить конфеты бесплатно",
    "Здесь можно получить конфеты бесплатно — выберите подходящий вариант",
    "Заберите подарок, пригласите друзей или выполните задание",
    "Выберите способ, которым хотите получить конфеты",
)
DAILY_OK = (
    "Вы получили ежедневный подарок: +{amount}🍬",
    "Ежедневный подарок получен: +{amount}🍬",
    "Подарок зачислен: +{amount}🍬",
    "Готово! Вы получили +{amount}🍬",
)
DAILY_WAIT = (
    "Подарок уже получен. Следующий будет доступен через {left}",
    "Вы уже забирали подарок. Приходите через {left}",
    "Следующий подарок можно будет забрать через {left}",
)
REF_INTROS = (
    "Отправьте эту ссылку друзьям. Когда друг впервые запустит бота и пройдёт проверку, вы получите +{reward}🍬.",
    "Поделитесь ссылкой с друзьями: за каждого, кто запустит бота и пройдёт проверку, вы получите +{reward}🍬.",
    "Приглашайте друзей по этой ссылке — за каждого, кто пройдёт проверку в боте, вам начислят +{reward}🍬.",
)
REF_COUNTS = ("Приглашено друзей: {count}", "Друзей приглашено: {count}", "Вы пригласили друзей: {count}")
REWARD_LINES = ("Награда: {reward}🍬", "За выполнение: {reward}🍬", "Вы получите: {reward}🍬")
SEND_PROMPTS = (
    "Пришлите скриншот выполнения задания одним фото.",
    "Отправьте скриншот, подтверждающий выполнение, одним фото.",
    "Прикрепите скриншот выполненного задания — одним фото.",
)
NOT_PHOTO = (
    "Нужен скриншот: пришлите фото или картинку файлом.",
    "Это не похоже на скриншот. Пришлите фото или картинку файлом.",
    "Не получилось принять файл. Отправьте скриншот фото или картинкой.",
)
SENT = (
    "Скриншот отправлен на проверку. Результат придёт сюда.",
    "Спасибо! Скриншот принят на проверку, о решении мы сообщим.",
    "Скриншот получен и отправлен на проверку.",
)
DONE_OK = ("Задание выполнено: +{reward}🍬", "Награда получена: +{reward}🍬", "Готово! Вы получили +{reward}🍬")
APPROVED = (
    "Задание «{task}» принято: +{reward}🍬",
    "Скриншот по заданию «{task}» принят. Начислено +{reward}🍬",
    "Задание «{task}» проверено: вам начислено +{reward}🍬",
)
REJECTED = (
    "Задание «{task}» отклонено. Вы можете отправить скриншот ещё раз.",
    "Скриншот по заданию «{task}» не принят. Попробуйте отправить его снова.",
    "Задание «{task}» не засчитано. Отправьте скриншот повторно.",
)
# Что видит пользователь, когда админ нажал «Отклонить» под его заданием. Фиксированный текст заказчика (не рандомизируется,
# одинаков у всех ботов и не зависит от названия задания); вид `rejected` в `tasks_menu` оставлен только ради сохранённых конфигов.
REJECTED_NOTICE = (
    "❌ Запрос о выполнении задания был отклонён, так как задание выполнено не полностью, "
    "либо Вы прикрепили не все скриншоты."
)
STATUS_PENDING = ("Скриншот на проверке", "Задание на проверке", "Ожидает проверки")
STATUS_DONE = ("Задание уже выполнено", "Вы уже выполнили это задание", "Награда за задание получена")
# Приписка к «уже выполнено» у одноразового задания. Фиксированный текст (не рандомизируется и не хранится в конфиге бота):
# так он появляется и у уже работающих ботов, а их сохранённые тексты не трогаются.
ONE_TIME_NOTE = "Повторно выполнить это задание нельзя."
INVITED = (
    "По вашей ссылке пришёл друг: +{reward}🍬",
    "Ваш друг присоединился по ссылке: +{reward}🍬",
    "Новый друг в боте! Вам начислено +{reward}🍬",
)

# Фиксированные тексты приёма скриншотов (не рандомизируются: в сохранённых конфигах ботов их нет, как в «Купить конфеты»).
SHOTS_PROMPT = (
    "Пришлите скриншоты выполнения задания: можно одним или несколькими сообщениями (до {max}). "
    "Когда всё отправите, нажмите «Отправить на проверку»."
)
SHOTS_COUNTER = "Принято скриншотов: {count} из {max}"
SHOTS_LIMIT = "Можно прикрепить не больше {max} скриншотов. Нажмите «Отправить на проверку»."
SHOTS_EMPTY = "Сначала пришлите хотя бы один скриншот."
SHOTS_SUBMIT = "Отправить на проверку ({count})"
SHOTS_SUBMIT_EMOJI = ("✅", "✅")
TASK_BLOCKED_ALERT = "❌ Отправка скриншотов для вас недоступна."
SHOTS_NOT_SENT = "Не удалось отправить скриншоты на проверку. Попробуйте позже."

_BAD = ("---", "***", "———", "═", "─", "│")

# Что рандомизируется как ТЕКСТ: ключ -> (список вариантов, пара эмодзи для подписи кнопки или None)
FIELDS: tuple[tuple[str, tuple[str, ...], tuple[str, str] | None], ...] = (
    ("label", LABELS, LABEL_EMOJI), ("daily_label", DAILY_LABELS, DAILY_EMOJI), ("ref_label", REF_LABELS, REF_EMOJI),
    ("send_label", SEND_LABELS, ACTION_EMOJI), ("done_label", DONE_LABELS, ACTION_EMOJI),
    ("hint", HINTS, None), ("daily_ok", DAILY_OK, None), ("daily_wait", DAILY_WAIT, None),
    ("ref_intro", REF_INTROS, None), ("ref_count", REF_COUNTS, None), ("reward_line", REWARD_LINES, None),
    ("send_prompt", SEND_PROMPTS, None), ("not_photo", NOT_PHOTO, None), ("sent", SENT, None),
    ("done_ok", DONE_OK, None), ("approved", APPROVED, None), ("rejected", REJECTED, None),
    ("status_pending", STATUS_PENDING, None), ("status_done", STATUS_DONE, None), ("invited", INVITED, None),
)
# обязательные подстановки в тексте
REQUIRED: dict[str, tuple[str, ...]] = {
    "daily_ok": ("{amount}",), "daily_wait": ("{left}",), "ref_intro": ("{reward}",), "ref_count": ("{count}",),
    "reward_line": ("{reward}",), "done_ok": ("{reward}",), "approved": ("{task}", "{reward}"),
    "rejected": ("{task}",), "invited": ("{reward}",),
}

# callback_data: префикс действия + хэш бота (уникальны у каждого бота). Ключ действия -> префикс.
CB_PREFIXES: dict[str, str] = {
    "open": "fo:", "daily": "fd:", "ref": "fr:", "back": "fb:", "menu": "fm:", "task": "ft:", "send": "fs:", "done": "fk:",
}
PREFIXES = tuple(CB_PREFIXES.values())
ID_ACTIONS = ("task", "send", "done")   # к этим callback_data добавляется id задания: «ft:<хэш>:<id>»
HASH_LEN = 10


def _check_tables() -> None:
    for _, words, emoji in FIELDS:
        if len(set(words)) != len(words):
            raise AssertionError("повторы в списке текстов")
        if any(any(bad in w for bad in _BAD) for w in words):
            raise AssertionError("декоративные разделители запрещены")
        if emoji is not None and any(CANDY in w for w in words):
            raise AssertionError("в подписях кнопок 🍬 только в статичной паре эмодзи")
    if len(set(PREFIXES)) != len(PREFIXES):
        raise AssertionError("префиксы callback_data совпадают")
    for key, needed in REQUIRED.items():
        words = next(w for k, w, _ in FIELDS if k == key)
        if any(not all(n in w for n in needed) for w in words):
            raise AssertionError(f"в вариантах «{key}» нет обязательной подстановки")


_check_tables()


def fill(template: str, **values) -> str:
    """Подставляет значения в {имя}. Не str.format: в шаблонах могут быть скобки, а значения — произвольный текст."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def label(text: str, emoji: tuple[str, str]) -> str:
    return f"{emoji[0]} {text} {emoji[1]}"


@dataclass(frozen=True)
class TasksConfig:
    idx: dict            # ключ -> индекс выбранного варианта (для справки/сигнатуры)
    t: dict              # ключ -> ГОТОВЫЙ текст (именно он показывается пользователям)
    cb: dict             # действие -> уникальный callback_data этого бота

    def text(self, key: str) -> str:
        return self.t[key]

    @property
    def heading(self) -> str:
        """Заголовок раздела = подпись кнопки, по которой пользователь сюда перешёл (с её эмодзи)."""
        return self.t["label"]

    def signature(self) -> str:
        return "|".join([self.t["label"], self.t["daily_label"], self.t["ref_label"], self.t["hint"], *self.cb.values()])

    def to_json(self) -> str:
        return json.dumps({"v": 1, "idx": self.idx, "t": self.t, "cb": self.cb}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TasksConfig":
        d = json.loads(raw)
        cfg = cls(idx=dict(d["idx"]), t={k: str(v) for k, v in d["t"].items()}, cb={k: str(v) for k, v in d["cb"].items()})
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for key, _, _ in FIELDS:
            if not self.t.get(key, "").strip():
                raise ValueError(f"нет текста «{key}»")
        for key, needed in REQUIRED.items():
            if not all(n in self.t[key] for n in needed):
                raise ValueError(f"в тексте «{key}» нет подстановки")
        if any(bad in "".join(self.t.values()) for bad in _BAD):
            raise ValueError("декоративные разделители запрещены")
        labels = [self.t[k] for k in ("label", "daily_label", "ref_label")]
        if len(set(labels)) != len(labels):
            raise ValueError("подписи кнопок совпадают")
        if set(self.cb) != set(CB_PREFIXES):
            raise ValueError("неполный набор callback_data")
        for action, cb in self.cb.items():
            if not cb.startswith(CB_PREFIXES[action]):
                raise ValueError("неверный префикс callback_data")
            if not cb.isascii() or len((cb + ":" + "9" * 18).encode()) > 64:
                raise ValueError("callback_data: только ASCII, до 64 байт (с id задания)")
        if len(set(self.cb.values())) != len(self.cb):
            raise ValueError("callback_data совпадают")


def generate_config(seed: str, taken: set[str] | None = None) -> TasksConfig:
    """Детерминированно по seed. Если итог совпал с чужим из `taken`, берётся следующая попытка
    (последовательность попыток тоже детерминирована)."""
    rng = random.Random(hashlib.sha256(("tasks_menu:" + seed).encode()).digest())
    taken = taken or set()
    cfg = None
    for _ in range(300):
        idx, t = {}, {}
        for key, words, emoji in FIELDS:
            i = rng.randrange(len(words))
            idx[key] = i
            t[key] = label(words[i], emoji) if emoji else words[i]
        salt = rng.getrandbits(64)
        cb = {
            action: prefix + hashlib.sha256(f"fc:{seed}:{salt}:{action}".encode()).hexdigest()[:HASH_LEN]
            for action, prefix in CB_PREFIXES.items()
        }
        cfg = TasksConfig(idx=idx, t=t, cb=cb)
        if cfg.signature() not in taken:
            break
    cfg.validate()
    return cfg


# ---------- задание из БД ----------

@dataclass(frozen=True)
class Task:
    id: int
    button_text: str
    about: str
    reward: int
    require_screenshot: bool
    one_time: bool = True   # награду можно получить один раз; после неё кнопка синяя, а повторить нельзя
    comment: str = ""       # пояснение для админа (в боте для чеков вместо названия); пользователь его не видит


def task_from_row(row) -> Task:
    return Task(
        id=int(row.id), button_text=str(row.button_text), about=str(row.about or ""),
        reward=int(row.reward), require_screenshot=bool(row.require_screenshot),
        one_time=bool(getattr(row, "one_time", True)), comment=str(getattr(row, "comment", "") or ""),
    )


def is_done(task: Task, status: str | None) -> bool:
    """Одноразовое задание, за которое пользователь уже получил награду (отклонённое и «на проверке» не считаются)."""
    return task.one_time and status == "approved"


def card_status(task: Task, status: str | None) -> str | None:
    """Статус для карточки: у неодноразового задания принятая попытка не блокирует новую, поэтому «выполнено» не показываем."""
    return None if status == "approved" and not task.one_time else status

# ---------- проверка ввода с сайта ----------

def normalize_button_text(value: str | None) -> str:
    text = " ".join((value or "").split())
    if not text:
        raise ValueError("Укажите текст кнопки")
    if len(text) > MAX_BUTTON:
        raise ValueError(f"Текст кнопки — не длиннее {MAX_BUTTON} символов")
    return text


def normalize_reward(value) -> int:
    try:
        text = str(value).strip().replace(" ", "")
        if not re.fullmatch(r"\d{1,12}", text):
            raise ValueError
        reward = int(text)
    except (ValueError, TypeError):
        raise ValueError("Награда — целое число конфет") from None
    if not 1 <= reward <= MAX_REWARD:
        raise ValueError(f"Награда — от 1 до {MAX_REWARD}")
    return reward


def normalize_description(value: str | None) -> str:
    """Описание задания «как введено» (оформление и варианты [а/б] — как у товаров, см. shop.format_about)."""
    about = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not about:
        raise ValueError("Укажите описание задания")
    if len(about) > MAX_DESC_RAW:
        raise ValueError(f"Описание задания — не длиннее {MAX_DESC_RAW} символов (считая скобки вариантов и значки оформления)")
    shown = shop.telegram_about_length(about)
    if shown > MAX_DESC:
        raise ValueError(f"В Telegram описание займёт до {shown} символов, а можно не больше {MAX_DESC}")
    return about


def normalize_comment(value: str | None) -> str:
    """Комментарий для админа — необязательный обычный текст (без оформления: в боте для чеков он экранируется)."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_COMMENT:
        raise ValueError(f"Комментарий к заданию — не длиннее {MAX_COMMENT} символов")
    return text


# ---------- время, ссылка, разбор /start ----------

def fmt_left(seconds: float) -> str:
    """«5ч 12м», «12м», «меньше минуты» — сколько ждать следующий подарок."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}ч {minutes}м" if minutes else f"{hours}ч"
    return f"{minutes}м" if minutes else "меньше минуты"


def ref_link(bot_username: str | None, user_id: int) -> str | None:
    name = (bot_username or "").strip().lstrip("@")
    return f"https://t.me/{name}?start=ref_{int(user_id)}" if name else None


_START_RE = re.compile(r"^/start(?:@\w+)?[ \t]+ref_(\d{1,15})\s*$")


def parse_start_referrer(text: str | None) -> int | None:
    """id пригласившего из «/start ref_<id>»; None — обычный /start или некорректная ссылка."""
    m = _START_RE.match((text or "").strip())
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


# ---------- «УДВОЕНИЕ наград» (app/comeback.py) ----------
# Пока действует, награды за задания (не за ежедневный подарок и не за реферала) ×BOOST_FACTOR: в меню заданий внизу идёт строка с
# остатком времени, а награда в кнопке и заголовке задания показана как «<зачёркнутая старая> новая».
BOOST_FACTOR = 2
BOOST_LINE = "🔥 Действует УДВОЕНИЕ суммы наград. Конец через: {left}"
_STRIKE = "\u0336"   # объединяемый «длинный штрих»: кнопки Telegram не поддерживают разметку, зачёркивание делаем знаком


def boosted(reward: int) -> int:
    return int(reward) * BOOST_FACTOR


def fmt_boost_left(seconds: float) -> str:
    """«23ч 59м»: всегда часы и минуты (минуты округляются вверх, меньше минуты не показываем)."""
    total = max(int(-(-float(seconds) // 60)), 1)
    hours, minutes = divmod(total, 60)
    return f"{hours}ч {minutes}м"


def boost_line_html(seconds: float) -> str:
    """Строка внизу меню (жирным): «🔥 Действует УДВОЕНИЕ суммы наград. Конец через: 23ч 59м»."""
    return f"<b>{_esc(fill(BOOST_LINE, left=fmt_boost_left(seconds)))}</b>"


def _reward_re(reward: int) -> "re.Pattern[str]":
    return re.compile(rf"(?<![\d.,])({int(reward)})(\s?){re.escape(CANDY)}")


def strike(text: str) -> str:
    return "".join(ch + _STRIKE for ch in text)


def boost_button_text(button_text: str, reward: int) -> str:
    """Подпись кнопки при удвоении: «200🍬» -> «2̶0̶0̶🍬 400🍬» (старая сумма зачёркнута, рядом новая). Награда в тексте кнопки — это то,
    что владелец сам написал (например, «Подписаться (200🍬)»); если суммы задания в подписи нет, текст не меняется."""
    return _reward_re(reward).sub(
        lambda m: f"{strike(m.group(1))}{m.group(2)}{CANDY} {boosted(reward)}{m.group(2)}{CANDY}", button_text
    )


def boost_title_html(button_text: str, reward: int) -> str:
    """Заголовок карточки задания (HTML, экранирован): «200🍬» -> «<s>200🍬</s> 400🍬»."""
    return _reward_re(reward).sub(
        lambda m: f"<s>{m.group(1)}{m.group(2)}{CANDY}</s> {boosted(reward)}{m.group(2)}{CANDY}", _esc(button_text)
    )


# ---------- тексты сообщений ----------

def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_menu(cfg: TasksConfig, deco, balance: int, boost_left: float | None = None) -> str:
    """Меню раздела: заголовок жирным, подсказка в общем маркере бота, баланс из БД; при действующем удвоении наград
    (boost_left — секунд до конца) внизу ещё строка с остатком времени."""
    text = (
        f"<b>{_esc(cfg.heading)}</b>\n\n"
        f"{deco.wrap(_esc(cfg.text('hint')))}\n\n"
        f"{BALANCE_EMOJI} Ваш баланс: {int(balance)}{CANDY}"
    )
    if boost_left:
        text += f"\n\n{boost_line_html(boost_left)}"
    return text


def render_ref(cfg: TasksConfig, link: str, count: int, reward: int = REFERRAL_REWARD) -> str:
    """Экран реферальной ссылки. Ссылка — в <code> (нажатием копируется), не рандомизируется."""
    return (
        f"<b>{_esc(cfg.text('ref_label'))}</b>\n\n"
        f"{_esc(fill(cfg.text('ref_intro'), reward=reward))}\n\n"
        f"<code>{_esc(link)}</code>\n\n"
        f"{_esc(fill(cfg.text('ref_count'), count=int(count)))}"
    )


def render_task(cfg: TasksConfig, task: Task, seed: str, status: str | None = None, boost_left: float | None = None) -> str:
    """Карточка задания: название жирным, описание из БД (оформление и варианты — как у товаров), награда, статус.
    При удвоении наград: сумма в названии «<s>200🍬</s> 400🍬», в строке награды — новая сумма, внизу — строка с остатком времени."""
    title = boost_title_html(task.button_text, task.reward) if boost_left else _esc(task.button_text)
    text = f"<b>{title}</b>"
    about = shop.format_about(task.about, seed, task.id)
    if about:
        text += f"\n\n{about}"
    reward = boosted(task.reward) if boost_left else task.reward
    text += f"\n\n{BALANCE_EMOJI} {_esc(fill(cfg.text('reward_line'), reward=reward))}"
    if status == "approved":
        text += f"\n\n✅ {_esc(cfg.text('status_done'))}"
        if task.one_time:
            text += f"\n{_esc(ONE_TIME_NOTE)}"
    elif status == "pending":
        text += f"\n\n🤍 {_esc(cfg.text('status_pending'))}"
    if boost_left:
        text += f"\n\n{boost_line_html(boost_left)}"
    return text


def render_send_prompt(
    cfg: TasksConfig, task: Task, count: int = 0, notice: str | None = None, boost_left: float | None = None
) -> str:
    """Экран приёма скриншотов: название, подсказка (сколько можно прислать), счётчик принятых и, если нужно, замечание
    (например, достигнут предел). При удвоении наград — как в карточке задания."""
    title = boost_title_html(task.button_text, task.reward) if boost_left else _esc(task.button_text)
    reward = boosted(task.reward) if boost_left else task.reward
    text = f"<b>{title}</b>\n\n{_esc(fill(SHOTS_PROMPT, max=MAX_SHOTS))}"
    text += f"\n\n{BALANCE_EMOJI} {_esc(fill(cfg.text('reward_line'), reward=reward))}"
    if count > 0:
        text += f"\n\n📎 {_esc(fill(SHOTS_COUNTER, count=count, max=MAX_SHOTS))}"
    if notice:
        text += f"\n{_esc(notice)}"
    if boost_left:
        text += f"\n\n{boost_line_html(boost_left)}"
    return text


# ---------- кнопки и разбор нажатий ----------

def menu_row(cfg: TasksConfig) -> tuple[str, str, str]:
    """Кнопка под приветствием: (подпись, callback_data, стиль) — для shop_buttons.build_keyboard(extra_rows=…)."""
    return (cfg.text("label"), cfg.cb["open"], MENU_STYLE)


def _cb(cfg: TasksConfig, action: str, task_id: int | None = None) -> str:
    return f"{cfg.cb[action]}:{int(task_id)}" if task_id is not None else cfg.cb[action]


def cooldown_suffix(seconds: float) -> str:
    """« (5ч 12м)» — сколько осталось до следующего подарка: всегда часы и минуты, минуты округляются вверх, «0ч 0м» не показываем."""
    total = max(int(-(-float(seconds) // 60)), 1)   # минуты вверх, не меньше одной
    hours, minutes = divmod(total, 60)
    return f" ({hours}ч {minutes}м)"


def daily_button_label(cfg: TasksConfig, left: float | None) -> str:
    """Подпись кнопки подарка. Во время кулдауна (left > 0 секунд) остаток вставляется после названия, перед закрывающим
    эмодзи: «🤍 Ежедневный подарок (5ч 12м) 🤍»."""
    text = cfg.text("daily_label")
    if left is None or left <= 0:
        return text
    tail = " " + DAILY_EMOJI[1]
    if text.endswith(tail):
        return text[: -len(tail)] + cooldown_suffix(left) + tail
    return text + cooldown_suffix(left)


def menu_rows(
    cfg: TasksConfig, tasks: list[Task], daily_left: float | None = None, boost: bool = False
) -> list[list[tuple[str, str]]]:
    """Ряды меню [(подпись, callback_data)] без aiogram (для тестов): подарок, ссылка, задания по порядку, «Назад».
    daily_left — сколько секунд осталось до следующего подарка (None / 0 — можно забирать). boost — действует удвоение наград:
    сумма задания в подписи кнопки показана как «зачёркнутая старая + новая»."""
    rows = [[(daily_button_label(cfg, daily_left), cfg.cb["daily"])], [(cfg.text("ref_label"), cfg.cb["ref"])]]
    rows += [[(boost_button_text(t.button_text, t.reward) if boost else t.button_text, _cb(cfg, "task", t.id))] for t in tasks]
    rows.append([(BACK_LABEL, cfg.cb["back"])])
    return rows


def menu_styles(
    tasks: list[Task], daily_left: float | None = None, statuses: dict[int, str] | None = None
) -> tuple[str, ...]:
    """Цвета кнопок меню. Задание зелёное; одноразовое, за которое уже получена награда (`statuses` — {id: статус}), синее."""
    daily = DAILY_WAIT_STYLE if daily_left is not None and daily_left > 0 else DAILY_READY_STYLE
    statuses = statuses or {}
    marks = tuple(TASK_DONE_STYLE if is_done(t, statuses.get(t.id)) else TASK_STYLE for t in tasks)
    return (daily, REF_STYLE) + marks + (BACK_STYLE,)


def task_rows(cfg: TasksConfig, task: Task, status: str | None) -> tuple[list[list[tuple[str, str]]], tuple[str, ...]]:
    """Кнопки карточки: действие (если задание можно выполнить) и «Назад» в меню раздела. У одноразового задания с уже
    полученной наградой действие остаётся на месте, но серое и не работает (нажатие даёт только всплывающее окно)."""
    rows: list[list[tuple[str, str]]] = []
    styles: list[str] = []
    done = is_done(task, status)
    if done or status not in ("approved", "pending"):
        if task.require_screenshot:
            rows.append([(cfg.text("send_label"), _cb(cfg, "send", task.id))])
        else:
            rows.append([(cfg.text("done_label"), _cb(cfg, "done", task.id))])
        styles.append(ACTION_DONE_STYLE if done else ACTION_STYLE)
    rows.append([(BACK_LABEL, cfg.cb["menu"])])
    styles.append(BACK_STYLE)
    return rows, tuple(styles)


def collect_rows(cfg: TasksConfig, task: Task, count: int) -> tuple[list[list[tuple[str, str]]], tuple[str, ...]]:
    """Кнопки экрана приёма скриншотов: «Отправить на проверку (N)» (появляется после первого скрина; это действие
    `done` с id задания) и красная «Назад» — отмена: принятые скрины не сохраняются."""
    rows: list[list[tuple[str, str]]] = []
    styles: list[str] = []
    if count > 0:
        rows.append([(label(fill(SHOTS_SUBMIT, count=count), SHOTS_SUBMIT_EMOJI), _cb(cfg, "done", task.id))])
        styles.append(ACTION_STYLE)
    rows.append([(BACK_LABEL, cfg.cb["menu"])])
    styles.append(BACK_STYLE)
    return rows, tuple(styles)


def back_rows(cfg: TasksConfig) -> tuple[list[list[tuple[str, str]]], tuple[str, ...]]:
    """Одна красная «Назад» (в меню раздела): экраны ссылки и ожидания скриншота."""
    return [[(BACK_LABEL, cfg.cb["menu"])]], (BACK_STYLE,)


def _keyboard(rows: list[list[tuple[str, str]]], styles: tuple[str, ...]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=cb, **({"style": style} if style else {})) for t, cb in row]
            for row, style in zip(rows, styles)
        ]
    )


def build_menu_keyboard(
    cfg: TasksConfig, tasks: list[Task], daily_left: float | None = None, statuses: dict[int, str] | None = None,
    boost: bool = False,
):
    return _keyboard(menu_rows(cfg, tasks, daily_left, boost), menu_styles(tasks, daily_left, statuses))


def build_task_keyboard(cfg: TasksConfig, task: Task, status: str | None):
    return _keyboard(*task_rows(cfg, task, status))


def build_collect_keyboard(cfg: TasksConfig, task: Task, count: int):
    return _keyboard(*collect_rows(cfg, task, count))


def build_back_keyboard(cfg: TasksConfig):
    return _keyboard(*back_rows(cfg))


def parse_callback(cfg: TasksConfig, data: str | None) -> tuple[str, int | None] | None:
    """(действие, id задания) по callback_data ЭТОГО бота; None — чужие или устаревшие данные.
    Действия: open, daily, ref, back, menu, task, send, done (у последних трёх — id задания)."""
    if not data:
        return None
    for action, key in cfg.cb.items():
        if data == key:
            return (action, None)
        head, sep, tail = data.rpartition(":")
        if action in ID_ACTIONS and sep and head == key and tail.isascii() and tail.isdigit() and len(tail) <= 18:
            return (action, int(tail))
    return None


def is_image_message(message) -> tuple[str, str] | None:
    """(file_id, 'photo'|'document') для скриншота: фото или картинка файлом; иначе None."""
    photo = getattr(message, "photo", None)
    if photo:
        return photo[-1].file_id, "photo"
    doc = getattr(message, "document", None)
    if doc is not None and str(getattr(doc, "mime_type", "") or "").startswith("image/"):
        return doc.file_id, "document"
    return None
