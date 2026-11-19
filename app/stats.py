import logging

from . import repo

log = logging.getLogger("bot.stats")


class UserStats:
    """Счётчик пользователей текущего бота.

    Все изменения идут в оперативную память (set), а в БД сбрасываются пачкой раз в несколько секунд —
    так частые события не превращаются в частые запросы к базе. Число для панели берётся из памяти.

    Важно: Bot API не умеет отдавать список пользователей бота, поэтому считаются те, кто написал боту
    (или запустил/заблокировал его) с момента, когда этот счётчик заработал.
    """

    def __init__(self, bot_id: int) -> None:
        self.bot_id = bot_id
        self._users: set[int] = set()
        self._to_add: set[int] = set()
        self._to_remove: set[int] = set()

    async def load(self) -> None:
        self._users = await repo.load_user_ids(self.bot_id)

    @property
    def count(self) -> int:
        return len(self._users)

    def add(self, user_id: int) -> None:
        if user_id not in self._users:
            self._users.add(user_id)
            self._to_add.add(user_id)
            self._to_remove.discard(user_id)

    def remove(self, user_id: int) -> None:
        if user_id in self._users:
            self._users.discard(user_id)
            self._to_remove.add(user_id)
            self._to_add.discard(user_id)

    async def flush(self) -> None:
        if not self._to_add and not self._to_remove:
            return
        add, remove = self._to_add, self._to_remove
        self._to_add, self._to_remove = set(), set()
        try:
            await repo.save_users(self.bot_id, add, remove)
        except Exception as e:
            log.warning("Не удалось сохранить пользователей бота %s: %s", self.bot_id, e)
            # вернём в очередь, но не поверх более свежих изменений
            self._to_add |= {u for u in add if u in self._users}
            self._to_remove |= {u for u in remove if u not in self._users}
