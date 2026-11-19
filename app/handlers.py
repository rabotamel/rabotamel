import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from . import captcha, greeting, messaging, repo
from .models import BotProfile
from .stats import UserStats
from .texts import BotTexts

log = logging.getLogger("bot.handlers")

# Повторный /start чаще раза в секунду не пересоздаёт капчу (защита от спама лишними запросами к Telegram)
CAPTCHA_MIN_INTERVAL = 1.0
# Обычное сообщение не пересоздаёт капчу, пока прошлая «свежая»: она всё ещё висит в чате. Через это время
# присылаем заново (вдруг человек её удалил у себя).
CAPTCHA_RESEND_AFTER = 1800.0


def build_router(stats: UserStats, texts: BotTexts, profile: BotProfile) -> Router:
    """Каждый запуск polling получает свежий Router (один роутер нельзя подключить к двум Dispatcher).

    Логика: новый пользователь (его нет в БД) сначала проходит капчу; в БД он записывается только
    после правильного ответа. Прошедшему капчу она больше не показывается. Не прошедшие не пишутся в БД
    вообще. Варианты капчи и приветствия берутся из `texts` в момент отправки, поэтому «Песочница»
    может заменить их без перезапуска бота. Картинка сообщений (`profile.message_image_url`) идёт над текстом
    во всех сообщениях, кроме капчи. Профиль при правке в панели подменяется перезапуском бота.
    """
    router = Router()
    # user_id -> (id сообщения с капчей, порядок кнопок, время показа, версия текстов).
    # Нужно, чтобы убирать старую капчу при повторном /start и не показывать подряд тот же порядок.
    # Хранится в памяти; при перезапуске просто начнётся заново.
    shown: dict[int, tuple[int, list[str], float, int]] = {}

    async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass  # уже удалено или старше 48 часов — не страшно

    async def send_captcha(bot: Bot, user_id: int, *, force: bool, deleted_id: int | None = None) -> None:
        now = time.monotonic()
        cfg = texts.captcha
        old = shown.get(user_id)
        if old and not force:
            # капча уже в чате и актуальна — новая не нужна
            if old[3] == texts.version and now - old[2] < CAPTCHA_RESEND_AFTER:
                return
        elif old and deleted_id is None and now - old[2] < CAPTCHA_MIN_INTERVAL:
            return  # слишком частый /start
        shown.pop(user_id, None)
        if old and old[0] != deleted_id:
            await safe_delete(bot, user_id, old[0])
        order = captcha.shuffled_order(cfg, previous=old[1] if old and old[3] == texts.version else None)
        msg = await bot.send_message(
            user_id,
            captcha.render_text(cfg),
            reply_markup=captcha.build_keyboard(cfg, order),
            parse_mode="HTML",
        )
        if len(shown) > 5000:
            shown.clear()
        shown[user_id] = (msg.message_id, order, now, texts.version)

    async def send_greeting(bot: Bot, user_id: int, balance: int) -> None:
        text = greeting.render(texts.greeting, profile.name, balance)
        try:
            await messaging.send_message(bot, user_id, text, profile.message_image_url)
        except TelegramForbiddenError:
            pass  # пользователь заблокировал бота
        except TelegramAPIError as e:
            log.warning("Приветствие пользователю %s не доставлено: %s", user_id, e)

    @router.message(CommandStart(), F.chat.type == "private")
    async def on_start(message: Message, bot: Bot) -> None:
        user = message.from_user
        if not user:
            return
        if stats.has(user.id):
            stats.touch(user.id, user.username)
            # баланс живёт только в БД (его меняют и «Песочница», и в будущем платежи) — один запрос по ключу
            await send_greeting(bot, user.id, await repo.get_candies(stats.bot_id, user.id))
        else:
            await send_captcha(bot, user.id, force=True)

    @router.message(F.chat.type == "private")
    async def on_private_message(message: Message, bot: Bot) -> None:
        user = message.from_user
        if not user:
            return
        if stats.has(user.id):
            # Основная логика для прошедших капчу будет здесь. Пока только актуализируем юзернейм (в памяти).
            stats.touch(user.id, user.username)
        else:
            # Любое другое сообщение от непрошедшего проверку — капча (если она уже в чате, повторно не шлём)
            await send_captcha(bot, user.id, force=False)

    @router.callback_query(F.data.startswith(captcha.CALLBACK_PREFIX))
    async def on_captcha_answer(call: CallbackQuery, bot: Bot) -> None:
        user_id = call.from_user.id
        message = call.message
        chat_id = message.chat.id if message else user_id
        msg_id = message.message_id if message else None

        # Уже прошёл (например, нажал на старое сообщение с капчей) — просто убираем его
        if stats.has(user_id):
            if msg_id is not None:
                await safe_delete(bot, chat_id, msg_id)
            await call.answer()
            return

        if msg_id is not None:
            await safe_delete(bot, chat_id, msg_id)

        if captcha.is_correct(texts.captcha, captcha.parse_answer(call.data)):
            shown.pop(user_id, None)
            # Только здесь пользователь становится «новым»: add() синхронный, поэтому второе одновременное
            # нажатие уже увидит has() == True и ничего не продублирует.
            if stats.add(user_id, call.from_user.username):
                await call.answer()
                await stats.flush()  # одна транзакция на всех, кто прошёл одновременно
                await send_greeting(bot, user_id, 0)
            else:
                await call.answer()
        else:
            await call.answer()
            # те же 4 фрукта и то же задание, но в новом порядке (старое сообщение уже удалено выше)
            await send_captcha(bot, user_id, force=True, deleted_id=msg_id)

    return router
