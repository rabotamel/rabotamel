"""Скидка (персональный промокод) и очередь автосообщения должны исчезать вместе с пользователем."""
import asyncio
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.cleanup_sql import ORPHAN_SQL, PER_USER_TABLES  # noqa: E402

try:
    import sqlalchemy  # noqa: F401
    HAVE_SA = True
except ImportError:
    HAVE_SA = False


class OrphanSqlTests(unittest.TestCase):
    """Разовая зачистка при запуске (в общем подмножестве SQL — проверяем на sqlite3)."""

    def test_only_rows_of_missing_users_are_removed(self):
        con = sqlite3.connect(":memory:")
        con.executescript(
            "CREATE TABLE bot_users (bot_id INT, user_id INT);"
            "CREATE TABLE user_promos (bot_id INT, user_id INT, code TEXT);"
            "CREATE TABLE auto_message_queue (bot_id INT, user_id INT);"
            "INSERT INTO bot_users VALUES (1, 10), (2, 10);"
            "INSERT INTO user_promos VALUES (1, 10, 'A'), (1, 11, 'B'), (2, 10, 'C'), (2, 12, 'D');"
            "INSERT INTO auto_message_queue VALUES (1, 10), (1, 11);"
        )
        for sql in ORPHAN_SQL:
            con.execute(sql)
        self.assertEqual(con.execute("SELECT code FROM user_promos ORDER BY code").fetchall(), [("A",), ("C",)])
        self.assertEqual(con.execute("SELECT user_id FROM auto_message_queue").fetchall(), [(10,)])
        self.assertEqual(len(ORPHAN_SQL), len(PER_USER_TABLES))


class FakeSession:
    def __init__(self, log, rows=()):
        self.log, self.rows = log, rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return self

    async def execute(self, stmt, params=None):
        self.log.append(str(stmt))
        rows = self.rows

        class R:
            def all(self_inner):
                return rows
        return R()


@unittest.skipUnless(HAVE_SA, "нужен sqlalchemy")
class DeletionPathsTests(unittest.TestCase):
    def setUp(self):
        from app import db, repo
        self.db, self.repo = db, repo
        self.log = []
        self.saved = db.SessionLocal
        db.SessionLocal = lambda: FakeSession(self.log, rows=[(1, 10), (1, 11), (2, 12)])

    def tearDown(self):
        self.db.SessionLocal = self.saved

    def test_save_users_remove_drops_promo_and_queue(self):
        asyncio.run(self.repo.save_users(1, {}, {}, {10}))
        text = "\n".join(self.log)
        self.assertIn("DELETE FROM bot_users", text)
        self.assertIn("DELETE FROM user_promos", text)
        self.assertIn("DELETE FROM auto_message_queue", text)

    def test_inactive_cleanup_drops_promo_and_queue_for_deleted_rows(self):
        rows = asyncio.run(self.repo.delete_inactive_users(None, 100))
        self.assertEqual(rows, [(1, 10), (1, 11), (2, 12)])
        text = "\n".join(self.log)
        self.assertEqual(text.count("DELETE FROM user_promos"), 2)        # по разу на каждого бота
        self.assertEqual(text.count("DELETE FROM auto_message_queue"), 2)

    def test_premium_and_history_are_not_touched(self):
        asyncio.run(self.repo.save_users(1, {}, {}, {10}))
        text = "\n".join(self.log)
        for table in ("user_premium", "user_history", "referrals", "task_completions", "topup_orders"):
            self.assertNotIn(f"DELETE FROM {table}", text)


if __name__ == "__main__":
    unittest.main()
