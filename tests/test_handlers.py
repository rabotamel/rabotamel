"""Проверка удаления прошлого приветствия при повторном /start (aiogram подменяется заглушками)."""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

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
    mods["aiogram.types"].KeyboardButton = mods["aiogram.types"].ReplyKeyboardMarkup = _Simple
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
from app import deco, gallery, messaging, shop, shop_buttons, suggest_ui, tasks  # noqa: E402


class FakeBot:
    def __init__(self):
        self.next_id = 100
        self.deleted = []
        self.plain = []   # служебные сообщения «в лоб» через bot.send_message (клавиатура «Меню»)
        self.carrier_ids = []          # id этих служебных сообщений
        self.deleted_carriers = []     # какие из них уже удалены (удаляются сразу: «🍭» в чате не остаётся)

    async def delete_message(self, chat_id, message_id):
        if message_id in self.carrier_ids:
            self.deleted_carriers.append(message_id)   # в self.deleted — только удаления приветствий и меню
            return
        self.deleted.append(message_id)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.next_id += 1
        self.plain.append((chat_id, text, reply_markup))
        self.carrier_ids.append(self.next_id)
        return types.SimpleNamespace(message_id=self.next_id)


class FakeStats:
    bot_id = 1

    def __init__(self, known):
        self.known = set(known)

    def has(self, uid):
        return uid in self.known

    def touch(self, *a):
        pass

    def touch_activity(self, uid, force=False):
        self.__dict__.setdefault("activity", []).append(uid)   # у подклассов может быть свой __init__
        return True


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
                suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), version=1)
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


class MenuButtonTests(unittest.TestCase):
    """Постоянная кнопка «Меню» у клавиатуры и кнопка «Купить конфеты» под приветствием."""

    def _setup(self):
        from app import greeting

        self.bot, self.sent = FakeBot(), []

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            b.next_id += 1
            self.sent.append((b.next_id, text, reply_markup))
            return types.SimpleNamespace(message_id=b.next_id)

        async def fake_candies(bot_id, uid):
            return 12

        orig_send, orig_candies = messaging.send_message, getattr(handlers.repo, "get_candies", None)
        messaging.send_message = fake_send
        handlers.repo.get_candies = fake_candies

        def restore():
            messaging.send_message = orig_send
            if orig_candies is not None:
                handlers.repo.get_candies = orig_candies
        self.addCleanup(restore)
        profile = types.SimpleNamespace(name="Бот", message_image_url=None)
        self.texts = types.SimpleNamespace(
            greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
            deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
            suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), version=1)
        return handlers.build_router(FakeStats({5}), self.texts, profile)

    @staticmethod
    def _user(text=None):
        return types.SimpleNamespace(text=text, message_id=900, from_user=types.SimpleNamespace(id=5, username="u"))

    def test_start_gives_menu_keyboard_once(self):
        router = self._setup()

        async def go():
            await router.handlers["on_start"](self._user("/start"), self.bot)
            await router.handlers["on_start"](self._user("/start"), self.bot)
        asyncio.run(go())
        self.assertEqual(len(self.bot.plain), 1)                      # клавиатура постоянная — повторно не шлём
        chat, text, kb = self.bot.plain[0]
        self.assertEqual((chat, text), (5, handlers.MENU_KEYBOARD_CARRIER))
        self.assertEqual(kb.keyboard[0][0].text, "Меню")
        self.assertTrue(kb.resize_keyboard and kb.is_persistent)
        self.assertEqual(self.bot.deleted_carriers, self.bot.carrier_ids)   # служебное «🍭» сразу удалено, кнопка «Меню» осталась

    def test_menu_button_acts_like_start_and_removes_old_message(self):
        router = self._setup()

        async def go():
            await router.handlers["on_start"](self._user("/start"), self.bot)
            first = self.sent[-1][0]
            await router.handlers["on_private_message"](self._user("Меню"), self.bot)
            self.assertEqual(self.bot.deleted, [900, first])           # нажатие «Меню» и прежнее приветствие убраны
            self.assertIn("Бот", self.sent[-1][1])
            self.assertIn("12🍬", self.sent[-1][1])
        asyncio.run(go())

    def test_greeting_has_buy_candies_button_below_shop_and_gallery(self):
        router = self._setup()
        asyncio.run(router.handlers["on_start"](self._user("/start"), self.bot))
        rows = self.sent[-1][2].inline_keyboard
        # самая нижняя — «Бесплатные конфеты» (подпись и callback_data этого бота), над ней — «Купить конфеты»
        self.assertEqual(rows[-1][0].text, self.texts.tasks_menu.text("label"))
        self.assertEqual(rows[-1][0].callback_data, self.texts.tasks_menu.cb["open"])
        self.assertEqual(rows[-2][0].text, "🍭 Купить конфеты 🍬")
        self.assertTrue(rows[-2][0].callback_data.startswith("tp"))
        labels = [b.text for row in rows[:-2] for b in row]
        self.assertEqual(sorted(labels), sorted([self.texts.shop_buttons.shop_label, self.texts.shop_buttons.gallery_label]))

    def test_balance_line_uses_lollipop(self):
        from app import greeting
        self.assertTrue(greeting.render(greeting.generate_config("z"), "Бот", 3).splitlines()[-1].startswith("🍭 "))


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
                suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), version=1)
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
    """Магазин: приветствие -> меню -> карточка товара -> назад -> назад; каждый переход — новое сообщение, прежнее удаляется."""

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

            async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
                edits.append(("markup", message_id, None, reply_markup))

        self.sent_markup = []

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            b.next_id += 1
            sent.append((b.next_id, text))
            self.sent_markup.append(reply_markup)
            return types.SimpleNamespace(message_id=b.next_id)

        async def fake_candies(bot_id, uid):
            return 42

        async def fake_list(only_visible=True, limit=None):
            rows = [r for r in self.catalog if r.is_visible or not only_visible]
            return rows[:limit] if limit else rows

        async def fake_get(product_id, only_visible=True):
            return next((r for r in self.catalog if r.id == product_id and (r.is_visible or not only_visible)), None)

        self.discount = 0          # скидка пользователя по промокоду, % (0 — нет)
        self.discount_left = timedelta(hours=23, minutes=39, seconds=30)   # сколько ещё действует скидка
        self.purchases = []        # вызовы repo.buy_product

        async def fake_promo(bot_id, uid):
            if not self.discount:
                return 0, None
            return self.discount, datetime.now(timezone.utc) + self.discount_left

        async def fake_buy(bot_id, uid, product_id):
            self.purchases.append(product_id)
            return types.SimpleNamespace(status="gone", name="", price=0, balance=0, percent=0)

        names = ("get_candies", "list_shop_products", "get_shop_product", "get_promo_discount", "buy_product")
        orig = {n: getattr(handlers.repo, n, None) for n in names}
        orig_send = messaging.send_message
        messaging.send_message = fake_send
        handlers.repo.get_candies, handlers.repo.list_shop_products, handlers.repo.get_shop_product = (
            fake_candies, fake_list, fake_get)
        handlers.repo.get_promo_discount, handlers.repo.buy_product = fake_promo, fake_buy

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
            suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), shop_menu=shop.generate_config("t"), version=1)
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

    def test_full_navigation_sends_new_message_and_deletes_old(self):
        rows = [
            types.SimpleNamespace(id=7, name="Абсолют", price=6789, about="**Мощный** набор", color="danger", is_visible=True),
            types.SimpleNamespace(id=3, name="Классика", price=50, about="", color="", is_visible=True),
            types.SimpleNamespace(id=9, name="Скрытый", price=1, about="", color="", is_visible=False),
            types.SimpleNamespace(id=4, name="Зелёный", price=120, about="", color="success", is_visible=True),
        ]
        bot, router, texts, call, edits, sent = self._setup(products=rows)
        cfg = texts.shop_menu

        async def go():
            await router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb, 10), bot)
            menu_id, text = sent[-1]
            markup = self.sent_markup[-1]
            self.assertEqual(bot.deleted, [10])                       # приветствие удалено, меню — новое сообщение
            self.assertEqual(text, shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertTrue(text.startswith(f"<b>{texts.shop_buttons.shop_label}</b>\n\n"))   # заголовок жирным
            self.assertIn("Ваш баланс: 42🍬", text)
            btns = self._buttons(markup)
            # порядок каталога, скрытый товар не показан, «Назад» последняя
            self.assertEqual([b.text for b in btns],
                             ["Абсолют — 6789🍬", "Классика — 50🍬", "Зелёный — 120🍬", "⬅️ Назад"])
            self.assertEqual([getattr(b, "style", None) for b in btns], ["danger", None, "success", "danger"])
            self.assertEqual([b.callback_data for b in btns[:3]], [f"{cfg.product_cb}:{i}" for i in (7, 3, 4)])

            await router.handlers["on_shop_menu_button"](call(btns[0].callback_data, menu_id), bot)
            card_id, text = sent[-1]
            markup = self.sent_markup[-1]
            self.assertEqual(bot.deleted, [10, menu_id])
            self.assertEqual(text, shop.render_product(shop.product_from_row(rows[0])))
            self.assertEqual(text.split("\n")[0], "<b>📦 Товар:</b> Абсолют")
            self.assertIn("<b>Мощный</b> набор", text)
            self.assertNotIn("**", text)
            btns2 = self._buttons(markup)
            self.assertEqual([b.text for b in btns2], ["🍭 Купить (6789🍬)", "⬅️ Назад"])
            self.assertEqual([b.style for b in btns2], ["success", "danger"])

            n = len(sent)
            # «Купить»: покупкой занимается repo.buy_product (здесь он отвечает «товар скрыт» -> свежее меню магазина)
            await router.handlers["on_shop_menu_button"](call(btns2[0].callback_data, card_id), bot)
            self.assertEqual(self.purchases, [7])
            self.assertEqual(len(sent), n + 1)
            self.assertEqual(sent[-1][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual(bot.deleted, [10, menu_id, card_id])
            fresh_id = sent[-1][0]   # свежее меню после «Купить»; дальше «Назад» нажимаем уже на нём

            await router.handlers["on_shop_menu_button"](call(cfg.product_back_cb, fresh_id), bot)
            menu2_id, text = sent[-1]
            self.assertEqual(text, shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual(bot.deleted, [10, menu_id, card_id, fresh_id])
            await router.handlers["on_shop_menu_button"](call(cfg.back_cb, menu2_id), bot)
            _, text = sent[-1]
            self.assertIn("Бот", text)                                # главное меню — приветствие
            self.assertIn("42🍬", text)
            self.assertEqual(texts.shop_buttons.shop_label, self.sent_markup[-1].inline_keyboard[0][0].text)
            self.assertEqual(bot.deleted, [10, menu_id, card_id, fresh_id, menu2_id])
            self.assertEqual(edits, [])                               # ни одного редактирования на месте

        asyncio.run(go())

    def test_long_card_goes_in_several_messages_and_cleans_up(self):
        about = "".join(f"Строка описания номер {i} для проверки длинной карточки\n" for i in range(100))[:5000]
        rows = [types.SimpleNamespace(id=1, name="Большой", price=10, about=about, color="", is_visible=True)]
        bot, router, texts, call, edits, sent = self._setup(products=rows)
        cfg = texts.shop_menu
        parts = shop.render_product_parts(shop.product_from_row(rows[0]), "")
        self.assertGreaterEqual(len(parts), 2)

        async def go():
            await router.handlers["on_shop_menu_button"](call(f"{cfg.product_cb}:1", msg_id=10), bot)
            # все части — новые сообщения (меню удалено), кнопки — под последней
            self.assertEqual(len(sent), len(parts))
            self.assertEqual(sent[0][1][:20], parts[0][:20])
            self.assertEqual([t for _, t in sent][-1][:20], parts[-1][:20])
            self.assertTrue(all(m is None for m in self.sent_markup[:-1]))
            self.assertEqual([b.text for b in self._buttons(self.sent_markup[-1])], ["🍭 Купить (10🍬)", "⬅️ Назад"])
            last_id = sent[-1][0]
            self.assertEqual(bot.deleted, [10])
            # «Назад» с последней части: меню — новым сообщением, эта часть и верхние части удалены
            await router.handlers["on_shop_menu_button"](call(cfg.product_back_cb, msg_id=last_id), bot)
            self.assertEqual(sent[-1][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual(sorted(bot.deleted), sorted([10, last_id] + [i for i, _ in sent[:len(parts) - 1]]))
            self.assertEqual(edits, [])
        asyncio.run(go())

    def test_empty_catalog_menu_opens_with_back_button_only(self):
        bot, router, texts, call, edits, sent = self._setup(products=[])
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
        self.assertEqual(sent[-1][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
        btns = self._buttons(self.sent_markup[-1])
        self.assertEqual([(b.text, b.callback_data, b.style) for b in btns],
                         [("⬅️ Назад", texts.shop_menu.back_cb, "danger")])

    def test_product_removed_or_hidden_or_old_button_shows_fresh_menu(self):
        bot, router, texts, call, edits, sent = self._setup()
        cfg = texts.shop_menu

        async def go():
            self.catalog[0].is_visible = False           # скрыт, пока пользователь смотрел меню
            await router.handlers["on_shop_menu_button"](call(f"{cfg.product_cb}:1"), bot)
            self.assertEqual(sent[-1][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual([b.text for b in self._buttons(self.sent_markup[-1])], ["⬅️ Назад"])
            await router.handlers["on_shop_menu_button"](call(f"{cfg.product_cb}:999"), bot)   # такого товара нет
            self.assertEqual(sent[-1][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
            await router.handlers["on_shop_menu_button"](call(cfg.product_cb), bot)            # кнопка версии без id
            self.assertEqual(sent[-1][1], shop.render_menu(texts.shop_buttons.shop_label, 42))
            self.assertEqual((len(sent), edits), (3, []))

        asyncio.run(go())

    # ---------- промокод: скидка в меню/карточке, покупка, ввод кода ----------

    def test_discount_shows_struck_price_buttons_and_promo_line(self):
        rows = [types.SimpleNamespace(id=7, name="Абсолют", price=100, about="", color="", is_visible=True)]
        bot, router, texts, call, edits, sent = self._setup(products=rows)
        self.discount = 15
        cfg = texts.shop_menu

        async def go():
            await router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb, 10), bot)
            menu_id, text = sent[-1]
            self.assertTrue(text.endswith("\n\n<b>🔥 Действует промокод на 15%! Скидка истечёт через 23ч 40м</b>"))
            btns = self._buttons(self.sent_markup[-1])
            self.assertEqual(btns[0].text, "Абсолют — " + shop.strike("100") + " 85🍬")
            self.assertEqual(btns[-1].text, "⬅️ Назад")
            await router.handlers["on_shop_menu_button"](call(btns[0].callback_data, menu_id), bot)
            card = sent[-1][1]
            self.assertIn("<s>100</s> 85🍬", card)
            self.assertTrue(card.endswith("<b>🔥 Действует промокод на 15%!</b>"))
            self.assertEqual([b.text for b in self._buttons(self.sent_markup[-1])], ["🍭 Купить (85🍬)", "⬅️ Назад"])

        asyncio.run(go())

    def _shop_button(self, markup, texts):
        return next(b for b in self._buttons(markup) if b.callback_data == texts.shop_buttons.shop_cb)

    def test_shop_menu_promo_line_shows_time_left_and_hides_without_time(self):
        bot, router, texts, call, edits, sent = self._setup()
        self.discount = 15
        for left, shown in ((timedelta(hours=23, minutes=39, seconds=30), "23ч 40м"), (timedelta(hours=1), "1ч 0м"),
                            (timedelta(minutes=5, seconds=10), "0ч 6м"), (timedelta(seconds=20), "0ч 1м")):
            self.discount_left = left
            asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
            self.assertTrue(sent[-1][1].endswith(f"<b>🔥 Действует промокод на 15%! Скидка истечёт через {shown}</b>"), shown)
        self.discount_left = timedelta(seconds=-5)    # срок вышел между запросами — скидки нет
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
        self.assertNotIn("промокод", sent[-1][1])

    def test_main_menu_shop_button_gets_discount_suffix(self):
        bot, router, texts, call, edits, sent = self._setup()
        self.discount = 15
        asyncio.run(router.handlers["on_shop_menu_button"](call(texts.shop_menu.back_cb, 10), bot))   # «Назад» -> главное меню
        label = self._shop_button(self.sent_markup[-1], texts).text
        base = texts.shop_buttons.shop_label
        self.assertEqual(label, base.replace(" 💫", " (🔥 -15% на 23ч) 💫"))   # перед закрывающим эмодзи, часы вниз
        self.assertEqual(self._shop_button(self.sent_markup[-1], texts).style, "primary")
        self.discount_left = timedelta(minutes=40)
        asyncio.run(router.handlers["on_shop_menu_button"](call(texts.shop_menu.back_cb, 10), bot))
        self.assertIn("(🔥 -15% на 40м)", self._shop_button(self.sent_markup[-1], texts).text)
        self.discount = 0
        asyncio.run(router.handlers["on_shop_menu_button"](call(texts.shop_menu.back_cb, 10), bot))
        self.assertEqual(self._shop_button(self.sent_markup[-1], texts).text, base)               # скидки нет — как раньше

    def test_activation_refreshes_main_menu_buttons_only_while_main_menu_is_shown(self):
        from app import promo

        bot, router, texts, call, edits, sent = self._setup()
        replies = []

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            replies.append(text)
            return types.SimpleNamespace(message_id=900)

        async def fake_activate(bot_id, uid, code):
            return "ok", 15

        async def fake_settings(*, fresh=False):
            return promo.AutoSettings(text="t", percent=15, reply="ok {percent}")
        orig = (messaging.send_message, handlers.repo.__dict__.get("activate_promo"), promo.load_settings)
        self.addCleanup(lambda: (setattr(messaging, "send_message", orig[0]), setattr(promo, "load_settings", orig[2]),
                                 setattr(handlers.repo, "activate_promo", orig[1]) if orig[1] else
                                 handlers.repo.__dict__.pop("activate_promo", None)))
        handlers.repo.activate_promo, promo.load_settings = fake_activate, fake_settings
        msg = types.SimpleNamespace(text="AB12CD34", message_id=50, from_user=types.SimpleNamespace(id=5, username="u"),
                                    chat=types.SimpleNamespace(id=5))

        async def go():
            await router.handlers["on_shop_menu_button"](call(texts.shop_menu.back_cb, 10), bot)   # показано главное меню
            main_id = sent[-1][0]
            messaging.send_message = fake_send
            self.discount = 15
            await router.handlers["on_private_message"](msg, bot)
            kinds = [e for e in edits if e[0] == "markup"]
            self.assertEqual([e[1] for e in kinds], [main_id])                                         # кнопки обновлены на месте
            self.assertIn("(🔥 -15% на 23ч)", self._shop_button(kinds[0][3], texts).text)
        asyncio.run(go())

    def test_activation_does_not_touch_other_screens(self):
        from app import promo

        bot, router, texts, call, edits, sent = self._setup()

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            return types.SimpleNamespace(message_id=900)

        async def fake_activate(bot_id, uid, code):
            return "ok", 15

        async def fake_settings(*, fresh=False):
            return promo.AutoSettings(text="t", percent=15, reply="ok")
        orig_send, orig_load = messaging.send_message, promo.load_settings
        had = "activate_promo" in handlers.repo.__dict__
        old = handlers.repo.__dict__.get("activate_promo")
        self.addCleanup(lambda: (setattr(promo, "load_settings", orig_load), setattr(handlers.repo, "activate_promo", old)
                                 if had else handlers.repo.__dict__.pop("activate_promo", None)))
        handlers.repo.activate_promo, promo.load_settings = fake_activate, fake_settings
        msg = types.SimpleNamespace(text="AB12CD34", message_id=50, from_user=types.SimpleNamespace(id=5, username="u"),
                                    chat=types.SimpleNamespace(id=5))

        async def go():
            await router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb, 10), bot)   # сейчас открыт МАГАЗИН
            messaging.send_message = fake_send
            self.discount = 15
            await router.handlers["on_private_message"](msg, bot)
            self.assertEqual([e for e in edits if e[0] == "markup"], [])                          # экран магазина не трогаем
        try:
            asyncio.run(go())
        finally:
            messaging.send_message = orig_send

    def test_no_discount_means_plain_menu(self):
        bot, router, texts, call, edits, sent = self._setup()
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
        self.assertNotIn("промокод", sent[-1][1])
        self.assertNotIn(shop.STRIKE, "".join(b.text for b in self._buttons(self.sent_markup[-1])))

    def test_buy_results(self):
        bot, router, texts, call, edits, sent = self._setup()
        cfg = texts.shop_menu
        answers = []

        def call_with_answers(data):
            c = call(data)

            async def answer(text=None, show_alert=False, **k):
                answers.append((text, show_alert))
            c.answer = answer
            return c

        async def go():
            for status, expected in (("funds", ("❌ У вас недостаточно средств", True)),
                                     ("ok", ("✅ Вы приобрели «Абсолют» за 85🍬", True))):
                async def fake_buy(bot_id, uid, product_id, status=status):
                    return types.SimpleNamespace(status=status, name="Абсолют", price=85, balance=1, percent=15)
                handlers.repo.buy_product = fake_buy
                answers.clear()
                n = len(sent)
                await router.handlers["on_shop_menu_button"](call_with_answers(f"{cfg.buy_cb}:1"), bot)
                self.assertEqual(answers, [expected])               # на нажатие отвечено ровно один раз
                self.assertEqual(len(sent), n + (1 if status == "ok" else 0))   # после покупки — обновлённое меню
            answers.clear()
            await router.handlers["on_shop_menu_button"](call_with_answers(cfg.buy_cb), bot)   # кнопка без id товара
            self.assertEqual(answers, [(None, False)])

        asyncio.run(go())

    def test_typing_own_promo_activates_and_replies(self):
        from app import promo

        bot, router, texts, call, edits, sent = self._setup()
        replies = []

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            replies.append((chat_id, text, image))
            return types.SimpleNamespace(message_id=1)

        messaging.send_message = fake_send
        state = {"status": "ok"}

        async def fake_activate(bot_id, uid, code):
            return (state["status"], 15 if state["status"] != "none" else None)

        async def fake_settings(*, fresh=False):
            return promo.AutoSettings(text="t", percent=15, reply="Скидка **{percent}%** твоя! [Ура/Класс]")
        orig_a, orig_l = getattr(handlers.repo, "activate_promo", None), promo.load_settings
        handlers.repo.activate_promo, promo.load_settings = fake_activate, fake_settings

        def restore():
            promo.load_settings = orig_l
            if orig_a is not None:
                handlers.repo.activate_promo = orig_a
            else:
                del handlers.repo.activate_promo
        self.addCleanup(restore)

        def text_msg(text):
            return types.SimpleNamespace(
                text=text, message_id=50, from_user=types.SimpleNamespace(id=5, username="u"),
                chat=types.SimpleNamespace(id=5))

        async def go():
            await router.handlers["on_private_message"](text_msg("ab12cd34"), bot)   # регистр не важен
            self.assertEqual(len(replies), 1)
            self.assertIn("<b>15%</b> твоя!", replies[0][1])
            self.assertNotIn("[", replies[0][1])
            self.assertIsNone(replies[0][2])                                          # короткий ответ — без картинки
            state["status"] = "used"
            await router.handlers["on_private_message"](text_msg("AB12CD34"), bot)
            self.assertEqual(replies[-1][1], promo.USED_TEXT)
            state["status"] = "none"                                                  # чужой/неизвестный код — тишина
            n = len(replies)
            await router.handlers["on_private_message"](text_msg("ZZZZ9999"), bot)
            await router.handlers["on_private_message"](text_msg("привет, как дела"), bot)
            await router.handlers["on_private_message"](text_msg("AB12CD3"), bot)     # 7 символов — не код
            self.assertEqual(len(replies), n)

        asyncio.run(go())

    def test_photo_message_is_replaced_not_edited(self):
        bot, router, texts, call, edits, sent = self._setup(photo=True)
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb), bot))
        self.assertEqual((edits, len(sent), bot.deleted), ([], 1, [10]))

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

    def test_send_failure_keeps_old_message(self):
        bot, router, texts, call, edits, sent = self._setup()

        async def broken(*a, **k):
            raise handlers.TelegramAPIError("chat not found")

        messaging.send_message = broken   # вернёт на место cleanup из _setup
        asyncio.run(router.handlers["on_shop_button"](call(texts.shop_buttons.shop_cb, 10), bot))
        self.assertEqual((sent, edits, bot.deleted), ([], [], []))   # экран не доставлен — прежний остаётся на месте


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
                suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), version=1)
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


class ActivityHookTests(unittest.TestCase):
    """Любое сообщение и нажатие кнопки прошедшего капчу пользователя отмечает активность (сам троттлинг — в UserStats)."""

    def _router(self, known):
        from app import greeting

        profile = types.SimpleNamespace(name="Бот", message_image_url=None)
        texts = types.SimpleNamespace(
            greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
            deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
            suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), version=1)
        self.stats = FakeStats(known)
        return handlers.build_router(self.stats, texts, profile)

    def test_private_message_marks_activity(self):
        router = self._router({5})
        msg = types.SimpleNamespace(text="привет", message_id=1, from_user=types.SimpleNamespace(id=5, username="u"))
        asyncio.run(router.handlers["on_private_message"](msg, FakeBot()))
        self.assertEqual(self.stats.activity, [5])

    def test_button_press_marks_activity(self):
        router = self._router({5})
        answered = []

        async def answer(*a, **k):
            answered.append(1)

        call = types.SimpleNamespace(
            data="zzz", from_user=types.SimpleNamespace(id=5, username="u"), message=None, answer=answer)
        asyncio.run(router.handlers["on_gallery_button"](call, FakeBot()))
        self.assertEqual(self.stats.activity, [5])


class SupportButtonTests(unittest.TestCase):
    """Кнопка «Поддержка» под приветствием: контакт с сайта, сообщение, красная «Назад» в приветствие."""

    def _setup(self, contact):
        from app import support
        bot, router, texts, call, edits, sent = ShopNavigationTests._setup(self)
        self.support, self.contact = support, contact

        async def fake_load(fresh=False):
            return support.Settings(self.contact)

        orig = support.load_settings
        support.load_settings = fake_load
        self.addCleanup(lambda: setattr(support, "load_settings", orig))
        return bot, router, texts, call, sent

    def _greet(self, router, bot):
        async def go():
            await router.handlers["on_start"](types.SimpleNamespace(
                text="/start", message_id=900, from_user=types.SimpleNamespace(id=5, username="u")), bot)
        asyncio.run(go())
        return ShopNavigationTests._buttons(self.sent_markup[-1])

    def test_button_is_last_and_hidden_without_contact(self):
        bot, router, texts, call, sent = self._setup("@help_team")
        btns = self._greet(router, bot)
        key = texts.gallery.back_cb
        self.assertEqual(btns[-1].text, self.support.button_label(key))
        self.assertEqual(btns[-1].callback_data, self.support.cb_open(key))
        self.assertEqual(btns[-1].style, "primary")
        self.assertIn("Поддержка" if "Поддержка" in btns[-1].text else "Связь", btns[-1].text)
        bot2, router2, texts2, call2, sent2 = self._setup("")
        btns2 = self._greet(router2, bot2)
        self.assertFalse(any(b.callback_data.startswith(self.support.OPEN_PREFIX) for b in btns2))

    def test_open_shows_exact_contact_with_red_back_and_back_returns_to_greeting(self):
        bot, router, texts, call, sent = self._setup("@help_team")
        key = texts.gallery.back_cb

        async def go():
            await router.handlers["on_support_button"](call(self.support.cb_open(key), 10), bot)
            msg_id, text = sent[-1]
            self.assertEqual(text, self.support.message_text(key, "@help_team"))
            self.assertTrue(text.endswith("@help_team"))
            self.assertIn("с ", text)
            self.assertEqual(bot.deleted, [10])                     # приветствие заменено экраном поддержки
            btns = ShopNavigationTests._buttons(self.sent_markup[-1])
            self.assertEqual([(b.text, b.style) for b in btns], [("⬅️ Назад", "danger")])
            await router.handlers["on_support_button"](call(btns[0].callback_data, msg_id), bot)
            self.assertEqual(bot.deleted, [10, msg_id])             # экран поддержки убран
            self.assertIn("12" if False else "42🍬", sent[-1][1])    # вернулось приветствие (с балансом)
            back_btns = ShopNavigationTests._buttons(self.sent_markup[-1])
            self.assertEqual(back_btns[-1].callback_data, self.support.cb_open(key))
        asyncio.run(go())

    def test_foreign_or_stale_callback_does_nothing(self):
        bot, router, texts, call, sent = self._setup("@help_team")

        async def go():
            await router.handlers["on_support_button"](call("su:deadbeef00", 10), bot)
            self.assertEqual(sent, [])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
