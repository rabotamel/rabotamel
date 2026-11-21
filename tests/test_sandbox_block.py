"""«Песочница» -> блокировка: разбор цели, область (Предложка / Отправка чеков), блокировка и разблокировка."""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import test_handlers  # noqa: E402,F401  (ставит заглушки aiogram)
NS = types.SimpleNamespace


def _import_sandbox():
    """Импорт app.sandbox; без sqlalchemy на время импорта подставляем заглушки тяжёлых модулей (БД, менеджер ботов)."""
    try:
        import sqlalchemy  # noqa: F401
        from app import sandbox as sb
        return sb
    except ImportError:
        pass
    import app

    stubs = {"repo": {}, "manager": {"BotManager": object}, "video_pool": {}, "stats": {"norm_username": lambda u: u}}
    saved = {n: sys.modules.get("app." + n) for n in (*stubs, "sandbox")}
    for n, attrs in stubs.items():
        m = types.ModuleType("app." + n)
        m.__dict__.update(attrs)
        sys.modules["app." + n] = m
        setattr(app, n, m)
    sys.modules.pop("app.sandbox", None)
    try:
        from app import sandbox as sb
    finally:
        for n in (*stubs, "sandbox"):
            sys.modules.pop("app." + n, None)
            if hasattr(app, n) and n != "sandbox":
                delattr(app, n)
            if saved[n] is not None:
                sys.modules["app." + n] = saved[n]
                setattr(app, n, saved[n])
    return sb


sandbox = _import_sandbox()


class Mgr:
    def __init__(self, running=True):
        self._running, self.bot = running, object()
        self.stats = NS(username_of=lambda uid: "bob" if uid == 777 else None, has=lambda uid: uid == 777,
                        find_by_username=lambda n: 777 if n == "bobby" else None)

    def is_running(self):
        return self._running


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def setter(name):
            async def f(uid, blocked, by=None):
                self.calls.append((name, uid, blocked))
                return True
            return f

        names = ("set_author_block", "set_receipt_block", "set_task_block")
        self.orig = tuple(getattr(sandbox.repo, n, None) for n in names)
        sandbox.repo.set_author_block, sandbox.repo.set_receipt_block, sandbox.repo.set_task_block = (
            setter("suggest"), setter("receipts"), setter("tasks"))

    def tearDown(self):
        for name, value in zip(("set_author_block", "set_receipt_block", "set_task_block"), self.orig):
            if value is None:
                delattr(sandbox.repo, name)
            else:
                setattr(sandbox.repo, name, value)

    def run_(self, coro):
        return asyncio.run(coro)

    def test_scopes_and_actions(self):
        m = Mgr()
        r = self.run_(sandbox.set_block(m, "@bobby", "suggest", True, True))
        self.assertEqual((r["user_id"], r["blocked"], r["changed"], r["who"]), (777, True, True, "@bob (777)"))
        self.run_(sandbox.set_block(m, "123456", "receipts", True, True))
        self.run_(sandbox.set_block(m, "t.me/bobby", "receipts", False, True))
        self.assertEqual(self.calls, [("suggest", 777, True), ("receipts", 123456, True), ("receipts", 777, False)])

    def test_tasks_scope_is_separate_from_receipts(self):
        m = Mgr()
        r = self.run_(sandbox.set_block(m, "@bobby", "tasks", True, True))
        self.assertEqual((r["scope"], r["blocked"], r["user_id"]), ("tasks", True, 777))
        self.run_(sandbox.set_block(m, "777", "tasks", False, True))
        self.assertEqual(self.calls, [("tasks", 777, True), ("tasks", 777, False)])   # чеки и предложка не затронуты
        self.assertEqual(sandbox.BLOCK_SCOPES, ("suggest", "receipts", "tasks"))

    def test_unknown_id_is_allowed_unknown_username_is_not(self):
        m = Mgr(running=False)
        self.assertEqual(self.run_(sandbox.set_block(m, "555555", "suggest", True, True))["who"], "555555")
        with self.assertRaises(sandbox.SandboxError) as e:
            self.run_(sandbox.set_block(m, "@nobody1", "suggest", True, True))
        self.assertEqual(e.exception.status, 409)   # юзернейм ищется среди пользователей запущенного бота

    def test_validation(self):
        m = Mgr()
        for args, status in ((("@bobby", "suggest", True, False), 400), (("@bobby", "oops", True, True), 400),
                             (("not a user!", "suggest", True, True), 400), (("@nobody1", "suggest", True, True), 404)):
            with self.assertRaises(sandbox.SandboxError) as e:
                self.run_(sandbox.set_block(m, *args))
            self.assertEqual(e.exception.status, status, args)
        self.assertEqual(self.calls, [])


class AutoMessageTestButtonTests(unittest.TestCase):
    """Вкладка «Автосообщения» -> «Проверить сообщение»: отправка сохранённого автосообщения конкретному пользователю."""

    def setUp(self):
        self.sent, self.removed, self.flushed = [], [], []
        self.error = None

        async def send_test(uid):
            if self.error is not None:
                raise self.error
            self.sent.append(uid)
            return {"promo": "ABCD1234", "percent": 15}

        async def flush():
            self.flushed.append(True)

        self.mgr = Mgr()
        self.mgr.stats.bot_id = 1
        self.mgr.stats.remove = self.removed.append
        self.mgr.stats.flush = flush
        self.boosted = []

        async def send_boost_test(uid):
            if self.error is not None:
                raise self.error
            self.boosted.append(uid)
            return {"hours": 24, "factor": 2}

        self.mgr.automsg = NS(send_test=send_test, send_boost_test=send_boost_test)

    def run_(self, *args):
        return asyncio.run(sandbox.test_automessage(self.mgr, *args))

    def test_sends_to_resolved_user(self):
        r = self.run_("@bobby", True)
        self.assertEqual((r["ok"], r["user_id"], r["who"], r["promo"], r["percent"]), (True, 777, "@bob (777)", "ABCD1234", 15))
        self.run_("777", True)
        self.assertEqual(self.sent, [777, 777])

    def test_kind_selects_what_to_check(self):
        r = self.run_("777", True, "boost")
        self.assertEqual((r["kind"], r["hours"], r["factor"]), ("boost", 24, 2))
        self.assertEqual((self.boosted, self.sent), ([777], []))          # скидка при этом не проверялась
        r = self.run_("777", True, "discount")
        self.assertEqual((r["kind"], r["promo"]), ("discount", "ABCD1234"))
        self.assertEqual((self.boosted, self.sent), ([777], [777]))
        self.assertEqual(self.run_("777", True)["kind"], "discount")      # по умолчанию — скидка, как раньше
        with self.assertRaises(sandbox.SandboxError) as e:
            self.run_("777", True, "other")
        self.assertEqual(e.exception.status, 400)

    def test_needs_confirm_valid_target_known_user_and_running_bot(self):
        for args, status in ((("@bobby", False), 400), (("not a user!", True), 400), (("@nobody1", True), 404),
                             (("555555", True), 404)):       # как у остальных действий песочницы: только прошедшие капчу
            with self.assertRaises(sandbox.SandboxError) as e:
                self.run_(*args)
            self.assertEqual(e.exception.status, status, args)
        self.mgr._running = False
        with self.assertRaises(sandbox.SandboxError) as e:
            self.run_("777", True)
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.sent, [])

    def test_disabled_message_is_reported_as_error(self):
        self.error = ValueError("Автосообщение выключено")
        with self.assertRaises(sandbox.SandboxError) as e:
            self.run_("777", True)
        self.assertEqual((e.exception.status, e.exception.message), (400, "Автосообщение выключено"))

    def test_user_who_blocked_the_bot_is_removed_like_in_candies(self):
        from aiogram.exceptions import TelegramForbiddenError

        self.error = TelegramForbiddenError("Forbidden: bot was blocked by the user")
        with self.assertRaises(sandbox.SandboxError) as e:
            self.run_("777", True)
        self.assertEqual(e.exception.status, 409)
        self.assertEqual((self.removed, self.flushed), ([777], [True]))
        self.removed.clear()
        self.error = TelegramForbiddenError("Forbidden: user is deactivated")   # другие 403 пользователя не удаляют
        with self.assertRaises(sandbox.SandboxError):
            self.run_("777", True)
        self.assertEqual(self.removed, [])


if __name__ == "__main__":
    unittest.main()
