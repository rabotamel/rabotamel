import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramNotFound, TelegramUnauthorizedError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import auth, avatars, moderation, push, repo, sandbox
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
    currency: Literal["candy", "rub", "usd"] = "candy"
    action: Literal["give", "take"]
    confirm: bool = False


class UserTargetIn(BaseModel):
    target: str = ""
    confirm: bool = False


class ConfirmIn(BaseModel):
    confirm: bool = False


class ModeratorIn(BaseModel):
    target: str = ""  # @username, username или Telegram user_id


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
