import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app import db
from app.config import get_settings
from app.manager import BotManager
from app.web import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def _startup(app: FastAPI) -> None:
    """Подключение к БД и запуск бота идут в фоне, чтобы HTTP-порт открылся сразу:
    Render для Web Service ждёт открытый порт и иначе считает деплой неудачным
    (например, пока Neon «просыпается» или если не задана переменная окружения)."""
    try:
        settings = get_settings()
    except RuntimeError as e:
        app.state.error = str(e)
        log.error("%s", e)
        return

    delay = 2.0
    while True:
        try:
            await db.init_db(settings.database_url)
            break
        except Exception as e:
            app.state.error = f"База данных недоступна: {e}"
            log.error("Не удалось подключиться к БД, повтор через %.0f с: %s", delay, e)
            await db.close_db()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    manager = BotManager(settings)
    app.state.manager = manager
    app.state.error = None
    manager.start()
    log.info("Приложение готово")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = None
    app.state.error = "запускается…"
    startup = asyncio.create_task(_startup(app), name="startup")
    try:
        yield
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        if app.state.manager is not None:
            await app.state.manager.stop()
        await db.close_db()


# Документацию API отключаем: панель закрыта паролем, а лишнего наружу светить не нужно
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


if __name__ == "__main__":
    # Render передаёт порт в переменной PORT; слушать нужно 0.0.0.0
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
