import asyncio
import logging
import time
from collections import OrderedDict

from . import repo

log = logging.getLogger("bot.stats")

# Троттлинг last_active_at: в БД активность пользователя пишется при первом взаимодействии и затем не чаще, чем раз в
# 6 часов (при 7-дневном пороге очистки этого более чем достаточно). Остальные сообщения/нажатия БД не трогают.
ACTIVITY_THROTTLE_SECONDS = 6 * 3600
# Предел кэша времени последней записи (LRU): вытеснение стоит лишь одной лишней записью при следующем обращении
ACTIVITY_CACHE_MAX = 100_000


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
      * если изменений нет, flush() не обращается к БД вовсе;
      * счётчики «за всё время» (`totals`): три числа читаются из БД ОДИН раз при запуске бота (`repo.load_user_totals`),
        дальше растут в памяти: «впервые прошёл капчу» приходит из того же пакетного сохранения (flush), приглашённый
        по ссылке — из `note_referral`. Сайт и события берут числа отсюда, к БД ради них не ходим;
      * активность (`bot_users.last_active_at`, по ней чистятся давно ушедшие, см. app/cleanup.py) пишется с
        троттлингом: кэш «когда в БД записывали активность» в памяти, повторные сообщения в пределах 6 часов БД не
        трогают, а те, что нужно записать, уходят той же пачкой — одним UPDATE на всех.
    """

    def __init__(self, bot_id: int, clock=time.time) -> None:
        self.bot_id = bot_id
        self._clock = clock
        # user_id -> время (unix) последней записи активности в БД; порядок = от давних к свежим (LRU)
        self._last_db_active: OrderedDict[int, float] = OrderedDict()
        self._to_activity: set[int] = set()
        self._names: dict[int, str | None] = {}   # user_id -> username; ключи = все прошедшие капчу
        self._to_add: dict[int, str | None] = {}
        self._to_rename: dict[int, str | None] = {}
        self._to_remove: set[int] = set()
        self._lock = asyncio.Lock()
        # «за всё время»: None, пока не загружено (или БД не ответила) — сайт покажет прочерк
        self._joined: int | None = None      # прошли капчу у этого бота за всё время (с вышедшими и удалёнными)
        self._referred: int | None = None    # из них приглашены по реферальной ссылке
        self._joined_all: int | None = None  # прошли капчу у всех ботов за всё время

    async def load(self) -> None:
        self._names = await repo.load_users(self.bot_id)
        try:
            self._joined, self._referred, self._joined_all = await repo.load_user_totals(self.bot_id)
        except Exception as e:   # статистика не должна мешать запуску бота
            log.warning("Не удалось загрузить счётчики пользователей бота %s: %s", self.bot_id, e)
        # кэш троттлинга заполняется из БД одним запросом: после перезапуска процесса (на бесплатном Render они частые)
        # уже записанная недавно активность заново не пишется
        stored = await repo.load_last_active(self.bot_id)
        fresh = sorted(((ts, uid) for uid, ts in stored.items() if uid in self._names))[-ACTIVITY_CACHE_MAX:]
        self._last_db_active = OrderedDict((uid, ts) for ts, uid in fresh)
        self._to_activity = set()

    @property
    def count(self) -> int:
        return len(self._names)

    @property
    def totals(self) -> dict | None:
        """Счётчики за всё время для сайта (только память): joined — у этого бота, referred — из них по ссылке,
        all_bots — у всех ботов. None — не загружены."""
        if self._joined is None or self._referred is None or self._joined_all is None:
            return None
        return {"joined": self._joined, "referred": self._referred, "all_bots": self._joined_all}

    def note_referral(self) -> None:
        """Засчитан новый приглашённый по ссылке (запись в БД уже сделана repo.register_referral)."""
        if self._referred is not None:
            self._referred += 1

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
        self._remember_activity(user_id, self._clock())  # новая строка в БД получает last_active_at = now() сама
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

    def _remember_activity(self, user_id: int, ts: float) -> None:
        self._last_db_active[user_id] = ts
        self._last_db_active.move_to_end(user_id)
        while len(self._last_db_active) > ACTIVITY_CACHE_MAX:
            self._last_db_active.popitem(last=False)

    def touch_activity(self, user_id: int, *, force: bool = False) -> bool:
        """Отмечает взаимодействие пользователя с ботом (сообщение, нажатие кнопки). БД не трогает сразу: True, если
        активность поставлена в очередь на запись (первое взаимодействие или с прошлой записи прошло больше 6 часов,
        либо force=True — для критичных действий вроде оплаты), и False, если запись не нужна (троттлинг).
        Синхронно и без обращений к БД, поэтому дёшево вызывать на каждое обновление."""
        if user_id not in self._names:
            return False
        now = self._clock()
        last = self._last_db_active.get(user_id)
        if not force and last is not None and 0 <= now - last < ACTIVITY_THROTTLE_SECONDS:
            return False
        self._remember_activity(user_id, now)
        self._to_activity.add(user_id)
        return True

    def forget(self, user_ids) -> int:
        """Убирает пользователей из памяти БЕЗ постановки удаления в очередь: их уже удалила автоочистка
        (app/cleanup.py). Если человек потом снова напишет боту, он пройдёт капчу как новый. Возвращает, сколько
        было в памяти."""
        n = 0
        for uid in user_ids:
            if self._names.pop(uid, None) is not None or uid in self._to_add:
                n += 1
            self._to_add.pop(uid, None)
            self._to_rename.pop(uid, None)
            self._to_activity.discard(uid)
            self._last_db_active.pop(uid, None)
        return n

    def remove(self, user_id: int) -> bool:
        """Убирает пользователя из памяти и ставит удаление в БД в очередь. True, если он был."""
        existed = user_id in self._names
        self._names.pop(user_id, None)
        self._to_add.pop(user_id, None)
        self._to_rename.pop(user_id, None)
        self._to_activity.discard(user_id)
        self._last_db_active.pop(user_id, None)
        self._to_remove.add(user_id)  # DELETE идемпотентен, лишняя строка в очереди ничего не стоит
        return existed

    def dirty(self) -> bool:
        return bool(self._to_add or self._to_rename or self._to_remove or self._to_activity)

    async def flush(self) -> None:
        if not self.dirty():
            return
        async with self._lock:
            # внутри блокировки: параллельный flush уже мог всё записать — тогда делать нечего
            if not self.dirty():
                return
            add, rename, remove = self._to_add, self._to_rename, self._to_remove
            active = self._to_activity
            self._to_add, self._to_rename, self._to_remove, self._to_activity = {}, {}, set(), set()
            if add or rename or remove:
                try:
                    new = await repo.save_users(self.bot_id, add, rename, remove)
                    if isinstance(new, int) and new > 0 and self._joined is not None and self._joined_all is not None:
                        self._joined += new         # впервые прошли капчу (вернувшиеся после удаления не в счёт)
                        self._joined_all += new
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
            # активность — после сохранения пачки (вставка новых строк уже прошла); удалённых и забытых пропускаем
            active = {uid for uid in active if uid in self._names and uid not in remove}
            if active:
                try:
                    await repo.touch_users_active(self.bot_id, list(active))
                except Exception as e:
                    log.warning("Не удалось сохранить активность пользователей бота %s: %s", self.bot_id, e)
                    self._to_activity |= {uid for uid in active if uid in self._names}
