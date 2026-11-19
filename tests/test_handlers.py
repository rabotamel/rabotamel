"""Проверка удаления прошлого приветствия при повторном /start (aiogram подменяется заглушками)."""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _install_stubs():
    try:
        import aiogram  # noqa: F401
        from aiogram import Router  # noqa: F401
        return
    except ImportError:
        pass

    class Any_:
        def __getattr__(self, name):
            return Any_()

        def __call__(self, *a, **k):
            return Any_()

        def __eq__(self, other):
            return Any_()

        __hash__ = object.__hash__

        def __and__(self, other):
            return Any_()

    class TelegramAPIError(Exception):
        pass

    class TelegramBadRequest(TelegramAPIError):
        pass

    class TelegramForbiddenError(TelegramAPIError):
        pass

    class Router:
        def __init__(self):
            self.handlers = {}

        def _reg(self, kind):
            def deco(*a, **k):
                def wrap(fn):
                    self.handlers[fn.__name__] = fn
                    return fn
                return wrap
            return deco

        def __getattr__(self, name):
            if name in ("message", "callback_query"):
                return self._reg(name)
            raise AttributeError(name)

    mods = {}
    for name in ("aiogram", "aiogram.exceptions", "aiogram.filters", "aiogram.types"):
        mods[name] = types.ModuleType(name)
    mods["aiogram"].Bot, mods["aiogram"].F, mods["aiogram"].Router = Any_, Any_(), Router
    mods["aiogram.exceptions"].TelegramAPIError = TelegramAPIError
    mods["aiogram.exceptions"].TelegramBadRequest = TelegramBadRequest
    mods["aiogram.exceptions"].TelegramForbiddenError = TelegramForbiddenError
    mods["aiogram.filters"].CommandStart = lambda *a, **k: None
    mods["aiogram.types"].CallbackQuery = mods["aiogram.types"].Message = Any_

    class BufferedInputFile:
        def __init__(self, file, filename=None):
            self.file, self.filename = file, filename

    mods["aiogram.types"].BufferedInputFile = BufferedInputFile
    mods["aiogram"].exceptions, mods["aiogram"].filters, mods["aiogram"].types = (
        mods["aiogram.exceptions"], mods["aiogram.filters"], mods["aiogram.types"])
    sys.modules.update(mods)


_install_stubs()


def _import_handlers():
    """Импорт app.handlers; если нет sqlalchemy, на время импорта подставляем заглушки app.repo/models/stats
    и убираем их из sys.modules, чтобы не мешать другим тестам."""
    try:
        import sqlalchemy  # noqa: F401
        from app import handlers as h
        return h
    except ImportError:
        pass
    import app

    names = ("repo", "models", "stats", "handlers")
    saved = {n: sys.modules.get("app." + n) for n in names}
    for n, attrs in (("repo", {}), ("models", {"BotProfile": object}), ("stats", {"UserStats": object})):
        m = types.ModuleType("app." + n)
        m.__dict__.update(attrs)
        sys.modules["app." + n] = m
        setattr(app, n, m)
    sys.modules.pop("app.handlers", None)
    try:
        from app import handlers as h
    finally:
        for n in names:
            sys.modules.pop("app." + n, None)
            if hasattr(app, n):
                delattr(app, n)
            if saved[n] is not None:
                sys.modules["app." + n] = saved[n]
                setattr(app, n, saved[n])
    return h


handlers = _import_handlers()
from app import messaging  # noqa: E402


class FakeBot:
    def __init__(self):
        self.next_id = 100
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class FakeStats:
    bot_id = 1

    def __init__(self, known):
        self.known = set(known)

    def has(self, uid):
        return uid in self.known

    def touch(self, *a):
        pass


class StartDeletesPreviousTests(unittest.TestCase):
    def test_repeated_start_deletes_previous_greeting(self):
        bot = FakeBot()
        sent = []

        async def fake_send(b, chat_id, text, image=None):
            b.next_id += 1
            sent.append(b.next_id)
            return types.SimpleNamespace(message_id=b.next_id)

        async def fake_candies(bot_id, uid):
            return 0

        orig_send, orig_candies = messaging.send_message, getattr(handlers.repo, "get_candies", None)
        messaging.send_message = fake_send
        handlers.repo.get_candies = fake_candies
        try:
            profile = types.SimpleNamespace(name="Бот", message_image_url=None)
            from app import greeting
            texts = types.SimpleNamespace(greeting=greeting.generate_config("t"), version=1)
            router = handlers.build_router(FakeStats({5}), texts, profile)
            on_start = router.handlers["on_start"]
            msg = types.SimpleNamespace(from_user=types.SimpleNamespace(id=5, username="u"))

            async def go():
                await on_start(msg, bot)   # первое приветствие — удалять нечего
                self.assertEqual(bot.deleted, [])
                await on_start(msg, bot)   # второй /start — первое приветствие удалено
                self.assertEqual(bot.deleted, [sent[0]])
                await on_start(msg, bot)
                self.assertEqual(bot.deleted, [sent[0], sent[1]])

            asyncio.run(go())
        finally:
            messaging.send_message = orig_send
            if orig_candies is not None:
                handlers.repo.get_candies = orig_candies


if __name__ == "__main__":
    unittest.main()
