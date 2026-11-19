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

    async def load_users(self, bot_id):
        return {1: "old", 2: None}

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


if __name__ == "__main__":
    unittest.main()
