# -*- coding: utf-8 -*-
"""Шина событий для SPA: агент и инструменты публикуют события, браузер забирает их long-polling'ом."""
import threading
import time


class EventBus(object):
    def __init__(self, max_events=3000):
        self._events = []
        self._seq = 0
        self._max = max_events
        self._cond = threading.Condition()

    def emit(self, event_type, **data):
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "type": event_type, "time": time.time()}
            event.update(data)
            self._events.append(event)
            if len(self._events) > self._max:
                del self._events[:len(self._events) - self._max]
            self._cond.notify_all()
        return event

    def since(self, seq, timeout=0.0):
        deadline = time.time() + timeout
        with self._cond:
            while True:
                items = [e for e in self._events if e["seq"] > seq]
                if items:
                    return items
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    def last_seq(self):
        with self._cond:
            return self._seq

    def clear(self):
        """Очистить историю (seq продолжает расти, чтобы клиенты не путались)."""
        with self._cond:
            self._events = []
        return self.emit("reset")
