"""Фоновая автоочистка пользователей, не взаимодействовавших с ботом больше 7 дней.

Зачем: `bot_users` растёт вместе с числом тех, кто хоть раз прошёл капчу; ушедшие навсегда копятся в БД (Neon) годами.

Как устроено (и почему это дёшево):
  * задача одна на приложение и просыпается раз в 24 часа; первый запуск — через несколько минут после старта,
    чтобы не грузить запуск. Время последнего прогона хранится в `kv_settings`, поэтому частые перезапуски
    (бесплатный Render усыпляет сервис) не превращают «раз в сутки» в «при каждом старте»;
  * один SQL-запрос удаляет пачку (`BATCH_SIZE`) по индексу `idx_users_last_active_at`; если записей больше,
    пачки идут одна за другой с короткой паузой — без долгих блокировок. В обычный день запрос возвращает 0 строк;
  * защита данных (баланс 🍬, незавершённые заказы пополнения, заявки предложки на модерации, действующий премиум)
    и сам запрос — в app/cleanup_sql.py;
  * очистка общая для всех ботов (`bot_users` содержит пользователей каждого бота, ключ — (bot_id, user_id)): запрос
    не зависит от того, какой бот сейчас активен. Работающему боту удалённых пользователей нужно забыть и в памяти
    (`UserStats.forget`), иначе он продолжит считать их прошедшими капчу;
  * в лог пишется только итог, и только если что-то удалено.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import repo

log = logging.getLogger("bot.cleanup")

INACTIVE_DAYS = 7
INTERVAL_SECONDS = 24 * 3600
START_DELAY_SECONDS = 180        # небольшая задержка после старта приложения
BATCH_SIZE = 1000                # порция удаления за один запрос
BATCH_PAUSE_SECONDS = 1.0        # пауза между порциями
RETRY_AFTER_ERROR_SECONDS = 3600
KV_LAST_RUN = "users_cleanup_last_run"


async def cleanup_once(
    *,
    days: int = INACTIVE_DAYS,
    batch: int = BATCH_SIZE,
    pause: float = BATCH_PAUSE_SECONDS,
    now: datetime | None = None,
    sleep=asyncio.sleep,
    before=None,
    on_deleted=None,
) -> int:
    """Один полный прогон: удаляет порциями всех неактивных дольше `days` суток (кроме защищённых). Возвращает число
    удалённых. before() — корутина, вызывается перед удалением (сбросить накопленную в памяти активность в БД);
    on_deleted(rows) — обычная функция, получает [(bot_id, user_id), ...] каждой удалённой порции."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    if before is not None:
        await before()
    total = 0
    try:
        while True:
            rows = await repo.delete_inactive_users(cutoff, batch)
            if not rows:
                break
            total += len(rows)
            if on_deleted is not None:
                on_deleted(rows)
            if len(rows) < batch:
                break
            await sleep(pause)   # накопилось много — даём БД передохнуть между порциями
    finally:
        if total:
            log.info("Автоочистка: удалено неактивных пользователей: %s", total)
    return total


async def _delay_before_first_run(now_ts: float) -> float:
    """Не раньше START_DELAY_SECONDS после старта и не раньше, чем через сутки после прошлого прогона."""
    delay = float(START_DELAY_SECONDS)
    try:
        raw = await repo.kv_get(KV_LAST_RUN)
        if raw:
            delay = max(delay, float(raw) + INTERVAL_SECONDS - now_ts)
    except Exception:
        log.debug("Не удалось прочитать время прошлой автоочистки", exc_info=True)
    return delay


async def run_loop(*, before=None, on_deleted=None, sleep=asyncio.sleep, clock=time.time) -> None:
    """Бесконечная фоновая задача (отменяется при остановке приложения)."""
    await sleep(await _delay_before_first_run(clock()))
    while True:
        try:
            await cleanup_once(before=before, on_deleted=on_deleted)
            try:
                await repo.kv_set(KV_LAST_RUN, str(int(clock())))
            except Exception:
                log.debug("Не удалось записать время автоочистки", exc_info=True)
            wait = INTERVAL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Автоочистка не удалась, повтор через час")
            wait = RETRY_AFTER_ERROR_SECONDS
        await sleep(wait)
