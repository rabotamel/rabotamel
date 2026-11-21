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

        self.orig = (getattr(sandbox.repo, "set_author_block", None), getattr(sandbox.repo, "set_receipt_block", None))
        sandbox.repo.set_author_block, sandbox.repo.set_receipt_block = setter("suggest"), setter("receipts")

    def tearDown(self):
        for name, value in zip(("set_author_block", "set_receipt_block"), self.orig):
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


if __name__ == "__main__":
    unittest.main()
