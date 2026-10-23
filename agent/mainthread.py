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
        raise MainThreadTimeout("Главный поток слайсера не ответил за %d с" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def on_main(func):
    """Декоратор для методов, которые должны выполняться в главном потоке."""
    def wrapper(*args, **kwargs):
        return run_on_main(func, *args, **kwargs)
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper
