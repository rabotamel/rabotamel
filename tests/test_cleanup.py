"""Оркестрация автоочистки без БД: вместо app.repo подставляется заглушка (как в test_stats.py)."""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeRepo(types.ModuleType):
    def __init__(self):
        super().__init__("app.repo")
        self.reset()

    def reset(self):
        self.queue = []          # что «лежит в БД» к удалению: [(bot_id, user_id), ...]
        self.calls = []          # (cutoff, batch) каждого запроса удаления
        self.kv = {}
        self.fail_delete = False

    async def delete_inactive_users(self, cutoff, batch):
        self.calls.append((cutoff, batch))
        if self.fail_delete:
            raise RuntimeError("db down")
        part, self.queue = self.queue[:batch], self.queue[batch:]
        return part

    async def kv_get(self, key):
        return self.kv.get(key)

    async def kv_set(self, key, value):
        self.kv[key] = value


fake = FakeRepo()
sys.modules.setdefault("app.repo", fake)
import app  # noqa: E402

if not hasattr(app, "repo"):
    app.repo = fake
from app import cleanup  # noqa: E402


def run(coro):
    return asyncio.run(coro)


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class CleanupOnceTests(unittest.TestCase):
    def setUp(self):
        cleanup.repo = fake      # на случай, если другой тест успел подменить app.repo
        fake.reset()

    def test_cutoff_is_seven_days_back(self):
        run(cleanup.cleanup_once(now=NOW))
        self.assertEqual(fake.calls[0][0], NOW - timedelta(days=7))

    def test_single_query_when_nothing_to_delete_and_no_logging(self):
        async def go():
            with self.assertNoLogs("bot.cleanup", level="INFO"):
                return await cleanup.cleanup_once(now=NOW)
        self.assertEqual(run(go()), 0)
        self.assertEqual(len(fake.calls), 1)

    def test_logs_only_total_when_something_deleted(self):
        fake.queue = [(1, u) for u in range(5)]
        async def go():
            with self.assertLogs("bot.cleanup", level="INFO") as cm:
                n = await cleanup.cleanup_once(now=NOW)
            return n, cm.output
        n, out = run(go())
        self.assertEqual(n, 5)
        self.assertEqual(len(out), 1)                 # одна строка на весь прогон
        self.assertIn("5", out[0])

    def test_large_backlog_is_deleted_in_batches_with_pauses(self):
        fake.queue = [(1, u) for u in range(2500)]
        pauses = []

        async def fake_sleep(sec):
            pauses.append(sec)

        n = run(cleanup.cleanup_once(now=NOW, batch=1000, pause=0.5, sleep=fake_sleep))
        self.assertEqual(n, 2500)
        self.assertEqual([b for _, b in fake.calls], [1000, 1000, 1000])   # 1000 + 1000 + 500 (неполная — конец)
        self.assertEqual(pauses, [0.5, 0.5])          # пауза между порциями, после последней её нет

    def test_exact_multiple_of_batch_needs_one_extra_empty_query(self):
        fake.queue = [(1, u) for u in range(2000)]
        n = run(cleanup.cleanup_once(now=NOW, batch=1000, sleep=lambda s: asyncio.sleep(0)))
        self.assertEqual(n, 2000)
        self.assertEqual(len(fake.calls), 3)

    def test_before_runs_first_and_on_deleted_gets_every_batch(self):
        fake.queue = [(1, 1), (2, 2), (1, 3)]
        order, seen = [], []

        async def before():
            order.append("before")

        def on_deleted(rows):
            order.append("deleted")
            seen.extend(rows)

        run(cleanup.cleanup_once(now=NOW, batch=2, sleep=lambda s: asyncio.sleep(0), before=before, on_deleted=on_deleted))
        self.assertEqual(order, ["before", "deleted", "deleted"])
        self.assertEqual(seen, [(1, 1), (2, 2), (1, 3)])

    def test_error_propagates(self):
        fake.fail_delete = True
        with self.assertRaises(RuntimeError):
            run(cleanup.cleanup_once(now=NOW))


class RunLoopScheduleTests(unittest.TestCase):
    """Расписание: первый запуск с задержкой после старта, затем раз в 24 часа; перезапуски не сокращают интервал."""

    def setUp(self):
        cleanup.repo = fake
        fake.reset()

    @staticmethod
    async def _drive(clock, *, sleeps_limit):
        sleeps = []

        class Stop(Exception):
            pass

        async def fake_sleep(sec):
            sleeps.append(sec)
            if len(sleeps) >= sleeps_limit:
                raise Stop

        try:
            await cleanup.run_loop(sleep=fake_sleep, clock=clock)
        except Stop:
            pass
        return sleeps

    def test_first_run_after_short_delay_then_every_24h(self):
        sleeps = run(self._drive(lambda: 1_000_000.0, sleeps_limit=3))
        self.assertEqual(sleeps, [cleanup.START_DELAY_SECONDS, 24 * 3600, 24 * 3600])
        self.assertEqual(len(fake.calls), 2)          # прогон после каждого из первых двух сна
        self.assertEqual(fake.kv[cleanup.KV_LAST_RUN], "1000000")

    def test_restart_within_a_day_waits_for_the_remaining_time(self):
        fake.kv[cleanup.KV_LAST_RUN] = str(1_000_000 - 3600)          # прошлый прогон был час назад
        sleeps = run(self._drive(lambda: 1_000_000.0, sleeps_limit=1))
        self.assertEqual(sleeps[0], 23 * 3600)

    def test_overdue_run_still_waits_start_delay(self):
        fake.kv[cleanup.KV_LAST_RUN] = str(1_000_000 - 5 * 24 * 3600)
        sleeps = run(self._drive(lambda: 1_000_000.0, sleeps_limit=1))
        self.assertEqual(sleeps[0], cleanup.START_DELAY_SECONDS)

    def test_failure_is_retried_in_an_hour_and_does_not_kill_the_loop(self):
        fake.fail_delete = True
        with self.assertLogs("bot.cleanup", level="ERROR"):      # ошибка пишется в лог (с трассировкой)
            sleeps = run(self._drive(lambda: 1_000_000.0, sleeps_limit=3))
        self.assertEqual(sleeps, [cleanup.START_DELAY_SECONDS, cleanup.RETRY_AFTER_ERROR_SECONDS,
                                  cleanup.RETRY_AFTER_ERROR_SECONDS])
        self.assertNotIn(cleanup.KV_LAST_RUN, fake.kv)   # неудачный прогон не считается выполненным


if __name__ == "__main__":
    unittest.main()
