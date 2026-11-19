import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from . import captcha, gallery, greeting, messaging, moderation, repo, shop_buttons, suggest, video_pool
from .metrics import metrics
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
    # user_id -> (id сообщения с капчей, порядок кнопок, время показа, версия текстов, фрукт из задания).
    # Нужно, чтобы убирать старую капчу при повторном /start и не показывать подряд тот же порядок и тот же фрукт.
    # Хранится в памяти; при перезапуске просто начнётся заново (проверка ответа от этого не зависит: фрукт из
    # задания восстанавливается по nonce в кнопках, см. captcha.target_for).
    shown: dict[int, tuple[int, list[str], float, int, str]] = {}
    # user_id -> id приветствия, присланного после /start или после прохождения капчи. При следующем /start
    # (и при новой капче) оно удаляется, чтобы в чате не копились одинаковые сообщения. В памяти: после
    # перезапуска бота прежнее приветствие просто останется в чате.
    last_greeting: dict[int, int] = {}
    # user_id -> id сообщения раздела «Галерея» (в памяти). Нужно, чтобы /start не оставлял его висеть рядом с приветствием.
    last_gallery: dict[int, int] = {}
    # user_id -> id сообщения с открытым меню предложки (в памяти). Пока запись есть, всё, что пользователь
    # присылает, обрабатывается как предложка. Сбрасывается кнопкой «Назад» и /start.
    suggest_menu: dict[int, int] = {}

    async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass  # уже удалено или старше 48 часов — не страшно

    async def send_captcha(
        bot: Bot, user_id: int, *, force: bool, deleted_id: int | None = None, nonce: str | None = None
    ) -> None:
        """nonce — оставить то же задание (после неверного ответа); без него фрукт выбирается заново и
        отличается от прошлого."""
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
        same_texts = bool(old) and old[3] == texts.version
        order = captcha.shuffled_order(cfg, previous=old[1] if same_texts else None)
        if nonce is None:
            nonce = captcha.new_nonce(cfg, profile.token, user_id, previous_target=old[4] if same_texts else None)
        target = captcha.target_for(cfg, profile.token, user_id, nonce)
        msg = await bot.send_message(
            user_id,
            captcha.render_text(cfg, target),
            reply_markup=captcha.build_keyboard(cfg, order, nonce),
            parse_mode="HTML",
        )
        if len(shown) > 5000:
            shown.clear()
        shown[user_id] = (msg.message_id, order, now, texts.version, target)

    async def drop_last_gallery(bot: Bot, user_id: int, keep: int | None = None) -> None:
        old_id = last_gallery.pop(user_id, None)
        if old_id is not None and old_id != keep:
            await safe_delete(bot, user_id, old_id)

    async def drop_suggest(bot: Bot, user_id: int) -> None:
        old_id = suggest_menu.pop(user_id, None)
        if old_id is not None:
            await safe_delete(bot, user_id, old_id)

    async def safe_answer(call: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
        try:
            await call.answer(text, show_alert=alert) if text else await call.answer()
        except TelegramAPIError:
            pass  # запрос устарел — не страшно

    async def forget_user(user_id: int) -> None:
        """Пользователь сам заблокировал бота: убираем его из БД (вместе с балансом) и забываем состояние в памяти.
        Если он потом разблокирует бота и напишет /start, пройдёт капчу заново, как новый."""
        last_greeting.pop(user_id, None)
        last_gallery.pop(user_id, None)
        suggest_menu.pop(user_id, None)
        shown.pop(user_id, None)
        if stats.remove(user_id):
            log.info("Пользователь %s заблокировал бота %s — удалён из базы", user_id, stats.bot_id)
        try:
            await stats.flush()
        except Exception:
            log.exception("Не удалось удалить из базы пользователя %s", user_id)

    async def on_forbidden(e: TelegramForbiddenError, user_id: int) -> None:
        """Любой 403 при отправке. Удаляем из базы только если пользователь точно сам заблокировал бота."""
        if messaging.is_blocked_by_user(e):
            await forget_user(user_id)

    async def reply(bot: Bot, user_id: int, text: str) -> None:
        """Короткое уведомление пользователю (результат проверки видео и т. п.): БЕЗ картинки бота."""
        try:
            await messaging.send_message(bot, user_id, text)
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
        except TelegramAPIError as e:
            log.warning("Сообщение пользователю %s не доставлено: %s", user_id, e)

    async def touch_moderator(user) -> None:
        """Модератор, добавленный по @username, получает ID, как только пишет боту."""
        try:
            await moderation.cache.touch(user.id, user.username)
        except Exception as e:  # БД недоступна — обычный пользователь от этого пострадать не должен
            log.warning("Проверка модератора %s не удалась: %s", user.id, e)

    async def drop_last_greeting(bot: Bot, user_id: int) -> None:
        old_id = last_greeting.pop(user_id, None)
        if old_id is not None:
            await safe_delete(bot, user_id, old_id)

    async def send_greeting(bot: Bot, user_id: int, balance: int) -> None:
        await drop_last_gallery(bot, user_id)  # приветствие и раздел галереи одновременно не висят
        await drop_suggest(bot, user_id)       # и меню предложки тоже (/start закрывает его)
        text = greeting.render(texts.greeting, profile.name, balance)
        try:
            # кнопки «Магазин» / «Галерея» крепятся прямо под приветствием (отдельного меню нет);
            # конфиг читается в момент отправки, а сохранён в БД, поэтому /start их не меняет
            msg = await messaging.send_message(
                bot, user_id, text, profile.message_image_url,
                reply_markup=shop_buttons.build_keyboard(texts.shop_buttons),
            )
            # подмена записи синхронная: при нескольких /start подряд прежнее приветствие удалит каждый следующий
            if len(last_greeting) > 5000:
                last_greeting.clear()
            old_id = last_greeting.get(user_id)
            last_greeting[user_id] = msg.message_id
            if old_id is not None and old_id != msg.message_id:
                await safe_delete(bot, user_id, old_id)
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)  # пользователь заблокировал бота
        except TelegramAPIError as e:
            log.warning("Приветствие пользователю %s не доставлено: %s", user_id, e)

    async def handle_suggestion(message: Message, bot: Bot) -> None:
        """Сообщение пользователя при открытом меню предложки: проверка и создание заявки.
        Без кулдауна: видео можно слать подряд. Тяжёлые файлы не скачиваются — в БД только метаданные и file_id."""
        user = message.from_user
        try:
            if await repo.is_author_blocked(user.id):
                return  # заблокированному автору заявки не создаются и модераторам не уходят; отвечать ему не нужно
            error = suggest.check(message)
            if error:
                await reply(bot, user.id, error)
                return
            video = message.video
            sub = await repo.create_submission(
                stats.bot_id, user.id, stats_username(user), video.file_id,
                getattr(video, "file_unique_id", "") or "", int(video.duration or 0),
            )
            metrics.add("submissions")
            await reply(bot, user.id, suggest.MSG_ACCEPTED)  # без картинки
            await moderation.deliver(bot, sub)
        except Exception:
            log.exception("Не удалось обработать предложку от %s", user.id)

    def stats_username(user) -> str | None:
        return (user.username or "").strip().lstrip("@").lower() or None

    async def send_suggest_menu(bot: Bot, user_id: int) -> None:
        text = texts.suggest.render_menu()
        try:
            msg = await messaging.send_message(
                bot, user_id, text, profile.message_image_url, reply_markup=gallery.build_suggest_keyboard(texts.gallery)
            )
            if len(suggest_menu) > 5000:
                suggest_menu.clear()
            suggest_menu[user_id] = msg.message_id
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
        except TelegramAPIError as e:
            log.warning("Меню предложки пользователю %s не доставлено: %s", user_id, e)

    async def buy_video(call: CallbackQuery, bot: Bot) -> None:
        """Покупка случайного видео из одобренных за gallery.PRICE 🍬. Списание атомарное и ДО отправки; если видео
        отправить не удалось — конфеты возвращаются. Успех — без всплывающих уведомлений."""
        user_id = call.from_user.id
        bot_id = stats.bot_id
        balance = await repo.spend_candies(bot_id, user_id, gallery.PRICE)
        if balance is None:
            await safe_answer(call, "❌ У вас недостаточно средств", alert=True)
            return
        sent, problem = False, None
        try:
            for _ in range(3):  # недействительный file_id пропускаем и берём следующее видео
                pick = await video_pool.pool.pick(bot_id, user_id)
                if pick is None:
                    problem = "Видео пока нет, конфеты не списаны"
                    break
                sub_id, file_id = pick
                try:
                    # без подписи; кнопки: та же покупка и «Назад»; защита от пересылки и сохранения обязательна
                    await bot.send_video(
                        user_id, file_id, reply_markup=gallery.build_video_keyboard(texts.gallery),
                        protect_content=True,
                    )
                except TelegramForbiddenError as e:
                    await on_forbidden(e, user_id)
                    break  # пользователь заблокировал бота
                except TelegramBadRequest as e:
                    log.warning("Видео %s не отправилось (%s), беру другое", sub_id, e)
                    video_pool.pool.mark_bad(sub_id)
                    continue
                except TelegramAPIError as e:
                    log.warning("Видео %s не отправилось: %s", sub_id, e)
                    problem = "Не удалось отправить видео, конфеты не списаны"
                    break
                video_pool.pool.mark_shown(bot_id, user_id, sub_id)
                metrics.add("videos_sent")
                sent = True
                break
            else:
                problem = "Не удалось отправить видео, конфеты не списаны"
        finally:
            if not sent:
                try:
                    await repo.add_candies(bot_id, user_id, gallery.PRICE)  # возврат
                except Exception:
                    log.exception("Не удалось вернуть %s🍬 пользователю %s", gallery.PRICE, user_id)
        await safe_answer(call, None if sent else (problem or "Не удалось отправить видео, конфеты не списаны"),
                          alert=not sent)

    @router.message(CommandStart(), F.chat.type == "private")
    async def on_start(message: Message, bot: Bot) -> None:
        user = message.from_user
        if not user:
            return
        await touch_moderator(user)
        if stats.has(user.id):
            stats.touch(user.id, user.username)
            # баланс живёт только в БД (его меняют и «Песочница», и в будущем платежи) — один запрос по ключу
            await send_greeting(bot, user.id, await repo.get_candies(stats.bot_id, user.id))
        else:
            # человек снова должен пройти капчу (например, его удалили в «Песочнице») — старое приветствие убираем
            await drop_last_greeting(bot, user.id)
            await drop_suggest(bot, user.id)
            await send_captcha(bot, user.id, force=True)

    @router.message(F.chat.type == "private")
    async def on_private_message(message: Message, bot: Bot) -> None:
        user = message.from_user
        if not user:
            return
        await touch_moderator(user)
        if stats.has(user.id):
            stats.touch(user.id, user.username)  # актуализируем юзернейм (в памяти)
            if user.id in suggest_menu:
                await handle_suggestion(message, bot)
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

        nonce = captcha.parse_nonce(call.data)
        # фрукт из задания: по nonce из кнопки (у старых сообщений без nonce — фрукт из конфигурации)
        target = captcha.target_for(texts.captcha, profile.token, user_id, nonce) if nonce else None
        if captcha.is_correct(texts.captcha, captcha.parse_answer(call.data), target):
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
            await send_captcha(bot, user_id, force=True, deleted_id=msg_id, nonce=nonce)

    async def send_gallery(bot: Bot, user_id: int) -> None:
        """Сообщение раздела «Галерея»: заголовок = название кнопки этого бота, маркер бота, баланс как в приветствии."""
        balance = await repo.get_candies(stats.bot_id, user_id)
        text = gallery.render(texts.gallery, texts.deco, texts.shop_buttons.gallery_heading, texts.greeting.template, balance,
                              texts.suggest.name)
        try:
            msg = await messaging.send_message(
                bot, user_id, text, profile.message_image_url, reply_markup=gallery.build_keyboard(texts.gallery, texts.suggest.label)
            )
            if len(last_gallery) > 5000:
                last_gallery.clear()
            last_gallery[user_id] = msg.message_id
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
        except TelegramAPIError as e:
            log.warning("Раздел галереи пользователю %s не доставлен: %s", user_id, e)

    @router.callback_query(F.data.startswith((shop_buttons.SHOP_PREFIX, shop_buttons.GALLERY_PREFIX)))
    async def on_shop_button(call: CallbackQuery, bot: Bot) -> None:
        # Действие определяется по callback_data ЭТОГО бота (texts.shop_buttons читается в момент нажатия).
        # Кнопки устаревшего (до «Перенаписать текста») или чужого приветствия просто гасят «часики».
        action = shop_buttons.parse_action(texts.shop_buttons, call.data)
        await call.answer()
        user_id = call.from_user.id
        if action is None or not stats.has(user_id):
            return
        if action == "gallery":
            # приветствие (оно же главное меню) удаляется, вместо него приходит раздел галереи
            if call.message is not None:
                await safe_delete(bot, call.message.chat.id, call.message.message_id)
            last_greeting.pop(user_id, None)
            await drop_last_gallery(bot, user_id, keep=None)
            await send_gallery(bot, user_id)
        # TODO: action == "shop" — логика магазина (продажа пачками)

    @router.callback_query(
        F.data.startswith((
            gallery.BUY_PREFIX, gallery.PREMIUM_PREFIX, gallery.BACK_PREFIX,
            gallery.SUGGEST_PREFIX, gallery.SUGGEST_BACK_PREFIX,
        ))
    )
    async def on_gallery_button(call: CallbackQuery, bot: Bot) -> None:
        action = gallery.parse_action(texts.gallery, call.data)
        user_id = call.from_user.id
        if action is None or not stats.has(user_id):
            await safe_answer(call)
            return
        if action == "buy":
            await buy_video(call, bot)  # сам отвечает на callback (алерт при нехватке конфет)
            return
        await safe_answer(call)
        if action == "back":
            if getattr(call.message, "video", None) is not None:
                # «Назад» под купленным видео: видео остаётся, меню галереи, из которого его открывали, удаляется,
                # а новое меню приходит ниже
                await drop_last_gallery(bot, user_id)
                await send_gallery(bot, user_id)
            else:
                # сообщение галереи удаляется, приветствие отправляется заново
                if call.message is not None:
                    await safe_delete(bot, call.message.chat.id, call.message.message_id)
                last_gallery.pop(user_id, None)
                await send_greeting(bot, user_id, await repo.get_candies(stats.bot_id, user_id))
        elif action == "suggest":
            # меню галереи заменяется меню предложки
            if call.message is not None:
                await safe_delete(bot, call.message.chat.id, call.message.message_id)
            last_gallery.pop(user_id, None)
            await drop_suggest(bot, user_id)
            await send_suggest_menu(bot, user_id)
        elif action == "suggest_back":
            if call.message is not None:
                await safe_delete(bot, call.message.chat.id, call.message.message_id)
            suggest_menu.pop(user_id, None)
            await drop_last_gallery(bot, user_id)
            await send_gallery(bot, user_id)
        # TODO: "premium" — премиум-подписка

    @router.my_chat_member(F.chat.type == "private", F.new_chat_member.status == "kicked")
    async def on_blocked_by_user(event) -> None:
        """Telegram присылает это, когда пользователь в личке заблокировал бота (статус «kicked») — самый надёжный
        сигнал: сообщение отправлять не нужно. Человек удаляется из базы."""
        await forget_user(event.chat.id)

    @router.callback_query(F.data.startswith(moderation.PREFIX))
    async def on_moderation(call: CallbackQuery, bot: Bot) -> None:
        """Кнопки модератора под заявкой. Доступ только у модераторов из списка (вкладка «Модераторы»)."""
        user = call.from_user
        parsed = moderation.parse(call.data)
        if parsed is None:
            await safe_answer(call)
            return
        if not await moderation.cache.touch(user.id, user.username):
            await safe_answer(call, "У вас нет доступа к модерации", alert=True)
            return
        action, sub_id = parsed
        try:
            done = await moderation.handle_action(bot, stats.bot_id, sub_id, action, user)
        except Exception:
            log.exception("Модерация заявки %s (%s) не удалась", sub_id, action)
            await safe_answer(call, "Ошибка, попробуйте ещё раз", alert=True)
            return
        await safe_answer(call, None if done else "Заявка уже обработана")

    return router
