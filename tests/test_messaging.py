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
    tps = types.ModuleType("aiogram.types")

    class BufferedInputFile:
        def __init__(self, file, filename):
            self.file, self.filename = file, filename

    tps.BufferedInputFile = BufferedInputFile
    pkg = types.ModuleType("aiogram")
    pkg.exceptions, pkg.types = exc, tps
    sys.modules["aiogram"], sys.modules["aiogram.exceptions"], sys.modules["aiogram.types"] = pkg, exc, tps

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
        messaging._fetch_failed.clear()
        self._orig_fetch = messaging._fetch_image

        async def no_download(url):  # в тестах в сеть не ходим: «скачать не вышло» -> ссылка уходит в Telegram как есть
            return None

        messaging._fetch_image = no_download

    def tearDown(self):
        messaging._fetch_image = self._orig_fetch

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

    def test_image_is_downloaded_and_uploaded_as_file(self):
        async def fake_fetch(url):
            return b"JPEGDATA"

        messaging._fetch_image = fake_fetch
        bot = FakeBot()
        run(messaging.send_message(bot, 1, "<b>Привет</b>", "https://x/y"))
        kind, photo, caption, mode = bot.calls[0]
        self.assertEqual((kind, caption, mode), ("photo", "<b>Привет</b>", "HTML"))
        self.assertNotIsInstance(photo, str)  # не ссылка, а загруженный файл
        run(messaging.send_message(bot, 1, "b", "https://x/y"))
        self.assertEqual(bot.calls[1][1], "FILE1")  # дальше по file_id

    def test_rejected_upload_falls_back_to_url(self):
        async def fake_fetch(url):
            return b"JPEGDATA"

        class PickyBot(FakeBot):
            async def send_photo(self, chat_id, photo, caption=None, parse_mode=None):
                if not isinstance(photo, str):
                    self.calls.append(("upload",))
                    raise TelegramBadRequest("Bad Request: IMAGE_PROCESS_FAILED")
                return await super().send_photo(chat_id, photo, caption, parse_mode)

        messaging._fetch_image = fake_fetch
        bot = PickyBot()
        run(messaging.send_message(bot, 1, "t", "https://x/y.jpg"))
        self.assertEqual(bot.calls[0], ("upload",))
        self.assertEqual(bot.calls[1][:2], ("photo", "https://x/y.jpg"))

    def test_prepare_jpeg(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (3000, 1000), (255, 0, 0, 128)).save(buf, "PNG")
        out = messaging._prepare_jpeg(buf.getvalue())
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.format, "JPEG")
        self.assertLessEqual(max(img.size), messaging.MAX_SIDE)
        with self.assertRaises(ValueError):
            messaging._prepare_jpeg(b"not an image")

    def test_blocked_user_error_propagates(self):
        bot = FakeBot(photo_error=TelegramForbiddenError("blocked"))
        with self.assertRaises(TelegramForbiddenError):
            run(messaging.send_message(bot, 1, "t", "https://x/y.jpg"))


if __name__ == "__main__":
    unittest.main()
