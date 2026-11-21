"""Отправка автосообщения через 30 минут после капчи (настройки и тексты — app/promo.py).

Как это устроено и почему так:
  * Пользователь, прошедший капчу, попадает в таблицу `auto_message_queue` (срок = сейчас + 30 мин) и получает таймер
    в памяти (`asyncio`). Опроса БД по расписанию нет — Neon не должен просыпаться ради пустых проверок.
  * Перезапуск приложения таймеры стирает, поэтому при запуске бот ОДНИМ запросом читает свою очередь и ставит таймеры
    заново (просроченным — сразу, с небольшой паузой между людьми, чтобы не упереться в лимиты Telegram).
  * В момент отправки запись забирается одним DELETE (`repo.claim_auto_message`): дважды сообщение не уйдёт.
    Настройки читаются именно тогда (а не при капче), поэтому правки на сайте действуют и на тех, кто уже ждёт.
  * Промокод выдаётся тоже при отправке (`repo.issue_promo`): у каждого пользователя свой, из 8 символов A-Z0-9.
  * Просроченное больше чем на сутки (бот долго был выключен) не отправляется — писать человеку через неделю странно.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

from . import messaging, promo, repo

log = logging.getLogger("bot.automsg")

OVERDUE_GAP = 0.1   # пауза между просроченными при запуске, с


class AutoMessenger:
    def __init__(self, bot, stats, profile, delay: float = promo.AUTO_DELAY) -> None:
        self.bot = bot
        self.stats = stats
        self.profile = profile
        self.delay = delay
        self.on_blocked = None       # async (user_id): пользователь сам заблокировал бота (ставит handlers.build_router)
        self._tasks: dict[int, asyncio.Task] = {}

    @property
    def seed(self) -> str:
        return str(getattr(self.profile, "token", None) or self.stats.bot_id)

    async def start(self) -> None:
        """Ставит таймеры тем, кто ждал сообщение до перезапуска."""
        try:
            rows = await repo.load_auto_queue(self.stats.bot_id)
        except Exception:
            log.exception("Не удалось прочитать очередь автосообщений")
            return
        now = datetime.now(timezone.utc)
        overdue = 0
        for user_id, due in sorted(rows, key=lambda r: r[1]):
            left = (due - now).total_seconds()
            if left < -promo.AUTO_STALE:
                await self._drop(user_id)
                continue
            if left <= 0:
                left = overdue * OVERDUE_GAP
                overdue += 1
            self._schedule(user_id, left)

    async def send_test(self, user_id: int) -> dict:
        """Проверка с сайта: шлёт автосообщение СЕЙЧАС, в обход очереди и 30-минутного таймера. Всё как в бою: настройки
        читаются свежими, промокод выдаётся при наличии {promo} (прежний код пользователя заменяется новым и скидка
        сбрасывается), сообщение идёт без картинки бота. Очередь не трогаем: если человек ещё ждёт автосообщение, оно
        придёт в своё время. ValueError — автосообщение выключено; ошибки Telegram пробрасываются вызывающему."""
        cfg = await promo.load_settings(fresh=True)
        if not cfg.enabled:
            raise ValueError("Автосообщение выключено: напишите сообщение и нажмите «Сохранить»")
        code = None
        if promo.has_variable(cfg.text) and cfg.percent:
            code = await repo.issue_promo(self.stats.bot_id, user_id, cfg.percent)
        text = promo.render(cfg.text, self.seed, code, cfg.percent)
        await messaging.send_message(self.bot, user_id, text)   # уведомление о скидке — без картинки бота
        return {"promo": code, "percent": cfg.percent}

    async def on_captcha_passed(self, user_id: int) -> None:
        """Пользователь прошёл капчу: если автосообщение включено, ставим его в очередь. Ошибки БД на капчу не влияют."""
        try:
            if not (await promo.load_settings()).enabled:
                return
            due = datetime.now(timezone.utc) + timedelta(seconds=self.delay)
            await repo.queue_auto_message(self.stats.bot_id, user_id, due)
        except Exception:
            log.exception("Не удалось поставить пользователя %s в очередь автосообщения", user_id)
            return
        self._schedule(user_id, self.delay)

    def forget(self, user_id: int) -> None:
        task = self._tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- внутреннее ----------

    def _schedule(self, user_id: int, delay: float) -> None:
        self.forget(user_id)
        task = asyncio.create_task(self._run(user_id, delay), name=f"automsg-{user_id}")
        self._tasks[user_id] = task
        task.add_done_callback(lambda t, uid=user_id: self._tasks.pop(uid, None) if self._tasks.get(uid) is t else None)

    async def _drop(self, user_id: int) -> None:
        try:
            await repo.claim_auto_message(self.stats.bot_id, user_id)
        except Exception:
            log.exception("Не удалось убрать просроченное автосообщение пользователя %s", user_id)

    async def _run(self, user_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await self.deliver(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Автосообщение пользователю %s не отправлено", user_id)

    async def deliver(self, user_id: int) -> bool:
        """Отправляет автосообщение сейчас. True — сообщение ушло."""
        if not await repo.claim_auto_message(self.stats.bot_id, user_id):
            return False                                  # уже отправлено (другой процесс) или убрано
        if not self.stats.has(user_id):
            return False                                  # пользователя уже нет в базе (заблокировал бота, удалён)
        cfg = await promo.load_settings()
        if not cfg.enabled:
            return False
        code = None
        if promo.has_variable(cfg.text) and cfg.percent:
            code = await repo.issue_promo(self.stats.bot_id, user_id, cfg.percent)
        text = promo.render(cfg.text, self.seed, code, cfg.percent)
        for attempt in (1, 2):
            try:
                await messaging.send_message(self.bot, user_id, text)   # уведомление о скидке — без картинки бота
                return True
            except TelegramRetryAfter as e:
                if attempt == 2:
                    log.warning("Автосообщение пользователю %s: флуд-контроль, пропуск", user_id)
                    return False
                await asyncio.sleep(min(float(e.retry_after), 30.0))
            except TelegramForbiddenError as e:
                if messaging.is_blocked_by_user(e) and self.on_blocked is not None:
                    await self.on_blocked(user_id)
                return False
            except TelegramAPIError as e:
                log.warning("Автосообщение пользователю %s не доставлено: %s", user_id, e)
                return False
        return False
