"""Логика вкладки «Песочница»: выдача/изъятие 🍬 (в конфетах, рублях или долларах), «Уникальные текста»
(перезапись всех рандомизируемых текстов), удаление пользователя, «Удалить все видео» (очистка галереи).

Все действия применяются к текущему основному боту и только пока он запущен (нужны его счётчики в памяти
и сам объект бота для уведомления). Пользователь ищется среди тех, кто прошёл капчу: ID — по словарю в памяти,
@username — по юзернеймам, которые бот запоминает у прошедших капчу (в БД не ходим).
"""
import logging
import re

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from . import currency, greeting, messaging, repo, video_pool
from .manager import BotManager
from .stats import norm_username

log = logging.getLogger("bot.sandbox")

CANDY = greeting.CANDY

_ID_RE = re.compile(r"^\d{3,15}$")
# Юзернейм Telegram: 5–32 символа, латиница/цифры/_, начинается с буквы
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/", re.IGNORECASE)


class SandboxError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _running(manager: BotManager):
    if not manager.is_running():
        raise SandboxError(409, "Основной бот сейчас не запущен — действие невозможно")
    return manager.stats, manager.bot


def parse_target(raw: str) -> tuple[str, int | str]:
    """('id', 123) или ('username', 'name'); иначе SandboxError(400)."""
    t = (raw or "").strip()
    t = _LINK_RE.sub("", t).strip().lstrip("@").strip("/")
    if _ID_RE.match(t):
        return "id", int(t)
    if _NAME_RE.match(t):
        return "username", t.lower()
    raise SandboxError(400, "Укажите числовой ID или юзернейм вида @username")


def resolve(manager: BotManager, raw: str) -> tuple[int, str]:
    """Возвращает (user_id, отображаемое имя). 404, если такого пользователя нет среди прошедших капчу."""
    stats, _ = _running(manager)
    kind, value = parse_target(raw)
    if kind == "id":
        uid = int(value)
        if not stats.has(uid):
            raise SandboxError(404, "Пользователь с таким ID не найден среди прошедших капчу этого бота")
    else:
        uid = stats.find_by_username(str(value))
        if uid is None:
            raise SandboxError(
                404,
                "Пользователь с таким юзернеймом не найден. Юзернейм бот запоминает у тех, кто прошёл капчу "
                "или писал боту после обновления; для остальных используйте числовой ID",
            )
    return uid, display(stats.username_of(uid), uid)


def display(username: str | None, uid: int) -> str:
    return f"@{username} ({uid})" if username else str(uid)


def _require_confirm(confirm: bool) -> None:
    if not confirm:
        raise SandboxError(400, "Действие не подтверждено")


def _gift_text(candies: int, cur: str, paid: str | None) -> str:
    """Что видит пользователь при выдаче: в конфетах — «Вы получили», за рубли/доллары — «Вы купили … за …»."""
    n = greeting.format_number(candies)
    if cur == currency.CANDY:
        return f"Вы получили +{n}{CANDY}"
    return f"Вы купили {n}{CANDY} за {paid}{currency.SYMBOLS[cur]}"


async def change_candies(
    manager: BotManager, target: str, amount: str, cur: str, give: bool, confirm: bool
) -> dict:
    """amount — введённая админом сумма в валюте `cur` (candy | rub | usd); она переводится в целые конфеты по курсу
    1🍬 = 0,8 ₽ = 0,01 $."""
    _require_confirm(confirm)
    try:
        value = currency.parse_amount(amount, cur)
    except ValueError as e:
        raise SandboxError(400, str(e)) from None
    candies = currency.to_candies(value, cur)
    if candies < 1:
        raise SandboxError(400, "Сумма меньше одной конфеты")
    if candies > currency.MAX_CANDIES:
        raise SandboxError(400, f"Не больше {currency.MAX_CANDIES} конфет за раз")
    paid = None if cur == currency.CANDY else currency.format_money(value)

    uid, who = resolve(manager, target)
    stats, bot = _running(manager)

    if give:
        balance = await repo.add_candies(stats.bot_id, uid, candies)
        if balance is None:
            raise SandboxError(404, "Пользователь не найден в базе (возможно, его только что удалили)")
        notified, notify_error = True, None
        try:
            # уведомление о конфетах — без картинки бота
            await messaging.send_message(bot, uid, _gift_text(candies, cur, paid))
        except TelegramForbiddenError as e:
            notified, notify_error = False, "пользователь заблокировал бота"
            if messaging.is_blocked_by_user(e):
                # человек сам заблокировал бота — удаляем его из базы
                stats.remove(uid)
                await stats.flush()
        except TelegramAPIError as e:
            notified, notify_error = False, str(e)
        log.info("Песочница: выдано %s пользователю %s у бота %s", candies, uid, stats.bot_id)
        return {
            "ok": True, "who": who, "user_id": uid, "balance": balance, "candies": candies, "paid": paid,
            "currency": cur, "notified": notified, "notify_error": notify_error,
        }

    # изъятие: уведомления нет; ниже нуля баланс не уходит, поэтому сообщаем, сколько реально изъято
    before = await repo.get_candies(stats.bot_id, uid)
    balance = await repo.add_candies(stats.bot_id, uid, -candies)
    if balance is None:
        raise SandboxError(404, "Пользователь не найден в базе (возможно, его только что удалили)")
    log.info("Песочница: изъято %s у пользователя %s бота %s", candies, uid, stats.bot_id)
    return {
        "ok": True, "who": who, "user_id": uid, "balance": balance, "candies": candies, "paid": paid,
        "currency": cur, "removed": max(0, min(candies, before)), "notified": None, "notify_error": None,
    }


async def remove_user(manager: BotManager, target: str, confirm: bool) -> dict:
    _require_confirm(confirm)
    uid, who = resolve(manager, target)
    stats, _ = _running(manager)
    stats.remove(uid)
    await stats.flush()  # удаляем из БД сразу, а не при очередном сбросе раз в 15 секунд
    log.info("Песочница: пользователь %s удалён из базы бота %s", uid, stats.bot_id)
    return {"ok": True, "who": who, "user_id": uid, "users": stats.count}


async def reroll(manager: BotManager, confirm: bool) -> dict:
    """«Уникальные текста»: заново подбирает все рандомизируемые тексты бота (капча, приветствие, ...)."""
    _require_confirm(confirm)
    _running(manager)
    await manager.reroll_texts()
    log.info("Песочница: тексты бота %s перерандомизированы", manager.profile.id if manager.profile else "?")
    return {"ok": True}


async def delete_all_videos(manager: BotManager, confirm: bool) -> dict:
    """«Удалить все видео»: очищает галерею текущего бота (одобренные видео, которые выдаёт «Купить видео»)."""
    _require_confirm(confirm)
    stats, _ = _running(manager)
    deleted = await repo.delete_gallery_videos(stats.bot_id)
    video_pool.pool.reset()
    log.info("Песочница: удалено видео из галереи бота %s: %s", stats.bot_id, deleted)
    return {"ok": True, "deleted": deleted, "videos": 0, "bot_id": stats.bot_id}
