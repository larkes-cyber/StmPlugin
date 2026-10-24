import threading
import time


class EventBus(object):
    def __init__(self, max_history=500):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events = []
        self._max_history = max_history
        self._seq = 0

    def emit(self, event_type, payload=None, **kwargs):
        chat_id = kwargs.pop("chat_id", "default")
        data = (
            dict(payload)
            if isinstance(payload, dict)
            else ({"value": payload} if payload is not None else {})
        )
        if kwargs:
            data.update(kwargs)

        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "seq": self._seq,
                "type": event_type,
                "chat_id": chat_id,
                "data": data,
                "timestamp": time.time(),
            }
            event.update(data)
            self._events.append(event)

            if len(self._events) > self._max_history:
                self._events.pop(0)

            self._cond.notify_all()
            return event

    def get_events(self, since_id=0, timeout=0.0, chat_id=None):
        deadline = time.time() + float(timeout) if timeout > 0 else 0.0

        with self._lock:
            while True:
                events = [e for e in self._events if e["id"] > since_id]
                if chat_id:
                    events = [e for e in events if e["chat_id"] == chat_id]

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
