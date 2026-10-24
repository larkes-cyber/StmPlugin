import json
import traceback

from UM.Logger import Logger

LOG_PREFIX = "AIAssistant: "


def log(level, message, *args):
    if args:
        try:
            message = message % args
        except (TypeError, ValueError):
            message = "%s %r" % (message, args)
    Logger.log(level, LOG_PREFIX + message)


def log_exception(message):
    log("e", "%s\n%s", message, traceback.format_exc())


def shorten(value, limit=500):
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)

    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "…(%d симв.)" % len(text)
