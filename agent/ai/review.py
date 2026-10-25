import threading

from ..core import Logger
from .tools import ToolError

REVIEW_TIMEOUT = 30 * 60

CONTINUE_INSTRUCTION = (
    "Пользователь проверил параметры и разрешил продолжить. Его изменения окончательные: "
    "не меняй их обратно. Если есть комментарий — учти его, затем продолжай работу."
)


class ReviewGate(object):
    """Проверка параметров пользователем. У каждого чата своя ожидающая проверка:
    параллельные чаты могут ждать подтверждения одновременно и не мешают друг другу."""

    def __init__(self, events, slicer, timeout=REVIEW_TIMEOUT):
        self._events = events
        self._slicer = slicer
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending = {}  # ключ чата → ожидающая проверка
        self._review_count = 0

    @property
    def pending_id(self):
        with self._lock:
            ids = [pending["id"] for pending in self._pending.values()]
        return ids[0] if ids else None

    def pending_ids(self):
        with self._lock:
            return dict((pending["chat_id"], pending["id"]) for pending in self._pending.values())

    def request(self, summary="", chat_id=None):
        proposal = self._slicer.review_proposal()
        pending = self._open_review(chat_id)
        review_id = pending["id"]

        Logger.log("i", "review %s requested (чат %s)", review_id, chat_id or "-")
        self._emit(
            pending,
            "review_requested",
            review_id=review_id,
            summary=summary,
            proposal=proposal,
        )

        try:
            answered = pending["answered"].wait(self._timeout)
        finally:
            self._close_review(pending)

        if not answered:
            Logger.log("w", "review %s timed out", review_id)
            self._emit(pending, "review_closed", review_id=review_id, status="timeout")
            raise ToolError("Пользователь не ответил за %d мин" % (self._timeout // 60))

        answer = pending["answer"]
        if answer["cancelled"]:
            Logger.log("i", "review %s cancelled", review_id)
            self._emit(pending, "review_closed", review_id=review_id, status="cancelled")
            raise ToolError("Пользователь остановил проверку параметров")

        result = self._slicer.apply_review_changes(answer["changes"])
        Logger.log("i", "review %s approved: %s", review_id, result)
        self._emit(
            pending,
            "review_closed",
            review_id=review_id,
            status="approved",
            applied=result["applied"],
            errors=result["errors"],
            comment=answer["comment"],
        )

        return {
            "approved": True,
            "user_changes": result["applied"],
            "errors": result["errors"],
            "comment": answer["comment"],
            "instruction": CONTINUE_INSTRUCTION,
        }

    def answer(self, review_id, changes=None, comment=""):
        with self._lock:
            pending = self._find(review_id)
            if pending is None:
                raise ValueError("Нет ожидающей проверки %s" % review_id)

            pending["answer"] = {
                "cancelled": False,
                "changes": changes or {},
                "comment": (comment or "").strip(),
            }
            pending["answered"].set()

    def cancel(self, chat_id=None, review_id=None):
        """Отменяет проверку чата (или конкретную по review_id). Без аргументов — все."""
        with self._lock:
            if review_id:
                found = self._find(review_id)
                targets = [found] if found is not None else []
            elif chat_id:
                found = self._pending.get(self._key(chat_id))
                targets = [found] if found is not None else []
            else:
                targets = list(self._pending.values())

            for pending in targets:
                pending["answer"] = {"cancelled": True, "changes": {}, "comment": ""}
                pending["answered"].set()

    @staticmethod
    def _key(chat_id):
        return chat_id or "_external"

    def _find(self, review_id):
        for pending in self._pending.values():
            if pending["id"] == review_id:
                return pending
        return None

    def _open_review(self, chat_id=None):
        key = self._key(chat_id)
        with self._lock:
            if key in self._pending:
                raise ToolError("В этом чате уже ждём ответа пользователя на предыдущую проверку")

            self._review_count += 1
            pending = {
                "id": "review_%d" % self._review_count,
                "chat_id": chat_id,
                "answered": threading.Event(),
                "answer": None,
            }
            self._pending[key] = pending
            return pending

    def _close_review(self, pending):
        key = self._key(pending.get("chat_id"))
        with self._lock:
            if self._pending.get(key) is pending:
                del self._pending[key]

    def _emit(self, pending, event_type, **data):
        chat_id = pending.get("chat_id")
        if chat_id:
            self._events.emit(event_type, data, chat_id=chat_id)
        else:
            self._events.emit(event_type, data)
