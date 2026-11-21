import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_handlers import FakeBot, FakeStats, handlers  # noqa: E402  (без sqlalchemy handlers импортируются с заглушкой repo)
from app import captcha, deco, gallery, greeting, messaging, shop_buttons, suggest_ui, tasks  # noqa: E402

comeback = handlers.comeback   # тот же модуль, что использует handlers (с той же заглушкой repo)

CFG = tasks.generate_config("cb")
DECO = deco.generate_config("cb")


def task(reward=200, button="Подписаться (200🍬)", one_time=True):
    return tasks.Task(id=3, button_text=button, about="Описание", reward=reward, require_screenshot=False, one_time=one_time)


class BoostRenderTests(unittest.TestCase):
    def test_time_format(self):
        self.assertEqual(tasks.fmt_boost_left(86400), "24ч 0м")
        self.assertEqual(tasks.fmt_boost_left(23 * 3600 + 59 * 60 + 1), "24ч 0м")   # минуты округляются вверх
        self.assertEqual(tasks.fmt_boost_left(3 * 3600 + 5 * 60), "3ч 5м")
        self.assertEqual(tasks.fmt_boost_left(5), "0ч 1м")

    def test_button_has_struck_old_and_new_reward(self):
        t = tasks.boost_button_text("Подписаться (200🍬)", 200)
        self.assertEqual(t, "Подписаться (2̶" + "0̶0̶ 400🍬)")
        self.assertEqual(t.replace("̶", ""), "Подписаться (200 400🍬)")
        self.assertEqual(tasks.boost_button_text("Без суммы", 200), "Без суммы")
        self.assertEqual(tasks.boost_button_text("Награда 1200🍬", 200), "Награда 1200🍬")   # чужое число не трогаем
        self.assertEqual(tasks.boost_button_text("A 50 🍬", 50).replace("̶", ""), "A 50 100 🍬")

    def test_title_uses_real_strikethrough_and_escapes(self):
        self.assertEqual(tasks.boost_title_html("Подписаться (200🍬) <x>", 200), "Подписаться (<s>200</s> 400🍬) &lt;x&gt;")

    def test_menu_text_and_buttons(self):
        left = 23 * 3600 + 59 * 60
        plain = tasks.render_menu(CFG, DECO, 10)
        boosted = tasks.render_menu(CFG, DECO, 10, left)
        self.assertNotIn("УДВОЕНИЕ", plain)
        self.assertTrue(boosted.startswith(plain))
        self.assertTrue(boosted.endswith("\n\n<b>🔥 Действует УДВОЕНИЕ суммы наград. Конец через: 23ч 59м</b>"))
        rows = tasks.menu_rows(CFG, [task()], None, True)
        self.assertIn("400🍬", rows[2][0][0])
        self.assertEqual(rows[2][0][1], tasks.menu_rows(CFG, [task()])[2][0][1])        # callback_data прежний
        self.assertEqual(tasks.menu_rows(CFG, [task()])[2][0][0], "Подписаться (200🍬)")  # без удвоения — как раньше
        self.assertEqual(rows[0], tasks.menu_rows(CFG, [task()], None)[0])              # подарок и ссылка не меняются

    def test_task_card(self):
        left = 3600 + 60
        c = tasks.render_task(CFG, task(), "seed", None, left)
        self.assertTrue(c.startswith("<b>Подписаться (<s>200</s> 400🍬)</b>"))
        self.assertIn("400", c.split("\n\n")[-2])                                        # строка награды — новая сумма
        self.assertTrue(c.endswith("<b>🔥 Действует УДВОЕНИЕ суммы наград. Конец через: 1ч 1м</b>"))
        plain = tasks.render_task(CFG, task(), "seed", None)
        self.assertNotIn("УДВОЕНИЕ", plain)
        self.assertNotIn("<s>", plain)
        p = tasks.render_send_prompt(CFG, task(), 0, None, left)
        self.assertIn("<s>200</s> 400🍬", p)
        self.assertTrue(p.endswith("1ч 1м</b>"))


class ComebackSettingsTests(unittest.TestCase):
    def test_settings_roundtrip_and_validation(self):
        s = comeback.normalize("  С возвращением, [друг/товарищ]! **Держите**  ")
        self.assertTrue(s.enabled)
        self.assertEqual(comeback.Settings.from_json(s.to_json()), s)
        self.assertFalse(comeback.normalize("   ").enabled)
        with self.assertRaises(ValueError):
            comeback.normalize("x" * (comeback.MAX_RAW + 1))
        self.assertFalse(comeback.Settings.from_json("мусор").enabled)

    def test_boost_left(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(comeback.boost_left(None, now))
        self.assertIsNone(comeback.boost_left(now, now))
        self.assertEqual(comeback.boost_left(now + timedelta(hours=1), now), 3600)
        self.assertEqual(comeback.boost_left(datetime(2026, 1, 1, 1), now), 3600)   # без часового пояса = UTC
        self.assertEqual(comeback.BOOST_DURATION, timedelta(hours=24))
        self.assertEqual(comeback.BOOST_FACTOR, 2)


class ReturnFlowTests(unittest.TestCase):
    """Капча пройдена: после приветствия приходит уведомление (только вернувшимся после смены бота) и включается удвоение."""

    def _run(self, returning, enabled=True):
        sent, boosts, captchas = [], [], []

        class Bot(FakeBot):
            async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
                self.next_id += 1
                captchas.append((text, reply_markup))   # капча уходит напрямую через bot.send_message
                return types.SimpleNamespace(message_id=self.next_id)

        profile = types.SimpleNamespace(name="Бот", message_image_url=None, token="1:SECRET")
        texts = types.SimpleNamespace(
            captcha=captcha.generate_config("t"), greeting=greeting.generate_config("t"),
            shop_buttons=shop_buttons.generate_config("t"), deco=deco.generate_config("t"),
            gallery=gallery.generate_config("t"), suggest=suggest_ui.generate_config("t"),
            tasks_menu=tasks.generate_config("t"), version=1)
        stats = FakeStats(set())
        stats.add = lambda uid, name=None: stats.known.add(uid) or True

        async def _flush():
            return None

        stats.flush = _flush
        router = handlers.build_router(stats, texts, profile)
        bot = Bot()

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            b.next_id += 1
            sent.append(text)
            return types.SimpleNamespace(message_id=b.next_id)

        async def settings(fresh=False):
            return comeback.Settings("Привет, **вернулся**!" if enabled else "")

        async def returned(bot_id, user_id):
            return returning

        async def set_boost(bot_id, user_id, until):
            boosts.append((bot_id, user_id, until))
            return True

        saved = (messaging.send_message, comeback.load_settings, handlers.repo.__dict__.get("returned_after_switch"),
                 handlers.repo.__dict__.get("set_reward_boost"), handlers.CAPTCHA_MIN_INTERVAL)
        messaging.send_message, comeback.load_settings = fake_send, settings
        handlers.repo.returned_after_switch, handlers.repo.set_reward_boost = returned, set_boost
        handlers.CAPTCHA_MIN_INTERVAL = 0

        async def go():
            async def answer(*a, **k):
                return None
            await router.handlers["on_start"](types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=9, username="u"), text="/start"), bot)
            text, markup_cb = captchas[-1]
            target = next(k for k, f in captcha.FRUITS.items() if f"<b>{f.acc}</b>" in text)
            data = next(b.callback_data for row in markup_cb.inline_keyboard for b in row
                        if b.callback_data.split(":")[1] == target)
            call = types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=9, username="u"), data=data, answer=answer,
                message=types.SimpleNamespace(chat=types.SimpleNamespace(id=9), message_id=bot.next_id))
            n = len(sent)
            await router.handlers["on_captcha_answer"](call, bot)
            return sent[n:]

        try:
            after = asyncio.run(go())
        finally:
            (messaging.send_message, comeback.load_settings, rs, rb, handlers.CAPTCHA_MIN_INTERVAL) = saved
            for name, val in (("returned_after_switch", rs), ("set_reward_boost", rb)):
                if val is None:
                    handlers.repo.__dict__.pop(name, None)
                else:
                    setattr(handlers.repo, name, val)
        return after, boosts

    def test_returning_user_gets_notice_after_greeting_and_boost(self):
        after, boosts = self._run(returning=True)
        self.assertTrue(after[-1].startswith("Привет, <b>вернулся</b>"))   # уведомление — после приветствия
        self.assertEqual(len(after), 2)
        (bot_id, uid, until), = boosts
        self.assertEqual((bot_id, uid), (1, 9))
        self.assertAlmostEqual((until - datetime.now(timezone.utc)).total_seconds(), 24 * 3600, delta=60)

    def test_new_user_or_disabled_notice_gets_only_greeting(self):
        for returning, enabled in ((False, True), (True, False)):
            after, boosts = self._run(returning, enabled)
            self.assertEqual((len(after), boosts), (1, []))


if __name__ == "__main__":
    unittest.main()
