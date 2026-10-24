import threading
import time


class EventBus(object):
    def __init__(self, max_events=3000):
        self._events = []
        self._last_seq = 0
        self._max_events = max_events
        self._condition = threading.Condition()

    def emit(self, event_type, **data):
        with self._condition:
            self._last_seq += 1
            event = {
                "seq": self._last_seq,
                "type": event_type,
                "time": time.time(),
            }
            event.update(data)
            self._events.append(event)

            overflow = len(self._events) - self._max_events
            if overflow > 0:
                del self._events[:overflow]

            self._condition.notify_all()
        return event

    def since(self, seq, timeout=0.0):
        deadline = time.time() + timeout
        with self._condition:
            while True:
                new_events = [event for event in self._events if event["seq"] > seq]
                if new_events:
                    return new_events

                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def last_seq(self):
        with self._condition:
            return self._last_seq

    def clear(self):
        with self._condition:
            self._events = []
        return self.emit("reset")
