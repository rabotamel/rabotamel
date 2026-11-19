"""Аватарки ботов.

сайт → Telegram:  push_avatar() скачивает картинку по ссылке, приводит к квадратному JPEG и отправляет методом
                  setMyProfilePhoto. Вызывается при запуске бота и при переключении токена.
Telegram → сайт:  get_tg_avatar() берёт актуальное фото бота из Telegram (кэш в памяти на 5 минут),
                  а наш сервер отдаёт его браузеру. Ссылка на файл содержит токен бота,
                  поэтому напрямую в браузер она никогда не уходит.
"""
import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import socket
import time
from urllib.parse import urljoin, urlparse

import aiohttp
from aiogram import Bot

log = logging.getLogger("bot.avatars")

MAX_DOWNLOAD = 5 * 1024 * 1024
CACHE_OK_TTL = 300
CACHE_FAIL_TTL = 60
OUT_SIZE = 640

# bot_id -> (кэш действителен до, (байты, content-type, etag) | None)
_cache: dict[int, tuple[float, tuple[bytes, str, str] | None]] = {}
_locks: dict[int, asyncio.Lock] = {}


class AvatarError(Exception):
    pass


def invalidate(bot_id: int) -> None:
    _cache.pop(bot_id, None)


def prime(bot_id: int, jpeg: bytes) -> None:
    """Кладёт в кэш только что загруженное в Telegram фото.
    Сразу после setMyProfilePhoto getUserProfilePhotos может ещё отдавать СТАРОЕ фото (например, поставленное
    через BotFather), и оно бы закрепилось в кэше на 5 минут — в панели казалось бы, что новая аватарка не встала.
    Через CACHE_OK_TTL кэш истечёт и фото снова возьмётся из Telegram."""
    etag = '"pushed-' + hashlib.sha1(jpeg).hexdigest()[:16] + '"'
    _cache[bot_id] = (time.monotonic() + CACHE_OK_TTL, (jpeg, "image/jpeg", etag))


# ---------- Telegram → сайт ----------

async def get_tg_avatar(bot_id: int, token: str) -> tuple[bytes, str, str] | None:
    hit = _cache.get(bot_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    lock = _locks.setdefault(bot_id, asyncio.Lock())
    async with lock:
        hit = _cache.get(bot_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        result = await _fetch_tg_photo(token)
        ttl = CACHE_OK_TTL if result else CACHE_FAIL_TTL
        _cache[bot_id] = (time.monotonic() + ttl, result)
        if len(_cache) > 200:
            _cache.pop(next(iter(_cache)))
        return result


async def _fetch_tg_photo(token: str) -> tuple[bytes, str, str] | None:
    bot = Bot(token=token)
    try:
        tg_id = int(token.split(":", 1)[0])
        photos = await bot.get_user_profile_photos(tg_id, limit=1)
        if not photos.photos:
            return None
        sizes = photos.photos[0]
        pick = next((p for p in sizes if p.width >= 320), sizes[-1])
        info = await bot.get_file(pick.file_id)
        buf = await bot.download_file(info.file_path)
        return buf.read(), "image/jpeg", f'"{pick.file_unique_id}"'
    except Exception as e:
        log.debug("Фото бота из Telegram получить не удалось: %s", type(e).__name__)
        return None
    finally:
        await bot.session.close()


# ---------- сайт → Telegram ----------

def _host_is_public(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


async def _download(url: str) -> bytes:
    """Скачивает картинку. Только http(s), только публичные адреса, до 3 редиректов, до 5 МБ."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(4):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise AvatarError("некорректная ссылка на аватарку")
            port = p.port or (443 if p.scheme == "https" else 80)
            if not await asyncio.to_thread(_host_is_public, p.hostname, port):
                raise AvatarError("ссылка ведёт на недоступный или внутренний адрес")
            async with session.get(url, allow_redirects=False) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status != 200:
                    raise AvatarError(f"сервер картинки ответил {r.status}")
                data = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > MAX_DOWNLOAD:
                        raise AvatarError("картинка больше 5 МБ")
                return bytes(data)
    raise AvatarError("слишком много редиректов")


def _to_jpeg(data: bytes) -> bytes:
    try:
        from PIL import Image, ImageOps
    except ImportError as e:
        raise AvatarError("на сервере не установлен Pillow") from e
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception as e:
        raise AvatarError("файл по ссылке не похож на изображение") from e

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if side > OUT_SIZE:
        img = img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


async def push_avatar(token: str, url: str) -> bytes:
    """Ставит фото бота в Telegram (setMyProfilePhoto). Возвращает отправленный JPEG.
    Бросает AvatarError с понятной причиной."""
    data = await _download(url)
    jpeg = await asyncio.to_thread(_to_jpeg, data)

    form = aiohttp.FormData()
    form.add_field("photo", json.dumps({"type": "static", "photo": "attach://avatar"}))
    form.add_field("avatar", jpeg, filename="avatar.jpg", content_type="image/jpeg")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(f"https://api.telegram.org/bot{token}/setMyProfilePhoto", data=form) as r:
                body = await r.json(content_type=None)
    except Exception as e:  # в тексте исключения может быть адрес с токеном — не пишем его
        raise AvatarError(f"не удалось связаться с Telegram ({type(e).__name__})") from None
    if not body.get("ok"):
        raise AvatarError(f"Telegram отклонил фото: {body.get('description', 'неизвестная ошибка')}")
    return jpeg
