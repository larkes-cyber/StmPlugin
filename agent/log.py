# -*- coding: utf-8 -*-
"""Логирование: внутри слайсера — UM.Logger (stderr.log), вне его — stderr."""
import sys
import traceback

try:
    from UM.Logger import Logger as _Logger
except ImportError:
    _Logger = None


def log(level, message, *args):
    if args:
        message = message % args
    if _Logger is not None:
        _Logger.log(level, "AIAssistant: " + message)
    else:
        sys.stderr.write("[%s] AIAssistant: %s\n" % (level, message))


def log_exception(message):
    log("e", "%s\n%s", message, traceback.format_exc())
