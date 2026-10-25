import collections
import threading
import time

from ..core import Logger


class SlicerGate(object):
    """
    Честная FIFO-очередь доступа к физическому столу и движку Cura.
    Позволяет параллельным чатам общаться с LLM одновременно,
    но к столу и нарезке выстраивает их строго по очереди без гонок данных.
    """

    def __init__(self, events):
        self._events = events
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._active_chat_id = None
        self._active_count = 0

    def acquire(self, chat_id=None):
        chat_id = chat_id or "default"
        ready_event = threading.Event()

        with self._lock:
            # Реентерабельность: если этот же чат уже владеет столом, просто увеличиваем счетчик
            if self._active_chat_id == chat_id:
                self._active_count += 1
                return True

            # Если стол полностью свободен и очереди нет — занимаем сразу
            if self._active_chat_id is None and not self._queue:
                self._active_chat_id = chat_id
                self._active_count = 1
                return True

            # Иначе встаем в очередь
            self._queue.append((chat_id, ready_event))
            Logger.log("i", "Чат %s встал в очередь к столу (впереди: %d)", chat_id, len(self._queue) - 1)
            self._events.emit(
                "status",
                {"state": "waiting_slicer", "message": "Ожидание очереди к столу слайсера…"},
                chat_id=chat_id,
            )

        # Ждем своей очереди
        ready_event.wait()
        return True

    def release(self, chat_id=None):
        with self._lock:
            if self._active_count > 1:
                self._active_count -= 1
                return

            self._active_chat_id = None
            self._active_count = 0

            # Будим следующий чат из очереди
            if self._queue:
                next_chat_id, ready_event = self._queue.popleft()
                self._active_chat_id = next_chat_id
                self._active_count = 1
                Logger.log("i", "Очередь передала стол чату %s", next_chat_id)
                ready_event.set()

    def __call__(self, chat_id=None):
        return _SlicerContext(self, chat_id)


class _SlicerContext(object):
    def __init__(self, gate, chat_id):
        self._gate = gate
        self._chat_id = chat_id

    def __enter__(self):
        self._gate.acquire(self._chat_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._gate.release(self._chat_id)
