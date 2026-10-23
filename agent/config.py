# -*- coding: utf-8 -*-
"""Настройки плагина. Хранятся в настройках приложения (Preferences), ключ не попадает в исходники."""
import os

PREFIX = "ai_assistant/"

DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "verify_ssl": True,
    "port": 8765,
    "temperature": 0.2,
    "output_dir": os.path.join(os.path.expanduser("~"), "Documents", "STE_AI_results"),
}


def _app_preferences():
    try:
        from UM.Application import Application
        app = Application.getInstance()
    except ImportError:
        return None
    if app is None:
        return None
    try:
        return app.getPreferences()
    except AttributeError:
        from UM.Preferences import Preferences
        return Preferences.getInstance()


def _coerce(value, default):
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return "" if value is None else str(value)


class Config(object):
    def __init__(self, preferences=None, use_app=True):
        self._prefs = preferences
        if self._prefs is None and use_app:
            self._prefs = _app_preferences()
        self._mem = dict(DEFAULTS)
        if self._prefs is not None:
            for key, default in DEFAULTS.items():
                try:
                    self._prefs.addPreference(PREFIX + key, default)
                except Exception:
                    pass

    def get(self, key):
        default = DEFAULTS[key]
        if self._prefs is None:
            return self._mem[key]
        return _coerce(self._prefs.getValue(PREFIX + key), default)

    def set(self, key, value):
        value = _coerce(value, DEFAULTS[key])
        if self._prefs is None:
            self._mem[key] = value
        else:
            self._prefs.setValue(PREFIX + key, value)

    def update(self, data):
        for key in DEFAULTS:
            if key not in data:
                continue
            value = data[key]
            # пустой ключ в форме означает «не менять»; для удаления есть clear_api_key
            if key == "api_key" and not value:
                continue
            self.set(key, value)
        if data.get("clear_api_key"):
            self.set("api_key", "")

    def public(self):
        result = {}
        for key in DEFAULTS:
            if key != "api_key":
                result[key] = self.get(key)
        api_key = self.get("api_key")
        result["api_key_set"] = bool(api_key)
        result["api_key_hint"] = ("…" + api_key[-4:]) if len(api_key) > 8 else ""
        return result

    def llm_ready(self):
        return bool(self.get("base_url") and self.get("api_key") and self.get("model"))
