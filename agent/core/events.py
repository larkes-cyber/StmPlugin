import threading
import time
import uuid

from .logger import Logger
from .serialize import to_jsonable


class EventBus(object):
    def __init__(self, max_history=500):
        self.session = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events = []
        self._listeners = []
        self._max_history = max_history
        self._seq = 0
        self._chat_resolver = None

    def set_chat_resolver(self, resolver):
        """resolver() -> chat_id для событий без явного chat_id (см. Context)."""
        self._chat_resolver = resolver

    def _default_chat(self):
        if self._chat_resolver is not None:
            try:
                chat_id = self._chat_resolver()
                if chat_id:
                    return chat_id
            except Exception:
                Logger.logException("Не удалось определить чат события")
        return "default"

    def subscribe(self, listener):
        with self._lock:
            self._listeners.append(listener)

    def emit(self, event_type, payload=None, **kwargs):
        chat_id = kwargs.pop("chat_id", None) or self._default_chat()
        data = (
            dict(payload)
            if isinstance(payload, dict)
            else ({"value": payload} if payload is not None else {})
        )
        if kwargs:
            data.update(kwargs)
        data = to_jsonable(data)

        with self._lock:
            self._seq += 1
            seq_id = self._seq
            event = {
                "id": seq_id,
                "seq": seq_id,
                "type": event_type,
                "chat_id": chat_id,
                "data": data,
                "timestamp": time.time(),
            }
            for k, v in data.items():
                if k not in ("id", "seq"):
                    event[k] = v
            self._events.append(event)

            if len(self._events) > self._max_history:
                self._events.pop(0)

            listeners = list(self._listeners)
            self._cond.notify_all()

        for listener in listeners:
            try:
                listener(event)
            except Exception:
                Logger.logException("Слушатель события %s", event_type)
        return event

    def get_events(self, since_id=0, timeout=0.0, chat_id=None):
        try:
            target_since = int(since_id or 0)
        except (ValueError, TypeError):
            target_since = 0

        deadline = time.time() + float(timeout) if timeout > 0 else 0.0

        with self._lock:
            while True:
                events = []
                for e in self._events:
                    eid = e.get("seq") if isinstance(e.get("seq"), int) else e.get("id")
                    if isinstance(eid, int) and eid > target_since:
                        events.append(e)

                if chat_id:
                    events = [e for e in events if e.get("chat_id") == chat_id]

                if events or timeout <= 0:
                    return events

                remaining = deadline - time.time()
                if remaining <= 0:
                    return []

                self._cond.wait(remaining)

    def since(self, since_id=0, timeout=0.0, chat_id=None):
        return self.get_events(since_id=since_id, timeout=timeout, chat_id=chat_id)

    def clear(self, chat_id=None):
        with self._lock:
            if chat_id is None:
                self._events = []
            else:
                self._events = [e for e in self._events if e["chat_id"] != chat_id]
            self._cond.notify_all()
