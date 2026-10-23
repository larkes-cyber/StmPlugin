# -*- coding: utf-8 -*-
"""Логирование.

Внутри слайсера всё идёт через UM.Logger и попадает в stderr.log и *.log в папке конфигурации
(Справка → Показать папку конфигурации). Вне слайсера (офлайн-тесты) — в stderr.
Все строки плагина начинаются с «AIAssistant:», уровни: d — отладка, i — инфо, w — предупреждение, e — ошибка.
"""
import json
import sys
import traceback

try:
    from UM.Logger import Logger as _Logger
except ImportError:
    _Logger = None


def log(level, message, *args):
    if args:
        try:
            message = message % args
        except (TypeError, ValueError):
            message = "%s %r" % (message, args)
    if _Logger is not None:
        _Logger.log(level, "AIAssistant: " + message)
    else:
        sys.stderr.write("[%s] AIAssistant: %s\n" % (level, message))


def log_exception(message):
    log("e", "%s\n%s", message, traceback.format_exc())


def short(value, limit=500):
    """Компактное представление аргументов/результатов для лога."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…(%d симв.)" % len(text)
