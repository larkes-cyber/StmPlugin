import functools
import sys
import threading
import traceback

from UM.Application import Application

from agent.core import Logger

DEFAULT_TIMEOUT = 120


class MainThreadTimeout(Exception):
    pass


def _app():
    return Application.getInstance()


def _main_thread_stack():
    frame = sys._current_frames().get(threading.main_thread().ident)
    if frame is None:
        return "(стек недоступен)"
    return "".join(traceback.format_stack(frame))


def _last_frame_line(stack):
    frame_lines = []
    for line in stack.splitlines():
        if line.strip().startswith("File"):
            frame_lines.append(line.strip())

    if frame_lines:
        return frame_lines[-1]
    return "место неизвестно"


def run_on_main(func, *args, **kwargs):
    timeout = kwargs.pop("_timeout", DEFAULT_TIMEOUT)
    app = _app()
    if app is None or threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    outcome = {}
    finished = threading.Event()

    def call():
        try:
            outcome["result"] = func(*args, **kwargs)
        except Exception as error:
            outcome["error"] = error
        finally:
            finished.set()

    app.callLater(call)

    if not finished.wait(timeout):
        stack = _main_thread_stack()
        function_name = getattr(func, "__name__", func)
        Logger.log("e", "main thread timeout (%ds) in %s. Main thread is stuck here:\n%s", timeout, function_name, stack)
        raise MainThreadTimeout(
            "Главный поток слайсера не ответил за %d с (%s). "
            "Возможно, в слайсере открыт диалог. Полный стек — в stderr.log"
            % (timeout, _last_frame_line(stack))
        )

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def on_main(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return run_on_main(func, *args, **kwargs)

    return wrapper
