"""«Живая» статистика за всё время (подвал сайта): трафик, HTTP-запросы, операции БД, события предложки.

Счётчики живут в памяти и только растут. В БД (`kv_settings`, ключ `metrics_totals`) они уходят пачкой раз в
`SAVE_EVERY` секунд и при штатной остановке — не на каждый запрос и не на каждый байт. Запись в БД идёт ДЕЛЬТОЙ
(сколько набежало с прошлого сохранения, прибавляется к сохранённому в одной транзакции), поэтому два процесса, работающих
одновременно (например, во время деплоя на Render), не затирают счётчики друг друга, а «за всё время» не теряется при рестарте.
Если ничего не изменилось — в БД не пишем вообще. Сама запись и опрос `/api/metrics` в счётчики не попадают.

ЧЕСТНО: это счётчики на уровне приложения, а не биллинг Render или БД. Трафик — тела и заголовки HTTP, пройденные через
приложение (без накладных расходов TLS/TCP и без трафика самого Telegram); операции БД — выполненные приложением SQL-команды.
Цифры приближённо отражают нагрузку на веб-сервер и базу, но с счётом хостинга могут не совпасть.
"""
import asyncio
import contextvars
import logging

log = logging.getLogger("bot.metrics")

KEY = "metrics_totals"
SAVE_EVERY = 45.0                  # секунд между сохранениями в БД
SKIP_PATHS = ("/api/metrics",)     # опрос подвала не раздувает собственные счётчики

FIELDS = ("traffic_in", "traffic_out", "http_requests", "db_ops", "submissions", "decisions", "videos_sent")

# внутри сохранения метрик запросы к БД не считаем (иначе счётчик считал бы сам себя)
_muted: contextvars.ContextVar[bool] = contextvars.ContextVar("metrics_muted", default=False)


class Metrics:
    def __init__(self) -> None:
        self._base = {f: 0 for f in FIELDS}      # итог по БД на момент последней синхронизации
        self._pending = {f: 0 for f in FIELDS}   # набежало с тех пор (ещё не в БД)
        self._dirty = False
        self._lock = asyncio.Lock()

    # ---- счётчики (синхронные, без обращений к БД; вызываются часто) ----
    def add(self, field: str, n: int = 1) -> None:
        if n:
            self._pending[field] += n
            self._dirty = True

    def db_op(self) -> None:
        if not _muted.get():
            self.add("db_ops")

    def snapshot(self) -> dict:
        """Актуальные значения из памяти (итог из БД + набежавшее). БД не трогает."""
        t = {f: self._base[f] + self._pending[f] for f in FIELDS}
        return {
            "traffic_bytes": t["traffic_in"] + t["traffic_out"],
            "traffic_in": t["traffic_in"],
            "traffic_out": t["traffic_out"],
            "http_requests": t["http_requests"],
            "db_ops": t["db_ops"],
            "events": t["submissions"] + t["decisions"] + t["videos_sent"],
            "submissions": t["submissions"],
            "decisions": t["decisions"],
            "videos_sent": t["videos_sent"],
        }

    # ---- сохранение ----
    async def load(self) -> None:
        """Подтягивает накопленное из БД при старте (то, что набежало до этого момента, не теряется)."""
        from . import repo

        token = _muted.set(True)
        try:
            self._base = _fill(await repo.add_metrics(KEY, {}))
        finally:
            _muted.reset(token)

    async def flush(self) -> None:
        if not self._dirty:
            return
        from . import repo

        async with self._lock:
            delta, self._pending = self._pending, {f: 0 for f in FIELDS}
            self._dirty = False
            token = _muted.set(True)
            try:
                self._base = _fill(await repo.add_metrics(KEY, delta))
            except Exception as e:
                log.warning("Не удалось сохранить счётчики статистики: %s", e)
                for f in FIELDS:           # вернуть набежавшее, чтобы не потерять
                    self._pending[f] += delta[f]
                self._dirty = True
            finally:
                _muted.reset(token)

    async def run(self) -> None:
        """Фоновая задача: раз в SAVE_EVERY секунд сохраняет счётчики. При остановке приложения — финальный flush снаружи."""
        while True:
            await asyncio.sleep(SAVE_EVERY)
            await self.flush()


def _fill(raw: dict) -> dict:
    return {f: max(0, int(raw.get(f, 0) or 0)) for f in FIELDS}


metrics = Metrics()


class MetricsMiddleware:
    """Чистый ASGI-middleware: считает HTTP-запросы и байты (тело + заголовки) входящего и исходящего трафика."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in SKIP_PATHS:
            return await self.app(scope, receive, send)

        m = metrics
        m.add("http_requests")
        m.add("traffic_in", sum(len(k) + len(v) + 4 for k, v in scope.get("headers", ())))

        async def counting_receive():
            message = await receive()
            if message["type"] == "http.request":
                m.add("traffic_in", len(message.get("body", b"")))
            return message

        async def counting_send(message):
            if message["type"] == "http.response.start":
                m.add("traffic_out", sum(len(k) + len(v) + 4 for k, v in message.get("headers", ())))
            elif message["type"] == "http.response.body":
                m.add("traffic_out", len(message.get("body", b"")))
            await send(message)

        await self.app(scope, counting_receive, counting_send)
