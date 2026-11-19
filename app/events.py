"""Шина событий в памяти: рассылает изменения подключённым браузерам (SSE) и хранит последние предупреждения."""
import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.alerts: deque[dict] = deque(maxlen=20)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # медленный клиент — пропускаем, он подтянет состояние при следующем событии

    def add_alert(self, level: str, title: str, text: str) -> dict:
        alert = {
            "id": str(int(time.time() * 1000)),
            "level": level,
            "title": title,
            "text": text,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.alerts.append(alert)
        self.publish({"type": "alert", "alert": alert})
        return alert

    def recent_alerts(self, hours: int = 24) -> list[dict]:
        limit = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return [a for a in self.alerts if a["ts"] >= limit]

    def close(self) -> None:
        self.publish({"type": "_close"})


bus = EventBus()
