import functools
import threading

from UM.Application import Application

from .config import config

DEFAULT_TIMEOUT = 120.0


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

    if not done.wait(timeout):
        func_name = getattr(func, "__name__", str(func))
        raise TimeoutError(
            "The ‘%s’ operation in the main thread exceeded the %ss timeout"
            % (func_name, timeout)
        )

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def on_main(func):
    """Декоратор для автоматического запуска метода в главном потоке Qt."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return run_on_main(func, *args, **kwargs)

    return wrapper
