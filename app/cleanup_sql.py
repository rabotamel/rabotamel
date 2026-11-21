"""SQL автоочистки неактивных пользователей.

Вынесен в отдельный модуль без зависимостей: тот же текст запроса проверяется в тестах на sqlite3, а в
приложении выполняется в PostgreSQL (Neon) через app.repo.delete_inactive_users. Синтаксис намеренно ограничен
общим подмножеством SQL (row-value IN, NOT EXISTS, RETURNING), поэтому запрос одинаково понимают обе базы.
"""


def build_delete_inactive_sql(open_topup_statuses: tuple[str, ...], pending_submission_status: str) -> str:
    """Удаляет ОДНУ пачку (до :batch строк) пользователей, неактивных дольше порога, и возвращает (bot_id, user_id).

    Параметры: :cutoff — «сейчас минус 7 дней», :now — «сейчас» (для срока премиума), :batch — размер пачки.

    Не удаляются (защита важных данных):
      * пользователи с положительным балансом 🍬 (candies > 0);
      * пользователи с незавершённым заказом пополнения (создаётся / ждёт оплаты / чек на проверке / оплачен, но не
        начислен): иначе начисление не найдёт пользователя и оплата «зависнет»;
      * пользователи с заявкой предложки, ожидающей решения модератора;
      * владельцы действующего премиума (общий для всех токенов бота, но рвать «платного» клиента капчей незачем).
    Завершённые заказы и решённые заявки на удаление не влияют: баланс там уже учтён, привязки к bot_users (FK) нет.

    Условия по last_active_at и candies продублированы во внешнем DELETE: если пользователь успел стать активным
    между подзапросом и удалением, строка не удалится (PostgreSQL перепроверяет условия внешнего запроса).
    Подзапрос идёт по индексу idx_users_last_active_at (ORDER BY last_active_at LIMIT) — без полного сканирования.
    """
    statuses = ", ".join(f"'{s}'" for s in open_topup_statuses)
    return f"""
DELETE FROM bot_users
WHERE (bot_id, user_id) IN (
    SELECT u.bot_id, u.user_id
    FROM bot_users u
    WHERE u.last_active_at < :cutoff
      AND u.candies <= 0
      AND NOT EXISTS (
          SELECT 1 FROM topup_orders t
          WHERE t.bot_id = u.bot_id AND t.user_id = u.user_id AND t.status IN ({statuses})
      )
      AND NOT EXISTS (
          SELECT 1 FROM video_submissions v
          WHERE v.bot_id = u.bot_id AND v.user_id = u.user_id AND v.status = '{pending_submission_status}'
      )
      AND NOT EXISTS (
          SELECT 1 FROM user_premium p
          WHERE p.user_id = u.user_id AND (p.premium_forever OR p.premium_until > :now)
      )
    ORDER BY u.last_active_at
    LIMIT :batch
)
AND last_active_at < :cutoff
AND candies <= 0
RETURNING bot_id, user_id
"""
