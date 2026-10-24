import os

from UM.Application import Application

PREFERENCE_PREFIX = "ai_assistant/"

DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "verify_ssl": True,
    "port": 8765,
    "temperature": 0.2,
    "output_dir": os.path.join(os.path.expanduser("~"), "Documents", "STE_AI_results"),
}

TRUE_STRINGS = ("true", "1", "yes", "on")


def _app_preferences():
    app = Application.getInstance()
    if app is None:
        return None
    return app.getPreferences()


def _coerce(value, default):
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in TRUE_STRINGS
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

    if value is None:
        return ""
    return str(value)


class Config(object):
    def __init__(self, preferences=None, use_app=True):
        self._preferences = preferences
        if self._preferences is None and use_app:
            self._preferences = _app_preferences()

        self._values = dict(DEFAULTS)

        if self._preferences is not None:
            for key, default in DEFAULTS.items():
                self._preferences.addPreference(PREFERENCE_PREFIX + key, default)

    def get(self, key):
        if self._preferences is None:
            return self._values[key]
        stored_value = self._preferences.getValue(PREFERENCE_PREFIX + key)
        return _coerce(stored_value, DEFAULTS[key])

    def set(self, key, value):
        value = _coerce(value, DEFAULTS[key])
        if self._preferences is None:
            self._values[key] = value
        else:
            self._preferences.setValue(PREFERENCE_PREFIX + key, value)

    def update(self, data):
        for key in DEFAULTS:
            if key not in data:
                continue

            value = data[key]
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
        if len(api_key) > 8:
            result["api_key_hint"] = "…" + api_key[-4:]
        else:
            result["api_key_hint"] = ""
        return result

    def llm_ready(self):
        has_url = bool(self.get("base_url"))
        has_key = bool(self.get("api_key"))
        has_model = bool(self.get("model"))
        return has_url and has_key and has_model
