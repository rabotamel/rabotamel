"""Проверка скриншотов заданий в БОТЕ ДЛЯ ЧЕКОВ («Бесплатные конфеты» -> задание со скриншотом).

Пользователь присылает скриншоты основному боту (app/handlers.py), а проверка идёт там же, куда приходят чеки об оплате
картой (CHECK_BOT_TOKEN, личные сообщения админов из вкладки «Модераторы»):
  1. `deliver` скачивает скрины ботом-магазином (его `file_id` чужому боту не подходит) и ботом для чеков присылает
     каждому админу альбом(ы) со всеми скринами, а следом — сообщение-пометку «выполнение задания» (пользователь,
     задание, награда, число скринов) с кнопками «✅ Подтвердить / ❌ Отклонить / 🚫 Заблокировать» (`tkchk:ok|no|bl:<id>`).
     У альбома кнопок быть не может, поэтому кнопки стоят под сообщением-пометкой.
  2. Решает первый нажавший админ: статус меняется одним UPDATE ... WHERE status = 'pending' вместе с начислением награды
     (repo.decide_task), у остальных админов сообщение-пометка обновляется, повторное нажатие — «уже обработана».
  3. Пользователю о результате пишет бот-магазин, чья это заявка (`HOOKS[bot_id]`, регистрируется в handlers.build_router).
     «Заблокировать» — заявка отклоняется БЕЗ уведомления, а пользователь попадает в `task_blocked_users` (отдельный список
     «Задания», не связанный с блокировкой чеков об оплате и «Предложки»): скриншоты заданий он больше отправлять не сможет.
Права админа — те же, что у чеков (`topup._is_admin`: модератор из вкладки «Модераторы»).
"""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import moderation, repo

log = logging.getLogger("bot.task_review")

PREFIX = "tkchk:"      # не пересекается с tpchk: (чеки об оплате)
ACTIONS = {"ok": "approve", "no": "reject", "bl": "block"}
BUTTONS = (("ok", "✅ Подтвердить"), ("no", "❌ Отклонить"), ("bl", "🚫 Заблокировать"))
PENDING_LIMIT = 20
ALBUM_SIZE = 10        # больше 10 файлов в одном альбоме Telegram не принимает

BLOCKED_ALERT = "Пользователь заблокирован: скриншоты заданий он больше отправлять не сможет."

# bot_id -> (бот-магазин, как написать пользователю). Нужен, чтобы скачать скрины и уведомить о решении.
Notify = Callable[[int, str, str, int], Awaitable[None]]   # (user_id, 'approved'|'rejected', название задания, награда)


@dataclass
class Hook:
    bot: Any
    notify: Notify


HOOKS: dict[int, Hook] = {}


def register(bot_id: int, bot: Any, notify: Notify) -> Hook:
    hook = Hook(bot, notify)
    if bot is not None:
        HOOKS[bot_id] = hook
    return hook


def detach(bot_id: int, hook: Hook | None = None) -> None:
    if hook is None or HOOKS.get(bot_id) is hook:
        HOOKS.pop(bot_id, None)


# ---------- callback_data и кнопки ----------

def callback(key: str, completion_id: int) -> str:
    return f"{PREFIX}{key}:{completion_id}"


def parse(data: str | None) -> tuple[str, int] | None:
    """('approve' | 'reject' | 'block', id заявки) или None."""
    if not data or not data.startswith(PREFIX):
        return None
    key, _, raw_id = data[len(PREFIX):].partition(":")
    if key not in ACTIONS or not raw_id.isdigit():
        return None
    return ACTIONS[key], int(raw_id)


def keyboard_rows(completion_id: int) -> list[list[tuple[str, str]]]:
    """Подтвердить и Отклонить — в одном ряду, «Заблокировать» — отдельным (как под чеком об оплате)."""
    first = [(label, callback(key, completion_id)) for key, label in BUTTONS[:2]]
    second = [(label, callback(key, completion_id)) for key, label in BUTTONS[2:]]
    return [first, second]


def build_keyboard(completion_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in keyboard_rows(completion_id)]
    )


# ---------- тексты ----------

def author_text(r: repo.TaskReview) -> str:
    return f"@{html.escape(r.username)} (<code>{r.user_id}</code>)" if r.username else f"<code>{r.user_id}</code>"


def request_text(r: repo.TaskReview, shots: int) -> str:
    """Сообщение-пометка под скринами: что это выполнение задания, кто, какое задание, какая награда.
    Если у задания есть комментарий (сайт, вкладка «Кнопки в боте»), он стоит вместо названия: это объяснение админу,
    что именно пользователь должен был сделать."""
    what = html.escape(r.comment.strip() or r.task_name, quote=False)
    return (
        f"📩 <b>Выполнение задания #{r.id}</b>\n\n"
        f"<b>Задание:</b> {what}\n"
        f"<b>Пользователь:</b> {author_text(r)}\n"
        f"<b>Награда:</b> {r.reward}🍬\n"
        f"<b>Скриншотов:</b> {shots}"
    )


def status_line(r: repo.TaskReview) -> str | None:
    who = moderation.moderator_text(r.moderator_username, r.moderator_name, r.moderator_id)
    if r.status == "approved":
        return f"✅ Подтверждено ({who}): начислено {r.reward}🍬"
    if r.status == "rejected":
        return f"❌ Отклонено ({who})"
    return None


# ---------- отправка админам ----------

def _input_file(data: bytes, name: str):
    from .topup import _input_file as make

    return make(data, name)


async def _send_files(bot, chat_id: int, items: list[tuple[bytes, str, str]]) -> None:
    """Скрины одним или несколькими альбомами (до 10 в каждом). Фото и файлы-картинки в один альбом смешивать нельзя,
    поэтому они идут отдельными альбомами; одиночный файл — обычным сообщением (в альбоме их должно быть 2+)."""
    from aiogram.types import InputMediaDocument, InputMediaPhoto

    for kind, media_cls, single in (
        ("photo", InputMediaPhoto, bot.send_photo), ("document", InputMediaDocument, bot.send_document),
    ):
        group = [(data, name) for data, name, k in items if k == kind]
        for i in range(0, len(group), ALBUM_SIZE):
            part = group[i:i + ALBUM_SIZE]
            if len(part) == 1:
                await single(chat_id, protect_content=True, **{kind: _input_file(*part[0])})
            else:
                await bot.send_media_group(
                    chat_id, media=[media_cls(media=_input_file(d, n)) for d, n in part], protect_content=True
                )


async def deliver(completion_id: int, targets: list[int] | None = None) -> int:
    """Присылает ботом для чеков скрины заявки и сообщение с кнопками каждому админу (по умолчанию — всем из вкладки
    «Модераторы», у которых известен ID). Возвращает, скольким доставлено; 0 — заявка остаётся на проверке и придёт по /pending."""
    from . import topup

    if not topup.CHECK_BOT_TOKEN:
        log.warning("Задание #%s ждёт проверки: не задан CHECK_BOT_TOKEN (бот для чеков)", completion_id)
        return 0
    reviews = await repo.get_task_reviews([completion_id])
    if not reviews or reviews[0].status != "pending":
        return 0
    r = reviews[0]
    targets = targets if targets is not None else await topup.receipt_targets()
    if not targets:
        log.info("Задание #%s ждёт: во вкладке «Модераторы» нет админов, которым можно написать", r.id)
        return 0
    hook = HOOKS.get(r.bot_id)
    files = await repo.get_task_files(r.id)
    if hook is None or hook.bot is None or not files:
        log.warning("Задание #%s не отправлено: бот-магазин не запущен или у заявки нет файлов", r.id)
        return 0
    try:
        check_bot = topup._ensure_check_bot()
        items = []
        for file_id, kind in files:
            data, name, _ = await topup._fetch_receipt(hook.bot, file_id)
            items.append((data, name, "photo" if kind == "photo" else "document"))
    except Exception:
        log.exception("Задание #%s: не удалось скачать скриншоты", r.id)
        return 0
    text, markup = request_text(r, len(items)), build_keyboard(r.id)

    async def one(chat_id: int) -> bool:
        try:
            await _send_files(check_bot, chat_id, items)
            msg = await check_bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=markup, protect_content=True
            )
        except Exception as e:   # админ не нажимал /start у бота для чеков или заблокировал его
            log.warning("Задание #%s не доставлено в чат %s (админ должен нажать /start у бота для чеков): %s", r.id, chat_id, e)
            return False
        await repo.save_task_review_message(r.id, chat_id, msg.message_id)
        return True

    delivered = sum(await asyncio.gather(*(one(c) for c in targets)))
    now = await repo.get_task_reviews([r.id])
    if now and now[0].status != "pending":   # пока шла рассылка, кто-то уже решил — дотянем остальные сообщения
        await sync_messages(check_bot, r.id)
    return delivered


async def send_pending(bot, chat_id: int) -> int:
    """/pending: заново присылает в чат заявки на проверке (до PENDING_LIMIT). Возвращает, сколько прислано."""
    sent = 0
    for r in await repo.list_pending_task_reviews(PENDING_LIMIT):
        try:
            sent += 1 if await deliver(r.id, [chat_id]) else 0
        except Exception:
            log.exception("/pending: не удалось прислать задание #%s", r.id)
    return sent


async def count_pending() -> int:
    try:
        return len(await repo.list_pending_task_reviews(PENDING_LIMIT))
    except Exception:
        return 0


# ---------- решение админа ----------

async def sync_messages(bot, completion_id: int, note: str | None = None) -> None:
    """Убирает кнопки и дописывает итог под сообщением-пометкой у ВСЕХ админов (по данным БД: безопасно вызывать
    повторно). `note` заменяет строку итога (нужно для «заблокирован», этого в БД нет)."""
    reviews = [r for r in await repo.get_task_reviews([completion_id]) if r.status != "pending"]
    if not reviews:
        return
    r = reviews[0]
    shots = len(await repo.get_task_files(r.id))
    text = f"{request_text(r, shots)}\n\n{note or status_line(r)}"

    async def edit(chat_id: int, message_id: int) -> None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=None)
        except Exception as e:
            log.debug("Не удалось обновить заявку у админа %s: %s", chat_id, e)

    jobs = [edit(c, m) for c, m in await repo.task_review_messages(r.id)]
    if jobs:
        await asyncio.gather(*jobs)


async def _notify_user(r: repo.TaskReview, key: str) -> None:
    hook = HOOKS.get(r.bot_id)
    if hook is None:
        log.warning("Задание #%s: бот-магазин не запущен, пользователь %s не уведомлён", r.id, r.user_id)
        return
    try:
        await hook.notify(r.user_id, key, r.task_name, r.reward)
    except Exception:
        log.exception("Задание #%s: не удалось уведомить пользователя %s", r.id, r.user_id)


async def on_callback(call, bot) -> None:
    """Нажатие «Подтвердить / Отклонить / Заблокировать» под заявкой; `bot` — бот для чеков."""
    from . import topup

    parsed = parse(getattr(call, "data", None))
    msg = call.message
    if parsed is None or msg is None:
        await topup._answer(call)
        return
    action, completion_id = parsed
    user = call.from_user
    if not await topup._is_admin(bot, msg.chat.id, user.id):
        await topup._answer(call, "Проверять задания могут только администраторы.", True)
        return
    reviews = await repo.get_task_reviews([completion_id])
    if not reviews:
        await topup._answer(call, "Заявка не найдена.", True)
        await topup._strip_buttons(bot, msg.chat.id, msg.message_id)
        return
    r = reviews[0]
    uname, fname = moderation._norm(user.username), (getattr(user, "full_name", None) or "")[:128] or None
    who = moderation.moderator_text(uname, fname, user.id)

    if action == "block":
        # без уведомления пользователю + запрет на отправку скриншотов заданий (чеки об оплате это не затрагивает)
        decided = await repo.decide_task(completion_id, None, False, user.id, uname, fname) if r.status == "pending" else None
        await repo.set_task_block(r.user_id, True, user.id)
        if decided is not None:
            await sync_messages(bot, completion_id, f"🚫 Отклонено, пользователь заблокирован ({who})")
        else:
            await topup._strip_buttons(bot, msg.chat.id, msg.message_id)
            await sync_messages(bot, completion_id)
        await topup._answer(call, BLOCKED_ALERT, True)
        return

    review = await repo.decide_task(completion_id, None, action == "approve", user.id, uname, fname)
    if review is None:
        await topup._answer(call, "Заявка уже обработана.", True)
        await topup._strip_buttons(bot, msg.chat.id, msg.message_id)
        await sync_messages(bot, completion_id)
        return
    if action == "approve":
        if review.balance is None:   # пользователя удалили из базы (очистка неактивных): начислять некому
            await sync_messages(bot, completion_id, f"⚠️ Подтверждено ({who}), но конфеты не начислены: пользователя нет в базе")
        else:
            await sync_messages(bot, completion_id)
            await _notify_user(review, "approved")
    else:
        await sync_messages(bot, completion_id)
        await _notify_user(review, "rejected")
    await topup._answer(call)
