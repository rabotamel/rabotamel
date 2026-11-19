"""Отправка сообщений бота пользователям.

Если у бота задана картинка для сообщений, текст уходит как подпись к ней — ОДНИМ сообщением (картинка сверху,
текст под ней). Исключение — капча: её шлют напрямую, без картинки (см. handlers.send_captcha).

Картинку сначала скачивает САМ сервер (как и аватарку) и загружает в Telegram файлом: так работает с любыми
ссылками, в том числе теми, которые Telegram не может открыть сам (защита от ботов у хостинга, редиректы, ссылки
без расширения, webp/png/gif и т. п.). Если скачать не вышло — пробуем отдать Telegram саму ссылку.
После первой удачной отправки запоминаем file_id — дальше картинка идёт без повторной загрузки. Если картинка не
отправилась совсем (ссылка недоступна, не изображение, подпись длиннее лимита Telegram) — человек всё равно
получает текст, а причина пишется в лог.
"""
import asyncio
import io
import logging
import re
import time
from html import unescape

from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger("bot.messaging")

CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото (в символах после разбора разметки)

# (id бота, ссылка) -> file_id. file_id действует только для того бота, который его получил.
_file_ids: dict[tuple[int, str], str] = {}

# замки на (бот, ссылка): пока первая отправка грузит картинку, остальные ждут и берут уже готовый file_id
_locks: dict[tuple[int, str], asyncio.Lock] = {}

# ссылка -> когда скачать не вышло: недавно проваленную ссылку не качаем заново на каждое сообщение (минуту)
_fetch_failed: dict[str, float] = {}
FETCH_RETRY_AFTER = 60.0

MAX_SIDE = 2560  # Telegram: сумма сторон фото не больше 10000, берём с запасом

_TAG_RE = re.compile(r"</?(?:b|i|u|s|a|code|pre|blockquote)\b[^>]*>")


def plain(text: str) -> str:
    """HTML-текст без тегов (запасной вариант, если Telegram не разобрал разметку)."""
    return unescape(_TAG_RE.sub("", text))


def is_blocked_by_user(e: BaseException) -> bool:
    """True, только если Telegram прямо говорит, что ПОЛЬЗОВАТЕЛЬ сам заблокировал бота («bot was blocked by the user»).
    Другие 403 (пользователь удалил аккаунт, не запускал бота, бота выгнали из чата) сюда не относятся."""
    from aiogram.exceptions import TelegramForbiddenError

    return isinstance(e, TelegramForbiddenError) and "bot was blocked by the user" in str(e).lower()


def _is_parse_error(e: TelegramBadRequest) -> bool:
    return "parse entities" in str(e).lower()


def _prepare_jpeg(data: bytes) -> bytes:
    """Приводит скачанную картинку к виду, который Telegram точно примет как фото (JPEG, стороны до 2560 px).
    Подходящий JPEG остаётся как есть, без повторного сжатия. Бросает ValueError, если это не изображение."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError("файл по ссылке не похож на изображение") from e

    orientation = img.getexif().get(0x0112, 1)
    if img.format == "JPEG" and orientation in (0, 1) and max(img.size) <= MAX_SIDE:
        return data

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


async def _fetch_image(url: str) -> bytes | None:
    """Скачивает картинку по ссылке на сервере. None — не вышло (причина в логе); тогда отдаём Telegram ссылку."""
    failed_at = _fetch_failed.get(url)
    if failed_at is not None and time.monotonic() - failed_at < FETCH_RETRY_AFTER:
        return None
    try:
        from . import avatars

        data = await avatars._download(url)  # только публичные адреса, редиректы, до 5 МБ
        result = await asyncio.to_thread(_prepare_jpeg, data)
        _fetch_failed.pop(url, None)
        return result
    except Exception as e:
        log.warning("Не удалось скачать картинку сообщений %s: %s", url, e)
        if len(_fetch_failed) > 200:
            _fetch_failed.clear()
        _fetch_failed[url] = time.monotonic()
        return None


async def _send_photo(bot, chat_id: int, caption: str, url: str, parse_mode: str | None, reply_markup=None):
    key = (bot.id, url)
    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}  # только если задана
    file_id = _file_ids.get(key)
    if file_id:
        try:
            return await bot.send_photo(chat_id, file_id, caption=caption, parse_mode=parse_mode, **extra)
        except TelegramBadRequest as e:
            if _is_parse_error(e):
                raise
            _file_ids.pop(key, None)  # file_id мог устареть — загрузим картинку заново

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # пока ждали замок, другая отправка могла уже получить file_id
        file_id = _file_ids.get(key)
        if file_id:
            try:
                return await bot.send_photo(chat_id, file_id, caption=caption, parse_mode=parse_mode, **extra)
            except TelegramBadRequest as e:
                if _is_parse_error(e):
                    raise
                _file_ids.pop(key, None)

        msg = None
        data = await _fetch_image(url)
        if data is not None:
            from aiogram.types import BufferedInputFile

            try:
                msg = await bot.send_photo(
                    chat_id, BufferedInputFile(data, filename="image.jpg"), caption=caption, parse_mode=parse_mode, **extra
                )
            except TelegramBadRequest as e:
                if _is_parse_error(e):
                    raise
                log.warning("Telegram не принял загруженную картинку (%s), пробую по ссылке", e)
        if msg is None:
            msg = await bot.send_photo(chat_id, url, caption=caption, parse_mode=parse_mode, **extra)

        photo = getattr(msg, "photo", None)
        if photo:
            if len(_file_ids) > 500:
                _file_ids.pop(next(iter(_file_ids)))
            _file_ids[key] = photo[-1].file_id
        if len(_locks) > 500:
            _locks.clear()
        return msg


async def send_message(bot, chat_id: int, text: str, image_url: str | None = None, reply_markup=None):
    """Шлёт HTML-текст; с картинкой — как фото с подписью. Ошибки доставки (например, пользователь заблокировал
    бота) пробрасываются как у обычного bot.send_message; разметку и картинку метод страхует сам."""
    if image_url and len(plain(text)) <= CAPTION_LIMIT:
        try:
            return await _send_photo(bot, chat_id, text, image_url, "HTML", reply_markup)
        except TelegramBadRequest as e:
            if _is_parse_error(e):
                log.warning("Подпись не ушла с разметкой (%s), отправляю без неё", e)
                try:
                    return await _send_photo(bot, chat_id, plain(text), image_url, None, reply_markup)
                except TelegramBadRequest as e2:
                    e = e2
            log.warning("Картинка к сообщению не отправилась (%s), отправляю только текст", e)

    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
    try:
        return await bot.send_message(chat_id, text, parse_mode="HTML", **extra)
    except TelegramBadRequest as e:
        if not _is_parse_error(e):
            raise
        log.warning("Сообщение не ушло с разметкой (%s), отправляю без неё", e)
        return await bot.send_message(chat_id, plain(text), parse_mode=None, **extra)
