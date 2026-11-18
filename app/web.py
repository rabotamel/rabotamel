import re
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramNotFound, TelegramUnauthorizedError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import repo
from .config import get_settings
from .models import STATUS_ACTIVE

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


# ---------- защита панели ----------

def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    s = get_settings()
    user_ok = secrets.compare_digest(creds.username.encode(), s.admin_user.encode())
    pass_ok = secrets.compare_digest(creds.password.encode(), s.admin_password.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def same_origin(request: Request) -> None:
    """Защита от CSRF: браузер сам подставляет Basic-логин, поэтому для POST проверяем Origin."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-origin request blocked")


def require_ready(request: Request) -> None:
    """Пока приложение не подключилось к БД (или не хватает настроек) — понятный 503 вместо 500."""
    if request.app.state.manager is None:
        raise HTTPException(status_code=503, detail=f"Сервис не готов: {request.app.state.error}")


router = APIRouter(dependencies=[Depends(require_ready), Depends(require_auth), Depends(same_origin)])


# ---------- хелперы ----------

def back(msg: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse("/" + (f"?{urlencode(params)}" if params else ""), status_code=303)


def clean_profile(name: str, description: str, avatar_url: str) -> tuple[str, str, str | None]:
    name, description, avatar_url = name.strip(), description.strip(), avatar_url.strip()
    if not name:
        raise ValueError("Укажите имя бота")
    if len(name) > 64:
        raise ValueError("Имя бота — не более 64 символов (лимит Telegram)")
    if len(description) > 512:
        raise ValueError("Описание — не более 512 символов (лимит Telegram)")
    if avatar_url and not avatar_url.lower().startswith(("http://", "https://")):
        raise ValueError("Ссылка на аватарку должна начинаться с http:// или https://")
    return name, description, avatar_url or None


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


# ---------- страницы ----------

@router.get("/")
async def index(request: Request, msg: str | None = None, err: str | None = None):
    bots = await repo.list_bots()
    manager = request.app.state.manager
    has_active = any(b.status == STATUS_ACTIVE for b in bots)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"bots": bots, "state": manager.state, "msg": msg, "err": err, "has_active": has_active},
    )


@router.post("/bots")
async def add_bot(
    request: Request,
    token: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    avatar_url: str = Form(""),
):
    token = token.strip()
    if not TOKEN_RE.match(token):
        return back(err="Токен выглядит неверно. Формат: 123456789:AA... (из @BotFather)")
    try:
        name, description, avatar = clean_profile(name, description, avatar_url)
    except ValueError as e:
        return back(err=str(e))
    if await repo.token_exists(token):
        return back(err="Такой токен уже добавлен")

    username, valid = await check_token(token)
    if not valid:
        return back(err="Telegram отклонил этот токен (недействителен или отозван)")

    await repo.add_bot(token, name, description, avatar, username)
    request.app.state.manager.poke()
    return back(msg=f"Бот «{name}» добавлен")


@router.post("/bots/{bot_id}/edit")
async def edit_bot(
    request: Request,
    bot_id: int,
    name: str = Form(...),
    description: str = Form(""),
    avatar_url: str = Form(""),
):
    try:
        name, description, avatar = clean_profile(name, description, avatar_url)
    except ValueError as e:
        return back(err=str(e))
    await repo.update_profile(bot_id, name, description, avatar)
    # если бот сейчас активен — перезапускаем его, чтобы новые имя/описание ушли в Telegram
    request.app.state.manager.poke(switch=await repo.is_active(bot_id))
    return back(msg="Профиль сохранён")


@router.post("/bots/{bot_id}/activate")
async def activate_bot(request: Request, bot_id: int):
    if not await repo.activate(bot_id):
        return back(err="Бот не найден")
    request.app.state.manager.poke(switch=True)
    return back(msg="Переключаю бота на выбранный токен")


@router.post("/bots/{bot_id}/requeue")
async def requeue_bot(request: Request, bot_id: int):
    await repo.requeue(bot_id)
    request.app.state.manager.poke()
    return back(msg="Токен возвращён в очередь")


@router.post("/bots/{bot_id}/delete")
async def delete_bot(request: Request, bot_id: int):
    was_active = await repo.is_active(bot_id)
    await repo.delete_bot(bot_id)
    request.app.state.manager.poke(switch=was_active)
    return back(msg="Бот удалён")
