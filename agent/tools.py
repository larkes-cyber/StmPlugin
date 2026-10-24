import threading
import time

from .core import Logger


class ToolError(Exception):
    pass


class Tool(object):
    def __init__(self, name, description, function, properties=None, required=None, group="general"):
        self.name = name
        self.description = description
        self.function = function
        self.properties = properties or {}
        self.required = list(required or [])
        self.group = group

    def schema(self):
        schema = {"type": "object", "properties": self.properties}
        if self.required:
            schema["required"] = self.required
        return schema


def _hide_image(result):
    if isinstance(result, dict) and "_image" in result:
        return dict(result, _image="[изображение]")
    return result


class ToolRegistry(object):
    def __init__(self, events=None):
        self._tools = []
        self._tools_by_name = {}
        self._events = events
        self._call_counter = 0
        self._lock = threading.Lock()

    def add(self, name, description, function, properties=None, required=None, group="general"):
        tool = Tool(name, description, function, properties, required, group)
        self._tools.append(tool)
        self._tools_by_name[name] = tool
        return tool

    def has(self, name):
        return name in self._tools_by_name

    def names(self):
        return [tool.name for tool in self._tools]

    def openai_tools(self):
        result = []
        for tool in self._tools:
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema(),
                },
            })
        return result

    def mcp_tools(self):
        result = []
        for tool in self._tools:
            result.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.schema(),
            })
        return result

    def call(self, name, args=None, source="agent", call_id=None):
        call_id = self._next_call_id(source, call_id)
        arguments = args if args is not None else {}

        Logger.log("d", "tool → %s [%s, %s] args=%s", name, source, call_id, arguments)
        self._emit("tool_call", call_id=call_id, name=name, args=arguments, source=source)

        started_at = time.time()
        outcome = self._execute(name, arguments)
        duration = round(time.time() - started_at, 2)

        result_for_chat = _hide_image(outcome.get("result"))
        if outcome["ok"]:
            Logger.log("d", "tool ← %s ok %.2fs result=%s", name, duration, result_for_chat)
        else:
            Logger.log("w", "tool ← %s FAILED %.2fs: %s", name, duration, outcome.get("error"))

        self._emit(
            "tool_result",
            call_id=call_id,
            name=name,
            ok=outcome["ok"],
            result=result_for_chat,
            error=outcome.get("error"),
            duration=duration,
            source=source,
        )
        return outcome

    def _next_call_id(self, source, call_id):
        with self._lock:
            self._call_counter += 1
            if call_id:
                return call_id
            return "%s_%d" % (source, self._call_counter)

    def _execute(self, name, arguments):
        tool = self._tools_by_name.get(name)
        if tool is None:
            return {"ok": False, "error": "Неизвестный инструмент: %s" % name}

        if not isinstance(arguments, dict):
            return {"ok": False, "error": "Аргументы должны быть JSON-объектом"}

        missing = [key for key in tool.required if key not in arguments]
        if missing:
            return {"ok": False, "error": "Не хватает аргументов: %s" % ", ".join(missing)}

        known_arguments = {key: value for key, value in arguments.items() if key in tool.properties}
        try:
            return {"ok": True, "result": tool.function(**known_arguments)}
        except ToolError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:
            Logger.logException("Tool %s failed" % name)
            return {"ok": False, "error": "%s: %s" % (type(error).__name__, error)}

    def _emit(self, event_type, **data):
        if self._events is not None:
            self._events.emit(event_type, **data)


def string_param(description, choices=None):
    param = {"type": "string", "description": description}
    if choices:
        param["enum"] = list(choices)
    return param


def number_param(description):
    return {"type": "number", "description": description}


def integer_param(description):
    return {"type": "integer", "description": description}


def boolean_param(description):
    return {"type": "boolean", "description": description}


def string_list_param(description):
    return {"type": "array", "items": {"type": "string"}, "description": description}


OBJECT_ID = string_param("ID объекта из list_objects (obj1, obj2…). Можно не указывать, если в сцене один объект.")
MODEL_NAME = string_param("Имя модели для папки results/<model_name>/ (обычно имя файла без расширения)")
PRINT_MODES = ["classic", "cylindrical", "cylindrical_full"]


def build_registry(slicer, events=None, review=None):
    registry = ToolRegistry(events)
    _add_settings_tools(registry, slicer)
    _add_model_tools(registry, slicer)
    _add_analysis_tools(registry, slicer)
    if review is not None:
        _add_review_tools(registry, review)
    _add_slicing_tools(registry, slicer)
    return registry


def _add_review_tools(registry, review):
    registry.add(
        name="request_user_review",
        description=(
            "Показать пользователю подобранные параметры и дождаться его подтверждения. Пользователь может "
            "изменить режим 3D/5D, материал, высоту слоя, заполнение, скорость, температуры, положение, масштаб "
            "и ориентацию модели. Изменения применяются автоматически и возвращаются в ответе. "
            "Вызывай один раз перед слайсингом, когда все параметры подобраны."
        ),
        function=review.request,
        properties={
            "summary": string_param("Кратко: что подобрано и почему (покажется пользователю)"),
        },
        required=["summary"],
        group="review",
    )


def _add_settings_tools(registry, slicer):
    registry.add(
        name="get_state",
        description=(
            "Текущее состояние слайсера: принтер, режим 3D/5D, профиль качества, материал, объекты в сцене, "
            "состояние слайсинга. Вызывай в начале работы."
        ),
        function=slicer.get_state,
        group="settings",
    )
    registry.add(
        name="get_settings",
        description=(
            "Прочитать значения настроек по ключам (глобальных или экструдера — определяется автоматически). "
            "Возвращает значение, единицы, допустимые варианты и диапазоны."
        ),
        function=slicer.get_settings,
        properties={
            "keys": string_list_param("Ключи настроек, например ['layer_height','infill_sparse_density']"),
        },
        required=["keys"],
        group="settings",
    )
    registry.add(
        name="search_settings",
        description=(
            "Найти настройки по части ключа или названия (англ.), например 'support', 'brim', 'retract'. "
            "Используй, если не уверен в точном ключе."
        ),
        function=slicer.search_settings,
        properties={
            "query": string_param("Поисковая строка"),
            "limit": integer_param("Максимум результатов, по умолчанию 25"),
        },
        required=["query"],
        group="settings",
    )
    registry.add(
        name="set_settings",
        description=(
            "Изменить одну или несколько настроек. Значения проверяются по типу и допустимому диапазону. "
            "Обязательно передавай reason — объяснение для пользователя."
        ),
        function=slicer.set_settings,
        properties={
            "changes": {
                "type": "object",
                "description": "Словарь ключ → значение, например {\"wall_line_count\": 4}",
                "additionalProperties": True,
            },
            "reason": string_param("Почему меняются эти настройки (покажется пользователю)"),
        },
        required=["changes", "reason"],
        group="settings",
    )
    registry.add(
        name="reset_settings",
        description="Сбросить пользовательские изменения настроек (все или по списку ключей) к значениям профиля.",
        function=slicer.reset_settings,
        properties={
            "keys": string_list_param("Ключи; пусто — сбросить все"),
        },
        group="settings",
    )
    registry.add(
        name="list_quality_profiles",
        description=(
            "Список профилей качества (quality_type) и пользовательских профилей для текущего принтера, "
            "а также активный."
        ),
        function=slicer.list_quality_profiles,
        group="settings",
    )
    registry.add(
        name="set_quality_profile",
        description="Выбрать профиль качества по quality_type или имени пользовательского профиля.",
        function=slicer.set_quality_profile,
        properties={
            "name": string_param("quality_type или имя профиля из list_quality_profiles"),
        },
        required=["name"],
        group="settings",
    )
    registry.add(
        name="list_materials",
        description="Список материалов, доступных для текущего принтера/экструдера, и активный материал.",
        function=slicer.list_materials,
        properties={
            "query": string_param("Фильтр по названию/типу, например 'PETG'"),
        },
        group="settings",
    )
    registry.add(
        name="set_material",
        description="Выбрать материал по id из list_materials.",
        function=slicer.set_material,
        properties={
            "material_id": string_param("id материала из list_materials"),
        },
        required=["material_id"],
        group="settings",
    )
    registry.add(
        name="save_profile",
        description="Сохранить текущие настройки как пользовательский профиль (чтобы не настраивать заново).",
        function=slicer.save_profile,
        properties={
            "name": string_param("Имя профиля"),
        },
        required=["name"],
        group="settings",
    )
    registry.add(
        name="get_print_mode",
        description="Текущий режим печати: classic (3D), cylindrical (5D Spiral), cylindrical_full (5D).",
        function=slicer.get_print_mode,
        group="settings",
    )
    registry.add(
        name="set_print_mode",
        description="Переключить режим печати 3D ↔ 5D.",
        function=slicer.set_print_mode,
        properties={
            "mode": string_param("Режим", PRINT_MODES),
            "reason": string_param("Почему выбран этот режим"),
        },
        required=["mode"],
        group="settings",
    )


def _add_model_tools(registry, slicer):
    registry.add(
        name="load_model",
        description=(
            "Загрузить файл модели (STL/OBJ/3MF) в сцену по пути на диске. "
            "Возвращает ID и габариты новых объектов."
        ),
        function=slicer.load_model,
        properties={
            "path": string_param("Абсолютный путь к файлу"),
        },
        required=["path"],
        group="models",
    )
    registry.add(
        name="list_objects",
        description="Список объектов в сцене: ID, имя, габариты (мм), положение, масштаб; размеры рабочей области.",
        function=slicer.list_objects,
        group="models",
    )
    registry.add(
        name="get_object_dimensions",
        description="Габариты и положение одного объекта.",
        function=slicer.get_object_dimensions,
        properties={
            "object_id": OBJECT_ID,
        },
        group="models",
    )
    registry.add(
        name="move_object",
        description="Переместить объект по столу. Координаты центра в мм, (0,0) — центр стола. relative=true — сдвиг.",
        function=slicer.move_object,
        properties={
            "object_id": OBJECT_ID,
            "x": number_param("X, мм"),
            "y": number_param("Y, мм"),
            "relative": boolean_param("Сдвиг вместо абсолютных координат"),
        },
        group="models",
    )
    registry.add(
        name="center_object",
        description="Поставить объект в центр стола (и опустить на стол).",
        function=slicer.center_object,
        properties={
            "object_id": OBJECT_ID,
        },
        group="models",
    )
    registry.add(
        name="rotate_object",
        description="Повернуть объект вокруг оси стола (z — вертикаль) на заданный угол; объект опускается на стол.",
        function=slicer.rotate_object,
        properties={
            "object_id": OBJECT_ID,
            "axis": string_param("Ось", ["x", "y", "z"]),
            "degrees": number_param("Угол, градусы"),
        },
        required=["axis", "degrees"],
        group="models",
    )
    registry.add(
        name="scale_object",
        description="Масштабировать объект в процентах от исходного размера (равномерно или по осям).",
        function=slicer.scale_object,
        properties={
            "object_id": OBJECT_ID,
            "percent": number_param("Равномерный масштаб, %"),
            "x": number_param("Масштаб по X, %"),
            "y": number_param("Масштаб по Y, %"),
            "z": number_param("Масштаб по Z, %"),
        },
        group="models",
    )
    registry.add(
        name="arrange_all",
        description="Автоматически расставить все объекты на столе.",
        function=slicer.arrange_all,
        group="models",
    )
    registry.add(
        name="delete_object",
        description="Удалить объект из сцены (object_id='all' — очистить сцену).",
        function=slicer.delete_object,
        properties={
            "object_id": string_param("ID объекта или 'all'"),
        },
        required=["object_id"],
        group="models",
    )


def _add_analysis_tools(registry, slicer):
    registry.add(
        name="analyze_geometry",
        description=(
            "Анализ модели: габариты, объём, площадь, замкнутость сетки, нависания и оценка поддержек в текущей "
            "ориентации, площадь контакта со столом, признаки осесимметричности (кандидат на 5D), подсказки."
        ),
        function=slicer.analyze_geometry,
        properties={
            "object_id": OBJECT_ID,
            "support_angle": number_param("Угол нависания для поддержек, ° (по умолчанию из настроек)"),
        },
        group="analysis",
    )
    registry.add(
        name="suggest_orientations",
        description=(
            "Перебрать ориентации модели и оценить каждую: объём поддержек, площадь контакта со столом, высота. "
            "Возвращает рейтинг; применить — apply_orientation."
        ),
        function=slicer.suggest_orientations,
        properties={
            "object_id": OBJECT_ID,
            "support_angle": number_param("Угол нависания, °"),
            "top_n": integer_param("Сколько вариантов вернуть"),
        },
        group="analysis",
    )
    registry.add(
        name="apply_orientation",
        description=(
            "Применить ориентацию из suggest_orientations (по rank) или по вектору down — направлению в текущей "
            "системе координат, которое станет «вниз» (на стол)."
        ),
        function=slicer.apply_orientation,
        properties={
            "object_id": OBJECT_ID,
            "rank": integer_param("Номер варианта из suggest_orientations (1 — лучший)"),
            "down": {"type": "array", "items": {"type": "number"}, "description": "Вектор [x,y,z]"},
        },
        group="analysis",
    )
    registry.add(
        name="get_scene_snapshot",
        description=(
            "Сделать снимок сцены (модели на столе, изометрия) и посмотреть на него. Используй, чтобы визуально "
            "проверить ориентацию и расположение после поворота/расстановки и перед экспортом."
        ),
        function=slicer.get_scene_snapshot,
        properties={
            "size": integer_param("Размер снимка в пикселях, 128–1024 (по умолчанию 512)"),
        },
        group="analysis",
    )


def _add_slicing_tools(registry, slicer):
    registry.add(
        name="start_slicing",
        description="Запустить слайсинг. По умолчанию ждёт завершения и возвращает время печати и расход материала.",
        function=slicer.start_slicing,
        properties={
            "wait": boolean_param("Ждать завершения (по умолчанию true)"),
        },
        group="slicing",
    )
    registry.add(
        name="get_slicing_status",
        description="Статус и прогресс слайсинга.",
        function=slicer.get_slicing_status,
        group="slicing",
    )
    registry.add(
        name="get_slicing_result",
        description="Результат последнего слайсинга: время печати, расход материала.",
        function=slicer.get_slicing_result,
        group="slicing",
    )
    registry.add(
        name="export_gcode",
        description="Сохранить G-code последнего слайсинга и проверить его (слои, перемещения, температуры).",
        function=slicer.export_gcode,
        properties={
            "path": string_param("Путь к файлу .gcode (необязательно)"),
            "model_name": MODEL_NAME,
        },
        group="slicing",
    )
    registry.add(
        name="save_project_3mf",
        description="Сохранить проект 3MF (модель + все настройки).",
        function=slicer.save_project_3mf,
        properties={
            "path": string_param("Путь к файлу .3mf (необязательно)"),
            "model_name": MODEL_NAME,
        },
        group="slicing",
    )
    registry.add(
        name="export_results",
        description=(
            "Сохранить результат для сдачи: results/<model_name>/project.3mf и result.gcode. "
            "Нужен выполненный слайсинг."
        ),
        function=slicer.export_results,
        properties={
            "model_name": MODEL_NAME,
        },
        required=["model_name"],
        group="slicing",
    )
