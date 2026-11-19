import asyncio
import logging

from . import repo

log = logging.getLogger("bot.stats")


def norm_username(username: str | None) -> str | None:
    """Юзернеймы в Telegram регистронезависимы — храним и сравниваем в нижнем регистре."""
    u = (username or "").strip().lstrip("@").lower()
    return u or None


class UserStats:
    """Пользователи текущего бота, прошедшие капчу.

    Пользователь попадает сюда (и в БД) только после успешной капчи; тех, кто её не прошёл, здесь нет и в БД они
    не пишутся — ни строки, ни счётчика.

    Экономия ресурсов:
      * вся проверка «прошёл ли капчу» идёт по словарю в памяти (O(1), без запросов к БД);
      * изменения копятся в памяти и уходят в БД ОДНОЙ транзакцией на пачку; одновременные flush() не
        дублируются (блокировка), поэтому несколько человек, прошедших капчу одновременно, дают один INSERT;
      * смена юзернейма пользователя тоже копится и пишется пачкой, а не на каждое его сообщение;
      * если изменений нет, flush() не обращается к БД вовсе.
    """

    def __init__(self, bot_id: int) -> None:
        self.bot_id = bot_id
        self._names: dict[int, str | None] = {}   # user_id -> username; ключи = все прошедшие капчу
        self._to_add: dict[int, str | None] = {}
        self._to_rename: dict[int, str | None] = {}
        self._to_remove: set[int] = set()
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        self._names = await repo.load_users(self.bot_id)

    @property
    def count(self) -> int:
        return len(self._names)

    def has(self, user_id: int) -> bool:
        return user_id in self._names

    def find_by_username(self, username: str) -> int | None:
        """Поиск по @username среди прошедших капчу (перебор в памяти; вызывается только из «Песочницы»)."""
        wanted = norm_username(username)
        if wanted is None:
            return None
        for uid, name in self._names.items():
            if name == wanted:
                return uid
        return None

    def username_of(self, user_id: int) -> str | None:
        return self._names.get(user_id)

    def add(self, user_id: int, username: str | None = None) -> bool:
        """True, если пользователь добавлен сейчас (его не было). Синхронно — защита от двойного нажатия.
        Ожидающее удаление (если пользователя перед этим удалили) не отменяется: при сбросе в БД сначала
        удалится старая строка, потом вставится новая — с нулевым балансом."""
        if user_id in self._names:
            return False
        name = norm_username(username)
        self._names[user_id] = name
        self._to_add[user_id] = name
        return True

    def touch(self, user_id: int, username: str | None) -> None:
        """Вызывается на каждое сообщение прошедшего капчу пользователя. Записи в БД не вызывает сразу:
        только если юзернейм изменился, и тогда он попадает в ближайшую пачку."""
        if user_id not in self._names:
            return
        name = norm_username(username)
        if self._names[user_id] == name:
            return
        self._names[user_id] = name
        if user_id in self._to_add:
            self._to_add[user_id] = name
        else:
            self._to_rename[user_id] = name

    def remove(self, user_id: int) -> bool:
        """Убирает пользователя из памяти и ставит удаление в БД в очередь. True, если он был."""
        existed = user_id in self._names
        self._names.pop(user_id, None)
        self._to_add.pop(user_id, None)
        self._to_rename.pop(user_id, None)
        self._to_remove.add(user_id)  # DELETE идемпотентен, лишняя строка в очереди ничего не стоит
        return existed

    def dirty(self) -> bool:
        return bool(self._to_add or self._to_rename or self._to_remove)

    async def flush(self) -> None:
        if not self.dirty():
            return
        async with self._lock:
            # внутри блокировки: параллельный flush уже мог всё записать — тогда делать нечего
            if not self.dirty():
                return
            add, rename, remove = self._to_add, self._to_rename, self._to_remove
            self._to_add, self._to_rename, self._to_remove = {}, {}, set()
            try:
                await repo.save_users(self.bot_id, add, rename, remove)
            except Exception as e:
                log.warning("Не удалось сохранить пользователей бота %s: %s", self.bot_id, e)
                # вернём в очередь, но не поверх более свежих изменений
                self._to_remove |= remove
                for uid in add:
                    if uid in self._names and uid not in self._to_add:
                        self._to_add[uid] = self._names[uid]
                for uid in rename:
                    if uid in self._names and uid not in self._to_add and uid not in self._to_rename:
                        self._to_rename[uid] = self._names[uid]
