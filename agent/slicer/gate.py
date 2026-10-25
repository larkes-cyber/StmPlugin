import threading

from ..ai import ToolError
from ..core import Logger
from ..core.dispatcher import current_chat, current_tool, main_waits_aborted

WAIT_POLL_SECONDS = 0.25


class SlicerGate(object):
    """
    Мьютекс слайсера с очередью.

    Сцена, профиль и бэкенд у STE Slicer одни на всех, поэтому в каждый момент со слайсером работает
    только один воркер чата. Остальные ждут здесь — их чат получает статус «queued» с указанием,
    чей запуск занял слайсер.

    - Реентерабельный: метод под замком может вызвать другой метод под замком (export_results →
      export_gcode) без самоблокировки.
    - Ожидание прерывается кнопкой «Стоп» (main_waits_aborted), а не висит до освобождения.
    - Используется как обычный замок: `with gate:`.
    """

    def __init__(self, events):
        self._events = events
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._owner_thread = None
        self._owner_chat = None
        self._owner_tool = None
        self._depth = 0
        self.title_of = None  # callback(chat_id) -> название чата, задаёт Context

    @property
    def owner_chat(self):
        with self._state_lock:
            return self._owner_chat

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

    def acquire(self):
        if self._lock.acquire(blocking=False):
            self._enter()
            return

        chat_id = current_chat()
        tool = current_tool()
        with self._state_lock:
            holder_chat = self._owner_chat
            holder_tool = self._owner_tool
        Logger.log("i", "Слайсер занят (чат %s, %s) — чат %s ждёт очереди для %s",
                   holder_chat or "-", holder_tool or "-", chat_id or "-", tool or "-")
        self._emit_status(chat_id, "queued", tool, holder_chat, holder_tool)

        while not self._lock.acquire(timeout=WAIT_POLL_SECONDS):
            if main_waits_aborted():
                self._emit_status(chat_id, "thinking", tool)
                raise ToolError("Остановлено пользователем, пока чат ждал очереди к слайсеру")

        self._enter()
        Logger.log("i", "Чат %s получил слайсер", chat_id or "-")
        self._emit_status(chat_id, "tool", tool)

    def release(self):
        with self._state_lock:
            self._depth -= 1
            if self._depth == 0:
                self._owner_thread = None
                self._owner_chat = None
                self._owner_tool = None
        self._lock.release()

    def _enter(self):
        with self._state_lock:
            if self._depth == 0:
                self._owner_thread = threading.get_ident()
                self._owner_chat = current_chat()
                self._owner_tool = current_tool()
            self._depth += 1

    def _emit_status(self, chat_id, state, tool, holder_chat=None, holder_tool=None):
        if not chat_id:
            return  # внешний MCP-клиент: чата нет, показывать статус некому
        data = {"state": state, "tool": tool}
        if state == "queued":
            data["holder_chat"] = holder_chat
            data["holder_tool"] = holder_tool
            data["holder_title"] = self._title(holder_chat)
        self._events.emit("status", data, chat_id=chat_id)

    def _title(self, chat_id):
        if not chat_id or self.title_of is None:
            return ""
        try:
            return self.title_of(chat_id) or ""
        except Exception:
            return ""
