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
            if name in ("message", "callback_query", "my_chat_member"):
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

    class _Simple:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mods["aiogram.types"].InlineKeyboardButton = _Simple
    mods["aiogram.types"].InlineKeyboardMarkup = _Simple
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
from app import deco, gallery, messaging, shop, shop_buttons, suggest_ui  # noqa: E402


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

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
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
            texts = types.SimpleNamespace(
                greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
                deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
                suggest=suggest_ui.generate_config("t"), version=1)
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


class GalleryNavigationTests(unittest.TestCase):
    def test_gallery_and_back_replace_messages(self):
        from app import greeting

        bot = FakeBot()
        sent = []

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            b.next_id += 1
            sent.append((b.next_id, text, reply_markup))
            return types.SimpleNamespace(message_id=b.next_id)

        async def fake_candies(bot_id, uid):
            return 7

        orig_send, orig_candies = messaging.send_message, getattr(handlers.repo, "get_candies", None)
        messaging.send_message = fake_send
        handlers.repo.get_candies = fake_candies
        try:
            profile = types.SimpleNamespace(name="Бот", message_image_url=None)
            texts = types.SimpleNamespace(
                greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
                deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
                suggest=suggest_ui.generate_config("t"), version=1)
            router = handlers.build_router(FakeStats({5}), texts, profile)

            def call(data, msg_id):
                async def answer(*a, **k):
                    pass
                return types.SimpleNamespace(
                    data=data, answer=answer, from_user=types.SimpleNamespace(id=5, username="u"),
                    message=types.SimpleNamespace(message_id=msg_id, chat=types.SimpleNamespace(id=5)))

            async def go():
                await router.handlers["on_shop_button"](call(texts.shop_buttons.gallery_cb, 10), bot)
                self.assertEqual(bot.deleted, [10])                 # приветствие удалено
                gid, text, _ = sent[-1]                             # и отправлена галерея
                self.assertIn(f"<b>{texts.shop_buttons.gallery_heading}</b>", text)
                self.assertTrue(text.endswith(f"7{greeting.CANDY}"))
                await router.handlers["on_gallery_button"](call(texts.gallery.back_cb, gid), bot)
                self.assertEqual(bot.deleted, [10, gid])            # галерея удалена
                self.assertIn("Бот", sent[-1][1])  # заново приветствие
                # чужой/устаревший callback ничего не делает
                n = len(sent)
                await router.handlers["on_gallery_button"](call("gk:foreign000", 99), bot)
                self.assertEqual(len(sent), n)

            asyncio.run(go())
        finally:
            messaging.send_message = orig_send
            if orig_candies is not None:
                handlers.repo.get_candies = orig_candies


class ShopNavigationTests(unittest.TestCase):
    """Магазин: приветствие -> меню -> карточка товара -> назад -> назад; всё в одном сообщении (обновление на месте)."""

    def _setup(self, known=(5,), photo=False, products=None):
        from app import greeting

        edits, sent = [], []
        # каталог в памяти вместо БД: id, name, price, about, color, is_visible
        self.catalog = list(products if products is not None else [
            types.SimpleNamespace(id=1, name="Абсолют", price=6789, about="**Мощный** набор", color="danger", is_visible=True),
        ])

        class Bot(FakeBot):
            async def edit_message_text(self, text, chat_id, message_id, parse_mode, reply_markup):
                edits.append(("text", message_id, text, reply_markup))

            async def edit_message_caption(self, chat_id, message_id, caption, parse_mode, reply_markup):
                edits.append(("caption", message_id, caption, reply_markup))

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            b.next_id += 1
            sent.append((b.next_id, text))
            return types.SimpleNamespace(message_id=b.next_id)

        async def fake_candies(bot_id, uid):
            return 42

        async def fake_list(only_visible=True, limit=None):
            rows = [r for r in self.catalog if r.is_visible or not only_visible]
            return rows[:limit] if limit else rows

        async def fake_get(product_id, only_visible=True):
            return next((r for r in self.catalog if r.id == product_id and (r.is_visible or not only_visible)), None)

        names = ("get_candies", "list_shop_products", "get_shop_product")
        orig = {n: getattr(handlers.repo, n, None) for n in names}
        orig_send = messaging.send_message
        messaging.send_message = fake_send
        handlers.repo.get_candies, handlers.repo.list_shop_products, handlers.repo.get_shop_product = (
            fake_candies, fake_list, fake_get)

        def restore():
            messaging.send_message = orig_send
            for n, v in orig.items():
                if v is not None:
                    setattr(handlers.repo, n, v)
                elif hasattr(handlers.repo, n):
                    delattr(handlers.repo, n)

        self.addCleanup(restore)
        profile = types.SimpleNamespace(name="Бот", message_image_url=None)
        texts = types.SimpleNamespace(
            greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
            deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
            suggest=suggest_ui.generate_config("t"), shop_menu=shop.generate_config("t"), version=1)
        router = handlers.build_router(FakeStats(known), texts, profile)

        def call(data, msg_id=10):
            async def answer(*a, **k):
                pass
            return types.SimpleNamespace(
                data=data, answer=answer, from_user=types.SimpleNamespace(id=5, username="u"),
                message=types.SimpleNamespace(message_id=msg_id, chat=types.SimpleNamespace(id=5),
                                              photo=[object()] if photo else None))

        return Bot(), router, texts, call, edits, sent

    @staticmethod
    def _buttons(markup):
        return [b for row in markup.inline_keyboard for b in row]

    def test_full_navigation_edits_one_message(self):
        rows = [
            types.SimpleNamespace(id=7, name="Абсолют", price=6789, about="**Мощный** набор", color="danger", is_visible=True),
            types.SimpleNamespace(id=3, name="Классика", price=50, about="", color="", is_visible=True),
            types.SimpleNamespace(id=9, name="Скрытый", price=1, about="", color="", is_visible=False),
            types.SimpleNamespace(id=4, name="Зелёный", price=120, about="", color="success", is_visible=True),
        ]
        bot, router, texts, call, edits, sent = self._setup(products=rows)
        cfg = texts.shop_menu

        async def go():
            await router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot)
            kind, mid, text, markup = edits[-1]
            self.assertEqual((kind, mid), ("text", 10))
            self.assertEqual(text, shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertTrue(text.startswith(f"<b>{texts.shop_buttons.shop_label}</b>\n\n"))   # заголовок жирным
            self.assertIn("Ваш баланс: 42🍬", text)
            btns = self._buttons(markup)
            # порядок каталога, скрытый товар не показан, «Назад» последняя
            self.assertEqual([b.text for b in btns],
                             ["Абсолют — 6789🍬", "Классика — 50🍬", "Зелёный — 120🍬", "⬅️ Назад"])
            self.assertEqual([getattr(b, "style", None) for b in btns], ["danger", None, "success", "danger"])
            self.assertEqual([b.callback_data for b in btns[:3]], [f"{cfg.product_cb}:{i}" for i in (7, 3, 4)])

            await router.handlers["on_shop_menu_button"](call(btns[0].callback_data), bot)
            _, _, text, markup = edits[-1]
            self.assertEqual(text, shop.render_product(shop.product_from_row(rows[0])))
            self.assertEqual(text.split("\n")[0], "<b>📦 Товар:</b> Абсолют")
            self.assertIn("<b>Мощный</b> набор", text)
            self.assertNotIn("**", text)
            btns2 = self._buttons(markup)
            self.assertEqual([b.text for b in btns2], ["🍭 Купить (6789🍬)", "⬅️ Назад"])
            self.assertEqual([b.style for b in btns2], ["success", "danger"])

            n = len(edits)
            await router.handlers["on_shop_menu_button"](call(btns2[0].callback_data), bot)   # заглушка: ничего не делает
            self.assertEqual((len(edits), sent, bot.deleted), (n, [], []))

            await router.handlers["on_shop_menu_button"](call(cfg.product_back_cb), bot)
            self.assertEqual(edits[-1][2], shop.render_menu(texts.shop_buttons.shop_label, 42))
            await router.handlers["on_shop_menu_button"](call(cfg.back_cb), bot)
            self.assertIn("Бот", edits[-1][2])                       # главное меню — приветствие
            self.assertIn("42🍬", edits[-1][2])
            self.assertEqual(texts.shop_buttons.shop_label, edits[-1][3].inline_keyboard[0][0].text)
            self.assertEqual((sent, bot.deleted), ([], []))          # новых сообщений не было, ничего не удалялось

        asyncio.run(go())

    def test_empty_catalog_menu_opens_with_back_button_only(self):
        bot, router, texts, call, edits, sent = self._setup(products=[])
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
        _, _, text, markup = edits[-1]
        self.assertEqual(text, shop.render_menu(texts.shop_buttons.shop_label, 42))
        btns = self._buttons(markup)
        self.assertEqual([(b.text, b.callback_data, b.style) for b in btns],
                         [("⬅️ Назад", texts.shop_menu.back_cb, "danger")])

    def test_product_removed_or_hidden_or_old_button_shows_fresh_menu(self):
        bot, router, texts, call, edits, sent = self._setup()
        cfg = texts.shop_menu

        async def go():
            self.catalog[0].is_visible = False           # скрыт, пока пользователь смотрел меню
            await router.handlers["on_shop_menu_button"](call(f"{cfg.product_cb}:1"), bot)
            self.assertEqual(edits[-1][2], shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual([b.text for b in self._buttons(edits[-1][3])], ["⬅️ Назад"])
            await router.handlers["on_shop_menu_button"](call(f"{cfg.product_cb}:999"), bot)   # такого товара нет
            self.assertEqual(edits[-1][2], shop.render_menu(texts.shop_buttons.shop_label, 42))
            await router.handlers["on_shop_menu_button"](call(cfg.product_cb), bot)            # кнопка версии без id
            self.assertEqual(edits[-1][2], shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual((sent, bot.deleted), ([], []))

        asyncio.run(go())

    def test_photo_message_edits_caption(self):
        bot, router, texts, call, edits, _ = self._setup(photo=True)
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
        self.assertEqual(edits[-1][0], "caption")

    def test_foreign_stale_and_unverified_do_nothing(self):
        bot, router, texts, call, edits, sent = self._setup()
        other = shop.generate_config("other-bot")
        stranger_bot, stranger_router, _, _, stranger_edits, _ = self._setup(known=())

        async def go():
            for cb in (other.product_cb, other.back_cb, "sp:foreign000", "sk:"):
                await router.handlers["on_shop_menu_button"](call(cb), bot)
            await stranger_router.handlers["on_shop_menu_button"](call(texts.shop_menu.product_cb), stranger_bot)

        asyncio.run(go())
        self.assertEqual((edits, sent, stranger_edits), ([], [], []))

    def test_edit_failure_falls_back_to_new_message(self):
        bot, router, texts, call, edits, sent = self._setup()

        async def broken(*a, **k):
            raise handlers.TelegramAPIError("message to edit not found")

        bot.edit_message_text = broken
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb, 10), bot))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
        self.assertEqual(bot.deleted, [10])                          # старое сообщение убрано


class CaptchaTargetChangesTests(unittest.TestCase):
    def test_every_start_asks_for_another_fruit_and_answer_is_checked(self):
        from app import captcha, greeting

        sent = []

        class Bot(FakeBot):
            async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
                self.next_id += 1
                sent.append((text, reply_markup))
                return types.SimpleNamespace(message_id=self.next_id)

        profile = types.SimpleNamespace(name="Бот", message_image_url=None, token="1:SECRET")
        cfg = captcha.generate_config("t")
        texts = types.SimpleNamespace(
            captcha=cfg, greeting=greeting.generate_config("t"),
            shop_buttons=shop_buttons.generate_config("t"),
            deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
                suggest=suggest_ui.generate_config("t"), version=1)
        stats = FakeStats(set())
        stats.add = lambda uid, name=None: stats.known.add(uid) or True

        async def _flush():
            return None

        stats.flush = _flush
        router = handlers.build_router(stats, texts, profile)
        on_start = router.handlers["on_start"]
        on_answer = router.handlers["on_captcha_answer"]
        bot = Bot()
        msg = types.SimpleNamespace(from_user=types.SimpleNamespace(id=9, username="u"))
        orig_delay = handlers.CAPTCHA_MIN_INTERVAL
        handlers.CAPTCHA_MIN_INTERVAL = 0  # в тесте /start идут подряд, без паузы в секунду

        def asked(text):
            return next(k for k, f in captcha.FRUITS.items() if f"<b>{f.acc}</b>" in text)

        async def go():
            prev = None
            for _ in range(30):
                await on_start(msg, bot)
                text, markup = sent[-1]
                target = asked(text)
                self.assertIn(target, cfg.fruits)
                self.assertNotEqual(target, prev)  # с каждым /start — другой фрукт
                prev = target

            # верный ответ по кнопке последней капчи пропускает человека
            text, markup = sent[-1]
            buttons = [b for row in markup.inline_keyboard for b in row]
            by_fruit = {b.callback_data.split(":")[1]: b.callback_data for b in buttons}
            answered = []

            async def answer(*a, **k):
                answered.append(1)

            def call(data):
                m = types.SimpleNamespace(chat=types.SimpleNamespace(id=9), message_id=sent_ids[-1])
                return types.SimpleNamespace(
                    from_user=types.SimpleNamespace(id=9, username="u"), message=m, data=data, answer=answer)

            sent_ids = [bot.next_id]
            wrong = next(k for k in by_fruit if k != prev)
            n_before = len(sent)
            await on_answer(call(by_fruit[wrong]), bot)
            self.assertEqual(len(sent), n_before + 1)          # неверно -> капча показана снова
            self.assertEqual(asked(sent[-1][0]), prev)         # и задание то же, меняется только порядок
            self.assertFalse(stats.has(9))

            text, markup = sent[-1]
            by_fruit = {b.callback_data.split(":")[1]: b.callback_data
                        for row in markup.inline_keyboard for b in row}
            sent_ids[:] = [bot.next_id]
            await on_answer(call(by_fruit[prev]), bot)
            self.assertTrue(stats.has(9))                      # верно -> пропущен

        orig_send, orig_candies = messaging.send_message, getattr(handlers.repo, "get_candies", None)

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            b.next_id += 1
            return types.SimpleNamespace(message_id=b.next_id)

        messaging.send_message = fake_send
        try:
            asyncio.run(go())
        finally:
            handlers.CAPTCHA_MIN_INTERVAL = orig_delay
            messaging.send_message = orig_send


if __name__ == "__main__":
    unittest.main()
