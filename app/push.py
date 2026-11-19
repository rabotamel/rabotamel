"""Web Push для администраторов. Ключи VAPID генерируются при первом запуске и хранятся в БД."""
import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import repo
from .config import get_settings

log = logging.getLogger("bot.push")

_private_pem: str | None = None
public_key: str | None = None  # в формате base64url для applicationServerKey в браузере
_tasks: set[asyncio.Task] = set()


async def init_vapid() -> None:
    global _private_pem, public_key
    pem = await repo.kv_get("vapid_private_pem")
    if not pem:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        await repo.kv_set("vapid_private_pem", pem)
        log.info("Сгенерированы VAPID-ключи для Web Push")

    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    _private_pem = pem
    public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def _deliver(sub, payload: str) -> bool:
    """Отправляет одно уведомление. True — доставлено сервису push; подписка отозванная (404/410) удаляется."""
    try:
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush не установлен — push-уведомления отключены")
        return False

    info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
    try:
        await asyncio.to_thread(
            webpush,
            subscription_info=info,
            data=payload,
            vapid_private_key=Vapid.from_pem(_private_pem.encode()),
            vapid_claims={"sub": get_settings().vapid_subject},
            ttl=3600,
        )
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):  # подписка отозвана
            await repo.delete_push_sub(sub.endpoint)
        else:
            log.warning("Push не отправлен: %s", e)
    except Exception as e:
        log.warning("Push не отправлен: %s", e)
    return False


async def notify_all(title: str, body: str) -> None:
    """Рассылает уведомление всем сохранённым подписчикам. Никогда не бросает исключений."""
    try:
        if _private_pem is None:
            return
        subs = await repo.list_push_subs()
        if not subs:
            return
        payload = json.dumps({"title": title, "body": body}, ensure_ascii=False)
        await asyncio.gather(*(_deliver(s, payload) for s in subs))
    except Exception:
        log.exception("Ошибка рассылки push")


async def _delayed_test(sub, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        payload = json.dumps(
            {
                "title": "Тестовое уведомление",
                "body": "Всё работает. Так будет выглядеть оповещение, если бот перестанет отвечать.",
            },
            ensure_ascii=False,
        )
        await _deliver(sub, payload)
    except Exception:
        log.exception("Ошибка тестового push")


async def schedule_test(endpoint: str, delay: float = 10) -> bool:
    """Через `delay` секунд отправляет тестовое уведомление на конкретное устройство.
    False — такой подписки на сервере нет (устройство нужно подписать заново)."""
    if _private_pem is None:
        return False
    sub = await repo.get_push_sub(endpoint)
    if sub is None:
        return False
    task = asyncio.create_task(_delayed_test(sub, delay))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
