"""Вход по одному паролю: подписанная cookie, без сторонних зависимостей."""
import hashlib
import hmac
import secrets
import time

from .config import get_settings

COOKIE = "session"
MAX_AGE = 30 * 24 * 3600  # 30 дней

_MAX_FAILS = 5
_LOCK_SECONDS = 60
_fails: dict[str, tuple[int, float]] = {}  # ip -> (число ошибок, время блокировки)


def _key() -> bytes:
    s = get_settings()
    base = s.session_secret or s.admin_password  # смена пароля разлогинивает всех
    return hashlib.sha256(("bot-rotator-session:" + base).encode()).digest()


def make_token() -> str:
    payload = str(int(time.time()) + MAX_AGE)
    sig = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.partition(".")
    expected = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def check_password(password: str) -> bool:
    return secrets.compare_digest(password.encode(), get_settings().admin_password.encode())


def locked_for(ip: str) -> int:
    """Сколько секунд ещё действует блокировка этого IP (0 — не заблокирован)."""
    fails, until = _fails.get(ip, (0, 0.0))
    remaining = int(until - time.time())
    return remaining if fails >= _MAX_FAILS and remaining > 0 else 0


def register_failure(ip: str) -> None:
    fails, until = _fails.get(ip, (0, 0.0))
    if until and until < time.time():
        fails = 0
    fails += 1
    _fails[ip] = (fails, time.time() + _LOCK_SECONDS if fails >= _MAX_FAILS else 0.0)
    if len(_fails) > 1000:  # не даём словарю расти бесконечно
        _fails.clear()


def clear_failures(ip: str) -> None:
    _fails.pop(ip, None)
