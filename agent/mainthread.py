# -*- coding: utf-8 -*-
"""Выполнение кода в главном потоке Qt.

Сцену, настройки и слайсинг можно трогать только из главного потока.
HTTP-сервер и агент работают в своих потоках, поэтому все обращения к API
слайсера проходят через run_on_main(): задача ставится через app.callLater,
а вызывающий поток ждёт результат.
"""
import threading
import traceback


class MainThreadTimeout(Exception):
    pass


def _app():
    try:
        from UM.Application import Application
    except ImportError:
        return None
    return Application.getInstance()


def run_on_main(func, *args, **kwargs):
    timeout = kwargs.pop("_timeout", 120)
    app = _app()
    if app is None or threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    box = {}
    done = threading.Event()

    def wrapper():
        try:
            box["result"] = func(*args, **kwargs)
        except Exception as e:  # исключение пробрасываем в вызывающий поток
            box["error"] = e
            box["traceback"] = traceback.format_exc()
        finally:
            done.set()

    app.callLater(wrapper)
    if not done.wait(timeout):
        where = _main_thread_stack()
        from .log import log
        log("e", "main thread timeout (%ds) in %s. Main thread is stuck here:\n%s",
            timeout, getattr(func, "__name__", func), where)
        last = [line for line in where.strip().splitlines() if line.strip().startswith("File")]
        raise MainThreadTimeout("Главный поток слайсера не ответил за %d с (%s). Возможно, в слайсере открыт "
                                "диалог. Полный стек — в stderr.log"
                                % (timeout, last[-1].strip() if last else "место неизвестно"))
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _main_thread_stack():
    """Стек главного потока — показывает, где именно он завис."""
    import sys
    frame = sys._current_frames().get(threading.main_thread().ident)
    if frame is None:
        return "(стек недоступен)"
    return "".join(traceback.format_stack(frame))


def on_main(func):
    """Декоратор для методов, которые должны выполняться в главном потоке."""
    def wrapper(*args, **kwargs):
        return run_on_main(func, *args, **kwargs)
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper
