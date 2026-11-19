import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# aiogram в окружении тестов может отсутствовать — подставляем минимальную заглушку исключений
try:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError  # noqa: F401
except ImportError:
    exc = types.ModuleType("aiogram.exceptions")

    class TelegramAPIError(Exception):
        pass

    class TelegramBadRequest(TelegramAPIError):
        pass

    class TelegramForbiddenError(TelegramAPIError):
        pass

    exc.TelegramAPIError, exc.TelegramBadRequest, exc.TelegramForbiddenError = (
        TelegramAPIError, TelegramBadRequest, TelegramForbiddenError)
    pkg = types.ModuleType("aiogram")
    pkg.exceptions = exc
    sys.modules["aiogram"], sys.modules["aiogram.exceptions"] = pkg, exc

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError  # noqa: E402

from app import messaging  # noqa: E402


class FakeBot:
    id = 42

    def __init__(self, photo_error=None, text_error=None):
        self.calls = []
        self.photo_error, self.text_error = photo_error, text_error

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None):
        self.calls.append(("photo", photo, caption, parse_mode))
        if self.photo_error:
            raise self.photo_error
        return types.SimpleNamespace(photo=[types.SimpleNamespace(file_id="FILE1")])

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append(("text", text, parse_mode))
        if self.text_error:
            raise self.text_error
        return types.SimpleNamespace(photo=None)


def run(coro):
    return asyncio.run(coro)


class MessagingTests(unittest.TestCase):
    def setUp(self):
        messaging._file_ids.clear()

    def test_without_image_is_plain_text(self):
        bot = FakeBot()
        run(messaging.send_message(bot, 1, "<b>Привет</b>"))
        self.assertEqual(bot.calls, [("text", "<b>Привет</b>", "HTML")])

    def test_image_goes_as_caption_in_one_message(self):
        bot = FakeBot()
        run(messaging.send_message(bot, 1, "<b>Привет</b>", "https://x/y.jpg"))
        self.assertEqual(bot.calls, [("photo", "https://x/y.jpg", "<b>Привет</b>", "HTML")])  # одно сообщение

    def test_file_id_is_reused(self):
        bot = FakeBot()
        run(messaging.send_message(bot, 1, "a", "https://x/y.jpg"))
        run(messaging.send_message(bot, 1, "b", "https://x/y.jpg"))
        self.assertEqual(bot.calls[1][1], "FILE1")

    def test_broken_image_falls_back_to_text(self):
        bot = FakeBot(photo_error=TelegramBadRequest("Bad Request: failed to get HTTP URL content"))
        run(messaging.send_message(bot, 1, "текст", "https://x/bad"))
        self.assertEqual(bot.calls[-1], ("text", "текст", "HTML"))

    def test_too_long_caption_is_sent_as_text(self):
        bot = FakeBot()
        run(messaging.send_message(bot, 1, "я" * 1025, "https://x/y.jpg"))
        self.assertEqual([c[0] for c in bot.calls], ["text"])

    def test_markup_error_retries_without_tags(self):
        class PickyBot(FakeBot):
            async def send_message(self, chat_id, text, parse_mode=None):
                self.calls.append(("text", text, parse_mode))
                if parse_mode == "HTML":
                    raise TelegramBadRequest("Bad Request: can't parse entities")
                return types.SimpleNamespace(photo=None)

        bot = PickyBot()
        run(messaging.send_message(bot, 1, "<b>x</b> &amp; y"))
        self.assertEqual(bot.calls[-1], ("text", "x & y", None))

    def test_blocked_user_error_propagates(self):
        bot = FakeBot(photo_error=TelegramForbiddenError("blocked"))
        with self.assertRaises(TelegramForbiddenError):
            run(messaging.send_message(bot, 1, "t", "https://x/y.jpg"))


if __name__ == "__main__":
    unittest.main()
