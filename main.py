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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await db.init_db(settings.database_url)

    manager = BotManager(settings)
    app.state.manager = manager
    manager.start()
    try:
        yield
    finally:
        await manager.stop()
        await db.close_db()


# Документацию API отключаем: панель закрыта паролем, а лишнего наружу светить не нужно
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
