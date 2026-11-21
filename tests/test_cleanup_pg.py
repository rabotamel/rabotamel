"""Интеграционная проверка миграции и очистки на настоящем PostgreSQL (например, пустая ветка Neon).

Запускается ТОЛЬКО если заданы TEST_DATABASE_URL и установлены sqlalchemy + asyncpg; иначе пропускается.
ВНИМАНИЕ: init_db создаёт таблицы приложения в указанной базе — используйте тестовую БД, не боевую.
    TEST_DATABASE_URL=postgresql://user:pass@host/db python -m unittest tests.test_cleanup_pg -v
"""
import asyncio
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

URL = os.environ.get("TEST_DATABASE_URL")
try:
    import asyncpg  # noqa: F401
    import sqlalchemy  # noqa: F401
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False


@unittest.skipUnless(URL and HAVE_DEPS, "нужны TEST_DATABASE_URL, sqlalchemy и asyncpg")
class PostgresCleanupTests(unittest.TestCase):
    def test_migration_is_idempotent_and_cleanup_rules_hold(self):
        from sqlalchemy import text

        from app import db, repo

        async def go():
            await db.init_db(URL)
            await db.close_db()
            await db.init_db(URL)       # повторное применение миграции ничего не ломает
            try:
                now = datetime.now(timezone.utc)
                token = f"test:{uuid.uuid4().hex}"
                async with db.SessionLocal() as s, s.begin():
                    idx = (await s.execute(text(
                        "SELECT 1 FROM pg_indexes WHERE tablename='bot_users' AND indexname='idx_users_last_active_at'"
                    ))).first()
                    self.assertIsNotNone(idx)
                    bot_id = (await s.execute(
                        text("INSERT INTO bot_profiles (name, token) VALUES ('t', :t) RETURNING id"), {"t": token}
                    )).scalar_one()
                    rows = [  # (user_id, дней назад, candies)
                        (1, 8, 0), (2, 30, 0), (3, 6, 0), (4, 40, 5), (5, 40, 0),
                    ]
                    for uid, ago, candies in rows:
                        await s.execute(
                            text("INSERT INTO bot_users (bot_id, user_id, candies, last_active_at) "
                                 "VALUES (:b, :u, :c, :t)"),
                            {"b": bot_id, "u": uid, "c": candies, "t": now - timedelta(days=ago)},
                        )
                    await s.execute(text(  # у пользователя 5 незавершённый заказ пополнения
                        "INSERT INTO topup_orders (bot_id, user_id, candies, method, currency, fiat_amount, status) "
                        "VALUES (:b, 5, 10, 'card', 'RUB', 100, 'review')"), {"b": bot_id})
                try:
                    deleted = await repo.delete_inactive_users(now - timedelta(days=7), 1000)
                    mine = sorted(u for b, u in deleted if b == bot_id)
                    self.assertEqual(mine, [1, 2])
                    async with db.SessionLocal() as s:
                        left = sorted((await s.execute(
                            text("SELECT user_id FROM bot_users WHERE bot_id = :b"), {"b": bot_id})).scalars())
                    self.assertEqual(left, [3, 4, 5])
                finally:
                    async with db.SessionLocal() as s, s.begin():
                        await s.execute(text("DELETE FROM topup_orders WHERE bot_id = :b"), {"b": bot_id})
                        await s.execute(text("DELETE FROM bot_profiles WHERE id = :b"), {"b": bot_id})
            finally:
                await db.close_db()

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
