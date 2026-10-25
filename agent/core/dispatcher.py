import functools
import threading
import time

from UM.Application import Application

from .config import config

DEFAULT_TIMEOUT = 120.0
_abort_main_waits = threading.Event()
_abort_lock = threading.Lock()
_aborted_chats = set()
_wait_local = threading.local()


class MainThreadAborted(Exception):
    """Ожидание главного потока прервано кнопкой «Стоп»."""


def bind_wait_chat(chat_id):
    _wait_local.chat_id = chat_id


def abort_chat_waits(chat_id):
    with _abort_lock:
        _aborted_chats.add(chat_id)


def clear_chat_waits(chat_id):
    with _abort_lock:
        _aborted_chats.discard(chat_id)


def abort_main_waits():
    _abort_main_waits.set()


def clear_main_waits_abort():
    _abort_main_waits.clear()


def main_waits_aborted():
    if _abort_main_waits.is_set():
        return True
    chat_id = getattr(_wait_local, "chat_id", None)
    if not chat_id:
        return False
    with _abort_lock:
        return chat_id in _aborted_chats


def is_main_thread():
    try:
        from PyQt5.QtCore import QCoreApplication, QThread

        qt_app = QCoreApplication.instance()
        if qt_app is not None:
            return QThread.currentThread() == qt_app.thread()
    except Exception:
        pass
    return threading.current_thread() is threading.main_thread()


def run_on_main(func, *args, **kwargs):
    configured_timeout = config.get("main_thread_timeout", DEFAULT_TIMEOUT)
    timeout = kwargs.pop("timeout", configured_timeout)

    app = Application.getInstance()
    if app is None or is_main_thread():
        return func(*args, **kwargs)

    done = threading.Event()
    outcome = {}

    def _wrapper():
        try:
            outcome["result"] = func(*args, **kwargs)
        except Exception as e:
            outcome["error"] = e
        finally:
            done.set()

    app.callLater(_wrapper)

    deadline = time.time() + float(timeout)
    while not done.is_set():
        if main_waits_aborted():
            raise MainThreadAborted("Операция прервана")
        remaining = deadline - time.time()
        if remaining <= 0:
            func_name = getattr(func, "__name__", str(func))
            raise TimeoutError(
                "The ‘%s’ operation in the main thread exceeded the %ss timeout"
                % (func_name, timeout)
            )
        done.wait(min(0.25, remaining))

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def on_main(func):
    """Декоратор для автоматического запуска метода в главном потоке Qt."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return run_on_main(func, *args, **kwargs)

    return wrapper
