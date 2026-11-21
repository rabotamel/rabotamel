"""Контакт поддержки: проверка ввода, тексты без искажения контакта, экспорт/импорт .txt."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import shop_io, support  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_accepts_username_link_and_id(self):
        for raw, want in (("@help_team", "@help_team"), ("help_team", "@help_team"), (" https://t.me/help_team ", "@help_team"),
                          ("t.me/Help_Team?start=1", "@Help_Team"), ("123456789", "123456789"), ("", ""), (None, "")):
            self.assertEqual(support.normalize(raw), want)

    def test_rejects_garbage(self):
        for raw in ("ab", "@a b c", "привет", "1bad_name", "-5", "0", "@" + "x" * 40, "a-b-c-d"):
            with self.assertRaises(ValueError, msg=raw):
                support.normalize(raw)

    def test_settings_roundtrip_and_bad_json(self):
        self.assertEqual(support.Settings.from_json(support.Settings("@help_team").to_json()).contact, "@help_team")
        self.assertEqual(support.Settings.from_json("не json").contact, "")
        self.assertFalse(support.Settings().enabled)


class TextTests(unittest.TestCase):
    def test_base_look_and_word_by_word_variants(self):
        self.assertEqual(support.button_label(None), "📲 Поддержка 👩🏻‍💻")
        labels = {support.button_label(f"k{i}") for i in range(200)}
        self.assertEqual(len(labels), 8)                         # 2 × 2 × 2 сочетания
        for lab in labels:
            left, word, right = lab.split(" ", 2)
            self.assertIn(left, support.BUTTON_LEFT)
            self.assertIn(word, support.BUTTON_WORD)
            self.assertIn(right, support.BUTTON_RIGHT)
        self.assertEqual(support.button_label("k1"), support.button_label("k1"))   # у бота постоянная

    def test_message_keeps_contact_untouched(self):
        for i in range(50):
            t = support.message_text(f"k{i}", "@help_team")
            self.assertTrue(t.endswith("@help_team"))
            title, blank, head, tail = t.split("\n")
            self.assertEqual(title, f"<b>{support.button_label(f'k{i}')}</b>")     # жирный заголовок = подпись кнопки
            self.assertEqual(blank, "")
            self.assertIn(head.split(" ")[0], support.MSG_LEAD)
            self.assertTrue(tail.startswith(tuple(support.MSG_POINTER)))
        self.assertEqual(support.message_text(None, "@help_team"),
                         "<b>📲 Поддержка 👩🏻‍💻</b>\n\nСвязь с администрацией (если не выдалась ссылка или есть вопросы):\n👉 @help_team")
        self.assertIn('tg://user?id=777', support.message_text(None, "777"))

    def test_back_is_fixed_and_red_and_callbacks_per_bot(self):
        self.assertEqual((support.BACK_LABEL, support.BACK_STYLE), ("⬅️ Назад", "danger"))
        self.assertNotEqual(support.cb_open("a"), support.cb_open("b"))
        self.assertEqual(support.parse_action("a", support.cb_open("a")), "open")
        self.assertEqual(support.parse_action("a", support.cb_back("a")), "back")
        self.assertIsNone(support.parse_action("a", support.cb_open("b")))
        self.assertLessEqual(len(support.cb_open("a").encode()), 64)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        support.forget_resolved()

    def test_id_becomes_username_when_telegram_gives_it(self):
        import asyncio
        import types

        calls = []

        class Bot:
            async def get_chat(self, chat_id):
                calls.append(chat_id)
                return types.SimpleNamespace(username="help_team")

        bot = Bot()
        self.assertEqual(asyncio.run(support.resolve_contact(bot, "777")), "@help_team")
        self.assertEqual(asyncio.run(support.resolve_contact(bot, "777")), "@help_team")
        self.assertEqual(calls, [777])                                  # второй раз — из памяти
        self.assertEqual(asyncio.run(support.resolve_contact(bot, "@x_team")), "@x_team")   # юзернейм не трогаем
        self.assertEqual(calls, [777])

    def test_falls_back_to_id_when_unknown_or_no_username(self):
        import asyncio
        import types

        class Broken:
            async def get_chat(self, chat_id):
                raise RuntimeError("chat not found")

        class NoName:
            async def get_chat(self, chat_id):
                return types.SimpleNamespace(username=None)

        self.assertEqual(asyncio.run(support.resolve_contact(Broken(), "888")), "888")
        support.forget_resolved()
        self.assertEqual(asyncio.run(support.resolve_contact(NoName(), "888")), "888")
        self.assertIn("tg://user?id=888", support.message_text(None, "888"))


class ExportImportTests(unittest.TestCase):
    def test_support_goes_through_txt(self):
        text = shop_io.export_all([], [], "@help_team")
        self.assertIn("\nsupport: @help_team\n", text)
        self.assertEqual(shop_io.parse_all(text).support, "@help_team")
        self.assertEqual(shop_io.parse_all(shop_io.export_all([], [], "")).support, "")          # пусто = очистить
        self.assertIsNone(shop_io.parse_all(shop_io.export_all([], [])).support)                 # нет строки = не трогать
        self.assertIsNone(shop_io.parse_all(shop_io.export_products([])).support)                # старый формат

    def test_bad_support_in_file_is_rejected(self):
        text = shop_io.export_all([], [], "@help_team").replace("support: @help_team", "support: ##")
        with self.assertRaises(ValueError):
            shop_io.parse_all(text)

    def test_header_order_is_free(self):
        text = "TG-BOT-ROTATOR BUTTONS v2\nsupport: 12345\ntasks: 0\nproducts: 0\n"
        p = shop_io.parse_all(text)
        self.assertEqual((p.support, p.products, p.tasks), ("12345", [], []))


if __name__ == "__main__":
    unittest.main()
