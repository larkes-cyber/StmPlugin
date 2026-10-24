import os
import webbrowser

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from UM.Application import Application
from UM.Extension import Extension

from agent.core import Logger, config
from .agent.context import Context


class AIAssistant(Extension):
    def __init__(self):
        super().__init__()

        config.setup()
        if config.name:
            Logger.set_name(config.name)

        self.setMenuName(config.name or "AI Assistant")
        self.addMenuItem("Открыть чат", self.showChat)
        self.addMenuItem("Папка результатов", self.openResultsFolder)
        self._context = None

        Logger.log("i", "plugin v%s loaded from %s", config.version, config.plugin_dir)
        Application.getInstance().engineCreatedSignal.connect(self._onEngineCreated)

    def _onEngineCreated(self):
        try:
            self._ensureStarted()
        except Exception:
            Logger.logException("failed to start server")

    def _ensureStarted(self):
        if self._context is None:
            context = Context()
            context.start()
            self._context = context
            Logger.log("i", "chat at %s, MCP at %s/mcp", context.url, context.url)
        return self._context

    def showChat(self):
        try:
            context = self._ensureStarted()
            Logger.log("i", "opening chat %s/", context.url)
            webbrowser.open(context.url + "/")
        except Exception:
            Logger.logException("failed to open chat")

    def openResultsFolder(self):
        try:
            context = self._ensureStarted()
            path = context.config.get("output_dir")
            if not os.path.isdir(path):
                os.makedirs(path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception:
            Logger.logException("failed to open results folder")
