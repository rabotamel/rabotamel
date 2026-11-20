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


class PureTests(unittest.TestCase):
    def test_rates_and_formatting(self):
        self.assertEqual((topup.to_rub(100), topup.to_usd(100), topup.to_stars(100)), (Decimal("80.0"), Decimal("1.00"), Decimal(100)))
        self.assertEqual((topup.money(topup.to_rub(7)), topup.money(topup.to_usd(150)), topup.money(topup.to_stars(5))),
                         ("5,6", "1,5", "5"))

    def test_parse_amount(self):
        self.assertEqual([topup.parse_amount(t) for t in ("100", " 1 000 ", "500 конфет", "x", "", None, "0", "-5", "1000001")],
                         [100, 1000, 500, None, None, None, None, 5, None])

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
        self.assertIn("1🍬 ➜ 0,8₽ ➜ 0,01$ ➜ 1⭐", topup.methods_text(None))
        t = topup.methods_text(250)
        self.assertIn("<b>200₽</b>", t)
        self.assertIn("<b>2,5$</b>", t)
        self.assertIn("<b>250⭐</b>", t)
        self.assertIn("<code>5599002131738728</code>", topup.card_text(250))
        self.assertIn("200₽", topup.card_text(250))

    def test_button_label_and_rows(self):
        k = topup.Keys.for_seed("s")
        self.assertEqual(topup.menu_row(k), [("🍭 Купить конфеты 🍬", k.cb("open"), "primary")])
        ready, notready = topup.methods_rows(k, True), topup.methods_rows(k, False)
        self.assertEqual([r[0][3] for r in ready], ["success", "success", "success", "danger"])
        self.assertEqual([r[0][3] for r in notready], [None, None, None, "danger"])
        self.assertEqual(ready[-1][0][0], "⬅️ Назад")


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

    async def list_topup_orders(self, status, method=None):
        return [o for o in self.orders.values() if o.status == status and (method is None or o.method == method)]


class Bot:
    def __init__(self):
        self.sent, self.edits_caption, self.deleted, self.admins = [], [], [], {101}
        self.next_id = 500

    async def send_photo(self, chat_id, **kw):
        self.sent.append(("photo", chat_id, kw))

    async def send_document(self, chat_id, **kw):
        self.sent.append(("document", chat_id, kw))

    async def get_chat_administrators(self, chat_id):
        return [NS(user=NS(is_bot=False, username="boss")), NS(user=NS(is_bot=True, username="somebot"))]

    async def get_chat_member(self, chat_id, user_id):
        return NS(status="administrator" if user_id in self.admins else "member")

    async def edit_message_caption(self, **kw):
        self.edits_caption.append(kw)

    async def edit_message_reply_markup(self, **kw):
        pass


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.repo.balance[(1, 5)] = 10
        self.saved = {n: getattr(topup.repo, n, None) for n in dir(self.repo) if not n.startswith("_") and n not in ("orders", "balance", "next")}
        for n in self.saved:
            setattr(topup.repo, n, getattr(self.repo, n))
        self.screens, self.news, self.deleted, self.alerts, self.main = [], [], [], [], []
        self.bot = Bot()

        async def fake_edit(bot, chat_id, mid, text, markup=None, *, has_photo=False):
            self.screens.append((mid, text, markup))

        async def fake_send(bot, chat_id, text, image=None, reply_markup=None):
            self.bot.next_id += 1
            self.news.append((chat_id, text, reply_markup))
            return NS(message_id=self.bot.next_id, chat=NS(id=chat_id))

        self.orig = (messaging.edit_message, messaging.send_message, topup.CRYPTO_PAY_TOKEN, topup._crypto_post, topup.admin_chat_id)
        messaging.edit_message, messaging.send_message = fake_edit, fake_send
        topup.CRYPTO_PAY_TOKEN = "tok"
        topup.admin_chat_id = lambda: -100

        async def show_screen(call, bot, user_id, text, mk):
            self.screens.append((call.message.message_id, text, mk))

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
        messaging.edit_message, messaging.send_message, topup.CRYPTO_PAY_TOKEN, topup._crypto_post, topup.admin_chat_id = self.orig
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
            self.assertEqual(self.deleted, [10, 77])          # прежний экран и число пользователя убраны
            self.assertEqual(self.tracked[5], self.bot.next_id)
            self.assertEqual(self.mod.state(5).amount, 250)
        self.run_(go())

    def test_card_flow_receipt_and_admin_approval(self):
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("100"), self.bot)
            await self.mod.on_callback(self.call(k.cb("card")), self.bot)
            self.assertIn("5599002131738728", self.screens[-1][1])
            await self.mod.on_callback(self.call(k.cb("paid")), self.bot)
            self.assertEqual(self.screens[-1][1], topup.CARD_CONFIRM_TEXT)
            await self.mod.on_message(self.msg(None, photo=True), self.bot)
            kind, chat, kw = self.bot.sent[-1]
            self.assertEqual((kind, chat, kw["photo"]), ("photo", -100, "F1"))
            self.assertIn("@boss", kw["caption"])
            self.assertNotIn("somebot", kw["caption"])
            self.assertIn("100🍬", kw["caption"])
            self.assertEqual(self.news[-1][1], topup.CARD_PROCESSING_TEXT)
            order = self.repo.orders[1]
            self.assertEqual((order.status, order.method, order.fiat_amount), ("review", "card", Decimal("80.00")))
            ok = kw["reply_markup"].inline_keyboard[0][0].callback_data
            # не админ — ничего не выдаётся
            await self.mod.on_callback(self.call(ok, uid=999, chat=-100), self.bot)
            self.assertEqual((order.status, self.repo.balance[(1, 5)]), ("review", 10))
            # админ — выдаётся ровно один раз
            await self.mod.on_callback(self.call(ok, uid=101, chat=-100), self.bot)
            self.assertEqual((order.status, self.repo.balance[(1, 5)]), ("credited", 110))
            self.assertIn("✅ Выдано 100🍬 за 80₽", self.bot.edits_caption[-1]["caption"])
            self.assertEqual(self.news[-1][1], "🍭 Вы приобрели 100🍬 за 80₽")
            await self.mod.on_callback(self.call(ok, uid=101, chat=-100), self.bot)
            self.assertEqual(self.repo.balance[(1, 5)], 110)
        self.run_(go())

    def test_reject_gives_nothing(self):
        k = self.mod.keys

        async def go():
            oid = await self.repo.create_topup_order(1, 5, 50, "card", "RUB", Decimal("40"), status="review", receipt_sender="@u")
            await self.mod.on_callback(self.call(k.cb("no", oid), uid=101, chat=-100), self.bot)
            self.assertEqual((self.repo.orders[oid].status, self.repo.balance[(1, 5)]), ("rejected", 10))
            self.assertEqual(self.news[-1][1].startswith("❌ Чек не подтверждён"), True)
        self.run_(go())

    def test_card_unavailable_without_admin_chat(self):
        topup.admin_chat_id = lambda: None
        k = self.mod.keys

        async def go():
            await self.mod.on_callback(self.call(k.cb("open")), self.bot)
            await self.mod.on_message(self.msg("100"), self.bot)
            await self.mod.on_callback(self.call(k.cb("card")), self.bot)
            self.assertEqual(self.alerts[-1], "❌ Оплата картой сейчас недоступна.")
        self.run_(go())

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
            self.assertIn("Вы успешно купили 300🍬", self.screens[-1][1])
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
