"""«Купить конфеты» (app/topup.py): расчёты, тексты, ключи, экраны, чек по карте, счета CryptoBot, права админов."""
import asyncio
import os
import sys
import types
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import test_handlers  # noqa: E402,F401  (ставит заглушки aiogram)
from app import messaging, topup  # noqa: E402

NS = types.SimpleNamespace


def _const(value):
    """Заглушка для async-функции, всегда возвращающей value."""
    async def f():
        return value
    return f


class PureTests(unittest.TestCase):
    def test_rates_and_formatting(self):
        self.assertEqual((topup.to_rub(100), topup.to_usd(100), topup.to_stars(100)), (Decimal("80.0"), Decimal("1.00"), Decimal(100)))
        self.assertEqual((topup.money(topup.to_rub(7)), topup.money(topup.to_usd(150)), topup.money(topup.to_stars(5))),
                         ("5,6", "1,5", "5"))

    def test_sandbox_star_rate_matches_topup(self):
        """«Песочница» выдаёт конфеты за звёзды по тому же курсу, что и «Купить конфеты»."""
        from app import currency
        for candies in (1, 7, 1000):
            stars = topup.to_stars(candies)
            self.assertEqual(currency.to_candies(stars, currency.STAR), candies)

    def test_parse_amount(self):
        self.assertEqual([topup.parse_amount(t) for t in (
            "100", " 1 000 ", "500 конфет", "10", "x", "", None, "0", "9", "-5", "-100", "10.5", "100,5", "1000001")],
                         [100, 1000, 500, 10, None, None, None, None, None, None, None, None, None, None])

    def test_amount_errors_are_specific(self):
        e = {t: topup.check_amount(t)[1] for t in ("5", "9", "0", "-50", "12.5", "12,5", "abc", "1000001")}
        self.assertEqual(e["5"], topup.AMOUNT_ERROR_MIN)
        self.assertEqual(e["9"], topup.AMOUNT_ERROR_MIN)
        self.assertEqual(e["0"], topup.AMOUNT_ERROR_MIN)
        self.assertIn("10", topup.AMOUNT_ERROR_MIN)
        self.assertEqual(e["-50"], topup.AMOUNT_ERROR_NEGATIVE)
        self.assertEqual((e["12.5"], e["12,5"]), (topup.AMOUNT_ERROR_FRACTION,) * 2)
        self.assertEqual(e["abc"], topup.AMOUNT_ERROR_FORMAT)
        self.assertEqual(e["1000001"], topup.AMOUNT_ERROR_MAX)
        self.assertEqual(topup.check_amount("10"), (10, None))

    def test_receipt_targets_come_from_moderators_tab(self):
        from app import moderation

        orig = moderation.cache.recipients

        async def fake(ids):
            return ids

        try:
            for rows, want in (([], []), ([11, 22, 11], [11, 22])):   # ID модераторов из БД; повторы отбрасываются
                moderation.cache.recipients = lambda rows=rows: fake(rows)
                self.assertEqual(asyncio.run(topup.receipt_targets()), want)
        finally:
            moderation.cache.recipients = orig

    def test_keys_are_per_bot_and_checked(self):
        a, b = topup.Keys.for_seed("tokA"), topup.Keys.for_seed("tokB")
        self.assertNotEqual(a.p, b.p)
        self.assertTrue(a.p.startswith("tp") and len(a.cb("ok", 123)) <= 64)
        self.assertEqual(a.parse(a.cb("open")), ("open", None))
        self.assertEqual(a.parse(a.cb("ok", 17)), ("ok", 17))
        self.assertIsNone(a.parse(b.cb("open")))
        self.assertIsNone(a.parse(None))
        self.assertIsNone(a.parse(f"{a.p}:ok:x"))

    def test_texts_use_same_payment_data(self):
        self.assertEqual(topup.CARD_NUMBER, "5599002131738728")
        self.assertEqual(
            topup.methods_text(None),
            "💳 Оплата проходит автоматически, данные карты мы не храним. Конфеты зачисляются сразу после оплаты.\n\n"
            "👇 Введите желаемое количество конфет и выберите удобный метод оплаты.\n\n"
            "🍭 Выбрано для покупки: 0🍬\n(Курс: 1🍬 ≈ 0,01$)",
        )
        self.assertIn("🍭 Выбрано для покупки: 250🍬\n(Курс: 1🍬 ≈ 0,01$)", topup.methods_text(250))
        self.assertEqual(topup.methods_text(0), topup.methods_text(None))
        self.assertIn("<code>5599002131738728</code>", topup.card_text(250))
        self.assertIn("200₽", topup.card_text(250))

    def test_button_label_and_rows(self):
        k = topup.Keys.for_seed("s")
        self.assertEqual(topup.menu_row(k), [("🍭 Купить конфеты 🍬", k.cb("open"), "primary")])
        ready, notready = topup.methods_rows(k, 250), topup.methods_rows(k, None)
        self.assertEqual([r[0][3] for r in ready], ["success", "success", "success", "danger"])
        self.assertEqual([r[0][0] for r in ready[:3]], ["💫 Telegram Stars ≈ 250⭐", "🤖 CryptoBot ≈ 2,5$", "💳 По карте ≈ 200₽"])
        self.assertEqual([r[0][1] for r in ready[:3]], [k.cb("stars"), k.cb("crypto"), k.cb("card")])
        # до ввода суммы: нули, серые (disabled) и без callback_data — нажать нельзя
        self.assertEqual([r[0][3] for r in notready], [topup.DISABLED] * 3 + ["danger"])
        self.assertEqual([r[0][0] for r in notready[:3]], ["💫 Telegram Stars ≈ 0⭐", "🤖 CryptoBot ≈ 0$", "💳 По карте ≈ 0₽"])
        self.assertEqual([(r[0][1], r[0][2]) for r in notready[:3]], [(None, None)] * 3)
        self.assertEqual(ready[-1][0][0], "⬅️ Назад")
        self.assertEqual(topup.methods_rows(k, 0), notready)

    def test_disabled_buttons_have_no_callback_in_markup(self):
        k = topup.Keys.for_seed("s")
        buttons = [b for row in topup.markup(topup.methods_rows(k, None)).inline_keyboard for b in row]
        for b in buttons[:3]:
            self.assertTrue(hasattr(b, "disabled"))
            self.assertFalse(hasattr(b, "callback_data") or hasattr(b, "style"))
        self.assertEqual((buttons[3].callback_data, buttons[3].style), (k.cb("back"), "danger"))
        live = [b for row in topup.markup(topup.methods_rows(k, 5)).inline_keyboard for b in row]
        self.assertTrue(all(hasattr(b, "callback_data") and not hasattr(b, "disabled") for b in live))


class FakeRepo:
    """Хранилище заказов в памяти с теми же правилами статусов, что в repo.py."""

    def __init__(self):
        self.orders, self.balance, self.next = {}, {}, 1

    async def create_topup_order(self, bot_id, user_id, candies, method, currency, fiat, **kw):
        o = NS(id=self.next, bot_id=bot_id, user_id=user_id, candies=candies, method=method, currency=currency,
               fiat_amount=fiat, status=kw.get("status", "creating"), invoice_id=None, invoice_url=None, expires_at=None,
               screen_chat_id=kw.get("screen_chat_id"), screen_message_id=kw.get("screen_message_id"),
               receipt_file_id=kw.get("receipt_file_id"), receipt_sender=kw.get("receipt_sender"))
        self.orders[o.id] = o
        self.next += 1
        return o.id

    async def get_topup_order(self, oid):
        return self.orders.get(oid)

    async def set_topup_invoice(self, oid, inv, url, exp):
        o = self.orders[oid]
        o.invoice_id, o.invoice_url, o.expires_at, o.status = inv, url, exp, "active"

    async def set_topup_screen(self, oid, chat, mid):
        self.orders[oid].screen_chat_id, self.orders[oid].screen_message_id = chat, mid

    async def finish_topup_order(self, oid, status):
        allowed = {"canceled": ("creating", "active"), "failed": ("creating",), "expired": ("active",), "rejected": ("review",)}[status]
        o = self.orders[oid]
        if o.status in allowed:
            o.status = status
            return True
        return False

    async def mark_topup_paid(self, oid, charge_id=None):
        o = self.orders[oid]
        if o.status in ("creating", "active", "review"):
            o.status = "paid"
            return True
        return False

    async def credit_topup_order(self, oid):
        o = self.orders[oid]
        if o.status != "paid" or (o.bot_id, o.user_id) not in self.balance:
            return None
        o.status = "credited"
        self.balance[(o.bot_id, o.user_id)] += o.candies
        return o

    blocked_receipts: set = set()

    async def is_receipt_blocked(self, uid):
        return uid in self.blocked_receipts

    async def set_receipt_block(self, uid, blocked, by=None):
        had = uid in self.blocked_receipts
        (self.blocked_receipts.add if blocked else self.blocked_receipts.discard)(uid)
        return had != blocked

    async def list_topup_orders(self, status, method=None):
        return [o for o in self.orders.values() if o.status == status and (method is None or o.method == method)]


class Bot:
    def __init__(self, admins=(101,)):
        self.sent, self.edits_caption, self.deleted, self.admins = [], [], [], set(admins)
        self.next_id = 500
        self.files = []
        self.markup_edits = []

    async def _send(self, kind, chat_id, kw):
        self.next_id += 1
        self.sent.append((kind, chat_id, kw))
        return NS(message_id=self.next_id, chat=NS(id=chat_id))

    async def send_photo(self, chat_id, **kw):
        return await self._send("photo", chat_id, kw)

    async def send_document(self, chat_id, **kw):
        return await self._send("document", chat_id, kw)

    async def get_chat_administrators(self, chat_id):
        return [NS(user=NS(is_bot=False, username="boss")), NS(user=NS(is_bot=True, username="somebot"))]

    async def get_chat_member(self, chat_id, user_id):
        return NS(status="administrator" if user_id in self.admins else "member")

    async def edit_message_caption(self, **kw):
        self.edits_caption.append(kw)

    async def edit_message_reply_markup(self, **kw):
        self.markup_edits.append(kw)

    # бот-магазин: скачивание файла чека
    async def get_file(self, file_id):
        self.files.append(file_id)
        return NS(file_path=f"photos/{file_id}.jpg")

    async def download_file(self, path, destination=None, **kw):
        destination.write(b"IMG:" + path.encode())
        return destination


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.repo.blocked_receipts = set()
        self.repo.balance[(1, 5)] = 10
        self.saved = {n: getattr(topup.repo, n, None) for n in dir(self.repo) if not n.startswith("_") and n not in ("orders", "balance", "next")}
        for n in self.saved:
            setattr(topup.repo, n, getattr(self.repo, n))
        self.screens, self.news, self.deleted, self.alerts, self.main = [], [], [], [], []
        self.images = []   # картинка, с которой ушло каждое сообщение из self.news
        self.bot = Bot()

        async def fake_edit(bot, chat_id, mid, text, markup=None, *, has_photo=False):
            self.screens.append((mid, text, markup))

        async def fake_send(bot, chat_id, text, image=None, reply_markup=None):
            self.bot.next_id += 1
            self.news.append((chat_id, text, reply_markup))
            self.images.append(image)
            return NS(message_id=self.bot.next_id, chat=NS(id=chat_id))

        self.check = Bot()   # бот для чеков (отдельный от бота-магазина self.bot)
        self.orig = (messaging.edit_message, messaging.send_message, topup.CRYPTO_PAY_TOKEN, topup._crypto_post,
                     topup.receipt_targets, topup.CHECK_BOT_TOKEN, topup._ensure_check_bot, topup._input_file)
        messaging.edit_message, messaging.send_message = fake_edit, fake_send
        topup.CRYPTO_PAY_TOKEN = "tok"
        topup.receipt_targets = _const([101, 102])   # два админа в личных сообщениях
        topup.CHECK_BOT_TOKEN = "check-token"
        topup._ensure_check_bot = lambda: self.check
        topup._input_file = lambda data, name: NS(data=data, name=name)
        topup._receipt_msgs.clear()

        async def show_screen(call, bot, user_id, text, mk):
            self.screens.append((call.message.message_id, text, mk))
            return 900 + len(self.screens)   # как в боте: экран — НОВОЕ сообщение с другим id

        async def safe_delete(bot, chat, mid):
            self.deleted.append(mid)

        async def go_main(call, bot, uid):
            self.main.append(uid)

        self.tracked = {}
        self.mod = topup.TopupModule(1, None, NS(message_image_url=None), topup.Keys.for_seed("t"), show_screen=show_screen,
                                     safe_delete=safe_delete, track=self.tracked.__setitem__, go_main=go_main,
                                     is_user=lambda uid: uid == 5)
        self.mod.bot = self.bot
        topup.ACTIVE[1] = self.mod
        topup._watch.clear()
        topup._retry.clear()

    def tearDown(self):
        (messaging.edit_message, messaging.send_message, topup.CRYPTO_PAY_TOKEN, topup._crypto_post,
         topup.receipt_targets, topup.CHECK_BOT_TOKEN, topup._ensure_check_bot, topup._input_file) = self.orig
        for n, v in self.saved.items():
            if v is not None:
                setattr(topup.repo, n, v)
            elif hasattr(topup.repo, n):
                delattr(topup.repo, n)
        topup.ACTIVE.pop(1, None)

    def call(self, data, uid=5, msg_id=10, chat=5):
        async def answer(text=None, show_alert=False):
            self.alerts.append(text)
        return NS(data=data, answer=answer, from_user=NS(id=uid, username="u", full_name="U"),
                  message=NS(message_id=msg_id, chat=NS(id=chat)))

    def msg(self, text=None, photo=False, uid=5):
        return NS(text=text, photo=[NS(file_id="F1")] if photo else None, document=None, message_id=77,
                  from_user=NS(id=uid, username="u", full_name="U"), chat=NS(id=uid))

    def run_(self, coro):
        return asyncio.run(coro)

    def test_open_asks_amount_and_payment_needs_amount(self):
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            self.assertEqual(self.screens[-1][1], topup.methods_text(None))
            await self.mod.on_callback(self.call(k.cb("card")), self.bot)
            self.assertEqual(self.alerts[-1], "❌ Сначала введите количество конфет.")
        self.run_(go())

    def test_amount_message_replaces_screen_and_is_removed(self):
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open"), msg_id=10), self.bot)
            self.assertTrue(self.mod.active(5))
            await self.mod.on_message(self.msg("250"), self.bot)
            chat, text, mk = self.news[-1]
            self.assertEqual(text, topup.methods_text(250))
            self.assertEqual(self.deleted, [901, 77])         # прежний экран (теперь это новое сообщение 901) и число пользователя убраны
            self.assertEqual(self.tracked[5], self.bot.next_id)
            self.assertEqual(self.mod.state(5).amount, 250)
        self.run_(go())

    def _send_receipt(self):
        """Пользователь проходит путь до отправки чека на 100🍬; возвращает (kind, chat, kwargs) первой копии у админа."""
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("100"), self.bot)
            await self.mod.on_callback(self.call(k.cb("card")), self.bot)
            self.assertIn("5599002131738728", self.screens[-1][1])
            await self.mod.on_callback(self.call(k.cb("paid")), self.bot)
            self.assertEqual(self.screens[-1][1], topup.CARD_CONFIRM_TEXT)
            await self.mod.on_message(self.msg(None, photo=True), self.bot)
        self.run_(go())

    def test_card_flow_receipt_goes_to_admins_in_check_bot(self):
        self._send_receipt()
        self.assertEqual(self.bot.sent, [])                          # бот-магазин админам ничего не шлёт
        self.assertEqual(self.bot.files, ["F1"])                     # файл скачан ботом-магазином (его file_id)
        self.assertEqual([(kind, chat) for kind, chat, _ in self.check.sent], [("photo", 101), ("photo", 102)])
        _, _, kw = self.check.sent[0]
        self.assertEqual((kw["photo"].data, kw["photo"].name), (b"IMG:photos/F1.jpg", "F1.jpg"))
        self.assertIn("100🍬", kw["caption"])
        self.assertIn("80", kw["caption"])
        self.assertNotIn("@boss", kw["caption"])                     # личные сообщения — никого не пингуем
        self.assertEqual([b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row], ["tpchk:ok:1", "tpchk:no:1", "tpchk:bl:1"])
        self.assertEqual(self.news[-1][1], topup.CARD_PROCESSING_TEXT)
        order = self.repo.orders[1]
        self.assertEqual((order.status, order.method, order.fiat_amount), ("review", "card", Decimal("80.00")))
        self.assertEqual(len(topup._receipt_msgs[1]), 2)

    def test_receipt_accepted_notice_has_no_bot_image(self):
        self.mod.profile = NS(message_image_url="http://img/x.jpg")   # у бота задана картинка сообщений
        self._send_receipt()
        self.assertEqual(self.news[-1][1], topup.CARD_PROCESSING_TEXT)
        self.assertIsNone(self.images[-1])                          # но «чек принят на проверку» уходит без неё
        self.assertEqual(self.mod.image, "http://img/x.jpg")

    def test_block_button_rejects_silently_and_blocks_receipts(self):
        self._send_receipt()
        order = self.repo.orders[1]
        (c1, m1), (c2, m2) = topup._receipt_msgs[1]
        k = self.mod.keys
        news_before = len(self.news)

        async def go():
            await topup._decide(self.call("tpchk:bl:1", uid=999, chat=c1, msg_id=m1), self.check, "bl", 1)   # не админ
            self.assertEqual((order.status, 5 in self.repo.blocked_receipts), ("review", False))
            await topup._decide(self.call("tpchk:bl:1", uid=101, chat=c1, msg_id=m1), self.check, "bl", 1)
            self.assertEqual((order.status, 5 in self.repo.blocked_receipts), ("rejected", True))
            self.assertEqual(len(self.news), news_before)                      # пользователю ничего не пишем
            self.assertEqual(self.repo.balance[(1, 5)], 10)
            self.assertIn("🚫 Отказано, пользователь заблокирован", self.check.edits_caption[-1]["caption"])
            self.assertEqual({(e["chat_id"], e["message_id"]) for e in self.check.edits_caption}, {(c1, m1), (c2, m2)})
            # заблокированный не может перейти к отправке чека
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("50"), self.bot)
            await self.mod.on_callback(self.call(k.cb("paid")), self.bot)
            self.assertEqual(self.alerts[-1], topup.RECEIPT_BLOCKED_ALERT)
        self.run_(go())

    def test_block_after_review_still_blocks_author(self):
        self._send_receipt()
        c1, m1 = topup._receipt_msgs[1][0]
        self.repo.orders[1].status = "credited"

        async def go():
            await topup._decide(self.call("tpchk:bl:1", uid=101, chat=c1, msg_id=m1), self.check, "bl", 1)
            self.assertIn(5, self.repo.blocked_receipts)
            self.assertEqual(self.repo.orders[1].status, "credited")
        self.run_(go())

    def test_admin_approves_once_and_other_copy_is_updated(self):
        self._send_receipt()
        order = self.repo.orders[1]
        (c1, m1), (c2, m2) = topup._receipt_msgs[1]

        async def go():
            # не админ — ничего не выдаётся
            await topup._decide(self.call("tpchk:ok:1", uid=999, chat=c1, msg_id=m1), self.check, "ok", 1)
            self.assertEqual((order.status, self.repo.balance[(1, 5)]), ("review", 10))
            # админ — выдаётся ровно один раз; пользователю пишет бот-магазин
            await topup._decide(self.call("tpchk:ok:1", uid=101, chat=c1, msg_id=m1), self.check, "ok", 1)
            self.assertEqual((order.status, self.repo.balance[(1, 5)]), ("credited", 110))
            self.assertEqual(self.news[-1][1], "🍭 Вы приобрели 100🍬 за 80₽")
            # обе копии чека (у обоих админов) обновлены, кнопок нет
            edited = {(e["chat_id"], e["message_id"]) for e in self.check.edits_caption}
            self.assertEqual(edited, {(c1, m1), (c2, m2)})
            self.assertIn("✅ Выдано 100🍬 за 80₽", self.check.edits_caption[-1]["caption"])
            self.assertIn("@u", self.check.edits_caption[-1]["caption"])
            self.assertTrue(all(e["reply_markup"] is None for e in self.check.edits_caption))
            self.assertEqual(self.bot.edits_caption, [])
            # второй админ жмёт на устаревшую копию — «уже обработан», баланс не меняется
            await topup._decide(self.call("tpchk:ok:1", uid=102, chat=c2, msg_id=m2), self.check, "ok", 1)
            self.assertEqual(self.alerts[-1], "Этот чек уже обработан.")
            self.assertEqual(self.repo.balance[(1, 5)], 110)
        self.run_(go())

    def test_reject_gives_nothing_and_notifies_user(self):
        async def go():
            oid = await self.repo.create_topup_order(1, 5, 50, "card", "RUB", Decimal("40"), status="review", receipt_sender="@u")
            await topup._decide(self.call(f"tpchk:no:{oid}", uid=101, chat=101), self.check, "no", oid)
            self.assertEqual((self.repo.orders[oid].status, self.repo.balance[(1, 5)]), ("rejected", 10))
            self.assertTrue(self.news[-1][1].startswith("❌ Чек не подтверждён"))
            self.assertIn("❌ Отказано", self.check.edits_caption[-1]["caption"])
        self.run_(go())

    def test_group_is_optional_but_still_works(self):
        topup.receipt_targets = _const([-100])
        self._send_receipt()
        kind, chat, kw = self.check.sent[-1]
        self.assertEqual((kind, chat), ("photo", -100))
        self.assertIn("@boss", kw["caption"])
        self.assertNotIn("somebot", kw["caption"])

        async def go():
            await topup._decide(self.call("tpchk:ok:1", uid=999, chat=-100), self.check, "ok", 1)   # не админ группы
            self.assertEqual(self.repo.orders[1].status, "review")
            await topup._decide(self.call("tpchk:ok:1", uid=101, chat=-100), self.check, "ok", 1)   # админ группы
            self.assertEqual(self.repo.orders[1].status, "credited")
        self.run_(go())

    def test_undeliverable_receipt_is_rejected_and_user_told(self):
        async def boom(chat_id, **kw):
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.check.send_photo = boom
        self._send_receipt()
        self.assertEqual(self.repo.orders[1].status, "rejected")
        self.assertEqual(self.news[-1][1], "❌ Не удалось отправить чек на проверку. Попробуйте позже.")
        self.assertNotIn(1, topup._receipt_msgs)

    def test_partial_delivery_is_enough(self):
        real = self.check.send_photo

        async def only_102(chat_id, **kw):
            if chat_id == 101:
                raise RuntimeError("admin has not pressed /start")
            return await real(chat_id, **kw)
        self.check.send_photo = only_102
        self._send_receipt()
        self.assertEqual(self.repo.orders[1].status, "review")
        self.assertEqual([c for c, _ in topup._receipt_msgs[1]], [102])

    def test_pending_resends_receipts_through_shop_bot_file(self):
        async def go():
            oid = await self.repo.create_topup_order(1, 5, 70, "card", "RUB", Decimal("56"), status="review",
                                                     receipt_file_id="OLD", receipt_sender="@u")
            await self.repo.create_topup_order(1, 5, 10, "card", "RUB", Decimal("8"), status="credited", receipt_file_id="DONE")
            n = await topup._send_pending(self.check, 101)
            self.assertEqual(n, 1)
            kind, chat, kw = self.check.sent[-1]
            self.assertEqual((kind, chat, self.bot.files), ("photo", 101, ["OLD"]))
            self.assertEqual(kw["reply_markup"].inline_keyboard[0][0].callback_data, f"tpchk:ok:{oid}")
            self.assertEqual(await topup._send_pending(self.check, 101), 1)   # снова пришлёт, пока чек не разобран
        self.run_(go())

    def test_admin_check_by_user_id_in_private_chat(self):
        async def go():
            self.assertTrue(await topup._is_admin(self.check, 101, 101))
            self.assertFalse(await topup._is_admin(self.check, 101, 999))     # чужой человек в чужом чате
            self.assertFalse(await topup._is_admin(self.check, 777, 777))     # не в списке админов
        self.run_(go())

    def test_card_unavailable_without_admins_or_check_bot(self):
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("100"), self.bot)
            topup.receipt_targets = _const([])
            await self.mod.on_callback(self.call(k.cb("card")), self.bot)
            self.assertEqual(self.alerts[-1], "❌ Оплата картой сейчас недоступна.")
            topup.receipt_targets = _const([101])
            topup.CHECK_BOT_TOKEN = ""
            await self.mod.on_callback(self.call(k.cb("card")), self.bot)
            self.assertEqual(self.alerts[-1], "❌ Оплата картой сейчас недоступна.")
        self.run_(go())

    def test_stars_and_crypto_unavailable_without_tokens(self):
        k = self.mod.keys
        topup.CRYPTO_PAY_TOKEN = ""
        saved = topup.STARS_BOT_TOKEN
        topup.STARS_BOT_TOKEN = ""

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("100"), self.bot)
            await self.mod.on_callback(self.call(k.cb("stars")), self.bot)
            self.assertEqual(self.alerts[-1], "❌ Оплата через Telegram Stars сейчас недоступна.")
            await self.mod.on_callback(self.call(k.cb("crypto")), self.bot)
            self.assertEqual(self.alerts[-1], "❌ Оплата через CryptoBot сейчас недоступна.")
            self.assertEqual(self.repo.orders, {})
        try:
            self.run_(go())
        finally:
            topup.STARS_BOT_TOKEN = saved

    def test_crypto_invoice_reused_and_credited_once(self):
        k = self.mod.keys
        calls = []

        async def fake_post(method, payload):
            calls.append((method, payload))
            return {"invoice_id": 42, "bot_invoice_url": "https://t.me/CryptoBot?start=IV42"}
        topup._crypto_post = fake_post

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("300"), self.bot)
            await self.mod.on_callback(self.call(k.cb("crypto")), self.bot)
            await self.mod.on_callback(self.call(k.cb("crypto")), self.bot)       # повторное нажатие — тот же счёт
            self.assertEqual([c[0] for c in calls], ["createInvoice"])
            payload = calls[0][1]
            self.assertEqual((payload["amount"], payload["fiat"], payload["payload"]), ("3", "USD", "topup:1"))
            self.assertIn("USDT,TON", payload["accepted_assets"])
            self.assertEqual(topup._watch, {1: 42})
            url_btn = self.screens[-1][2].inline_keyboard[0][0]
            self.assertEqual(url_btn.url, "https://t.me/CryptoBot?start=IV42")
            # CryptoBot сообщает об оплате
            async def paid_post(method, payload):
                return {"items": [{"invoice_id": 42, "status": "paid"}]}
            topup._crypto_post = paid_post
            await topup._check_invoices()
            self.assertEqual((self.repo.orders[1].status, self.repo.balance[(1, 5)], topup._watch), ("credited", 310, {}))
            # экран счёта не редактируется: «успешно оплачен» приходит новым сообщением, сообщение со счётом удаляется
            invoice_msg = 900 + len(self.screens)
            self.assertEqual(self.repo.orders[1].screen_message_id, invoice_msg)
            self.assertIn("Вы успешно купили 300🍬", self.news[-2][1])
            self.assertEqual(self.deleted[-1], invoice_msg)
            self.assertEqual(self.mod.state(5).message_id, self.bot.next_id - 1)
            self.assertEqual(self.news[-1][1], "🍭 Вы приобрели 300🍬 за 3$")
            await topup._check_invoices()                                          # повторная проверка ничего не добавляет
            self.assertEqual(self.repo.balance[(1, 5)], 310)
        self.run_(go())

    def test_expired_invoice_stops_being_watched(self):
        async def fake_post(method, payload):
            return {"items": [{"invoice_id": 7, "status": "expired"}]}
        topup._crypto_post = fake_post

        async def go():
            oid = await self.repo.create_topup_order(1, 5, 10, "cryptobot", "USD", Decimal("0.1"), status="active")
            topup.watch(oid, 7)
            await topup._check_invoices()
            self.assertEqual((self.repo.orders[oid].status, topup._watch), ("expired", {}))
        self.run_(go())

    def test_paid_for_user_not_in_db_is_retried_not_lost(self):
        async def go():
            oid = await self.repo.create_topup_order(1, 999, 10, "cryptobot", "USD", Decimal("0.1"), status="active")
            await self.repo.mark_topup_paid(oid)
            await topup.finalize(oid)
            self.assertEqual((self.repo.orders[oid].status, oid in topup._retry), ("paid", True))
            self.repo.balance[(1, 999)] = 0                                         # пользователь появился
            await topup._retry_credits()
            self.assertEqual((self.repo.orders[oid].status, self.repo.balance[(1, 999)], topup._retry), ("credited", 10, {}))
        self.run_(go())

    def test_back_leaves_section_without_cancelling_invoice(self):
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_callback(self.call(k.cb("back")), self.bot)
            self.assertEqual((self.main, self.mod.active(5)), ([5], False))
        self.run_(go())

    def test_foreign_and_unverified_do_nothing(self):
        other = topup.Keys.for_seed("other")

        async def go():
            await self.mod.on_callback(self.call(other.cb("open")), self.bot)
            await self.mod.on_callback(self.call(self.mod.keys.cb("open"), uid=6), self.bot)
        self.run_(go())
        self.assertEqual((self.screens, self.news, self.mod.states), ([], [], {}))


if __name__ == "__main__":
    unittest.main()
