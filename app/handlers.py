import asyncio
import html
import logging
import time
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from . import (
    captcha, gallery, greeting, messaging, moderation, promo, repo, shop, shop_buttons, suggest, task_review, tasks, topup,
    video_pool,
)
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

# Подпись постоянной кнопки у клавиатуры (под полем ввода) — по заданию владельца фиксированная
MENU_BUTTON = "Меню"
# Текст служебного сообщения, к которому крепится клавиатура с кнопкой «Меню» (клавиатуру нельзя повесить на
# приветствие: у него уже есть inline-кнопки)
MENU_KEYBOARD_CARRIER = "🍭"


def build_router(
    stats: UserStats, texts: BotTexts, profile: BotProfile, bot: Bot | None = None, automsg=None
) -> Router:
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
    main_msg: dict[int, int] = {}   # id сообщения с главным меню (приветствие + кнопки): сверяется с last_greeting
    # user_id -> id сообщения раздела «Галерея» (в памяти). Нужно, чтобы /start не оставлял его висеть рядом с приветствием.
    last_gallery: dict[int, int] = {}
    # user_id -> id сообщения с открытым меню предложки (в памяти). Пока запись есть, всё, что пользователь
    # присылает, обрабатывается как предложка. Сбрасывается кнопкой «Назад» и /start.
    suggest_menu: dict[int, int] = {}
    # user_id -> id служебного сообщения с клавиатурой «Меню» (в памяти). Клавиатура в Telegram постоянная, поэтому
    # шлём её один раз за запуск: после капчи и (для тех, кто прошёл капчу раньше) при первом /start.
    menu_kb: dict[int, int] = {}
    # user_id -> id «верхних» частей длинной карточки товара (в памяти). Карточка длиннее лимита Telegram уходит
    # несколькими сообщениями; кнопки — под последним, а эти части убираются, как только пользователь уходит с карточки.
    product_parts: dict[int, list[int]] = {}
    # user_id -> id задания, скриншот к которому бот сейчас ждёт (в памяти; сбрасывается «Назад», /start и «Меню»).
    task_wait: dict[int, int] = {}
    # user_id -> принятые скриншоты [(file_id, 'photo'|'document')] до нажатия «Отправить на проверку» (в памяти: при
    # перезапуске бота набор пропадает, пользователь просто пришлёт скрины заново) и отложенное обновление экрана-счётчика
    # (чтобы альбом из нескольких фото давал один экран, а не по экрану на каждое фото).
    task_shots: dict[int, list[tuple[str, str]]] = {}
    task_refresh: dict[int, asyncio.Task] = {}
    # user_id новичка -> id пригласившего из «/start ref_<id>». Награда уходит пригласившему, когда новичок проходит
    # капчу (в памяти: если бот перезапустился до капчи, ссылка просто не засчитается).
    ref_pending: dict[int, int] = {}

    def clear_task_wait(user_id: int) -> None:
        """Конец приёма скриншотов (назад, /start, «Меню», отправка): ожидание, принятые скрины и отложенный экран."""
        task_wait.pop(user_id, None)
        task_shots.pop(user_id, None)
        pending = task_refresh.pop(user_id, None)
        if pending is not None and not pending.done():
            pending.cancel()

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

    async def ensure_menu_keyboard(bot: Bot, user_id: int, *, force: bool = False) -> None:
        """Постоянная кнопка «Меню» у клавиатуры. Прежнее служебное сообщение заменяется новым (не копятся)."""
        if user_id in menu_kb and not force:
            return
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

        try:
            msg = await bot.send_message(
                user_id, MENU_KEYBOARD_CARRIER,
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text=MENU_BUTTON)]], resize_keyboard=True, is_persistent=True
                ),
            )
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
            return
        except TelegramAPIError as e:
            log.warning("Клавиатура «Меню» пользователю %s не доставлена: %s", user_id, e)
            return
        if len(menu_kb) > 5000:
            menu_kb.clear()
        old_id = menu_kb.get(user_id)
        menu_kb[user_id] = msg.message_id
        if old_id is not None and old_id != msg.message_id:
            await safe_delete(bot, user_id, old_id)

    async def main_keyboard(user_id: int | None = None):
        """Кнопки под приветствием: «Магазин» / «Галерея» (рандомизированные), ниже «Купить конфеты», ещё ниже —
        «Бесплатные конфеты». Пока у пользователя действует скидка по промокоду, к кнопке магазина дописано
        «(🔥 -N% на Hч)»."""
        suffix = ""
        if user_id is not None:
            percent, left = await get_discount(user_id)
            suffix = shop.promo_button_suffix(percent, left)
        return shop_buttons.build_keyboard(
            texts.shop_buttons, extra_rows=[*topup_module.menu_rows(), tasks.menu_row(texts.tasks_menu)],
            shop_suffix=suffix,
        )

    async def drop_product_parts(bot: Bot, user_id: int) -> None:
        for old_id in product_parts.pop(user_id, []):
            await safe_delete(bot, user_id, old_id)

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
        menu_kb.pop(user_id, None)
        product_parts.pop(user_id, None)
        clear_task_wait(user_id)
        ref_pending.pop(user_id, None)
        topup_module.reset(user_id)
        shown.pop(user_id, None)
        if automsg is not None:
            automsg.forget(user_id)
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

    if automsg is not None:
        automsg.on_blocked = forget_user   # автосообщение не дошло, потому что человек заблокировал бота

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
        await drop_product_parts(bot, user_id)  # и верхние части длинной карточки товара
        topup_module.reset(user_id)            # и раздел «Купить конфеты» закрывается
        clear_task_wait(user_id)               # и приём скриншотов задания отменяется
        text = greeting.render(texts.greeting, profile.name, balance)
        try:
            # кнопки «Магазин» / «Галерея» крепятся прямо под приветствием (отдельного меню нет);
            # конфиг читается в момент отправки, а сохранён в БД, поэтому /start их не меняет
            msg = await messaging.send_message(
                bot, user_id, text, profile.message_image_url, reply_markup=await main_keyboard(user_id),
            )
            # подмена записи синхронная: при нескольких /start подряд прежнее приветствие удалит каждый следующий
            if len(last_greeting) > 5000:
                last_greeting.clear()
            old_id = last_greeting.get(user_id)
            last_greeting[user_id] = msg.message_id
            main_msg[user_id] = msg.message_id
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
        premium = await has_premium(user_id)
        note = "" if premium else ", конфеты не списаны"
        price = 0 if premium else gallery.PRICE   # премиум: видео бесплатно и без защиты от пересылки
        if premium:
            balance = await repo.get_candies(bot_id, user_id)
        else:
            balance = await repo.spend_candies(bot_id, user_id, price)
        if balance is None:
            await safe_answer(call, "❌ У вас недостаточно средств", alert=True)
            return
        sent, problem = False, None
        try:
            for _ in range(3):  # недействительный file_id пропускаем и берём следующее видео
                pick = await video_pool.pool.pick(bot_id, user_id)
                if pick is None:
                    problem = "Видео пока нет" + note
                    break
                sub_id, file_id = pick
                try:
                    # без подписи; кнопки: та же покупка и «Назад»; защита от пересылки и сохранения обязательна
                    # подпись — строка баланса (как в приветствии и галерее), уже с учётом списания
                    await bot.send_video(
                        user_id, file_id, caption=gallery.balance_line(texts.greeting.template, greeting.format_number(balance)),
                        parse_mode="HTML", reply_markup=gallery.build_video_keyboard(texts.gallery, premium),
                        protect_content=not premium,
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
                    problem = "Не удалось отправить видео" + note
                    break
                video_pool.pool.mark_shown(bot_id, user_id, sub_id)
                metrics.add("videos_sent")
                sent = True
                break
            else:
                problem = "Не удалось отправить видео" + note
        finally:
            if not sent and price > 0:
                try:
                    await repo.add_candies(bot_id, user_id, price)  # возврат
                except Exception:
                    log.exception("Не удалось вернуть %s🍬 пользователю %s", price, user_id)
        await safe_answer(call, None if sent else (problem or "Не удалось отправить видео" + note),
                          alert=not sent)

    @router.message(CommandStart(), F.chat.type == "private")
    async def on_start(message: Message, bot: Bot) -> None:
        user = message.from_user
        if not user:
            return
        await touch_moderator(user)
        referrer = tasks.parse_start_referrer(getattr(message, "text", None))
        if referrer and referrer != user.id and not stats.has(user.id):
            if len(ref_pending) > 5000:
                ref_pending.clear()
            ref_pending[user.id] = referrer   # засчитаем, когда пройдёт капчу
        if stats.has(user.id):
            stats.touch(user.id, user.username)
            stats.touch_activity(user.id)   # last_active_at: в БД не чаще раза в 6 часов (троттлинг в UserStats)
            await ensure_menu_keyboard(bot, user.id)   # прошедшие капчу до обновления получают кнопку «Меню» при первом /start
            # баланс живёт только в БД (его меняют и «Песочница», и в будущем платежи) — один запрос по ключу
            await send_greeting(bot, user.id, await repo.get_candies(stats.bot_id, user.id))
        else:
            # человек снова должен пройти капчу (например, его удалили в «Песочнице») — старое приветствие убираем
            await drop_last_greeting(bot, user.id)
            await drop_suggest(bot, user.id)
            await send_captcha(bot, user.id, force=True)

    async def handle_promo(message: Message, bot: Bot, code: str) -> None:
        """Сообщение из 8 букв/цифр от прошедшего капчу пользователя: если это ЕГО промокод — включаем скидку и отвечаем
        текстом с сайта («Автосообщения»). Чужие и несуществующие коды игнорируются молча, как любой другой текст."""
        user_id = message.from_user.id
        try:
            status, percent = await repo.activate_promo(stats.bot_id, user_id, code)
            if status == "none":
                return
            if status == "used":
                await reply(bot, user_id, promo.USED_TEXT)
                return
            if status == "expired":
                await reply(bot, user_id, promo.EXPIRED_TEXT)
                return
            cfg = await promo.load_settings()
            raw = cfg.reply or promo.DEFAULT_REPLY
            await reply(bot, user_id, promo.render(raw, seed_of_bot(), code, percent))
            if status == "ok":
                await refresh_main_keyboard(bot, user_id)
        except Exception:
            log.exception("Не удалось обработать промокод пользователя %s", user_id)

    @router.message(F.chat.type == "private")
    async def on_private_message(message: Message, bot: Bot) -> None:
        user = message.from_user
        if not user:
            return
        await touch_moderator(user)
        if stats.has(user.id):
            stats.touch(user.id, user.username)  # актуализируем юзернейм (в памяти)
            stats.touch_activity(user.id)        # last_active_at (с троттлингом, см. UserStats)
            if getattr(message, "text", None) == MENU_BUTTON:
                # кнопка «Меню»: как /start — приветствие приходит заново, прежнее меню (галерея, предложка, магазин,
                # «Купить конфеты») убирается; само сообщение «Меню» стираем, чтобы не копились в чате
                await safe_delete(bot, user.id, message.message_id)
                await send_greeting(bot, user.id, await repo.get_candies(stats.bot_id, user.id))
            elif topup_module.active(user.id):
                await topup_module.on_message(message, bot)
            elif user.id in task_wait:
                await handle_task_screenshot(message, bot)
            elif user.id in suggest_menu:
                await handle_suggestion(message, bot)
            elif promo.parse_code(getattr(message, "text", None)):
                await handle_promo(message, bot, promo.parse_code(message.text))
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
                await ensure_menu_keyboard(bot, user_id, force=True)  # кнопка «Меню» у клавиатуры — после капчи всегда
                await send_greeting(bot, user_id, 0)
                await grant_referral(bot, user_id)   # пришёл по реферальной ссылке — награда пригласившему
                if automsg is not None:
                    await automsg.on_captcha_passed(user_id)   # автосообщение уйдёт через 30 минут (app/automsg.py)
            else:
                await call.answer()
        else:
            await call.answer()
            # те же 4 фрукта и то же задание, но в новом порядке (старое сообщение уже удалено выше)
            await send_captcha(bot, user_id, force=True, deleted_id=msg_id, nonce=nonce)

    async def get_premium_state(user_id: int) -> tuple[object, bool]:
        """(premium_until, premium_forever); при сбое БД — премиума нет (покупка видео работает как обычно)."""
        try:
            return await repo.get_premium(user_id)
        except Exception:
            log.exception("Не удалось прочитать премиум пользователя %s", user_id)
            return None, False

    async def has_premium(user_id: int) -> bool:
        until, forever = await get_premium_state(user_id)
        return gallery.premium_active(until, forever)

    async def send_premium_menu(bot: Bot, user_id: int, balance: int | None = None) -> None:
        """Экран «Премиум подписка»: статус, баланс и три тарифа. Как раздел галереи он запоминается в last_gallery
        (/start и «Назад» убирают его так же, как меню галереи)."""
        if balance is None:
            balance = await repo.get_candies(stats.bot_id, user_id)
        until, forever = await get_premium_state(user_id)
        text = gallery.render_premium(texts.gallery, texts.greeting.template, greeting.format_number(balance), until, forever)
        try:
            msg = await messaging.send_message(
                bot, user_id, text, profile.message_image_url, reply_markup=gallery.build_premium_keyboard(texts.gallery)
            )
            if len(last_gallery) > 5000:
                last_gallery.clear()
            last_gallery[user_id] = msg.message_id
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
        except TelegramAPIError as e:
            log.warning("Меню премиума пользователю %s не доставлено: %s", user_id, e)

    async def buy_premium(call: CallbackQuery, bot: Bot, key: str) -> None:
        """Покупка тарифа премиума: списание и выдача — одна транзакция (repo.buy_premium). Меню приходит заново."""
        user_id = call.from_user.id
        opt = gallery.PREMIUM_OPTIONS[key]
        until, forever = await get_premium_state(user_id)
        if forever:
            await safe_answer(call, "💎 У вас уже безлимитный премиум", alert=True)
            return
        result = await repo.buy_premium(stats.bot_id, user_id, int(opt["price"]), opt["days"])
        if result is None:
            await safe_answer(call, "❌ У вас недостаточно средств", alert=True)
            return
        balance = result[0]
        if call.message is not None:
            await safe_delete(bot, call.message.chat.id, call.message.message_id)
        last_gallery.pop(user_id, None)
        await send_premium_menu(bot, user_id, balance)
        await safe_answer(call, f"❤️‍🔥 Вы успешно приобрели премиум {gallery.PREMIUM_BOUGHT[key]}.", alert=True)

    async def send_gallery(bot: Bot, user_id: int) -> None:
        """Сообщение раздела «Галерея»: заголовок = название кнопки этого бота, маркер бота, баланс как в приветствии."""
        balance = await repo.get_candies(stats.bot_id, user_id)
        premium = await has_premium(user_id)
        text = gallery.render(texts.gallery, texts.deco, texts.shop_buttons.gallery_heading, texts.greeting.template, balance,
                              texts.suggest.name)
        try:
            msg = await messaging.send_message(
                bot, user_id, text, profile.message_image_url, reply_markup=gallery.build_keyboard(texts.gallery, texts.suggest.label, premium)
            )
            if len(last_gallery) > 5000:
                last_gallery.clear()
            last_gallery[user_id] = msg.message_id
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
        except TelegramAPIError as e:
            log.warning("Раздел галереи пользователю %s не доставлен: %s", user_id, e)

    async def show_screen(call: CallbackQuery, bot: Bot, user_id: int, text: str, markup) -> int | None:
        """Переходы по разделу «Магазин» (приветствие -> магазин -> карточка товара -> «Купить конфеты» и обратно), как
        и в остальном боте: сообщение не редактируется, а присылается новое, прежнее удаляется. Новое отправляется
        первым, поэтому при сбое отправки старый экран остаётся на месте. Возвращает id нового сообщения."""
        await drop_product_parts(bot, user_id)   # уходим с длинной карточки товара — её верхние части убираем
        msg = getattr(call, "message", None)   # call=None: экран после обычного сообщения (скриншот задания)
        try:
            sent = await messaging.send_message(bot, user_id, text, profile.message_image_url, reply_markup=markup)
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
            return None
        except TelegramAPIError as e:
            log.warning("Раздел магазина пользователю %s не доставлен: %s", user_id, e)
            return None
        stale = {last_greeting.get(user_id), msg.message_id if msg is not None else None}
        last_greeting[user_id] = sent.message_id   # это «главное меню»: /start его заменит
        for old_id in stale - {None, sent.message_id}:
            await safe_delete(bot, user_id, old_id)
        return sent.message_id

    async def show_product_card(call: CallbackQuery, bot: Bot, user_id: int, parts: list[str], markup) -> None:
        """Карточка товара. Короткая — как раньше, одним сообщением. Длиннее лимита Telegram (описание до 5000 символов) —
        несколькими: первая часть заменяет сообщение магазина, остальные идут следом, кнопки — под последней."""
        if len(parts) == 1:
            await show_screen(call, bot, user_id, parts[0], markup)
            return
        first_id = await show_screen(call, bot, user_id, parts[0], None)
        if first_id is None:
            return
        upper = [first_id]
        for i, part in enumerate(parts[1:], start=1):
            last = i == len(parts) - 1
            try:
                sent = await messaging.send_message(bot, user_id, part, None, reply_markup=markup if last else None)
            except TelegramForbiddenError as e:
                await on_forbidden(e, user_id)
                return
            except TelegramAPIError as e:
                log.warning("Часть карточки товара пользователю %s не доставлена: %s", user_id, e)
                return
            if last:
                last_greeting[user_id] = sent.message_id   # сообщение с кнопками — «главное меню»: /start его заменит
            else:
                upper.append(sent.message_id)
        product_parts[user_id] = upper

    async def get_discount(user_id: int) -> tuple[int, float | None]:
        """Скидка пользователя по промокоду: (%, секунд до конца). (0, None) — нет (или БД не ответила — тогда
        показываем обычные цены)."""
        try:
            percent, expires = await repo.get_promo_discount(stats.bot_id, user_id)
        except Exception as e:
            log.warning("Не удалось прочитать промокод пользователя %s: %s", user_id, e)
            return 0, None
        if not percent or expires is None:
            return 0, None
        left = (expires - datetime.now(timezone.utc)).total_seconds()
        return (percent, left) if left > 0 else (0, None)

    async def refresh_main_keyboard(bot: Bot, user_id: int) -> None:
        """Скидка только что включилась: кнопки главного меню обновляются на месте, чтобы на кнопке магазина сразу
        появилась приписка. Только если главное меню сейчас и есть последнее сообщение (иначе ничего не трогаем)."""
        message_id = main_msg.get(user_id)
        if message_id is None or last_greeting.get(user_id) != message_id:
            return
        try:
            await bot.edit_message_reply_markup(chat_id=user_id, message_id=message_id,
                                                reply_markup=await main_keyboard(user_id))
        except Exception as e:   # сообщение удалено/не изменилось — не страшно, кнопка обновится при следующем показе меню
            log.info("Кнопки главного меню пользователя %s не обновились: %s", user_id, e)

    async def show_shop_menu(call: CallbackQuery, bot: Bot, user_id: int) -> None:
        """Меню магазина: заголовок жирным, баланс из БД, кнопки видимых товаров из БД и «Назад» (товаров нет — только «Назад»)."""
        balance = await repo.get_candies(stats.bot_id, user_id)
        products = [shop.product_from_row(r) for r in await repo.list_shop_products(limit=shop.MAX_PRODUCTS)]
        percent, left = await get_discount(user_id)
        text = shop.render_menu(texts.shop_buttons.shop_heading, balance, percent, left)
        await show_screen(call, bot, user_id, text, shop.build_menu_keyboard(texts.shop_menu, products, percent))

    async def show_main_menu(call: CallbackQuery, bot: Bot, user_id: int) -> None:
        """Главное меню = приветствие с кнопками «Магазин» / «Галерея» (то же сообщение, обновлённое на месте)."""
        balance = await repo.get_candies(stats.bot_id, user_id)
        text = greeting.render(texts.greeting, profile.name, balance)
        sent_id = await show_screen(call, bot, user_id, text, await main_keyboard(user_id))
        if sent_id is not None:
            main_msg[user_id] = sent_id

    async def topup_go_main(call: CallbackQuery, bot: Bot, user_id: int) -> None:
        await show_main_menu(call, bot, user_id)

    def track_screen(user_id: int, message_id: int) -> None:
        last_greeting[user_id] = message_id   # экран «Купить конфеты» — тоже «главное меню»: /start его заменит

    topup_module = topup.TopupModule(
        stats.bot_id, bot, profile, topup.Keys.for_seed(str(getattr(profile, "token", None) or stats.bot_id)),
        show_screen=show_screen, safe_delete=safe_delete, track=track_screen, go_main=topup_go_main, is_user=stats.has,
    )
    if bot is not None:
        topup.ensure_started()   # фоновая проверка оплат (один раз на приложение)

    @router.callback_query(F.data.startswith(topup_module.keys.p))
    async def on_topup(call: CallbackQuery, bot: Bot) -> None:
        """«🍭 Купить конфеты 🍬»: экраны оплаты и кнопки админов под чеками (app/topup.py)."""
        stats.touch_activity(call.from_user.id)
        await topup_module.on_callback(call, bot)

    @router.callback_query(F.data.startswith((shop_buttons.SHOP_PREFIX, shop_buttons.GALLERY_PREFIX)))
    async def on_shop_button(call: CallbackQuery, bot: Bot) -> None:
        # Действие определяется по callback_data ЭТОГО бота (texts.shop_buttons читается в момент нажатия).
        # Кнопки устаревшего (до «Перенаписать текста») или чужого приветствия просто гасят «часики».
        action = shop_buttons.parse_action(texts.shop_buttons, call.data)
        await call.answer()
        user_id = call.from_user.id
        stats.touch_activity(user_id)
        if action is None or not stats.has(user_id):
            return
        if action == "gallery":
            # приветствие (оно же главное меню) удаляется, вместо него приходит раздел галереи
            if call.message is not None:
                await safe_delete(bot, call.message.chat.id, call.message.message_id)
            last_greeting.pop(user_id, None)
            await drop_last_gallery(bot, user_id, keep=None)
            await send_gallery(bot, user_id)
        elif action == "shop":
            await show_shop_menu(call, bot, user_id)

    @router.callback_query(F.data.startswith(shop.PREFIXES))
    async def on_shop_menu_button(call: CallbackQuery, bot: Bot) -> None:
        """Кнопки раздела «Магазин»: товар, «Купить», «Назад». Действие определяется по callback_data ЭТОГО бота
        (texts.shop_menu читается в момент нажатия): кнопки устаревших или чужих сообщений просто гасят «часики»."""
        parsed = shop.parse_callback(texts.shop_menu, call.data)
        user_id = call.from_user.id
        stats.touch_activity(user_id)
        known = parsed is not None and stats.has(user_id)
        if not (known and parsed[0] == "buy"):   # у «Купить» ответ на нажатие — всплывающее окно с итогом (buy_product)
            await safe_answer(call)
        if not known:
            return
        action, product_id = parsed
        if action == "product":
            row = await repo.get_shop_product(product_id) if product_id is not None else None
            if row is None:
                # товар скрыт или удалён (или это кнопка старого сообщения без id) — вместо карточки актуальное меню
                await show_shop_menu(call, bot, user_id)
                return
            product = shop.product_from_row(row)
            seed = str(getattr(profile, "token", None) or stats.bot_id)   # seed бота: токен (как у остальных текстов)
            percent, _ = await get_discount(user_id)
            await show_product_card(call, bot, user_id, shop.render_product_parts(product, seed, percent),
                                    shop.build_product_keyboard(texts.shop_menu, product, percent))
        elif action == "product_back":
            await show_shop_menu(call, bot, user_id)
        elif action == "back":
            await show_main_menu(call, bot, user_id)
        elif action == "buy":
            if product_id is None:   # кнопка старого сообщения без id товара
                await safe_answer(call)
                await show_shop_menu(call, bot, user_id)
            else:
                await buy_product(call, bot, user_id, product_id)

    async def buy_product(call: CallbackQuery, bot: Bot, user_id: int, product_id: int) -> None:
        """«Купить» в карточке товара: списание 🍬 по цене со скидкой (пока действуют 24 часа после ввода промокода)
        одной транзакцией; скидка при этом не гасится. Не хватает конфет — окно «❌ У вас недостаточно средств».
        Дальше — обновлённое меню магазина (баланс уже новый, строка про промокод на месте, пока скидка действует)."""
        try:
            result = await repo.buy_product(stats.bot_id, user_id, product_id)
        except Exception:
            log.exception("Покупка товара %s пользователем %s не удалась", product_id, user_id)
            await safe_answer(call, "❌ Не удалось совершить покупку, попробуйте позже", alert=True)
            return
        if result.status == "gone":
            await safe_answer(call)
            await show_shop_menu(call, bot, user_id)   # товар скрыли или удалили, пока человек смотрел карточку
        elif result.status == "funds":
            await safe_answer(call, "❌ У вас недостаточно средств", alert=True)
        else:
            await safe_answer(call, f"✅ Вы приобрели «{result.name}» за {result.price}{shop.CANDY}", alert=True)
            await show_shop_menu(call, bot, user_id)

    # ---------- «Бесплатные конфеты»: подарок, реферальная ссылка, задания из БД (app/tasks.py) ----------

    def seed_of_bot() -> str:
        return str(getattr(profile, "token", None) or stats.bot_id)

    async def grant_referral(bot: Bot, invited_id: int) -> None:
        """Новичок прошёл капчу по реферальной ссылке: пригласивший получает награду (один раз за друга)."""
        referrer = ref_pending.pop(invited_id, None)
        if referrer is None:
            return
        try:
            balance = await repo.register_referral(stats.bot_id, invited_id, referrer, tasks.REFERRAL_REWARD)
        except Exception:
            log.exception("Не удалось засчитать приглашение %s -> %s", referrer, invited_id)
            return
        if balance is not None:
            if hasattr(stats, "note_referral"):
                stats.note_referral()   # счётчик «приглашено по ссылке» на сайте (в памяти, без БД)
            await reply(bot, referrer, tasks.fill(texts.tasks_menu.text("invited"), reward=tasks.REFERRAL_REWARD))

    _UNKNOWN = object()   # «остаток кулдауна подарка ещё не известен» (None — известен: подарок можно забирать)

    def daily_left_of(last) -> float | None:
        """Сколько секунд осталось до следующего подарка по моменту последнего получения; None — можно забирать."""
        if last is None:
            return None
        left = (last + tasks.DAILY_COOLDOWN - datetime.now(timezone.utc)).total_seconds()
        return left if left > 0 else None

    async def edit_screen(call, bot: Bot, user_id: int, text: str, markup) -> None:
        """Обновляет экран НА МЕСТЕ (без удаления и повторной отправки). Если сообщение отредактировать нельзя (удалено,
        слишком старое, подпись длиннее лимита) — присылает новый экран, как обычный переход."""
        msg = getattr(call, "message", None)
        if msg is None:
            await show_screen(call, bot, user_id, text, markup)
            return
        try:
            await messaging.edit_message(
                bot, user_id, msg.message_id, text, markup, has_photo=bool(getattr(msg, "photo", None))
            )
        except TelegramForbiddenError as e:
            await on_forbidden(e, user_id)
            return
        except TelegramAPIError as e:
            log.info("Меню пользователя %s не отредактировано (%s) — присылаю новое", user_id, e)
            await show_screen(call, bot, user_id, text, markup)
            return
        last_greeting[user_id] = msg.message_id   # это по-прежнему «главное меню»: /start его заменит

    async def show_free_menu(call, bot: Bot, user_id: int, balance: int | None = None, daily_left=_UNKNOWN,
                             edit: bool = False) -> None:
        """Меню «Бесплатные конфеты»: подарок (зелёный, на время кулдауна — синий с остатком времени), ссылка, видимые
        задания из БД и «Назад». Заданий нет — только подарок и ссылка. edit=True — отредактировать сообщение на месте,
        иначе новое сообщение (прежнее удаляется). Баланс и остаток кулдауна, если не переданы, берутся одним запросом."""
        if balance is None or daily_left is _UNKNOWN:
            db_balance, last = await repo.get_balance_and_daily(stats.bot_id, user_id)
            if balance is None:
                balance = db_balance
            if daily_left is _UNKNOWN:
                daily_left = daily_left_of(last)
        items = [tasks.task_from_row(r) for r in await repo.list_tasks(limit=tasks.MAX_TASKS)]
        # статусы нужны только для цвета кнопок одноразовых заданий (синяя — награда уже получена)
        statuses = await repo.get_task_statuses(stats.bot_id, user_id) if any(t.one_time for t in items) else {}
        text = tasks.render_menu(texts.tasks_menu, texts.deco, balance)
        markup = tasks.build_menu_keyboard(texts.tasks_menu, items, daily_left, statuses)
        if edit:
            await edit_screen(call, bot, user_id, text, markup)
        else:
            await show_screen(call, bot, user_id, text, markup)

    async def show_task_card(call, bot: Bot, user_id: int, row) -> None:
        task = tasks.task_from_row(row)
        completion = await repo.get_completion(stats.bot_id, user_id, task.id)
        status = tasks.card_status(task, completion.status if completion else None)
        await show_screen(
            call, bot, user_id, tasks.render_task(texts.tasks_menu, task, seed_of_bot(), status),
            tasks.build_task_keyboard(texts.tasks_menu, task, status),
        )

    async def claim_daily_gift(call: CallbackQuery, bot: Bot, user_id: int) -> None:
        cfg = texts.tasks_menu
        ok, balance, last = await repo.claim_daily(stats.bot_id, user_id, tasks.DAILY_REWARD, tasks.DAILY_COOLDOWN)
        if ok:
            # то же сообщение, на месте: новый баланс и синяя кнопка подарка с остатком кулдауна
            await show_free_menu(call, bot, user_id, balance, tasks.DAILY_COOLDOWN.total_seconds(), edit=True)
            await safe_answer(call, tasks.fill(cfg.text("daily_ok"), amount=tasks.DAILY_REWARD), alert=True)
        elif last is not None:
            left = (last + tasks.DAILY_COOLDOWN - datetime.now(timezone.utc)).total_seconds()
            await safe_answer(call, tasks.fill(cfg.text("daily_wait"), left=tasks.fmt_left(left)), alert=True)
        else:
            await safe_answer(call)

    async def show_ref_screen(call: CallbackQuery, bot: Bot, user_id: int) -> None:
        link = tasks.ref_link(getattr(profile, "username", None), user_id)
        if link is None:
            await safe_answer(call, "Реферальная ссылка пока недоступна", alert=True)
            return
        count = await repo.count_referrals(stats.bot_id, user_id)
        await show_screen(call, bot, user_id, tasks.render_ref(texts.tasks_menu, link, count),
                          tasks.build_back_keyboard(texts.tasks_menu))

    async def show_collect_screen(call, bot: Bot, user_id: int, row, notice: str | None = None) -> None:
        """Экран приёма скриншотов: описание, счётчик принятых и кнопки «Отправить на проверку (N)» / «Назад»."""
        task = tasks.task_from_row(row)
        count = len(task_shots.get(user_id, ()))
        await show_screen(
            call, bot, user_id, tasks.render_send_prompt(texts.tasks_menu, task, count, notice),
            tasks.build_collect_keyboard(texts.tasks_menu, task, count),
        )

    def schedule_collect_refresh(bot: Bot, user_id: int, notice: str | None = None) -> None:
        """Обновляет экран-счётчик через короткую паузу после последнего скриншота: альбом из нескольких фото приходит
        отдельными сообщениями, а экран нужен один — с итоговым числом."""
        old = task_refresh.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()

        async def run() -> None:
            try:
                await asyncio.sleep(tasks.COLLECT_REFRESH_DELAY)
                if task_refresh.get(user_id) is asyncio.current_task():
                    task_refresh.pop(user_id, None)
                task_id = task_wait.get(user_id)
                if task_id is None:   # человек уже вышел (назад, /start)
                    return
                row = await repo.get_task(task_id)
                if row is None or not row.require_screenshot:   # задание скрыли/удалили/изменили, пока человек готовил скрины
                    clear_task_wait(user_id)
                    await show_free_menu(None, bot, user_id)
                    return
                await show_collect_screen(None, bot, user_id, row, notice)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Не удалось обновить экран приёма скриншотов у %s", user_id)

        task_refresh[user_id] = asyncio.create_task(run())

    async def handle_task_screenshot(message: Message, bot: Bot) -> None:
        """Сообщение пользователя, пока бот ждёт скриншоты задания: фото (или картинка файлом) добавляется к набору
        (до tasks.MAX_SHOTS), экран-счётчик обновляется. На проверку набор уходит кнопкой «Отправить на проверку»."""
        user = message.from_user
        found = tasks.is_image_message(message)
        if found is None:
            await reply(bot, user.id, texts.tasks_menu.text("not_photo"))   # ждём дальше
            return
        shots = task_shots.setdefault(user.id, [])
        notice = None
        if len(shots) >= tasks.MAX_SHOTS:
            notice = tasks.fill(tasks.SHOTS_LIMIT, max=tasks.MAX_SHOTS)
        else:
            shots.append(found)
        schedule_collect_refresh(bot, user.id, notice)

    async def submit_task_shots(call: CallbackQuery, bot: Bot, user_id: int, task_id: int) -> None:
        """«Отправить на проверку (N)»: набор скриншотов -> заявка (pending) -> бот для чеков (app/task_review.py)."""
        cfg = texts.tasks_menu
        shots = list(task_shots.get(user_id, ()))
        row = await repo.get_task(task_id)
        if row is None or not row.require_screenshot:   # задание скрыли/удалили/изменили, пока человек готовил скрины
            clear_task_wait(user_id)
            await safe_answer(call)
            await show_free_menu(call, bot, user_id)
            return
        if not shots:
            await safe_answer(call, tasks.SHOTS_EMPTY, alert=True)
            return
        if await repo.is_receipt_blocked(user_id):   # заблокировали, пока человек готовил скрины
            clear_task_wait(user_id)
            await safe_answer(call, tasks.TASK_BLOCKED_ALERT, alert=True)
            await show_free_menu(call, bot, user_id)
            return
        clear_task_wait(user_id)   # до обращения к БД: второе нажатие не отправит набор повторно
        try:
            cid = await repo.submit_task_screenshot(
                stats.bot_id, user_id, row.id, int(row.reward), shots, stats_username(call.from_user),
                repeatable=not bool(getattr(row, "one_time", True)),
            )
        except Exception:
            log.exception("Не удалось сохранить скриншоты задания от %s", user_id)
            await safe_answer(call, tasks.SHOTS_NOT_SENT, alert=True)
            await show_free_menu(call, bot, user_id)
            return
        await safe_answer(call)
        if cid is None:   # одноразовое уже выполнено или задание уже на проверке
            await show_task_card(call, bot, user_id, row)
            return
        await reply(bot, user_id, cfg.text("sent"))
        await show_free_menu(call, bot, user_id)
        try:
            await task_review.deliver(cid)   # недоставленное придёт админам по /pending у бота для чеков
        except Exception:
            log.exception("Не удалось отправить задание #%s в бот для чеков", cid)

    @router.callback_query(F.data.startswith(tasks.PREFIXES))
    async def on_tasks_button(call: CallbackQuery, bot: Bot) -> None:
        """Кнопки «Бесплатные конфеты»: меню, ежедневный подарок, реферальная ссылка, задания. Действие определяется по
        callback_data ЭТОГО бота (texts.tasks_menu читается в момент нажатия): кнопки старых или чужих сообщений гасят «часики»."""
        cfg = texts.tasks_menu
        parsed = tasks.parse_callback(cfg, call.data)
        user_id = call.from_user.id
        stats.touch_activity(user_id)
        if parsed is None or not stats.has(user_id):
            await safe_answer(call)
            return
        action, task_id = parsed
        if action == "done" and task_id is not None and task_wait.get(user_id) == task_id and task_shots.get(user_id):
            await submit_task_shots(call, bot, user_id, task_id)   # «Отправить на проверку (N)» на экране приёма скринов
            return
        clear_task_wait(user_id)   # любой переход отменяет приём скринов; «Отправить скриншот» включит его заново
        if action == "daily":
            await claim_daily_gift(call, bot, user_id)   # сам отвечает на callback
            return
        if action == "ref":
            await safe_answer(call)
            await show_ref_screen(call, bot, user_id)
            return
        if action in ("open", "menu"):
            await safe_answer(call)
            await show_free_menu(call, bot, user_id)
            return
        if action == "back":
            await safe_answer(call)
            await show_main_menu(call, bot, user_id)
            return
        # задания: кнопка устаревшего сообщения (задание скрыто/удалено) вместо карточки даёт актуальное меню
        row = await repo.get_task(task_id) if task_id is not None else None
        if row is None:
            await safe_answer(call)
            await show_free_menu(call, bot, user_id)
            return
        if action == "task":
            await safe_answer(call)
            await show_task_card(call, bot, user_id, row)
            return
        task = tasks.task_from_row(row)
        completion = await repo.get_completion(stats.bot_id, user_id, row.id)
        status = completion.status if completion else None
        if tasks.is_done(task, status):
            # одноразовое задание, награда уже получена: серая кнопка не работает — только всплывающее окно, экран не трогаем
            await safe_answer(call, f"{cfg.text('status_done')}. {tasks.ONE_TIME_NOTE}", alert=True)
            return
        if status == "pending" or (status == "approved" and task.one_time):
            await safe_answer(call)
            await show_task_card(call, bot, user_id, row)   # на проверке — карточка с актуальным статусом
            return
        if row.require_screenshot:   # «send» (и «done» по кнопке старого сообщения, если скриншот теперь нужен)
            if await repo.is_receipt_blocked(user_id):
                await safe_answer(call, tasks.TASK_BLOCKED_ALERT, alert=True)
                return
            await safe_answer(call)
            task_wait[user_id] = row.id
            await show_collect_screen(call, bot, user_id, row)
            return
        balance = await repo.complete_task_instant(
            stats.bot_id, user_id, row.id, int(row.reward), stats_username(call.from_user), repeatable=not task.one_time
        )
        if balance is None:
            await safe_answer(call)
            await show_task_card(call, bot, user_id, row)
            return
        await show_free_menu(call, bot, user_id, balance)
        await safe_answer(call, tasks.fill(cfg.text("done_ok"), reward=int(row.reward)), alert=True)

    async def notify_task_decision(user_id: int, key: str, task_name: str, reward: int) -> None:
        """Решение админа из бота для чеков (app/task_review.py): пользователю пишет ЭТОТ бот-магазин, текст — его вариант."""
        name = html.escape(task_name, quote=False)
        await reply(bot, user_id, tasks.fill(texts.tasks_menu.text(key), task=name, reward=reward))

    if bot is not None:
        task_review.register(stats.bot_id, bot, notify_task_decision)   # скачать скрины и уведомить о решении

    @router.callback_query(
        F.data.startswith((
            gallery.BUY_PREFIX, gallery.PREMIUM_PREFIX, gallery.BACK_PREFIX,
            gallery.SUGGEST_PREFIX, gallery.SUGGEST_BACK_PREFIX,
        ))
    )
    async def on_gallery_button(call: CallbackQuery, bot: Bot) -> None:
        action = gallery.parse_action(texts.gallery, call.data)
        user_id = call.from_user.id
        stats.touch_activity(user_id)
        if action is None or not stats.has(user_id):
            await safe_answer(call)
            return
        if action == "buy":
            await buy_video(call, bot)  # сам отвечает на callback (алерт при нехватке конфет)
            return
        if action == "premium_buy":
            key = gallery.parse_premium_key(texts.gallery, call.data)
            if key in gallery.PREMIUM_OPTIONS:
                await buy_premium(call, bot, key)  # сам отвечает на callback
            else:
                await safe_answer(call)
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
        elif action == "premium":
            # меню галереи заменяется меню премиума
            if call.message is not None:
                await safe_delete(bot, call.message.chat.id, call.message.message_id)
            last_gallery.pop(user_id, None)
            await send_premium_menu(bot, user_id)
        elif action == "premium_back":
            if call.message is not None:
                await safe_delete(bot, call.message.chat.id, call.message.message_id)
            last_gallery.pop(user_id, None)
            await send_gallery(bot, user_id)

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
