"""Модерация «Предложки»: кто модератор, рассылка заявок, кнопки «Принять / Отклонить / Заблокировать».

Права модератора действуют только в боте и никак не связаны с доступом к сайту (там один пароль администратора).

* Список модераторов лежит в БД (таблица `moderators`, редактируется во вкладке «Модераторы»). Здесь он кэшируется
  в памяти на короткое время (и сбрасывается при правке с сайта), чтобы не ходить в БД на каждое сообщение.
* Модератор, добавленный по юзернейму, получает user_id при первом обращении к боту (`cache.touch`).
* Заявка уходит каждому модератору, у которого уже известен ID и который запускал бота; сообщения запоминаются в БД.
* Решение принимает ровно один модератор: статус меняется одним UPDATE ... WHERE status = 'pending'
  (repo.decide_submission), после чего у ВСЕХ модераторов кнопки пропадают, а подпись показывает, кто и что решил.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from . import repo, suggest, video_pool

log = logging.getLogger("bot.moderation")

PREFIX = "mod:"
# короткий код в callback_data -> действие
ACTIONS = {"a": "approve", "r": "reject", "b": "block"}
BUTTONS = (("a", "✅ Принять"), ("r", "❌ Отклонить"), ("b", "🚫 Заблокировать"))

CACHE_TTL = 30.0


def _norm(username: str | None) -> str | None:
    u = (username or "").strip().lstrip("@").lower()
    return u or None


# ---------- кэш модераторов ----------

class ModeratorCache:
    def __init__(self) -> None:
        self._rows: list[tuple[int | None, str | None]] = []   # (user_id или None, юзернейм)
        self._at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._at = 0.0

    async def rows(self) -> list[tuple[int | None, str | None]]:
        if time.monotonic() - self._at > CACHE_TTL:
            async with self._lock:
                if time.monotonic() - self._at > CACHE_TTL:
                    mods = await repo.list_moderators()
                    self._rows = [(m.user_id, m.username) for m in mods]
                    self._at = time.monotonic()
        return self._rows

    async def recipients(self) -> list[int]:
        """ID модераторов, которым можно писать (у записей «только юзернейм» ID ещё неизвестен)."""
        return [uid for uid, _ in await self.rows() if uid is not None]

    async def touch(self, user_id: int, username: str | None) -> bool:
        """Вызывается, когда пользователь обращается к боту. Привязывает его к записи модератора по юзернейму
        (или обновляет сменившийся юзернейм) и возвращает True, если он модератор."""
        name = _norm(username)
        rows = await self.rows()
        known = [n for uid, n in rows if uid == user_id]
        pending = bool(name) and any(uid is None and n == name for uid, n in rows)
        renamed = bool(known) and known[0] != name
        if pending or renamed:
            try:
                await repo.bind_moderator(user_id, name)
            except Exception:
                log.exception("Не удалось привязать модератора %s", user_id)
            self.invalidate()
            rows = await self.rows()
        return any(uid == user_id for uid, _ in rows)


cache = ModeratorCache()


# ---------- кнопки и подписи ----------

def callback(action_key: str, sub_id: int) -> str:
    return f"{PREFIX}{action_key}:{sub_id}"


def parse(data: str | None) -> tuple[str, int] | None:
    """('approve' | 'reject' | 'block', id заявки) или None."""
    if not data or not data.startswith(PREFIX):
        return None
    _, _, rest = data.partition(PREFIX)
    key, _, raw_id = rest.partition(":")
    if key not in ACTIONS or not raw_id.isdigit():
        return None
    return ACTIONS[key], int(raw_id)


def keyboard_rows(sub_id: int) -> list[list[tuple[str, str]]]:
    return [[(label, callback(key, sub_id))] for key, label in BUTTONS]


def build_keyboard(sub_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in keyboard_rows(sub_id)]
    )


def _empty_keyboard():
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[])


def fmt_duration(seconds: int) -> str:
    h, rest = divmod(max(int(seconds), 0), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def author_text(sub: repo.Submission) -> str:
    if sub.author_username:
        return f"@{html.escape(sub.author_username)} ({sub.user_id})"
    return str(sub.user_id)


def request_caption(sub: repo.Submission) -> str:
    return f"📩 Заявка #{sub.id}\nАвтор: {author_text(sub)}\nДлительность: {fmt_duration(sub.duration)}"


def moderator_text(username: str | None, name: str | None, user_id: int | None) -> str:
    """@username, а если его нет — имя и ID."""
    if username:
        return "@" + html.escape(username)
    if name:
        return html.escape(name) + (f" (ID {user_id})" if user_id else "")
    return f"ID {user_id}" if user_id else "модератором"


def status_line(sub: repo.Submission) -> str | None:
    who = moderator_text(sub.moderator_username, sub.moderator_name, sub.moderator_id)
    return {
        "approve": f"✅ Заявка принята модератором {who}",
        "reject": f"❌ Заявка отклонена модератором {who}",
        "block": f"🚫 Автор заблокирован модератором {who}",
        "block_auto": f"❌ Заявка отклонена: автор заблокирован модератором {who}",
    }.get(sub.decision or "")


# ---------- рассылка и синхронизация ----------

async def sync_messages(bot, sub_ids: list[int]) -> None:
    """Убирает кнопки и дописывает итог под сообщением заявки у всех модераторов (по данным БД, поэтому
    безопасно вызывать повторно)."""
    subs = [s for s in await repo.get_submissions(sub_ids) if s.status != "pending"]
    if not subs:
        return
    messages = await repo.submission_messages([s.id for s in subs])
    jobs = []
    for sub in subs:
        line = status_line(sub)
        if not line:
            continue
        caption = f"{request_caption(sub)}\n\n{line}"
        for chat_id, message_id in messages.get(sub.id, []):
            jobs.append(_edit(bot, chat_id, message_id, caption))
    if jobs:
        await asyncio.gather(*jobs)


async def _edit(bot, chat_id: int, message_id: int, caption: str) -> None:
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id, caption=caption, parse_mode="HTML",
            reply_markup=_empty_keyboard(),
        )
    except TelegramAPIError as e:
        # «message is not modified», сообщение удалено модератором и т. п. — не страшно
        log.debug("Не удалось обновить заявку у модератора %s: %s", chat_id, e)


async def deliver(bot, sub: repo.Submission) -> int:
    """Отправляет видео всем модераторам с тремя кнопками. Возвращает, скольким дошло.
    protect_content=True: модератор не может пересылать и сохранять видео."""
    recipients = await cache.recipients()
    if not recipients:
        log.info("Заявка %s ждёт: в списке нет модераторов, которым можно написать", sub.id)
        return 0
    caption, markup = request_caption(sub), build_keyboard(sub.id)

    async def one(chat_id: int) -> bool:
        try:
            msg = await bot.send_video(
                chat_id, sub.file_id, caption=caption, parse_mode="HTML",
                reply_markup=markup, protect_content=True,
            )
        except TelegramForbiddenError:
            return False  # модератор ещё не запускал бота или заблокировал его — без спама в логах
        except TelegramAPIError as e:
            log.warning("Заявка %s не доставлена модератору %s: %s", sub.id, chat_id, e)
            return False
        await repo.save_submission_message(sub.id, chat_id, msg.message_id)
        return True

    delivered = sum(await asyncio.gather(*(one(c) for c in recipients)))
    # пока шла рассылка, кто-то из первых получивших мог уже принять решение — дотянем остальные сообщения
    now = await repo.get_submissions([sub.id])
    if now and now[0].status != "pending":
        await sync_messages(bot, [sub.id])
    return delivered


async def handle_action(bot, bot_id: int, sub_id: int, action: str, user) -> bool:
    """Решение модератора. False — заявка уже обработана (у этого модератора кнопки всё равно уберутся)."""
    decision = await repo.decide_submission(
        bot_id, sub_id, action, user.id, _norm(user.username),
        (getattr(user, "full_name", None) or "")[:128] or None, reward=suggest.REWARD,
    )
    if decision is None:
        await sync_messages(bot, [sub_id])
        return False
    if action == "approve":
        video_pool.pool.add(bot_id, decision.sub.id, decision.sub.file_id)
    await sync_messages(bot, [sub_id, *decision.auto_rejected])
    return True
