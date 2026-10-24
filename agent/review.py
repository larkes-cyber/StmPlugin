import threading

from .log import log
from .tools import ToolError

REVIEW_TIMEOUT = 30 * 60

CONTINUE_INSTRUCTION = (
    "Пользователь проверил параметры и разрешил продолжить. Его изменения окончательные: "
    "не меняй их обратно. Если есть комментарий — учти его, затем продолжай работу."
)


class ReviewGate(object):
    def __init__(self, events, slicer, timeout=REVIEW_TIMEOUT):
        self._events = events
        self._slicer = slicer
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending = None
        self._review_count = 0

    @property
    def pending_id(self):
        pending = self._pending
        if pending is None:
            return None
        return pending["id"]

    def request(self, summary=""):
        proposal = self._slicer.review_proposal()
        pending = self._open_review()
        review_id = pending["id"]

        log("i", "review %s requested", review_id)
        self._events.emit("review_requested", review_id=review_id, summary=summary, proposal=proposal)

        answered = pending["answered"].wait(self._timeout)
        self._close_review(pending)

        if not answered:
            log("w", "review %s timed out", review_id)
            self._events.emit("review_closed", review_id=review_id, status="timeout")
            raise ToolError("Пользователь не ответил за %d мин" % (self._timeout // 60))

        answer = pending["answer"]
        if answer["cancelled"]:
            log("i", "review %s cancelled", review_id)
            self._events.emit("review_closed", review_id=review_id, status="cancelled")
            raise ToolError("Пользователь остановил проверку параметров")

        result = self._slicer.apply_review_changes(answer["changes"])
        log("i", "review %s approved: %s", review_id, result)
        self._events.emit(
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
            pending = self._pending
            if pending is None or pending["id"] != review_id:
                raise ValueError("Нет ожидающей проверки %s" % review_id)

            pending["answer"] = {
                "cancelled": False,
                "changes": changes or {},
                "comment": (comment or "").strip(),
            }
            pending["answered"].set()

    def cancel(self):
        with self._lock:
            pending = self._pending
            if pending is None:
                return

            pending["answer"] = {"cancelled": True, "changes": {}, "comment": ""}
            pending["answered"].set()

    def _open_review(self):
        with self._lock:
            if self._pending is not None:
                raise ToolError("Уже ждём ответа пользователя на предыдущую проверку")

            self._review_count += 1
            self._pending = {
                "id": "review_%d" % self._review_count,
                "answered": threading.Event(),
                "answer": None,
            }
            return self._pending

    def _close_review(self, pending):
        with self._lock:
            if self._pending is pending:
                self._pending = None
