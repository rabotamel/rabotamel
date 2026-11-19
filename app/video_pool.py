"""Пул одобренных видео для кнопки «Купить видео» в галерее.

Одобренные заявки (status = approved) читаются из БД один раз на бота и кэшируются в памяти вместе с file_id
(минуту; сразу пополняются, когда модератор принимает видео), поэтому каждая покупка не ходит в БД. Видео выбирается
случайно, но с защитой от частых повторов: у каждого пользователя помнятся последние показанные ролики, и пока есть
другие — они не повторяются; в маленьком пуле хотя бы не выпадает одно и то же видео два раза подряд.
Видео, которое Telegram отказался отправить (file_id недействителен), на время исключается из выдачи.
"""
import random
import time
from collections import deque

from . import repo

POOL_TTL = 60.0        # через сколько секунд пул перечитывается из БД
RECENT_LIMIT = 8       # сколько последних видео помним у каждого пользователя
BAD_TTL = 600.0        # на сколько исключаем видео, которое не удалось отправить


class VideoPool:
    def __init__(self) -> None:
        self._pools: dict[int, tuple[float, list[tuple[int, str]]]] = {}
        self._recent: dict[tuple[int, int], deque[int]] = {}
        self._bad: dict[int, float] = {}

    def invalidate(self, bot_id: int | None = None) -> None:
        if bot_id is None:
            self._pools.clear()
        else:
            self._pools.pop(bot_id, None)

    def add(self, bot_id: int, sub_id: int, file_id: str) -> None:
        """Только что одобренное видео сразу попадает в уже загруженный пул (если он не загружен — прочитается при покупке)."""
        entry = self._pools.get(bot_id)
        if entry is not None and all(i != sub_id for i, _ in entry[1]):
            entry[1].append((sub_id, file_id))

    async def items(self, bot_id: int) -> list[tuple[int, str]]:
        entry = self._pools.get(bot_id)
        if entry is None or time.monotonic() - entry[0] > POOL_TTL:
            entry = (time.monotonic(), await repo.load_approved_pool(bot_id))
            self._pools[bot_id] = entry
        return entry[1]

    def _good(self, items: list[tuple[int, str]]) -> list[tuple[int, str]]:
        now = time.monotonic()
        for sub_id, until in list(self._bad.items()):
            if until <= now:
                del self._bad[sub_id]
        return [it for it in items if it[0] not in self._bad]

    async def pick(self, bot_id: int, user_id: int) -> tuple[int, str] | None:
        """(id заявки, file_id) или None, если подходящих видео нет."""
        items = self._good(await self.items(bot_id))
        if not items:
            return None
        recent = self._recent.get((bot_id, user_id))
        if recent:
            fresh = [it for it in items if it[0] not in recent]
            if not fresh:  # пул меньше списка недавних — хотя бы не то, что показали только что
                fresh = [it for it in items if it[0] != recent[-1]]
            items = fresh or items
        return random.choice(items)

    def mark_shown(self, bot_id: int, user_id: int, sub_id: int) -> None:
        if len(self._recent) > 20000:
            self._recent.clear()
        self._recent.setdefault((bot_id, user_id), deque(maxlen=RECENT_LIMIT)).append(sub_id)

    def mark_bad(self, sub_id: int) -> None:
        if len(self._bad) > 5000:
            self._bad.clear()
        self._bad[sub_id] = time.monotonic() + BAD_TTL


pool = VideoPool()
