"""Супервизор бота: запуск на активном токене, мониторинг, ротация токенов «на горячую»."""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from . import repo
from .config import Settings
from .handlers import build_router
from .models import BotProfile

log = logging.getLogger("bot.manager")


class WatchedSession(AiohttpSession):
    """HTTP-сессия aiogram, которая следит за ответами Telegram.

    aiogram внутри polling глотает почти все ошибки getUpdates и бесконечно повторяет запрос,
    поэтому «токен умер» без такого перехвата снаружи не заметить.
    """

    def __init__(self, on_fatal, network_threshold: int, flood_threshold: int, **kwargs):
        super().__init__(**kwargs)
        self._on_fatal = on_fatal
        self._network_threshold = network_threshold
        self._flood_threshold = flood_threshold
        self._network_errors = 0

    async def make_request(self, bot, method, *args, **kwargs):
        try:
            result = await super().make_request(bot, method, *args, **kwargs)
        except (TelegramUnauthorizedError, TelegramNotFound) as e:
            # 401/404 от Telegram: токен недействителен, отозван или бот удалён/заблокирован
            self._on_fatal(f"Telegram отклонил токен: {type(e).__name__}: {e}")
            raise
        except TelegramRetryAfter as e:
            if e.retry_after > self._flood_threshold:
                self._on_fatal(f"Флуд-контроль: Telegram просит ждать {e.retry_after} с")
            raise
        except TelegramNetworkError as e:
            self._network_errors += 1
            if self._network_errors >= self._network_threshold:
                self._on_fatal(f"{self._network_errors} сетевых ошибок подряд: {e}")
            raise
        else:
            self._network_errors = 0
            return result


class BotManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._wake = asyncio.Event()    # «в базе что-то изменилось, проснись»
        self._switch = asyncio.Event()  # «останови текущего бота и перечитай активный токен»
        self._stopping = False
        self._task: asyncio.Task | None = None
        self.state: dict = {"running": False, "bot_id": None, "username": None, "since": None, "note": "запуск…"}

    # ---------- управление из веба / lifespan ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervisor(), name="bot-supervisor")

    async def stop(self) -> None:
        self._stopping = True
        self._switch.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=20)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    def poke(self, switch: bool = False) -> None:
        """switch=True — перезапустить бота (смена токена или профиля)."""
        self._wake.set()
        if switch:
            self._switch.set()

    # ---------- основной цикл ----------

    async def _supervisor(self) -> None:
        while not self._stopping:
            try:
                # ВАЖНО: сбрасываем _switch ДО чтения из БД, иначе запрос на переключение,
                # пришедший между чтением и сбросом, потеряется.
                self._switch.clear()
                profile = await repo.claim_next()

                if profile is None:
                    self._set_state(False, None, None, "нет доступных токенов — добавьте токен в панели")
                    log.warning("Нет доступных токенов, жду новых")
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=30)
                    except TimeoutError:
                        pass
                    continue

                self._wake.clear()
                log.info("Запускаю бота id=%s (%s)", profile.id, profile.name)
                reason = await self._run_bot(profile)
                self._set_state(False, None, None, "остановлен")

                if reason:
                    log.error("Токен id=%s выведен из ротации: %s", profile.id, reason)
                    await repo.mark_blocked(profile.id, reason)
                    # цикл продолжится и claim_next() возьмёт следующий токен из очереди
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в супервизоре, повтор через 5 с")
                await asyncio.sleep(5)

    async def _run_bot(self, profile: BotProfile) -> str | None:
        """Гоняет одного бота, пока он жив. Возвращает причину (str), если токен надо заменить,
        или None, если остановка штатная (переключение из панели / выключение приложения)."""
        stop = asyncio.Event()
        reasons: list[str] = []

        def on_fatal(reason: str) -> None:
            if not reasons:
                reasons.append(reason)
            stop.set()

        async def forward_switch() -> None:
            await self._switch.wait()
            stop.set()

        session = WatchedSession(
            on_fatal=on_fatal,
            network_threshold=self.settings.network_fail_threshold,
            flood_threshold=self.settings.flood_rotate_threshold,
        )
        bot = Bot(token=profile.token, session=session)
        forwarder = asyncio.create_task(forward_switch())
        stopper = asyncio.create_task(stop.wait())
        dp: Dispatcher | None = None
        polling: asyncio.Task | None = None

        try:
            # 1. проверяем токен
            me = None
            while not stop.is_set():
                try:
                    me = await bot.get_me()
                    break
                except TelegramRetryAfter as e:
                    await self._sleep(stop, e.retry_after)
                except (TelegramUnauthorizedError, TelegramNotFound):
                    break  # on_fatal уже вызван сессией
                except Exception as e:
                    log.warning("get_me не удался: %s", e)
                    await self._sleep(stop, 5)
            if me is None or stop.is_set():
                return reasons[0] if reasons else None

            await repo.set_username(profile.id, me.username)

            # 2. профиль + приветствие
            await self._apply_profile(bot, profile)
            if stop.is_set():
                return reasons[0] if reasons else None
            await self._greet(bot, me.username)

            self._set_state(True, profile.id, me.username, "работает")

            # 3. polling с защитой от «падений»
            crashes = 0
            loop = asyncio.get_running_loop()
            while not stop.is_set():
                dp = Dispatcher()
                dp.include_router(build_router(self.settings.greeting))
                polling = asyncio.create_task(
                    dp.start_polling(
                        bot,
                        handle_signals=False,
                        close_bot_session=False,
                        allowed_updates=dp.resolve_used_update_types(),
                    )
                )
                started = loop.time()
                done, _ = await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    break

                exc = None if polling.cancelled() else polling.exception()
                if loop.time() - started > 120:
                    crashes = 0
                crashes += 1
                log.error("Polling завершился (попытка %s/%s): %r", crashes, self.settings.max_polling_crashes, exc)
                if crashes >= self.settings.max_polling_crashes:
                    on_fatal(f"Polling падает: {exc!r}")
                    break
                await self._sleep(stop, 3 * crashes)

            return reasons[0] if reasons else None

        finally:
            forwarder.cancel()
            stopper.cancel()
            if polling is not None and not polling.done():
                try:
                    await dp.stop_polling()
                except Exception:
                    pass
                await asyncio.wait({polling}, timeout=5)
                if not polling.done():
                    polling.cancel()
                    await asyncio.gather(polling, return_exceptions=True)
            try:
                await bot.session.close()
            except Exception:
                pass

    # ---------- вспомогательное ----------

    async def _apply_profile(self, bot: Bot, profile: BotProfile) -> None:
        """Синхронизирует имя и описание бота в Telegram с данными из БД.
        Пустые значения не отправляем, чтобы случайно не стереть то, что задано в BotFather."""
        name = (profile.name or "").strip()[:64]
        description = (profile.description or "").strip()[:512]
        try:
            if name:
                current = await bot.get_my_name()
                if current.name != name:
                    await bot.set_my_name(name=name)
                    log.info("Имя бота обновлено: %s", name)
            if description:
                current_d = await bot.get_my_description()
                if current_d.description != description:
                    await bot.set_my_description(description=description)
                    log.info("Описание бота обновлено")
        except TelegramRetryAfter as e:
            log.warning("Лимит Telegram на смену профиля, повторю при следующем старте: %s с", e.retry_after)
        except Exception as e:
            log.warning("Не удалось обновить профиль бота: %s", e)

    async def _greet(self, bot: Bot, username: str | None) -> None:
        greeting = self.settings.greeting
        log.info("[@%s] %s", username, greeting)
        chat_id = self.settings.admin_chat_id
        if chat_id is None:
            return
        try:
            await bot.send_message(chat_id, greeting)
        except Exception as e:  # например, админ ещё не нажимал /start у этого бота
            log.warning("Не удалось отправить приветствие в чат %s: %s", chat_id, e)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _set_state(self, running: bool, bot_id: int | None, username: str | None, note: str) -> None:
        self.state = {
            "running": running,
            "bot_id": bot_id,
            "username": username,
            "since": datetime.now(timezone.utc) if running else None,
            "note": note,
        }
