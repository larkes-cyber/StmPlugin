from typing import Optional

from UM.Logger import Logger as _UMLogger


class PluginLogger(object):
    def __init__(self, name: Optional[str] = None) -> None:
        self.set_name(name)

    def set_name(self, name: Optional[str]) -> None:
        self.name = name
        self.prefix = ("[%s] " % name) if name else ""

    def log(self, level: str, msg: str, *args) -> None:
        text = (msg % args) if args else str(msg)
        _UMLogger.log(level, self.prefix + text)

    def logException(self, msg: str, *args) -> None:
        text = (msg % args) if args else str(msg)
        _UMLogger.logException("e", self.prefix + text)


Logger = PluginLogger()
