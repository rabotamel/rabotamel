"""Проверка SQL автоочистки на реальной СУБД (sqlite3 из стандартной библиотеки).

Запрос из app/cleanup_sql.py написан в общем подмножестве SQL, поэтому исполняется и в SQLite, и в PostgreSQL.
Здесь проверяются правила: что удаляется, что защищено, как работают батчи и мультибот. Версия для настоящего
PostgreSQL — в tests/test_cleanup_pg.py (запускается только при заданном TEST_DATABASE_URL)."""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.cleanup_sql import build_delete_inactive_sql  # noqa: E402

OPEN = ("creating", "active", "review", "paid")
SQL = build_delete_inactive_sql(OPEN, "pending")

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=7)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def days_ago(n: float) -> str:
    return iso(NOW - timedelta(days=n))


@unittest.skipIf(sqlite3.sqlite_version_info < (3, 35), "нужен RETURNING (SQLite 3.35+)")
class DeleteInactiveSqlTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE bot_users (bot_id INTEGER, user_id INTEGER, candies INTEGER NOT NULL DEFAULT 0,
                                    last_active_at TEXT NOT NULL, PRIMARY KEY (bot_id, user_id));
            CREATE INDEX idx_users_last_active_at ON bot_users (last_active_at);
            CREATE TABLE topup_orders (id INTEGER PRIMARY KEY, bot_id INTEGER, user_id INTEGER, status TEXT);
            CREATE TABLE video_submissions (id INTEGER PRIMARY KEY, bot_id INTEGER, user_id INTEGER, status TEXT);
            CREATE TABLE user_premium (user_id INTEGER PRIMARY KEY, premium_until TEXT, premium_forever INTEGER);
            """
        )

    def user(self, bot, uid, ago, candies=0):
        self.db.execute("INSERT INTO bot_users VALUES (?,?,?,?)", (bot, uid, candies, days_ago(ago)))

    def run_sql(self, batch=1000):
        cur = self.db.execute(SQL, {"cutoff": iso(CUTOFF), "now": iso(NOW), "batch": batch})
        return sorted(cur.fetchall())

    def left(self):
        return sorted(self.db.execute("SELECT bot_id, user_id FROM bot_users").fetchall())

    # --- 2) удаление неактивных более 7 дней ---
    def test_deletes_only_users_inactive_more_than_7_days(self):
        self.user(1, 1, 8)
        self.user(1, 2, 30)
        self.user(1, 3, 6.9)        # ещё не 7 суток
        self.user(1, 4, 0.1)
        deleted = self.run_sql()
        self.assertEqual(deleted, [(1, 1), (1, 2)])
        self.assertEqual(self.left(), [(1, 3), (1, 4)])

    def test_nothing_to_delete_returns_empty(self):
        self.user(1, 1, 1)
        self.assertEqual(self.run_sql(), [])
        self.assertEqual(self.left(), [(1, 1)])

    # --- 3) сохранение пользователей с балансом, заявками и т. п. ---
    def test_keeps_user_with_positive_balance(self):
        self.user(1, 1, 90, candies=5)
        self.user(1, 2, 90, candies=0)
        self.assertEqual(self.run_sql(), [(1, 2)])
        self.assertEqual(self.left(), [(1, 1)])

    def test_keeps_user_with_unfinished_topup_order(self):
        for i, status in enumerate(OPEN, start=1):
            self.user(1, i, 40)
            self.db.execute("INSERT INTO topup_orders (bot_id, user_id, status) VALUES (1, ?, ?)", (i, status))
        self.assertEqual(self.run_sql(), [])
        self.assertEqual(len(self.left()), len(OPEN))

    def test_finished_topup_orders_do_not_protect(self):
        for i, status in enumerate(("credited", "canceled", "rejected", "failed", "expired"), start=1):
            self.user(1, i, 40)
            self.db.execute("INSERT INTO topup_orders (bot_id, user_id, status) VALUES (1, ?, ?)", (i, status))
        self.assertEqual(len(self.run_sql()), 5)

    def test_keeps_user_with_pending_submission_but_not_with_decided_one(self):
        self.user(1, 1, 40)
        self.user(1, 2, 40)
        self.user(1, 3, 40)
        self.db.execute("INSERT INTO video_submissions (bot_id, user_id, status) VALUES (1, 1, 'pending')")
        self.db.execute("INSERT INTO video_submissions (bot_id, user_id, status) VALUES (1, 2, 'approved')")
        self.assertEqual(self.run_sql(), [(1, 2), (1, 3)])
        self.assertEqual(self.left(), [(1, 1)])

    def test_keeps_users_with_active_premium(self):
        for uid in (1, 2, 3, 4):
            self.user(1, uid, 40)
        self.db.execute("INSERT INTO user_premium VALUES (1, NULL, 1)")                           # навсегда
        self.db.execute("INSERT INTO user_premium VALUES (2, ?, 0)", (iso(NOW + timedelta(days=3)),))   # действует
        self.db.execute("INSERT INTO user_premium VALUES (3, ?, 0)", (iso(NOW - timedelta(days=3)),))   # истёк
        self.assertEqual(self.run_sql(), [(1, 3), (1, 4)])
        self.assertEqual(self.left(), [(1, 1), (1, 2)])

    # --- 4) мультибот ---
    def test_multibot_rows_are_independent(self):
        self.user(1, 10, 40)               # тот же человек у двух ботов: у первого давно не заходил,
        self.user(2, 10, 0.5)              # у второго активен
        self.user(2, 20, 40, candies=3)
        self.user(3, 30, 40)
        self.db.execute("INSERT INTO topup_orders (bot_id, user_id, status) VALUES (2, 30, 'active')")  # чужой бот
        self.assertEqual(self.run_sql(), [(1, 10), (3, 30)])   # заказ у бота 2 не защищает строку бота 3
        self.assertEqual(self.left(), [(2, 10), (2, 20)])

    def test_topup_of_other_bot_protects_only_own_row(self):
        self.user(1, 10, 40)
        self.user(2, 10, 40)
        self.db.execute("INSERT INTO topup_orders (bot_id, user_id, status) VALUES (2, 10, 'review')")
        self.assertEqual(self.run_sql(), [(1, 10)])

    # --- батчи ---
    def test_batches_delete_oldest_first_and_stop_when_done(self):
        for uid in range(1, 26):
            self.user(1, uid, 10 + uid)           # чем больше uid, тем давнее активность
        self.user(1, 99, 1)
        first = self.run_sql(batch=10)
        self.assertEqual(len(first), 10)
        self.assertEqual({u for _, u in first}, set(range(16, 26)))   # сначала самые давние
        self.assertEqual(len(self.run_sql(batch=10)), 10)
        last = self.run_sql(batch=10)
        self.assertEqual(len(last), 5)            # меньше размера пачки — цикл очистки на этом заканчивается
        self.assertEqual(self.run_sql(batch=10), [])
        self.assertEqual(self.left(), [(1, 99)])

    def test_protected_users_do_not_block_batches(self):
        for uid in range(1, 6):
            self.user(1, uid, 100, candies=1)     # самые давние, но с балансом
        for uid in range(10, 14):
            self.user(1, uid, 20)
        self.assertEqual(len(self.run_sql(batch=3)), 3)
        self.assertEqual(len(self.run_sql(batch=3)), 1)
        self.assertEqual(len(self.left()), 5)


if __name__ == "__main__":
    unittest.main()
