"""Счётчики «за всё время»: в памяти, сохранение дельтой, middleware трафика."""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import metrics as mt  # noqa: E402


class FakeRepo:
    """Хранилище-заглушка с тем же поведением, что repo.add_metrics: прибавляет дельту и отдаёт итоги."""

    def __init__(self):
        self.data, self.calls, self.fail = {}, 0, False

    async def add_metrics(self, key, delta):
        self.calls += 1
        if self.fail:
            raise RuntimeError("db down")
        for k, v in delta.items():
            self.data[k] = self.data.get(k, 0) + v
        return dict(self.data)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self._saved = sys.modules.get("app.repo")
        sys.modules["app.repo"] = self.repo  # `from . import repo` берёт атрибут пакета, поэтому ставим и его
        import app
        self._attr = getattr(app, "repo", None)
        app.repo = self.repo
        self.m = mt.Metrics()

    def tearDown(self):
        import app
        if self._saved is not None:
            sys.modules["app.repo"] = self._saved
        else:
            sys.modules.pop("app.repo", None)
        if self._attr is not None:
            app.repo = self._attr
        elif hasattr(app, "repo"):
            delattr(app, "repo")

    def test_snapshot_is_memory_only_and_sums_events(self):
        self.m.add("traffic_in", 100)
        self.m.add("traffic_out", 400)
        self.m.add("http_requests", 3)
        self.m.add("submissions"); self.m.add("decisions", 2); self.m.add("videos_sent", 4)
        s = self.m.snapshot()
        self.assertEqual((s["traffic_bytes"], s["http_requests"], s["events"]), (500, 3, 7))
        self.assertEqual(self.repo.calls, 0)  # чтение значений в БД не ходит

    def test_flush_writes_delta_once_and_skips_when_idle(self):
        async def go():
            await self.m.flush()
            self.assertEqual(self.repo.calls, 0)  # ничего не менялось — БД не трогаем
            self.m.add("db_ops", 10)
            await self.m.flush()
            await self.m.flush()
            self.assertEqual(self.repo.calls, 1)
            self.assertEqual(self.repo.data["db_ops"], 10)
            self.m.add("db_ops", 5)
            await self.m.flush()
            self.assertEqual(self.repo.data["db_ops"], 15)
            self.assertEqual(self.m.snapshot()["db_ops"], 15)

        asyncio.run(go())

    def test_totals_survive_restart_and_failed_save_is_not_lost(self):
        async def go():
            self.m.add("http_requests", 7)
            self.repo.fail = True
            await self.m.flush()
            self.assertEqual(self.m.snapshot()["http_requests"], 7)  # не потеряно
            self.repo.fail = False
            await self.m.flush()
            # «перезапуск»: новый объект подтягивает итоги из БД
            fresh = mt.Metrics()
            fresh.add("http_requests", 2)  # набежало до загрузки
            await fresh.load()
            self.assertEqual(fresh.snapshot()["http_requests"], 9)
            self.assertEqual(self.repo.data["http_requests"], 7)  # load ничего не пишет

        asyncio.run(go())

    def test_db_ops_inside_save_are_not_counted(self):
        self.m.db_op()
        token = mt._muted.set(True)
        try:
            self.m.db_op()
        finally:
            mt._muted.reset(token)
        self.assertEqual(self.m.snapshot()["db_ops"], 1)


class MiddlewareTests(unittest.TestCase):
    def run_request(self, path):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"a", b"bb")]})
            await send({"type": "http.response.body", "body": b"x" * 100})

        async def go():
            mt.metrics.__init__()
            mw = mt.MetricsMiddleware(app)
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"yyyy"}

            async def send(m):
                sent.append(m)

            await mw({"type": "http", "path": path, "headers": [(b"host", b"h")]}, receive, send)
            return mt.metrics.snapshot(), sent

        return asyncio.run(go())

    def test_counts_request_and_bytes(self):
        s, sent = self.run_request("/api/state")
        self.assertEqual(s["http_requests"], 1)
        self.assertEqual(s["traffic_in"], (4 + 1 + 4) + 4)    # заголовок host + тело
        self.assertEqual(s["traffic_out"], (1 + 2 + 4) + 100)  # заголовок ответа + тело
        self.assertEqual(len(sent), 2)  # ответ приложения доходит без изменений

    def test_metrics_polling_is_not_counted(self):
        s, sent = self.run_request("/api/metrics")
        self.assertEqual(s["http_requests"], 0)
        self.assertEqual(s["traffic_bytes"], 0)
        self.assertEqual(len(sent), 2)

    def test_non_http_scope_passes_through(self):
        async def go():
            mt.metrics.__init__()
            called = []

            async def app(scope, receive, send):
                called.append(scope["type"])

            await mt.MetricsMiddleware(app)({"type": "lifespan"}, None, None)
            return called, mt.metrics.snapshot()["http_requests"]

        self.assertEqual(asyncio.run(go()), (["lifespan"], 0))


if __name__ == "__main__":
    unittest.main()
