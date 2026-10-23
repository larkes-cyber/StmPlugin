# -*- coding: utf-8 -*-
"""Реестр инструментов. Один и тот же набор отдаётся в LLM (tools) и в MCP (tools/list, tools/call)."""
import threading
import time

from .log import log_exception


class ToolError(Exception):
    """Ожидаемая ошибка инструмента: её текст уходит агенту и пользователю."""


class Tool(object):
    def __init__(self, name, description, func, properties=None, required=None, group="general"):
        self.name = name
        self.description = description
        self.func = func
        self.properties = properties or {}
        self.required = list(required or [])
        self.group = group

    def schema(self):
        schema = {"type": "object", "properties": self.properties}
        if self.required:
            schema["required"] = self.required
        return schema


class ToolRegistry(object):
    def __init__(self, events=None):
        self._tools = []
        self._by_name = {}
        self._events = events
        self._counter = 0
        self._lock = threading.Lock()

    def add(self, name, description, func, properties=None, required=None, group="general"):
        tool = Tool(name, description, func, properties, required, group)
        self._tools.append(tool)
        self._by_name[name] = tool
        return tool

    def has(self, name):
        return name in self._by_name

    def names(self):
        return [t.name for t in self._tools]

    def openai_tools(self):
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.schema()}}
                for t in self._tools]

    def mcp_tools(self):
        return [{"name": t.name, "description": t.description, "inputSchema": t.schema()} for t in self._tools]

    def _emit(self, event_type, **data):
        if self._events is not None:
            self._events.emit(event_type, **data)

    def call(self, name, args=None, source="agent", call_id=None):
        with self._lock:
            self._counter += 1
            if not call_id:
                call_id = "%s_%d" % (source, self._counter)
        if args is None:
            args = {}
        self._emit("tool_call", call_id=call_id, name=name, args=args, source=source)
        started = time.time()
        tool = self._by_name.get(name)

        if tool is None:
            outcome = {"ok": False, "error": "Неизвестный инструмент: %s" % name}
        elif not isinstance(args, dict):
            outcome = {"ok": False, "error": "Аргументы должны быть JSON-объектом"}
        else:
            missing = [key for key in tool.required if key not in args]
            if missing:
                outcome = {"ok": False, "error": "Не хватает аргументов: %s" % ", ".join(missing)}
            else:
                # LLM иногда добавляет лишние поля — отбрасываем всё, чего нет в схеме
                clean = dict((k, v) for k, v in args.items() if k in tool.properties)
                try:
                    outcome = {"ok": True, "result": tool.func(**clean)}
                except ToolError as e:
                    outcome = {"ok": False, "error": str(e)}
                except Exception as e:
                    log_exception("Tool %s failed" % name)
                    outcome = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

        duration = round(time.time() - started, 2)
        self._emit("tool_result", call_id=call_id, name=name, ok=outcome["ok"],
                   result=outcome.get("result"), error=outcome.get("error"),
                   duration=duration, source=source)
        return outcome


# ---------------------------------------------------------------------------
# Описание инструментов поверх SlicerAPI
# ---------------------------------------------------------------------------

def _str(desc, enum=None):
    prop = {"type": "string", "description": desc}
    if enum:
        prop["enum"] = list(enum)
    return prop


def _num(desc):
    return {"type": "number", "description": desc}


def _int(desc):
    return {"type": "integer", "description": desc}


def _bool(desc):
    return {"type": "boolean", "description": desc}


OBJECT_ID = _str("ID объекта из list_objects (obj1, obj2…). Можно не указывать, если в сцене один объект.")
MODEL_NAME = _str("Имя модели для папки results/<model_name>/ (обычно имя файла без расширения)")


def build_registry(slicer, events=None):
    r = ToolRegistry(events)

    # --- Состояние, настройки, профили -------------------------------------------------
    r.add("get_state",
          "Текущее состояние слайсера: принтер, режим 3D/5D, профиль качества, материал, объекты в сцене, "
          "состояние слайсинга. Вызывай в начале работы.",
          slicer.get_state, group="settings")
    r.add("get_settings",
          "Прочитать значения настроек по ключам (глобальных или экструдера — определяется автоматически). "
          "Возвращает значение, единицы, допустимые варианты и диапазоны.",
          slicer.get_settings,
          {"keys": {"type": "array", "items": {"type": "string"},
                    "description": "Ключи настроек, например ['layer_height','infill_sparse_density']"}},
          ["keys"], group="settings")
    r.add("search_settings",
          "Найти настройки по части ключа или названия (англ.), например 'support', 'brim', 'retract'. "
          "Используй, если не уверен в точном ключе.",
          slicer.search_settings,
          {"query": _str("Поисковая строка"), "limit": _int("Максимум результатов, по умолчанию 25")},
          ["query"], group="settings")
    r.add("set_settings",
          "Изменить одну или несколько настроек. Значения проверяются по типу и допустимому диапазону. "
          "Обязательно передавай reason — объяснение для пользователя.",
          slicer.set_settings,
          {"changes": {"type": "object", "description": "Словарь ключ → значение, например {\"wall_line_count\": 4}",
                       "additionalProperties": True},
           "reason": _str("Почему меняются эти настройки (покажется пользователю)")},
          ["changes", "reason"], group="settings")
    r.add("reset_settings",
          "Сбросить пользовательские изменения настроек (все или по списку ключей) к значениям профиля.",
          slicer.reset_settings,
          {"keys": {"type": "array", "items": {"type": "string"}, "description": "Ключи; пусто — сбросить все"}},
          group="settings")
    r.add("list_quality_profiles",
          "Список профилей качества (quality_type) и пользовательских профилей для текущего принтера, а также активный.",
          slicer.list_quality_profiles, group="settings")
    r.add("set_quality_profile",
          "Выбрать профиль качества по quality_type или имени пользовательского профиля.",
          slicer.set_quality_profile, {"name": _str("quality_type или имя профиля из list_quality_profiles")},
          ["name"], group="settings")
    r.add("list_materials",
          "Список материалов, доступных для текущего принтера/экструдера, и активный материал.",
          slicer.list_materials, {"query": _str("Фильтр по названию/типу, например 'PETG'")}, group="settings")
    r.add("set_material",
          "Выбрать материал по id из list_materials.",
          slicer.set_material, {"material_id": _str("id материала из list_materials")},
          ["material_id"], group="settings")
    r.add("save_profile",
          "Сохранить текущие настройки как пользовательский профиль (чтобы не настраивать заново).",
          slicer.save_profile, {"name": _str("Имя профиля")}, ["name"], group="settings")
    r.add("get_print_mode",
          "Текущий режим печати: classic (3D), cylindrical (5D Spiral), cylindrical_full (5D).",
          slicer.get_print_mode, group="settings")
    r.add("set_print_mode",
          "Переключить режим печати 3D ↔ 5D.",
          slicer.set_print_mode,
          {"mode": _str("Режим", ["classic", "cylindrical", "cylindrical_full"]),
           "reason": _str("Почему выбран этот режим")},
          ["mode"], group="settings")

    # --- Модели и сцена -------------------------------------------------------------
    r.add("load_model",
          "Загрузить файл модели (STL/OBJ/3MF) в сцену по пути на диске. Возвращает ID и габариты новых объектов.",
          slicer.load_model, {"path": _str("Абсолютный путь к файлу")}, ["path"], group="models")
    r.add("list_objects",
          "Список объектов в сцене: ID, имя, габариты (мм), положение, масштаб; размеры рабочей области.",
          slicer.list_objects, group="models")
    r.add("get_object_dimensions",
          "Габариты и положение одного объекта.",
          slicer.get_object_dimensions, {"object_id": OBJECT_ID}, group="models")
    r.add("move_object",
          "Переместить объект по столу. Координаты центра в мм, (0,0) — центр стола. relative=true — сдвиг.",
          slicer.move_object,
          {"object_id": OBJECT_ID, "x": _num("X, мм"), "y": _num("Y, мм"), "relative": _bool("Сдвиг вместо абсолютных координат")},
          group="models")
    r.add("center_object", "Поставить объект в центр стола (и опустить на стол).",
          slicer.center_object, {"object_id": OBJECT_ID}, group="models")
    r.add("rotate_object",
          "Повернуть объект вокруг оси стола (z — вертикаль) на заданный угол; объект опускается на стол.",
          slicer.rotate_object,
          {"object_id": OBJECT_ID, "axis": _str("Ось", ["x", "y", "z"]), "degrees": _num("Угол, градусы")},
          ["axis", "degrees"], group="models")
    r.add("scale_object",
          "Масштабировать объект в процентах от исходного размера (равномерно или по осям).",
          slicer.scale_object,
          {"object_id": OBJECT_ID, "percent": _num("Равномерный масштаб, %"),
           "x": _num("Масштаб по X, %"), "y": _num("Масштаб по Y, %"), "z": _num("Масштаб по Z, %")},
          group="models")
    r.add("arrange_all", "Автоматически расставить все объекты на столе.", slicer.arrange_all, group="models")
    r.add("delete_object", "Удалить объект из сцены (object_id='all' — очистить сцену).",
          slicer.delete_object, {"object_id": _str("ID объекта или 'all'")}, ["object_id"], group="models")

    # --- Анализ геометрии -----------------------------------------------------------------
    r.add("analyze_geometry",
          "Анализ модели: габариты, объём, площадь, замкнутость сетки, нависания и оценка поддержек в текущей "
          "ориентации, площадь контакта со столом, признаки осесимметричности (кандидат на 5D), подсказки.",
          slicer.analyze_geometry,
          {"object_id": OBJECT_ID, "support_angle": _num("Угол нависания для поддержек, ° (по умолчанию из настроек)")},
          group="analysis")
    r.add("suggest_orientations",
          "Перебрать ориентации модели и оценить каждую: объём поддержек, площадь контакта со столом, высота. "
          "Возвращает рейтинг; применить — apply_orientation.",
          slicer.suggest_orientations,
          {"object_id": OBJECT_ID, "support_angle": _num("Угол нависания, °"), "top_n": _int("Сколько вариантов вернуть")},
          group="analysis")
    r.add("apply_orientation",
          "Применить ориентацию из suggest_orientations (по rank) или по вектору down — направлению в текущей "
          "системе координат, которое станет «вниз» (на стол).",
          slicer.apply_orientation,
          {"object_id": OBJECT_ID, "rank": _int("Номер варианта из suggest_orientations (1 — лучший)"),
           "down": {"type": "array", "items": {"type": "number"}, "description": "Вектор [x,y,z]"}},
          group="analysis")

    # --- Слайсинг и экспорт ---------------------------------------------------------------
    r.add("start_slicing",
          "Запустить слайсинг. По умолчанию ждёт завершения и возвращает время печати и расход материала.",
          slicer.start_slicing, {"wait": _bool("Ждать завершения (по умолчанию true)")}, group="slicing")
    r.add("get_slicing_status", "Статус и прогресс слайсинга.", slicer.get_slicing_status, group="slicing")
    r.add("get_slicing_result", "Результат последнего слайсинга: время печати, расход материала.",
          slicer.get_slicing_result, group="slicing")
    r.add("export_gcode",
          "Сохранить G-code последнего слайсинга и проверить его (слои, перемещения, температуры).",
          slicer.export_gcode, {"path": _str("Путь к файлу .gcode (необязательно)"), "model_name": MODEL_NAME},
          group="slicing")
    r.add("save_project_3mf",
          "Сохранить проект 3MF (модель + все настройки).",
          slicer.save_project_3mf, {"path": _str("Путь к файлу .3mf (необязательно)"), "model_name": MODEL_NAME},
          group="slicing")
    r.add("export_results",
          "Сохранить результат для сдачи: results/<model_name>/project.3mf и result.gcode. "
          "Нужен выполненный слайсинг.",
          slicer.export_results, {"model_name": MODEL_NAME}, ["model_name"], group="slicing")
    return r
