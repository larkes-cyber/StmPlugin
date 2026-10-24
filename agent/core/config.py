import json
import os
from typing import Any, Dict, Optional

from UM.Application import Application

from .logger import Logger

PLUGIN_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PREF_PREFIX = "ai_assistant/"

DEFAULT_SETTINGS = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.2,
    "verify_ssl": True,
    "server_addr": "127.0.0.1:8765",
    "output_dir": os.path.join(os.path.expanduser("~"), "Documents", "STE_AI_results"),
}


class Config(object):
    def __init__(self, plugin_dir: str = PLUGIN_DIR) -> None:
        self.plugin_dir = plugin_dir
        self._logger = Logger
        self.manifest = self._load_manifest()
        self.name = self.manifest.get("name", "")
        self.version = self.manifest.get("version", "1.0.0")

        self._fallback_values = dict(DEFAULT_SETTINGS)
        self._prefs = None

    def setup(self) -> None:
        app = Application.getInstance()
        self._prefs = app.getPreferences() if app else None

        if self._prefs:
            for key, default_val in DEFAULT_SETTINGS.items():
                pref_key = PREF_PREFIX + key
                self._prefs.addPreference(pref_key, default_val)
        else:
            self._logger.log(
                "w", "Preferences Cura недоступны, используются значения по умолчанию"
            )

    def _load_manifest(self) -> Dict[str, Any]:
        manifest_path = os.path.join(self.plugin_dir, "plugin.json")
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._logger.log("w", "Не удалось прочитать plugin.json (%s)", e)
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        if key not in DEFAULT_SETTINGS:
            self._logger.log("w", "Запрошен неизвестный параметр конфигурации '%s'", key)
            return default

        expected_default = DEFAULT_SETTINGS[key]

        if not self._prefs:
            return self._fallback_values.get(key, expected_default)

        raw_val = self._prefs.getValue(PREF_PREFIX + key)
        if raw_val is None:
            return expected_default

        if isinstance(expected_default, bool):
            return str(raw_val).lower() in ("true", "1", "yes")

        if isinstance(expected_default, (int, float)):
            try:
                return type(expected_default)(raw_val)
            except (ValueError, TypeError):
                self._logger.log(
                    "w",
                    "Повреждённое значение для '%s': %s. Взят default: %s",
                    key,
                    raw_val,
                    expected_default,
                )
                return expected_default

        return str(raw_val)

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULT_SETTINGS:
            self._logger.log(
                "w", "Попытка записать неизвестный параметр конфигурации '%s'", key
            )
            return

        self._fallback_values[key] = value
        if self._prefs:
            self._prefs.setValue(PREF_PREFIX + key, value)

    def update(self, new_settings: Dict[str, Any]) -> None:
        for key, val in new_settings.items():
            if key in DEFAULT_SETTINGS:
                if key == "api_key" and not val:
                    continue
                self.set(key, val)

        if new_settings.get("clear_api_key"):
            self.set("api_key", "")

    def public_dict(self) -> Dict[str, Any]:
        data = {k: self.get(k) for k in DEFAULT_SETTINGS if k != "api_key"}
        data["api_key_set"] = bool(self.get("api_key"))
        data["name"] = self.name
        data["version"] = self.version
        return data

    def get_server_binding(self) -> tuple:
        """Парсит 'server_addr' ('host:port') в кортеж (host, int_port)."""
        raw = str(self.get("server_addr", "127.0.0.1:8765")).strip()
        if ":" in raw:
            host, port_str = raw.rsplit(":", 1)
            try:
                return host.strip(), int(port_str.strip())
            except ValueError:
                return host.strip(), 8765
        return raw or "127.0.0.1", 8765

    def public(self) -> Dict[str, Any]:
        return self.public_dict()

    def llm_ready(self) -> bool:
        return bool(
            (self.get("base_url") or "").strip()
            and self.get("api_key")
            and self.get("model")
        )


config = Config()
