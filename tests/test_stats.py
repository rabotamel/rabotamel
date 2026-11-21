"""Проверка учёта пользователей без БД: вместо app.repo подставляется заглушка."""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeRepo(types.ModuleType):
    def __init__(self):
        super().__init__("app.repo")
        self.calls = []
        self.fail = False
        self.stored_activity = {}      # user_id -> unix-время, которое «лежит в БД» при load()
        self.activity_calls = []       # каждый вызов touch_users_active (= один UPDATE в БД)
        self.activity_fail = False

    async def load_users(self, bot_id):
        return {1: "old", 2: None}

    async def load_last_active(self, bot_id):
        return dict(self.stored_activity)

    async def touch_users_active(self, bot_id, user_ids):
        await asyncio.sleep(0.01)
        if self.activity_fail:
            raise RuntimeError("db down")
        self.activity_calls.append(sorted(user_ids))

    async def save_users(self, bot_id, add, rename, remove):
        await asyncio.sleep(0.01)
        if self.fail:
            raise RuntimeError("db down")
        self.calls.append((dict(add), dict(rename), set(remove)))


fake = FakeRepo()
sys.modules["app.repo"] = fake
import app  # noqa: E402

app.repo = fake
from app.stats import UserStats, norm_username  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class StatsTests(unittest.TestCase):
    def setUp(self):
        fake.calls.clear()
        fake.fail = False
        fake.stored_activity = {}
        fake.activity_calls.clear()
        fake.activity_fail = False

    def test_only_added_users_are_counted_and_unknown_are_not_written(self):
        async def go():
            st = UserStats(7)
            await st.load()
            self.assertEqual(st.count, 2)
            self.assertFalse(st.has(99))
            await st.flush()                      # изменений нет — БД не трогаем
            self.assertEqual(fake.calls, [])
            self.assertTrue(st.add(99, "@New_User"))
            self.assertFalse(st.add(99, "x"))     # повтор не дублируется
            await st.flush()
            self.assertEqual(fake.calls, [({99: "new_user"}, {}, set())])
        run(go())

    def test_concurrent_flushes_make_one_write(self):
        async def go():
            st = UserStats(7)
            for uid in range(10, 20):
                st.add(uid, None)
            await asyncio.gather(*(st.flush() for _ in range(10)))
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(fake.calls[0][0]), 10)
        run(go())

    def test_touch_batches_username_change_and_ignores_same_name(self):
        async def go():
            st = UserStats(7)
            await st.load()
            st.touch(1, "OLD")                    # то же имя в другом регистре — не изменение
            self.assertFalse(st.dirty())
            st.touch(1, "fresh")
            st.touch(1, "fresher")
            st.touch(555, "ghost")                # не прошёл капчу — игнорируется
            await st.flush()
            self.assertEqual(fake.calls, [({}, {1: "fresher"}, set())])
            self.assertFalse(st.has(555))
        run(go())

    def test_remove_then_readd_deletes_first_so_balance_resets(self):
        async def go():
            st = UserStats(7)
            await st.load()
            self.assertTrue(st.remove(1))
            self.assertFalse(st.has(1))
            self.assertTrue(st.add(1, "again"))
            await st.flush()
            add, rename, remove = fake.calls[0]
            self.assertEqual((add, remove), ({1: "again"}, {1}))   # save_users: сначала DELETE, потом INSERT
        run(go())

    def test_failed_flush_is_retried(self):
        async def go():
            st = UserStats(7)
            st.add(50, "u")
            fake.fail = True
            await st.flush()
            self.assertTrue(st.dirty())
            fake.fail = False
            await st.flush()
            self.assertEqual(fake.calls, [({50: "u"}, {}, set())])
        run(go())

    def test_find_by_username(self):
        async def go():
            st = UserStats(7)
            await st.load()
            self.assertEqual(st.find_by_username("@OLD"), 1)
            self.assertIsNone(st.find_by_username("nobody"))
        run(go())

    def test_norm_username(self):
        self.assertEqual(norm_username(" @Abc_1 "), "abc_1")
        self.assertIsNone(norm_username(""))
        self.assertIsNone(norm_username(None))


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


HOUR = 3600


class ActivityThrottleTests(unittest.TestCase):
    """last_active_at пишется в БД с троттлингом: первое взаимодействие и далее не чаще раза в 6 часов."""

    def setUp(self):
        fake.calls.clear()
        fake.activity_calls.clear()
        fake.stored_activity = {}
        fake.activity_fail = False
        fake.fail = False

    def test_first_interaction_is_written_once_and_repeats_within_6h_do_not_hit_db(self):
        async def go():
            clock = FakeClock()
            st = UserStats(7, clock=clock)
            await st.load()
            self.assertTrue(st.touch_activity(1))           # первое взаимодействие — в очередь
            await st.flush()
            self.assertEqual(fake.activity_calls, [[1]])
            for _ in range(200):                             # поток сообщений и нажатий кнопок
                clock.t += 1
                self.assertFalse(st.touch_activity(1))
            clock.t += 5 * HOUR + 50 * 60                    # итого меньше 6 ч после первой записи — всё ещё мало
            self.assertFalse(st.touch_activity(1))
            self.assertFalse(st.dirty())                     # очередь пуста: flush не обратится к БД
            await st.flush()
            self.assertEqual(fake.activity_calls, [[1]])
        run(go())

    def test_written_again_after_more_than_6_hours(self):
        async def go():
            clock = FakeClock()
            st = UserStats(7, clock=clock)
            await st.load()
            st.touch_activity(1)
            await st.flush()
            clock.t += 6 * HOUR + 1
            self.assertTrue(st.touch_activity(1))
            await st.flush()
            self.assertEqual(fake.activity_calls, [[1], [1]])
        run(go())

    def test_many_users_go_in_one_batch_update(self):
        async def go():
            st = UserStats(7, clock=FakeClock())
            await st.load()
            for uid in range(100, 150):
                st.add(uid, None)
            st.touch_activity(1)
            st.touch_activity(2)
            await st.flush()
            self.assertEqual(len(fake.activity_calls), 1)    # одним запросом на всех
            self.assertEqual(fake.activity_calls[0], [1, 2])  # новые (add) получили last_active_at по умолчанию в БД
        run(go())

    def test_cache_is_seeded_from_db_so_restart_does_not_rewrite_everyone(self):
        async def go():
            clock = FakeClock()
            fake.stored_activity = {1: clock.t - 2 * HOUR, 2: clock.t - 8 * HOUR, 999: clock.t}
            st = UserStats(7, clock=clock)
            await st.load()
            self.assertFalse(st.touch_activity(1))           # записано 2 часа назад — БД не трогаем
            self.assertTrue(st.touch_activity(2))            # 8 часов назад — пора обновить
            await st.flush()
            self.assertEqual(fake.activity_calls, [[2]])
        run(go())

    def test_force_bypasses_throttle(self):
        async def go():
            st = UserStats(7, clock=FakeClock())
            await st.load()
            st.touch_activity(1)
            await st.flush()
            self.assertTrue(st.touch_activity(1, force=True))   # критичное действие (например, оплата)
            await st.flush()
            self.assertEqual(fake.activity_calls, [[1], [1]])
        run(go())

    def test_unknown_user_is_ignored(self):
        async def go():
            st = UserStats(7, clock=FakeClock())
            await st.load()
            self.assertFalse(st.touch_activity(555))         # не прошёл капчу — в БД строки нет
            self.assertFalse(st.dirty())
        run(go())

    def test_failed_activity_write_is_retried(self):
        async def go():
            st = UserStats(7, clock=FakeClock())
            await st.load()
            st.touch_activity(1)
            fake.activity_fail = True
            await st.flush()
            self.assertTrue(st.dirty())
            fake.activity_fail = False
            await st.flush()
            self.assertEqual(fake.activity_calls, [[1]])
        run(go())

    def test_removed_user_activity_is_not_written(self):
        async def go():
            st = UserStats(7, clock=FakeClock())
            await st.load()
            st.touch_activity(1)
            st.remove(1)
            await st.flush()
            self.assertEqual(fake.activity_calls, [])
            self.assertEqual(fake.calls, [({}, {}, {1})])
        run(go())

    def test_forget_drops_users_deleted_by_cleanup_without_queueing_delete(self):
        async def go():
            st = UserStats(7, clock=FakeClock())
            await st.load()
            st.touch_activity(1)
            self.assertEqual(st.forget([1, 777]), 1)
            self.assertFalse(st.has(1))
            self.assertFalse(st.dirty())                      # DELETE в БД уже выполнила автоочистка
            self.assertTrue(st.add(1, "again"))               # вернулся — проходит капчу как новый
        run(go())


if __name__ == "__main__":
    unittest.main()
