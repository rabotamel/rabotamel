"""«Автосообщения» и промокоды: настройки, коды, скидка, планировщик (БД и Telegram подменяются заглушками)."""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _prepare_imports():
    """Без sqlalchemy / aiogram (их может не быть в окружении проверки) подставляем пустые заглушки, как другие тесты."""
    import app

    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        if "app.repo" not in sys.modules:
            m = types.ModuleType("app.repo")
            sys.modules["app.repo"] = m
            app.repo = m
    try:
        import aiogram.exceptions as exc
    except ImportError:
        exc = types.ModuleType("aiogram.exceptions")
        sys.modules["aiogram.exceptions"] = exc
        sys.modules.setdefault("aiogram", types.ModuleType("aiogram")).exceptions = exc
    # другие тесты (test_handlers) уже могли подставить свои заглушки без нужных нам классов — дописываем недостающие
    if not hasattr(exc, "TelegramAPIError"):
        exc.TelegramAPIError = type("TelegramAPIError", (Exception,), {})
    for name in ("TelegramBadRequest", "TelegramForbiddenError", "TelegramRetryAfter"):
        if not hasattr(exc, name):
            setattr(exc, name, type(name, (exc.TelegramAPIError,), {}))
    if not hasattr(exc.TelegramRetryAfter, "retry_after"):
        exc.TelegramRetryAfter.retry_after = 0


_prepare_imports()
from app import automsg, messaging, promo, shop  # noqa: E402
from aiogram.exceptions import TelegramForbiddenError  # noqa: E402


class CodeTests(unittest.TestCase):
    def test_generated_code_format(self):
        codes = {promo.generate_code() for _ in range(500)}
        self.assertEqual(len(codes), 500)
        for c in codes:
            self.assertRegex(c, r"^[A-Z0-9]{8}$")

    def test_parse_code(self):
        self.assertEqual(promo.parse_code(" ab12cd34 "), "AB12CD34")
        for bad in (None, "", "AB12CD3", "AB12CD345", "AB12 CD34", "привет123", "AB12-D34", "ab12cd34 ещё"):
            self.assertIsNone(promo.parse_code(bad), bad)


class SettingsTests(unittest.TestCase):
    def test_normalize_ok(self):
        s = promo.normalize("Привет! Код: {promo}", "15%", "Скидка действует")
        self.assertEqual((s.text, s.percent, s.reply, s.enabled), ("Привет! Код: {promo}", 15, "Скидка действует", True))

    def test_percent_rules(self):
        for bad in ("0", "100", "-5", "abc", "1.5", "1000"):
            with self.assertRaises(ValueError, msg=bad):
                promo.normalize("{promo}", bad, "")
        self.assertEqual(promo.normalize("Привет", "", "").percent, None)      # без {promo} процент не обязателен
        self.assertEqual(promo.normalize("{promo}", 99, "").percent, 99)

    def test_promo_requires_percent(self):
        with self.assertRaises(ValueError):
            promo.normalize("Ваш код {promo}", "", "")
        with self.assertRaises(ValueError):
            promo.normalize("Привет", "", "Ваш код {promo}")

    def test_reply_without_text_rejected(self):
        with self.assertRaises(ValueError):
            promo.normalize("", "10", "ответ")

    def test_empty_means_disabled(self):
        s = promo.normalize("  ", "", "")
        self.assertFalse(s.enabled)

    def test_length_limits_use_worst_case_variant(self):
        long_variant = "x" * (promo.MAX_TEXT + 1)
        with self.assertRaises(ValueError):
            promo.normalize(f"[а/{long_variant}]", "", "")
        promo.normalize("**" + "x" * promo.MAX_TEXT + "**", "", "")        # значки оформления в длину не входят

    def test_json_roundtrip_and_garbage(self):
        s = promo.AutoSettings("т", 20, "о")
        self.assertEqual(promo.AutoSettings.from_json(s.to_json()), s)
        for raw in (None, "", "не json", "[]", '{"percent": "abc"}'):
            self.assertFalse(promo.AutoSettings.from_json(raw).enabled)


class RenderTests(unittest.TestCase):
    def test_formatting_variables_and_code(self):
        out = promo.render("**Привет**, твой код: `{promo}` / {promo}", "seed", "AB12CD34", 15)
        self.assertEqual(out, "<b>Привет</b>, твой код: <code>AB12CD34</code> / AB12CD34")

    def test_variants_stable_per_bot_and_differ_between_bots(self):
        raw = "[привет/здравствуй] [друг/товарищ] [а/б] [в/г] [д/е]"
        self.assertEqual(promo.render(raw, "bot1"), promo.render(raw, "bot1"))
        self.assertTrue(len({promo.render(raw, f"bot{i}") for i in range(30)}) > 1)
        self.assertNotIn("[", promo.render(raw, "bot1"))

    def test_html_is_escaped(self):
        self.assertEqual(promo.render("<b>x</b> & {promo}", "s", "AAAAAAAA"), "&lt;b&gt;x&lt;/b&gt; &amp; AAAAAAAA")

    def test_percent_variable(self):
        self.assertEqual(promo.render("-{percent}%", "s", None, 15), "-15%")


class DiscountTests(unittest.TestCase):
    def test_discounted_price(self):
        d = shop.discounted_price
        self.assertEqual((d(100, 15), d(100, 0), d(6789, 10)), (85, 100, 6110))
        self.assertEqual(d(99, 15), 84)          # 84,15 -> 84
        self.assertEqual(d(10, 15), 9)           # 8,5 -> половина вверх
        self.assertEqual(d(1, 99), 1)            # не бесплатно
        self.assertEqual(d(100, 150), 1)         # процент ограничен 99

    def test_labels(self):
        p = shop.Product(id=1, name="Товар", price=100)
        self.assertEqual(shop.product_label(p), "Товар — 100🍬")
        struck = shop.product_label(p, 15)
        self.assertEqual(struck, "Товар — " + "1\u0336" + "0\u0336" + "0\u0336" + " 85🍬")
        self.assertEqual(shop.buy_label(p, 15), "🍭 Купить (85🍬)")
        self.assertEqual(shop.price_text(100, 15).count(shop.STRIKE), 3)   # зачёркнуто только число, 🍬 старой цены нет

    def test_menu_and_card_text(self):
        p = shop.Product(id=1, name="Т", price=100, about="описание")
        self.assertNotIn("промокод", shop.render_menu("🍫 Магазин 💫", 5))
        menu = shop.render_menu("🍫 Магазин 💫", 5, 15)
        self.assertTrue(menu.endswith("🍭 Ваш баланс: 5🍬\n\n<b>🔥 Действует промокод на 15%!</b>"))
        card = shop.render_product(p, "", 15)
        self.assertIn("<s>100</s> 85🍬", card)
        self.assertTrue(card.endswith("<b>🔥 Действует промокод на 15%!</b>"))
        self.assertEqual(shop.render_product(p, ""), shop.render_product(p, "", 0))

    def test_time_left_formats(self):
        self.assertEqual(shop.PROMO_DURATION.total_seconds(), 24 * 3600)
        for sec, hm, short in ((86400, "24ч 0м", "24ч"), (85199, "23ч 40м", "23ч"), (3600, "1ч 0м", "1ч"),
                               (3599, "1ч 0м", "1ч"), (3540, "0ч 59м", "59м"), (3541, "1ч 0м", "1ч"), (600, "0ч 10м", "10м"),
                               (61, "0ч 2м", "2м"), (1, "0ч 1м", "1м")):
            self.assertEqual(shop.left_hm(sec), hm, sec)
            self.assertEqual(shop.left_short(sec), short, sec)

    def test_button_suffix_and_menu_line(self):
        self.assertEqual(shop.promo_button_suffix(15, 85000), "(🔥 -15% на 23ч)")
        self.assertEqual(shop.promo_button_suffix(20, 1500), "(🔥 -20% на 25м)")
        for percent, left in ((0, 100), (15, None), (15, 0), (15, -1)):
            self.assertEqual(shop.promo_button_suffix(percent, left), "")
        self.assertEqual(shop.promo_line(15), "<b>🔥 Действует промокод на 15%!</b>")
        self.assertEqual(shop.promo_line(15, 85200), "<b>🔥 Действует промокод на 15%! Скидка истечёт через 23ч 40м</b>")
        self.assertTrue(shop.render_menu("🍫 Магазин 💫", 5, 15, 3000).endswith("Скидка истечёт через 0ч 50м</b>"))
        self.assertNotIn("истечёт", shop.render_menu("🍫 Магазин 💫", 5, 0, 3000))     # без скидки строки нет

    def test_shop_button_label_with_suffix(self):
        from app import shop_buttons

        cfg = shop_buttons.generate_config("t")
        self.assertEqual(shop_buttons.shop_label_with(cfg, ""), cfg.shop_label)
        label = shop_buttons.shop_label_with(cfg, "(🔥 -15% на 23ч)")
        self.assertTrue(label.endswith(" 💫"))                                          # закрывающее эмодзи осталось с краю
        self.assertEqual(label.replace(" (🔥 -15% на 23ч)", ""), cfg.shop_label)       # убрали приписку — прежняя подпись
        self.assertLessEqual(len(label), 64)
        rows = shop_buttons.layout_rows(cfg, "(🔥 -15% на 23ч)")
        self.assertIn(label, [b[0] for row in rows for b in row])
        self.assertEqual(shop_buttons.layout_rows(cfg), shop_buttons.layout_rows(cfg, ""))
        self.assertEqual(shop_buttons.parse_action(cfg, cfg.shop_cb), "shop")          # callback_data не меняется

    def test_menu_rows_apply_discount_to_every_product(self):
        cfg = shop.generate_config("t")
        ps = [shop.Product(id=i, name=f"Т{i}", price=100 * i) for i in (1, 2, 3)]
        rows = shop.menu_rows(cfg, ps, 20)
        self.assertEqual([r[0][0].split(" — ")[1].split(" ")[-1] for r in rows[:3]], ["80🍬", "160🍬", "240🍬"])
        self.assertEqual(rows[-1][0][0], shop.BACK_LABEL)
        self.assertEqual(shop.menu_rows(cfg, ps)[0][0][0], "Т1 — 100🍬")   # без скидки как раньше


class FakeBot:
    pass


class FakeStats:
    bot_id = 1

    def __init__(self, known):
        self.known = set(known)

    def has(self, uid):
        return uid in self.known


class AutoMessengerTests(unittest.TestCase):
    def setUp(self):
        self.queue = {}          # user_id -> due_at
        self.issued = []
        self.sent = []
        self.settings = promo.AutoSettings("Привет! Код: **{promo}**", 15, "ответ")

        async def queue_auto_message(bot_id, uid, due):
            self.queue[uid] = due

        async def load_auto_queue(bot_id):
            return list(self.queue.items())

        async def claim(bot_id, uid):
            return self.queue.pop(uid, None) is not None

        async def issue(bot_id, uid, percent):
            self.issued.append((uid, percent))
            return "ABCD1234"

        async def load_settings(*, fresh=False):
            return self.settings

        async def send(bot, chat_id, text, image=None, reply_markup=None):
            self.sent.append((chat_id, text, image))

        self._saved = (
            getattr(automsg.repo, "queue_auto_message", None), getattr(automsg.repo, "load_auto_queue", None),
            getattr(automsg.repo, "claim_auto_message", None), getattr(automsg.repo, "issue_promo", None),
            promo.load_settings, messaging.send_message,
        )
        automsg.repo.queue_auto_message, automsg.repo.load_auto_queue = queue_auto_message, load_auto_queue
        automsg.repo.claim_auto_message, automsg.repo.issue_promo = claim, issue
        promo.load_settings, messaging.send_message = load_settings, send
        self.addCleanup(self._restore)
        self.profile = types.SimpleNamespace(token="tok", message_image_url="http://img")

    def _restore(self):
        names = ("queue_auto_message", "load_auto_queue", "claim_auto_message", "issue_promo")
        for n, v in zip(names, self._saved[:4]):
            if v is not None:
                setattr(automsg.repo, n, v)
            elif hasattr(automsg.repo, n):
                delattr(automsg.repo, n)
        promo.load_settings, messaging.send_message = self._saved[4], self._saved[5]

    def _messenger(self, known=(7,), delay=0.01):
        return automsg.AutoMessenger(FakeBot(), FakeStats(known), self.profile, delay=delay)

    def test_delay_is_thirty_minutes(self):
        self.assertEqual(promo.AUTO_DELAY, 1800)
        self.assertEqual(automsg.AutoMessenger(FakeBot(), FakeStats([]), self.profile).delay, 1800)

    def test_message_sent_after_delay_with_unique_code_without_image(self):
        async def go():
            m = self._messenger()
            await m.on_captcha_passed(7)
            self.assertIn(7, self.queue)                     # запись в БД создана сразу
            self.assertEqual(self.sent, [])
            await asyncio.sleep(0.1)
            self.assertEqual(self.sent, [(7, "Привет! Код: <b>ABCD1234</b>", None)])
            self.assertEqual(self.issued, [(7, 15)])
            self.assertNotIn(7, self.queue)                  # запись забрана — повторно не уйдёт
            await m.close()
        asyncio.run(go())

    def test_disabled_settings_do_not_queue(self):
        self.settings = promo.AutoSettings()

        async def go():
            m = self._messenger()
            await m.on_captcha_passed(7)
            await asyncio.sleep(0.05)
            self.assertEqual((self.queue, self.sent), ({}, []))
        asyncio.run(go())

    def test_settings_edit_applies_to_waiting_users(self):
        async def go():
            m = self._messenger(delay=0.05)
            await m.on_captcha_passed(7)
            self.settings = promo.AutoSettings("Новый текст", None, "")
            await asyncio.sleep(0.15)
            self.assertEqual(self.sent, [(7, "Новый текст", None)])
            self.assertEqual(self.issued, [])                 # без {promo} код не выдаётся
        asyncio.run(go())

    def test_user_gone_or_already_claimed_gets_nothing(self):
        async def go():
            m = self._messenger(known=())                     # пользователя уже нет в базе
            await m.on_captcha_passed(7)
            await asyncio.sleep(0.1)
            self.assertEqual(self.sent, [])
            m2 = self._messenger()
            await m2.on_captcha_passed(7)
            self.queue.pop(7)                                 # другой процесс уже забрал запись
            await asyncio.sleep(0.1)
            self.assertEqual(self.sent, [])
        asyncio.run(go())

    def test_restart_restores_timers_and_drops_stale(self):
        now = datetime.now(timezone.utc)
        self.queue = {1: now - timedelta(seconds=5), 2: now + timedelta(seconds=0.05),
                      3: now - timedelta(seconds=promo.AUTO_STALE + 60)}

        async def go():
            m = self._messenger(known=(1, 2, 3))
            await m.start()
            await asyncio.sleep(0.3)
            self.assertEqual(sorted(c for c, _, _ in self.sent), [1, 2])   # просроченный сутки назад — не шлём
            self.assertEqual(self.queue, {})
            await m.close()
        asyncio.run(go())

    def test_blocked_user_triggers_callback_and_other_403_do_not(self):
        calls = []

        async def forbidden_send(bot, chat_id, text, image=None, reply_markup=None):
            raise TelegramForbiddenError("Forbidden: bot was blocked by the user")

        async def on_blocked(uid):
            calls.append(uid)

        async def go():
            messaging.send_message = forbidden_send
            m = self._messenger()
            m.on_blocked = on_blocked
            await m.on_captcha_passed(7)
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [7])

            async def other_403(bot, chat_id, text, image=None, reply_markup=None):
                raise TelegramForbiddenError("Forbidden: user is deactivated")
            messaging.send_message = other_403
            await m.on_captcha_passed(7)
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [7])
        asyncio.run(go())

    def test_send_test_sends_now_with_fresh_code_without_image_or_touching_queue(self):
        async def go():
            m = self._messenger(delay=60)
            await m.on_captcha_passed(7)                     # человек ещё ждёт своё сообщение
            res = await m.send_test(7)
            self.assertEqual(self.sent, [(7, "Привет! Код: <b>ABCD1234</b>", None)])
            self.assertEqual(self.issued, [(7, 15)])
            self.assertEqual(res, {"promo": "ABCD1234", "percent": 15})
            self.assertIn(7, self.queue)                     # очередь не тронута: боевое сообщение придёт в своё время
            await m.close()
        asyncio.run(go())

    def test_send_test_without_promo_variable_issues_no_code(self):
        self.settings = promo.AutoSettings("Просто текст", None, "")
        res = asyncio.run(self._messenger().send_test(7))
        self.assertEqual((self.issued, res), ([], {"promo": None, "percent": None}))
        self.assertEqual(self.sent, [(7, "Просто текст", None)])

    def test_send_test_requires_enabled_message(self):
        self.settings = promo.AutoSettings()
        with self.assertRaises(ValueError):
            asyncio.run(self._messenger().send_test(7))
        self.assertEqual((self.sent, self.issued), ([], []))

    def test_send_boost_test_sends_return_notice_without_image_and_starts_boost(self):
        from app import comeback

        boosts = []

        async def comeback_settings(fresh=False):
            return comeback.Settings(self.return_text)

        async def set_boost(bot_id, uid, until):
            boosts.append((bot_id, uid, until))
            return True

        saved = (comeback.load_settings, getattr(automsg.repo, "set_reward_boost", None))
        comeback.load_settings, automsg.repo.set_reward_boost = comeback_settings, set_boost
        self.addCleanup(lambda: (setattr(comeback, "load_settings", saved[0]),
                                 setattr(automsg.repo, "set_reward_boost", saved[1]) if saved[1] else
                                 (hasattr(automsg.repo, "set_reward_boost") and delattr(automsg.repo, "set_reward_boost"))))
        self.return_text = "С возвращением!"
        res = asyncio.run(self._messenger().send_boost_test(7))
        self.assertEqual(res, {"hours": 24, "factor": 2})
        self.assertEqual(self.sent, [(7, "С возвращением!", None)])         # без картинки бота
        self.assertEqual((len(boosts), boosts[0][1]), (1, 7))
        self.assertEqual(self.issued, [])                                 # скидка не затрагивается
        self.return_text = ""
        with self.assertRaises(ValueError):
            asyncio.run(self._messenger().send_boost_test(7))
        self.assertEqual(len(boosts), 1)

    def test_forget_cancels_timer(self):
        async def go():
            m = self._messenger(delay=0.05)
            await m.on_captcha_passed(7)
            m.forget(7)
            await asyncio.sleep(0.15)
            self.assertEqual(self.sent, [])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
