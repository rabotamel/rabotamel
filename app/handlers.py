from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


def build_router(greeting: str) -> Router:
    """Каждый запуск polling получает свежий Router (один роутер нельзя подключить к двум Dispatcher).
    Здесь потом появится основная логика бота."""
    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(greeting)

    return router
