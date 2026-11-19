"""Отправка сообщений бота пользователям.

Если у бота задана картинка для сообщений, текст уходит как подпись к ней — ОДНИМ сообщением (картинка сверху,
текст под ней). Исключение — капча: её шлют напрямую, без картинки (см. handlers.send_captcha).

Картинка отправляется по ссылке (Telegram сам её скачивает). После первой удачной отправки запоминаем file_id —
дальше картинка идёт без повторной загрузки. Если картинка не отправилась (ссылка недоступна, не изображение,
подпись длиннее лимита Telegram) — человек всё равно получает текст.
"""
import logging
import re
from html import unescape

from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger("bot.messaging")

CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото (в символах после разбора разметки)

# (id бота, ссылка) -> file_id. file_id действует только для того бота, который его получил.
_file_ids: dict[tuple[int, str], str] = {}

_TAG_RE = re.compile(r"</?(?:b|i|u|s|a|code|pre|blockquote)\b[^>]*>")


def plain(text: str) -> str:
    """HTML-текст без тегов (запасной вариант, если Telegram не разобрал разметку)."""
    return unescape(_TAG_RE.sub("", text))


def _is_parse_error(e: TelegramBadRequest) -> bool:
    return "parse entities" in str(e).lower()


async def _send_photo(bot, chat_id: int, caption: str, url: str, parse_mode: str | None):
    key = (bot.id, url)
    file_id = _file_ids.get(key)
    if file_id:
        try:
            return await bot.send_photo(chat_id, file_id, caption=caption, parse_mode=parse_mode)
        except TelegramBadRequest as e:
            if _is_parse_error(e):
                raise
            _file_ids.pop(key, None)  # file_id мог устареть — пробуем ещё раз по ссылке
    msg = await bot.send_photo(chat_id, url, caption=caption, parse_mode=parse_mode)
    photo = getattr(msg, "photo", None)
    if photo:
        if len(_file_ids) > 500:
            _file_ids.pop(next(iter(_file_ids)))
        _file_ids[key] = photo[-1].file_id
    return msg


async def send_message(bot, chat_id: int, text: str, image_url: str | None = None):
    """Шлёт HTML-текст; с картинкой — как фото с подписью. Ошибки доставки (например, пользователь заблокировал
    бота) пробрасываются как у обычного bot.send_message; разметку и картинку метод страхует сам."""
    if image_url and len(plain(text)) <= CAPTION_LIMIT:
        try:
            return await _send_photo(bot, chat_id, text, image_url, "HTML")
        except TelegramBadRequest as e:
            if _is_parse_error(e):
                log.warning("Подпись не ушла с разметкой (%s), отправляю без неё", e)
                try:
                    return await _send_photo(bot, chat_id, plain(text), image_url, None)
                except TelegramBadRequest as e2:
                    e = e2
            log.warning("Картинка к сообщению не отправилась (%s), отправляю только текст", e)

    try:
        return await bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramBadRequest as e:
        if not _is_parse_error(e):
            raise
        log.warning("Сообщение не ушло с разметкой (%s), отправляю без неё", e)
        return await bot.send_message(chat_id, plain(text), parse_mode=None)
