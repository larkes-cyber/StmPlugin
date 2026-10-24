from steslicer.Settings.ExtruderManager import ExtruderManager
from steslicer.SteSlicerApplication import SteSlicerApplication

from ..ai import ToolError
from ..core import Logger, on_main

PRINT_MODE_KEY = "printing_mode"
PRINT_MODES = ("classic", "cylindrical", "cylindrical_full")
MODE_LABELS = {
    "classic": "3D (classic)",
    "cylindrical": "5D Spiral (cylindrical)",
    "cylindrical_full": "5D (cylindrical_full)",
}
TRUE_STRINGS = ("true", "1", "yes", "on")


def _app():
    return SteSlicerApplication.getInstance()


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class SlicerSettings(object):
    """Управление стеком настроек, материалами, профилями и режимами 3D/5D печати."""

    def __init__(self, events):
        self._events = events

    def global_stack(self):
        stack = _app().getGlobalContainerStack()
        if stack is None:
            raise ToolError("Не выбран принтер — добавьте принтер в STE Slicer")
        return stack

    def extruder_stack(self):
        return ExtruderManager.getInstance().getActiveExtruderStack()

    def stack_for(self, key):
        g_stack = self.global_stack()
        if g_stack.getProperty(key, "settable_per_extruder"):
            e_stack = self.extruder_stack()
            if e_stack is not None:
                return e_stack, "extruder"
        return g_stack, "global"

    def require_key(self, key):
        if self.global_stack().getProperty(key, "type") is None:
            raise ToolError(
                "Неизвестная настройка '%s' — найдите ключ через search_settings" % key
            )

    def describe(self, key):
        self.require_key(key)
        g_stack = self.global_stack()
        stack, scope = self.stack_for(key)
        setting_type = g_stack.getProperty(key, "type")

        info = {
            "key": key,
            "label": g_stack.getProperty(key, "label"),
            "type": setting_type,
            "value": _jsonable(stack.getProperty(key, "value")),
            "unit": g_stack.getProperty(key, "unit") or "",
            "scope": scope,
            "user_changed": stack.userChanges.getProperty(key, "value") is not None,
        }
        if setting_type == "enum":
            info["options"] = _jsonable(g_stack.getProperty(key, "options"))

        for prop_name, info_key in (("minimum_value", "min"), ("maximum_value", "max")):
            bound = stack.getProperty(key, prop_name)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                info[info_key] = bound
        return info

    def convert_value(self, key, value):
        g_stack = self.global_stack()
        stack, _ = self.stack_for(key)
        setting_type = g_stack.getProperty(key, "type")

        if setting_type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in TRUE_STRINGS
            return bool(value)
        if setting_type == "int":
            return int(round(float(value)))
        if setting_type == "float":
            return float(value)
        if setting_type == "enum":
            options = g_stack.getProperty(key, "options") or {}
            val_str = str(value)
            if val_str in options:
                return val_str
            for opt_key, label in options.items():
                if str(label).lower() == val_str.lower():
                    return opt_key
            raise ToolError(
                "Недопустимое значение '%s' для %s. Варианты: %s"
                % (value, key, ", ".join(options.keys()))
            )
        return str(value)

    @on_main
    def get_settings(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        settings = []
        errors = []
        for key in keys:
            try:
                settings.append(self.describe(key))
            except ToolError as error:
                errors.append({"key": key, "error": str(error)})
        return {"settings": settings, "errors": errors}

    @on_main
    def search_settings(self, query, limit=25):
        g_stack = self.global_stack()
        words = str(query).lower().split()
        limit = int(limit or 25)

        found = []
        for key in sorted(g_stack.getAllKeys()):
            setting_type = g_stack.getProperty(key, "type")
            if setting_type in (None, "category"):
                continue

            label = str(g_stack.getProperty(key, "label") or "")
            searchable_text = (key + " " + label).lower()
            if all(word in searchable_text for word in words):
                stack, scope = self.stack_for(key)
                found.append(
                    {
                        "key": key,
                        "label": label,
                        "type": setting_type,
                        "value": _jsonable(stack.getProperty(key, "value")),
                        "unit": g_stack.getProperty(key, "unit") or "",
                        "scope": scope,
                    }
                )
            if len(found) >= limit:
                break
        return {"query": query, "results": found}

    @on_main
    def set_settings(self, changes, reason=""):
        applied, errors = self._apply_changes(changes)
        if not applied and errors:
            raise ToolError("Не удалось применить настройки: %s" % errors[0]["error"])

        self._events.emit(
            "settings_changed", {"changes": applied, "errors": errors, "reason": reason}
        )
        return {"applied": applied, "errors": errors}

    @on_main
    def reset_settings(self, keys=None):
        g_stack = self.global_stack()
        e_stack = self.extruder_stack()
        if keys:
            if isinstance(keys, str):
                keys = [keys]
            reset_keys = keys
        else:
            reset_keys = set(g_stack.userChanges.getAllKeys())
            if e_stack:
                reset_keys.update(e_stack.userChanges.getAllKeys())

        reset = []
        for key in sorted(reset_keys):
            for stack, scope in ((e_stack, "extruder"), (g_stack, "global")):
                if stack and stack.userChanges.getProperty(key, "value") is not None:
                    stack.userChanges.setProperty(key, "value", None)
                    reset.append({"key": key, "scope": scope})

        self._events.emit(
            "settings_changed",
            {"reset": reset, "reason": "Сброс к значениям по умолчанию"},
        )
        return {"reset": reset}

    @on_main
    def get_print_mode(self):
        return self._mode_info()

    @on_main
    def set_print_mode(self, mode, reason=""):
        if mode not in PRINT_MODES:
            raise ToolError("Режим должен быть одним из: %s" % ", ".join(PRINT_MODES))

        applied, errors = self._apply_changes({PRINT_MODE_KEY: mode})
        if errors:
            raise ToolError(errors[0]["error"])

        self._events.emit(
            "settings_changed",
            {"changes": applied, "reason": reason or ("Режим: " + MODE_LABELS[mode])},
        )
        return self._mode_info()

    @on_main
    def apply_chat_state(self, state):
        """Пакетное восстановление состояния настроек из снимка сессии чата."""
        if not isinstance(state, dict):
            return

        if state.get("print_mode"):
            try:
                self.set_print_mode(state["print_mode"])
            except Exception as e:
                Logger.log("w", "Не удалось восстановить режим печати: %s", e)

        if state.get("settings"):
            try:
                self.set_settings(
                    state["settings"], reason="Восстановление настроек чата"
                )
            except Exception as e:
                Logger.log("w", "Не удалось восстановить настройки чата: %s", e)

    def _apply_changes(self, changes):
        applied = []
        errors = []
        for key, value in changes.items():
            try:
                self.require_key(key)
                new_value = self.convert_value(key, value)
                stack, scope = self.stack_for(key)
                old_val = stack.getProperty(key, "value")
                stack.userChanges.setProperty(key, "value", new_value)
                actual_val = stack.getProperty(key, "value")
            except ToolError as error:
                Logger.log("w", "Настройка %s отклонена: %s", key, error)
                errors.append({"key": key, "error": str(error)})
                continue

            applied.append(
                {
                    "key": key,
                    "label": self.global_stack().getProperty(key, "label"),
                    "old": _jsonable(old_val),
                    "new": _jsonable(actual_val),
                    "scope": scope,
                }
            )
        return applied, errors

    def _mode_info(self):
        g_stack = self.global_stack()
        stack, scope = self.stack_for(PRINT_MODE_KEY)
        mode = stack.getProperty(PRINT_MODE_KEY, "value") or "classic"
        options = g_stack.getProperty(PRINT_MODE_KEY, "options") or {}
        return {
            "key": PRINT_MODE_KEY,
            "mode": mode,
            "label": MODE_LABELS.get(mode, mode),
            "scope": scope,
            "options": [opt for opt in PRINT_MODES if opt in options],
        }
