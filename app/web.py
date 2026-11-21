import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramNotFound, TelegramUnauthorizedError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import auth, avatars, comeback, moderation, promo, push, repo, sandbox, shop, shop_io, support, tasks
from .metrics import metrics
from .events import bus
from .models import STATUS_ACTIVE, STATUS_QUEUED

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


# ---------- зависимости ----------

def require_ready(request: Request) -> None:
    """Пока приложение не подключилось к БД (или не хватает настроек) — понятный 503 вместо 500."""
    if request.app.state.manager is None:
        raise HTTPException(status_code=503, detail=f"Сервис не готов: {request.app.state.error}")


def same_origin(request: Request) -> None:
    """Защита от CSRF: для изменяющих запросов проверяем, что Origin совпадает с адресом сайта."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-origin request blocked")


def is_authed(request: Request) -> bool:
    return auth.verify_token(request.cookies.get(auth.COOKIE))


def api_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Требуется вход")


router = APIRouter(dependencies=[Depends(require_ready), Depends(same_origin)])
api = APIRouter(prefix="/api", dependencies=[Depends(api_auth)])


# ---------- модели запросов ----------

class BotIn(BaseModel):
    token: str = ""
    name: str = ""
    description: str = ""
    avatar_url: str = ""
    message_image_url: str = ""


class ReorderIn(BaseModel):
    ids: list[int]


class PushSubIn(BaseModel):
    endpoint: str
    keys: dict[str, str] = {}


class PushUnsubIn(BaseModel):
    endpoint: str


class PushTestIn(BaseModel):
    endpoint: str


class CandiesIn(BaseModel):
    target: str = ""
    amount: str = ""  # строкой, чтобы не терять знаки после запятой у рублей/долларов
    currency: Literal["candy", "rub", "usd", "star"] = "candy"
    action: Literal["give", "take"]
    confirm: bool = False


class UserTargetIn(BaseModel):
    target: str = ""
    confirm: bool = False


class BlockIn(BaseModel):
    target: str = ""
    scope: Literal["suggest", "receipts", "tasks"]
    action: Literal["block", "unblock"]
    confirm: bool = False


class ConfirmIn(BaseModel):
    confirm: bool = False


class ModeratorIn(BaseModel):
    target: str = ""  # @username, username или Telegram user_id


class ShopImportIn(BaseModel):
    text: str = ""
    confirm: bool = False   # False — только проверить файл и показать, что будет заменено; True — применить


class AboutIn(BaseModel):
    about: str = ""


class AutoMessageIn(BaseModel):
    text: str = ""                  # сообщение, которое уходит через 30 минут после капчи (как введено)
    percent: int | str | None = ""  # скидка по промокоду, %
    reply: str = ""                 # ответ бота, когда человек ввёл свой промокод


class SupportIn(BaseModel):
    contact: str = Field("", max_length=300)


class ReturnNoticeIn(BaseModel):
    text: str = ""                  # уведомление «вы вернулись» (как введено); пусто — выключено


class TaskIn(BaseModel):
    button_text: str = ""           # подпись кнопки задания в боте
    about: str = ""                 # описание задания (оформление и [а/б] — как у товаров)
    reward: int | str = ""          # награда в конфетах 🍬
    screenshot: bool = False        # требовать ли прикрепить скриншот (награду тогда начисляет модератор)
    visible: bool = True            # показывать ли задание в боте
    one_time: bool = True           # одноразовое: после награды повторно выполнить нельзя (кнопка синяя)
    comment: str = ""               # пояснение для админа: что сделал пользователь (в боте для чеков — вместо названия)


class ProductIn(BaseModel):
    name: str = ""
    price: int | str = ""       # целое число конфет (строкой тоже можно)
    about: str = ""             # «О товаре» как есть: **жирный**, __подчёркнутый__, *курсив*, ~~зачёркнутый~~ и т. д.
    color: str = ""             # стандартный | красный | зелёный | синий (или default/red/green/blue)
    visible: bool = True        # показывать ли товар в магазине


# ---------- хелперы ----------

def _clean_url(url: str, what: str) -> str | None:
    url = url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, f"{what} должна начинаться с http:// или https://")
    return url or None


def clean_profile(
    name: str, description: str, avatar_url: str, message_image_url: str = ""
) -> tuple[str, str, str | None, str | None]:
    name, description = name.strip(), description.strip()
    if not name:
        raise HTTPException(400, "Укажите имя бота")
    if len(name) > 64:
        raise HTTPException(400, "Имя бота — не более 64 символов (лимит Telegram)")
    if len(description) > 512:
        raise HTTPException(400, "Описание — не более 512 символов (лимит Telegram)")
    return (
        name,
        description,
        _clean_url(avatar_url, "Ссылка на аватарку"),
        _clean_url(message_image_url, "Ссылка на картинку для сообщений"),
    )


async def check_token(token: str) -> tuple[str | None, bool]:
    """Возвращает (username, валиден). Если Telegram недоступен — (None, True): токен добавим, проверим позже."""
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        return me.username, True
    except (TelegramUnauthorizedError, TelegramNotFound):
        return None, False
    except Exception:
        return None, True
    finally:
        await bot.session.close()


async def validate_new_token(token: str) -> str | None:
    """Проверяет токен нового бота (формат, уникальность, Telegram). Возвращает username бота (или None)."""
    if not TOKEN_RE.match(token):
        raise HTTPException(400, "Токен выглядит неверно. Формат: 123456789:AA... (из @BotFather)")
    if await repo.token_exists(token):
        raise HTTPException(400, "Такой токен уже добавлен")
    username, valid = await check_token(token)
    if not valid:
        raise HTTPException(400, "Telegram отклонил этот токен (недействителен или отозван)")
    return username


def _bot_dict(b, queue: int | None) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "description": b.description,
        "avatar_url": b.avatar_url,
        # «версия» аватарки: меняется, когда ссылка или факт её отправки в Telegram меняются, и браузер
        # не показывает старую картинку из своего кэша
        "avatar_v": hashlib.md5(f"{b.avatar_url}|{b.avatar_applied}".encode()).hexdigest()[:8],
        "message_image_url": b.message_image_url,
        "username": b.username,
        "status": b.status,
        "queue": queue,
        "masked_token": b.masked_token,
        # False — бот-шаблон без токена: стоит в очереди, но запустить его нельзя, пока токен не добавлен
        "has_token": b.has_token,
        "last_error": b.last_error,
    }


def _moderator_dict(m) -> dict:
    return {
        "id": m.id,
        "user_id": m.user_id,
        "username": m.username,
        # True — добавлен по юзернейму, ID появится, когда человек впервые напишет боту
        "pending": m.user_id is None,
    }


async def build_state(request: Request) -> dict:
    manager = request.app.state.manager
    bots = await repo.list_ordered()

    items: list[dict] = []
    num = 0
    for b in bots:
        if b.status in (STATUS_ACTIVE, STATUS_QUEUED):
            num += 1
            items.append(_bot_dict(b, num))
        else:
            items.append(_bot_dict(b, None))

    active = next((b for b in bots if b.status == STATUS_ACTIVE), None)
    current = None
    if active:
        st = manager.state
        running = bool(st["running"] and st["bot_id"] == active.id)
        current = {
            **_bot_dict(active, 1),
            "running": running,
            "note": st["note"] if not running else "работает",
            "users": manager.users_count() if running else None,
            # счётчики за всё время — только из памяти (UserStats), БД на каждое обновление страницы не трогаем
            "totals": manager.user_totals() if running else None,
            # сколько видео сейчас в галерее этого бота (одобренные заявки)
            "videos": await repo.count_gallery_videos(active.id) if running else None,
            "since": active.active_since.isoformat() if active.active_since else None,
        }

    return {
        "bots": items,
        "current": current,
        "has_any": bool(bots),
        "moderators": [_moderator_dict(m) for m in await repo.list_moderators()],
        "alerts": bus.recent_alerts(),
        "push_key": push.public_key,
    }


# ---------- вход / выход ----------

@router.get("/login")
async def login_page(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    wait = auth.locked_for(ip)
    if wait:
        return templates.TemplateResponse(
            request, "login.html", {"error": f"Слишком много попыток. Подождите {wait} с."}, status_code=429
        )
    if not auth.check_password(password):
        auth.register_failure(ip)
        return templates.TemplateResponse(request, "login.html", {"error": "Неверный пароль"}, status_code=401)

    auth.clear_failures(ip)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.COOKIE,
        auth.make_token(),
        max_age=auth.MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/")
async def index(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(BASE / "static" / "app.html", headers={"Cache-Control": "no-store"})


# ---------- API ----------

@api.get("/state")
async def get_state(request: Request):
    return await build_state(request)


@api.get("/metrics")
async def get_metrics():
    """Лёгкий опрос для подвала сайта: значения только из памяти, БД не трогаем."""
    return JSONResponse(metrics.snapshot(), headers={"Cache-Control": "no-store"})


@api.get("/events")
async def events(request: Request):
    """Server-Sent Events: изменения состояния и число пользователей в реальном времени."""
    queue = bus.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"  # держим соединение живым за прокси
                    if await request.is_disconnected():
                        break
                    continue
                if event.get("type") == "_close":
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.post("/bots")
async def add_bot(body: BotIn, request: Request):
    token = body.token.strip()
    name, description, avatar, image = clean_profile(body.name, body.description, body.avatar_url, body.message_image_url)

    # токен можно не указывать: бот встанет в очередь как шаблон без токена, токен добавляется позже
    username = None
    if token:
        username = await validate_new_token(token)

    await repo.add_bot(token or None, name, description, avatar, username, image)
    request.app.state.manager.poke()
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/bots/reorder")
async def reorder(body: ReorderIn):
    await repo.reorder(body.ids)
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/bots/{bot_id}/edit")
async def edit_bot(bot_id: int, body: BotIn, request: Request):
    name, description, avatar, image = clean_profile(body.name, body.description, body.avatar_url, body.message_image_url)

    # токен можно добавить только боту-шаблону (без токена); после этого бот становится «настоящим» и может запускаться
    new_token = body.token.strip()
    if new_token:
        bot = await repo.get_bot(bot_id)
        if bot is None:
            raise HTTPException(404, "Бот не найден")
        if bot.has_token:
            raise HTTPException(400, "У этого бота токен уже есть — заменить его нельзя")
        username = await validate_new_token(new_token)
        if not await repo.set_token(bot_id, new_token, username):
            raise HTTPException(409, "Не удалось сохранить токен: бот изменился, обновите страницу")

    await repo.update_profile(bot_id, name, description, avatar, image)
    avatars.invalidate(bot_id)
    # если бот сейчас активен — перезапускаем его: новые имя/описание/аватарка уходят в Telegram,
    # а обработчики получают свежий профиль (картинка сообщений)
    request.app.state.manager.poke(switch=await repo.is_active(bot_id))
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/bots/{bot_id}/activate")
async def activate_bot(bot_id: int, request: Request):
    bot = await repo.get_bot(bot_id)
    if bot is not None and not bot.has_token:
        raise HTTPException(400, "У этого бота нет токена — это шаблон. Сначала добавьте токен (шестерёнка → редактировать)")
    if not await repo.activate(bot_id):
        raise HTTPException(404, "Бот не найден")
    request.app.state.manager.poke(switch=True)
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/bots/{bot_id}/requeue")
async def requeue_bot(bot_id: int, request: Request):
    await repo.requeue(bot_id)
    request.app.state.manager.poke()
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, request: Request):
    was_active = await repo.is_active(bot_id)
    await repo.delete_bot(bot_id)
    request.app.state.manager.poke(switch=was_active)
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.get("/bots/{bot_id}/avatar")
async def bot_avatar(bot_id: int, request: Request):
    """Картинка бота для панели.
    Если на сайте задана новая ссылка, которая ещё не ушла в Telegram, показываем её.
    Иначе показываем актуальное фото бота из Telegram (так видно и то, что поменяли через BotFather)."""
    bot = await repo.get_bot(bot_id)
    if bot is None:
        raise HTTPException(404, "Бот не найден")
    no_store = {"Cache-Control": "no-store"}

    if bot.avatar_url and bot.avatar_url != bot.avatar_applied:
        return RedirectResponse(bot.avatar_url, status_code=302, headers=no_store)

    tg = await avatars.get_tg_avatar(bot.id, bot.token) if bot.has_token else None
    if tg:
        data, ctype, etag = tg
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=data, media_type=ctype, headers=headers)

    if bot.avatar_url:
        return RedirectResponse(bot.avatar_url, status_code=302, headers=no_store)
    raise HTTPException(404, "Аватарки нет")


@api.get("/bots/{bot_id}/token")
async def reveal_token(bot_id: int):
    bot = await repo.get_bot(bot_id)
    if bot is None:
        raise HTTPException(404, "Бот не найден")
    if not bot.has_token:
        raise HTTPException(404, "У этого бота нет токена")
    return {"token": bot.token}


# ---------- «Песочница» ----------

@api.post("/sandbox/candies")
async def sandbox_candies(body: CandiesIn, request: Request):
    try:
        return await sandbox.change_candies(
            request.app.state.manager, body.target, body.amount, body.currency, body.action == "give", body.confirm
        )
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)


@api.post("/sandbox/randomize")
async def sandbox_randomize(body: ConfirmIn, request: Request):
    try:
        return await sandbox.reroll(request.app.state.manager, body.confirm)
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)


@api.post("/sandbox/delete-videos")
async def sandbox_delete_videos(body: ConfirmIn, request: Request):
    try:
        res = await sandbox.delete_all_videos(request.app.state.manager, body.confirm)
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)
    bus.publish({"type": "gallery", "bot_id": res["bot_id"], "videos": 0})
    return res


@api.post("/sandbox/block")
async def sandbox_block(body: BlockIn, request: Request):
    try:
        return await sandbox.set_block(request.app.state.manager, body.target, body.scope, body.action == "block", body.confirm)
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)


@api.post("/sandbox/delete-user")
async def sandbox_delete_user(body: UserTargetIn, request: Request):
    try:
        res = await sandbox.remove_user(request.app.state.manager, body.target, body.confirm)
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)
    st = request.app.state.manager.stats
    if st:
        bus.publish({"type": "stats", "bot_id": st.bot_id, "users": res["users"]})
    return res


# ---------- модераторы «Предложки» ----------

@api.post("/moderators")
async def add_moderator(body: ModeratorIn):
    """Добавляет модератора по @username или ID. Человека может не быть среди пользователей бота."""
    try:
        kind, value = sandbox.parse_target(body.target)
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)
    user_id, username = (int(value), None) if kind == "id" else (None, str(value))
    mod = await repo.add_moderator(user_id, username)
    if mod is None:
        raise HTTPException(409, "Этот модератор уже в списке")
    moderation.cache.invalidate()
    bus.publish({"type": "changed"})
    return {"ok": True, "moderator": _moderator_dict(mod)}


@api.delete("/moderators/{mod_id}")
async def delete_moderator(mod_id: int):
    if not await repo.delete_moderator(mod_id):
        raise HTTPException(404, "Модератор не найден")
    moderation.cache.invalidate()
    bus.publish({"type": "changed"})
    return {"ok": True}


# ---------- автосообщения и промокоды ----------

@api.get("/automessage")
async def get_automessage():
    cfg = await promo.load_settings(fresh=True)
    return {
        "text": cfg.text, "percent": cfg.percent, "reply": cfg.reply,
        "delay_minutes": promo.AUTO_DELAY // 60, "max_text": promo.MAX_TEXT, "max_raw": promo.MAX_RAW,
    }


@api.post("/automessage")
async def save_automessage(body: AutoMessageIn):
    try:
        cfg = promo.normalize(body.text, body.percent, body.reply)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await promo.save_settings(cfg)
    return {"ok": True, "text": cfg.text, "percent": cfg.percent, "reply": cfg.reply}


@api.post("/automessage/test")
async def test_automessage(body: UserTargetIn, request: Request):
    """Проверка без ожидания: шлёт сохранённое автосообщение выбранному пользователю (@username или ID) прямо сейчас."""
    try:
        return await sandbox.test_automessage(request.app.state.manager, body.target, body.confirm)
    except sandbox.SandboxError as e:
        raise HTTPException(e.status, e.message)


@api.post("/automessage/length")
async def automessage_length(body: AboutIn):
    """Счётчик под полями: сколько введено и сколько займёт в Telegram в худшем случае."""
    text = body.about.replace("\r\n", "\n").replace("\r", "\n").strip()
    return {
        "site": len(text), "site_max": promo.MAX_RAW,
        "telegram": promo.telegram_length(text), "telegram_max": promo.MAX_TEXT,
    }


# ---------- уведомление «вы вернулись» + удвоение наград (вкладка «Автосообщения») ----------

@api.get("/returnnotice")
async def get_return_notice():
    cfg = await comeback.load_settings(fresh=True)
    return {"text": cfg.text, "boost_hours": int(comeback.BOOST_DURATION.total_seconds() // 3600),
            "factor": comeback.BOOST_FACTOR, "max_text": comeback.MAX_TEXT, "max_raw": comeback.MAX_RAW}


@api.post("/returnnotice")
async def save_return_notice(body: ReturnNoticeIn):
    try:
        cfg = comeback.normalize(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await comeback.save_settings(cfg)
    return {"ok": True, "text": cfg.text}


@api.post("/returnnotice/length")
async def return_notice_length(body: AboutIn):
    text = body.about.replace("\r\n", "\n").replace("\r", "\n").strip()
    return {
        "site": len(text), "site_max": comeback.MAX_RAW,
        "telegram": comeback.telegram_length(text), "telegram_max": comeback.MAX_TEXT,
    }


# ---------- товары магазина ----------

def _product_dict(row) -> dict:
    p = shop.product_from_row(row)
    return {
        "id": p.id, "name": p.name, "price": p.price,
        "about": p.about, "about_html": shop.format_about(p.about),  # как введено / как увидит пользователь
        "color": p.color, "visible": bool(row.is_visible), "position": row.position,
    }


def _clean_product(body: ProductIn) -> dict:
    try:
        return {
            "name": shop.normalize_name(body.name), "price": shop.normalize_price(body.price),
            "about": shop.normalize_about(body.about), "color": shop.normalize_color(body.color),
            "is_visible": bool(body.visible),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@api.get("/shop/products")
async def list_shop_products():
    """Весь каталог (и скрытые товары) в порядке показа."""
    return {"products": [_product_dict(r) for r in await repo.list_shop_products(only_visible=False)]}


@api.post("/shop/products")
async def add_shop_product(body: ProductIn):
    values = _clean_product(body)
    if len(await repo.list_shop_products(only_visible=False)) >= 500:
        raise HTTPException(400, "Слишком много товаров")
    row = await repo.add_shop_product(
        values["name"], values["price"], values["about"], values["color"], values["is_visible"]
    )
    bus.publish({"type": "changed"})
    return {"ok": True, "product": _product_dict(row)}


@api.post("/shop/about-length")
async def shop_about_length(body: AboutIn):
    """Счётчик под полем «О товаре»: сколько символов введено на сайте (со скобками вариантов и значками оформления)
    и сколько займёт в Telegram в худшем случае (в каждой [а/б] самый длинный вариант, без значков оформления)."""
    about = body.about.replace("\r\n", "\n").replace("\r", "\n").strip()
    return {
        "site": len(about), "site_max": shop.MAX_ABOUT_RAW,
        "telegram": shop.telegram_about_length(about), "telegram_max": shop.MAX_ABOUT,
    }


@api.post("/shop/products/reorder")
async def reorder_shop_products(body: ReorderIn):
    await repo.reorder_shop_products(body.ids)
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/shop/products/{product_id}/edit")
async def edit_shop_product(product_id: int, body: ProductIn):
    row = await repo.update_shop_product(product_id, **_clean_product(body))
    if row is None:
        raise HTTPException(404, "Товар не найден")
    bus.publish({"type": "changed"})
    return {"ok": True, "product": _product_dict(row)}


@api.delete("/shop/products/{product_id}")
async def delete_shop_product(product_id: int):
    if not await repo.delete_shop_product(product_id):
        raise HTTPException(404, "Товар не найден")
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.get("/support")
async def get_support():
    return {"contact": (await support.load_settings(fresh=True)).contact}


@api.post("/support")
async def save_support(body: SupportIn):
    """Контакт поддержки для кнопки «Поддержка» в боте: @username или числовой ID; пусто — кнопка скрыта."""
    try:
        contact = support.normalize(body.contact)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await support.save_settings(support.Settings(contact))
    return {"ok": True, "contact": contact}


@api.get("/buttons/export")
@api.get("/shop/export")
async def export_buttons():
    """Все тексты вкладки «Кнопки в боте» одним .txt-файлом UTF-8: товары (и скрытые, в порядке показа) и задания."""
    text = shop_io.export_all(
        await repo.list_shop_products(only_visible=False), await repo.list_tasks(only_visible=False),
        (await support.load_settings(fresh=True)).contact,
    )
    name = f"bot-buttons-{datetime.now().strftime('%Y%m%d-%H%M')}.txt"
    return Response(
        text, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@api.post("/buttons/import")
@api.post("/shop/import")
async def import_buttons(body: ShopImportIn):
    """Восстановление вкладки «Кнопки в боте» из файла экспорта. Без confirm — только проверка (ничего не меняется); с
    confirm: true товары заменяются, а задания синхронизируются с файлом одной транзакцией. Файл старого формата (только
    товары) задания не трогает. Повреждённый файл -> 400, ничего не изменено."""
    try:
        parsed = shop_io.parse_all(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    current = len(await repo.list_shop_products(only_visible=False))
    current_tasks = len(await repo.list_tasks(only_visible=False))
    count_tasks = None if parsed.tasks is None else len(parsed.tasks)
    if not body.confirm:
        return {"ok": True, "applied": False, "count": len(parsed.products), "current": current,
                "names": [it.name for it in parsed.products[:10]],
                "tasks": count_tasks, "current_tasks": current_tasks,
                "task_names": [it.button_text for it in (parsed.tasks or [])[:10]],
                "support": parsed.support, "current_support": (await support.load_settings(fresh=True)).contact}
    try:
        await repo.restore_buttons(parsed.products, parsed.tasks)
    except Exception:
        logging.getLogger("bot.web").exception("Восстановление вкладки «Кнопки в боте» из файла не удалось")
        raise HTTPException(500, "Не удалось восстановить данные из файла, текущие данные не изменены")
    if parsed.support is not None:
        await support.save_settings(support.Settings(parsed.support))
    bus.publish({"type": "changed"})
    return {"ok": True, "applied": True, "count": len(parsed.products), "current": current,
            "tasks": count_tasks, "current_tasks": current_tasks}


# ---------- задания («Бесплатные конфеты»; настраиваются во вкладке «Кнопки в боте») ----------

def _task_dict(row) -> dict:
    t = tasks.task_from_row(row)
    return {
        "id": t.id, "button_text": t.button_text, "about": t.about, "reward": t.reward,
        "screenshot": t.require_screenshot, "visible": bool(row.is_visible), "one_time": t.one_time,
        "comment": t.comment, "position": row.position,
    }


def _clean_task(body: TaskIn) -> dict:
    try:
        return {
            "button_text": tasks.normalize_button_text(body.button_text), "about": tasks.normalize_description(body.about),
            "reward": tasks.normalize_reward(body.reward), "require_screenshot": bool(body.screenshot),
            "is_visible": bool(body.visible), "one_time": bool(body.one_time),
            "comment": tasks.normalize_comment(body.comment),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@api.get("/tasks")
async def list_tasks():
    """Все задания (и скрытые) в порядке показа."""
    return {
        "tasks": [_task_dict(r) for r in await repo.list_tasks(only_visible=False)],
        "daily_reward": tasks.DAILY_REWARD, "referral_reward": tasks.REFERRAL_REWARD,
    }


@api.post("/tasks")
async def add_task(body: TaskIn):
    values = _clean_task(body)
    if len(await repo.list_tasks(only_visible=False)) >= 200:
        raise HTTPException(400, "Слишком много заданий")
    row = await repo.add_task(
        values["button_text"], values["about"], values["reward"], values["require_screenshot"], values["is_visible"],
        values["one_time"], values["comment"],
    )
    bus.publish({"type": "changed"})
    return {"ok": True, "task": _task_dict(row)}


@api.post("/tasks/about-length")
async def task_about_length(body: AboutIn):
    """Счётчик под полем «Описание задания» (те же правила подсчёта, что у товаров, но свои лимиты)."""
    about = body.about.replace("\r\n", "\n").replace("\r", "\n").strip()
    return {
        "site": len(about), "site_max": tasks.MAX_DESC_RAW,
        "telegram": shop.telegram_about_length(about), "telegram_max": tasks.MAX_DESC,
    }


@api.post("/tasks/reorder")
async def reorder_tasks(body: ReorderIn):
    await repo.reorder_tasks(body.ids)
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/tasks/{task_id}/edit")
async def edit_task(task_id: int, body: TaskIn):
    row = await repo.update_task(task_id, **_clean_task(body))
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    bus.publish({"type": "changed"})
    return {"ok": True, "task": _task_dict(row)}


@api.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    if not await repo.delete_task(task_id):
        raise HTTPException(404, "Задание не найдено")
    bus.publish({"type": "changed"})
    return {"ok": True}


@api.post("/push/subscribe")
async def push_subscribe(body: PushSubIn):
    p256dh, auth_key = body.keys.get("p256dh"), body.keys.get("auth")
    if not (body.endpoint.startswith("https://") and p256dh and auth_key):
        raise HTTPException(400, "Некорректная подписка")
    await repo.add_push_sub(body.endpoint, p256dh, auth_key)
    return {"ok": True}


@api.post("/push/test")
async def push_test(body: PushTestIn):
    """Тестовое уведомление на это устройство через 10 секунд."""
    if not await push.schedule_test(body.endpoint, delay=10):
        raise HTTPException(404, "Это устройство не подписано на уведомления")
    return {"ok": True, "delay": 10}


@api.post("/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubIn):
    await repo.delete_push_sub(body.endpoint)
    return {"ok": True}


router.include_router(api)
