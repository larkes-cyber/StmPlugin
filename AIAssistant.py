# -*- coding: utf-8 -*-
"""Точка входа плагина: пункт меню, запуск локального сервера, открытие чата в браузере."""
import os
import webbrowser

from UM.Extension import Extension
from UM.Logger import Logger
from UM.Application import Application


class AIAssistant(Extension):
    def __init__(self):
        super().__init__()
        self.setMenuName("AI Assistant")
        self.addMenuItem("Открыть чат", self.showChat)
        self.addMenuItem("Папка результатов", self.openResultsFolder)
        self._ctx = None
        # MCP-сервер должен быть доступен сразу после старта слайсера, а не только после открытия чата
        try:
            Application.getInstance().engineCreatedSignal.connect(self._onEngineCreated)
        except Exception:
            Logger.logException("w", "AIAssistant: engineCreatedSignal is not available")

    def _onEngineCreated(self):
        try:
            self._ensureStarted()
        except Exception:
            Logger.logException("e", "AIAssistant: failed to start server")

    def _ensureStarted(self):
        if self._ctx is None:
            from .agent.context import Context
            ctx = Context()
            ctx.start()
            self._ctx = ctx
            Logger.log("i", "AIAssistant: chat at %s, MCP at %s/mcp", ctx.url, ctx.url)
        return self._ctx

    def showChat(self):
        try:
            ctx = self._ensureStarted()
            webbrowser.open(ctx.url + "/")
        except Exception:
            Logger.logException("e", "AIAssistant: failed to open chat")

    def openResultsFolder(self):
        try:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            ctx = self._ensureStarted()
            path = ctx.config.get("output_dir")
            if not os.path.isdir(path):
                os.makedirs(path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception:
            Logger.logException("e", "AIAssistant: failed to open results folder")
