from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.types import ChatMemberUpdated, Message

from .stats import UserStats


def build_router(greeting: str, stats: UserStats) -> Router:
    """Каждый запуск polling получает свежий Router (один роутер нельзя подключить к двум Dispatcher).
    Здесь потом появится основная логика бота."""
    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if message.from_user:
            stats.add(message.from_user.id)
        await message.answer(greeting)

    @router.message()
    async def on_any_message(message: Message) -> None:
        # любое сообщение в личке — пользователь «зашёл» в бота
        if message.from_user and message.chat.type == "private":
            stats.add(message.from_user.id)

    @router.my_chat_member()
    async def on_member_change(update: ChatMemberUpdated) -> None:
        if update.chat.type != "private":
            return
        status = update.new_chat_member.status
        if status in (ChatMemberStatus.KICKED, ChatMemberStatus.LEFT):
            stats.remove(update.chat.id)  # пользователь заблокировал бота
        elif status == ChatMemberStatus.MEMBER:
            stats.add(update.chat.id)

    return router
