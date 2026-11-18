import os
from dataclasses import dataclass
from functools import lru_cache


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_user: str
    admin_password: str
    admin_chat_id: int | None
    greeting: str
    # Сколько подряд сетевых ошибок к Telegram считаем «критической ошибкой соединения»
    network_fail_threshold: int
    # Если Telegram просит подождать дольше N секунд (флуд-контроль) — меняем токен
    flood_rotate_threshold: int
    # Сколько раз подряд может упасть polling, прежде чем токен будет заменён
    max_polling_crashes: int


@lru_cache
def get_settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Не задана переменная окружения DATABASE_URL (строка подключения Neon)")

    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        raise RuntimeError(
            "Не задана переменная окружения ADMIN_PASSWORD. "
            "Без пароля админ-панель с токенами ботов оставлять нельзя."
        )

    chat_id_raw = os.environ.get("ADMIN_CHAT_ID", "").strip()

    return Settings(
        database_url=database_url,
        admin_user=os.environ.get("ADMIN_USER", "admin"),
        admin_password=admin_password,
        admin_chat_id=int(chat_id_raw) if chat_id_raw else None,
        greeting=os.environ.get("GREETING_TEXT", "Привет, это тестовый код."),
        network_fail_threshold=_int("NETWORK_FAIL_THRESHOLD", 8),
        flood_rotate_threshold=_int("FLOOD_ROTATE_THRESHOLD", 300),
        max_polling_crashes=_int("MAX_POLLING_CRASHES", 3),
    )
