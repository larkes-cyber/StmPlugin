import base64
import math
import os
import re
import threading
import time

import numpy
from PyQt5.QtCore import QBuffer, QUrl
from UM.Backend.Backend import BackendState
from UM.Math.Quaternion import Quaternion
from UM.Math.Vector import Vector
from UM.Mesh.MeshWriter import MeshWriter
from UM.Operations.GravityOperation import GravityOperation
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
from UM.Operations.RotateOperation import RotateOperation
from UM.Operations.ScaleOperation import ScaleOperation
from UM.Operations.TranslateOperation import TranslateOperation
from UM.PluginRegistry import PluginRegistry
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from steslicer.Settings.ExtruderManager import ExtruderManager
from steslicer.SteSlicerApplication import SteSlicerApplication
from steslicer.Snapshot import Snapshot

from . import geometry
from .core import Logger
from .core import on_main, run_on_main
from .ai import ToolError

PRINT_MODE_KEY = "printing_mode"
PRINT_MODES = ("classic", "cylindrical", "cylindrical_full")
MODE_LABELS = {
    "classic": "3D (classic)",
    "cylindrical": "5D Spiral (cylindrical)",
    "cylindrical_full": "5D (cylindrical_full)",
}
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")

PLATE_AXES_IN_SCENE = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 0.0, -1.0),
    "z": (0.0, 1.0, 0.0),
}

BACKEND_STATE_NAMES = {
    BackendState.NotStarted: "pending",
    BackendState.Processing: "processing",
    BackendState.Done: "done",
    BackendState.Error: "error",
    BackendState.Disabled: "disabled",
}

TRUE_STRINGS = ("true", "1", "yes", "on", "да")
TEMPERATURE_PATTERN = re.compile(r"\bS(\d+(?:\.\d+)?)")

REVIEW_SETTING_KEYS = (
    "layer_height",
    "cylindrical_layer_height",
    "infill_sparse_density",
    "speed_print",
    "material_print_temperature",
    "material_bed_temperature",
)
FIVE_AXIS_ONLY_KEYS = ("cylindrical_layer_height",)
USER_CHANGE_REASON = "Изменено пользователем"

LOAD_TIMEOUT = 120
SLICE_TIMEOUT = 900
WRITE_TIMEOUT = 120
DEFAULT_SNAPSHOT_SIZE = 512


def _app():
    return SteSlicerApplication.getInstance()


def _jsonable(value):
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


def _round(value, digits=2):
    return round(float(value), digits)


def _safe_name(name):
    name = re.sub(r"[^\w\-. ]+", "_", name or "model", flags=re.UNICODE).strip(" .")
    return name or "model"


def check_gcode(path):
    stats = {
        "size_kb": round(os.path.getsize(path) / 1024.0, 1),
        "lines": 0,
        "moves": 0,
        "layers": 0,
        "max_nozzle_temp": None,
        "max_bed_temp": None,
        "flavor": None,
    }

    with open(path, "r", encoding="utf-8", errors="replace") as gcode_file:
        for line in gcode_file:
            stats["lines"] += 1
            if line.startswith(("G0", "G1", "G2", "G3")):
                stats["moves"] += 1
            elif line.startswith(";LAYER:"):
                stats["layers"] += 1
            elif line.startswith(("M104", "M109", "M140", "M190")):
                _update_max_temperature(stats, line)
            elif line.startswith(";FLAVOR:") and stats["flavor"] is None:
                stats["flavor"] = line.split(":", 1)[1].strip()

    problems = []
    if stats["moves"] == 0:
        problems.append("В файле нет команд перемещения")
    if stats["layers"] == 0:
        problems.append("Не найдены маркеры слоёв ;LAYER:")

    stats["valid"] = not problems
    stats["problems"] = problems
    return stats


def _update_max_temperature(stats, line):
    match = TEMPERATURE_PATTERN.search(line)
    if match is None:
        return

    if line.startswith(("M140", "M190")):
        key = "max_bed_temp"
    else:
        key = "max_nozzle_temp"

    temperature = float(match.group(1))
    if stats[key] is None or temperature > stats[key]:
        stats[key] = temperature


class SlicerAPI(object):
    def __init__(self, events, config):
        self._events = events
        self._config = config
        self._object_ids = {}
        self._next_object_number = 1
        self._suggestions = {}
        self._backend = None
        self._slice = {
            "state": "idle",
            "progress": 0.0,
            "message": "",
            "started": None,
            "finished": None,
        }
        self._last_progress_emit = 0.0

    def _global_stack(self):
        stack = _app().getGlobalContainerStack()
        if stack is None:
            raise ToolError("Не выбран принтер — добавьте принтер в STE Slicer")
        return stack

    def _extruder_stack(self):
        return ExtruderManager.getInstance().getActiveExtruderStack()

    def _stack_for(self, key):
        global_stack = self._global_stack()
        if global_stack.getProperty(key, "settable_per_extruder"):
            extruder_stack = self._extruder_stack()
            if extruder_stack is not None:
                return extruder_stack, "extruder"
        return global_stack, "global"

    def _require_key(self, key):
        if self._global_stack().getProperty(key, "type") is None:
            raise ToolError("Неизвестная настройка '%s' — найдите ключ через search_settings" % key)

    def _describe(self, key):
        self._require_key(key)
        global_stack = self._global_stack()
        stack, scope = self._stack_for(key)
        setting_type = global_stack.getProperty(key, "type")

        info = {
            "key": key,
            "label": global_stack.getProperty(key, "label"),
            "type": setting_type,
            "value": _jsonable(stack.getProperty(key, "value")),
            "unit": global_stack.getProperty(key, "unit") or "",
            "scope": scope,
            "user_changed": stack.userChanges.getProperty(key, "value") is not None,
        }
        if setting_type == "enum":
            info["options"] = _jsonable(global_stack.getProperty(key, "options"))

        for property_name, info_key in (("minimum_value", "min"), ("maximum_value", "max")):
            bound = stack.getProperty(key, property_name)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                info[info_key] = bound
        return info

    def _convert(self, key, value):
        global_stack = self._global_stack()
        stack, _ = self._stack_for(key)
        setting_type = global_stack.getProperty(key, "type")

        try:
            value = self._convert_to_type(key, value, setting_type, global_stack)
        except (TypeError, ValueError):
            raise ToolError("Значение '%s' не подходит для %s (тип %s)" % (value, key, setting_type))

        if setting_type in ("int", "float"):
            self._check_bounds(key, value, stack)
        return value

    @staticmethod
    def _convert_to_type(key, value, setting_type, global_stack):
        if setting_type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in TRUE_STRINGS
            return bool(value)

        if setting_type == "int":
            return int(round(float(value)))

        if setting_type == "float":
            return float(value)

        if setting_type == "enum":
            options = global_stack.getProperty(key, "options") or {}
            value = str(value)
            if value in options:
                return value
            for option_key, label in options.items():
                if str(label).lower() == value.lower():
                    return option_key
            raise ToolError(
                "Недопустимое значение '%s' для %s. Варианты: %s" % (value, key, ", ".join(options.keys()))
            )

        if setting_type in ("str", "extruder", "optional_extruder"):
            return str(value)

        return value

    @staticmethod
    def _check_bounds(key, value, stack):
        minimum = stack.getProperty(key, "minimum_value")
        maximum = stack.getProperty(key, "maximum_value")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolError("%s = %s меньше минимума %s" % (key, value, minimum))
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolError("%s = %s больше максимума %s" % (key, value, maximum))

    def _apply_changes(self, changes):
        applied = []
        errors = []
        for key, value in changes.items():
            try:
                self._require_key(key)
                new_value = self._convert(key, value)
                stack, scope = self._stack_for(key)
                old_value = stack.getProperty(key, "value")
                stack.userChanges.setProperty(key, "value", new_value)
                actual_value = stack.getProperty(key, "value")
            except ToolError as error:
                Logger.log("w", "setting %s rejected: %s", key, error)
                errors.append({"key": key, "error": str(error)})
                continue

            applied.append({
                "key": key,
                "label": self._global_stack().getProperty(key, "label"),
                "old": _jsonable(old_value),
                "new": _jsonable(actual_value),
                "scope": scope,
            })
            Logger.log("d", "setting %s [%s]: %r -> %r (requested %r)", key, scope, old_value, actual_value, value)
        return applied, errors

    def _validation_errors(self):
        error_keys = []
        for stack in (self._global_stack(), self._extruder_stack()):
            if stack is None:
                continue
            for key in stack.getErrorKeys():
                if key not in error_keys:
                    error_keys.append(key)
        return error_keys

    @on_main
    def get_state(self):
        global_stack = self._global_stack()
        return {
            "printer": global_stack.getName(),
            "print_mode": self._mode_info(),
            "quality": self._active_quality(),
            "material": self._active_material(),
            "objects": [self._node_info(node) for node in self._object_nodes()],
            "slicing": dict(self._slice),
            "build_volume_mm": self._build_volume(),
        }

    @on_main
    def get_settings(self, keys):
        if isinstance(keys, str):
            keys = [keys]

        settings = []
        errors = []
        for key in keys:
            try:
                settings.append(self._describe(key))
            except ToolError as error:
                errors.append({"key": key, "error": str(error)})
        return {"settings": settings, "errors": errors}

    @on_main
    def search_settings(self, query, limit=25):
        global_stack = self._global_stack()
        words = str(query).lower().split()
        limit = int(limit or 25)

        found = []
        for key in sorted(global_stack.getAllKeys()):
            setting_type = global_stack.getProperty(key, "type")
            if setting_type in (None, "category"):
                continue

            label = str(global_stack.getProperty(key, "label") or "")
            searchable_text = (key + " " + label).lower()
            if all(word in searchable_text for word in words):
                stack, scope = self._stack_for(key)
                found.append({
                    "key": key,
                    "label": label,
                    "type": setting_type,
                    "value": _jsonable(stack.getProperty(key, "value")),
                    "unit": global_stack.getProperty(key, "unit") or "",
                    "scope": scope,
                })

            if len(found) >= limit:
                break

        return {"query": query, "results": found}

    @on_main
    def set_settings(self, changes, reason=""):
        if not isinstance(changes, dict) or not changes:
            raise ToolError("changes должен быть непустым объектом ключ → значение")

        applied, errors = self._apply_changes(changes)
        if applied:
            self._events.emit("settings_changed", changes=applied, reason=reason)

        return {
            "applied": applied,
            "errors": errors,
            "validation_errors": self._validation_errors(),
        }

    @on_main
    def reset_settings(self, keys=None):
        stacks = [self._global_stack()]
        extruder_stack = self._extruder_stack()
        if extruder_stack is not None:
            stacks.append(extruder_stack)

        for stack in stacks:
            if keys:
                for key in keys:
                    stack.userChanges.removeInstance(key)
            else:
                stack.userChanges.clear()

        if keys:
            reason = "Сброс настроек: " + ", ".join(keys)
        else:
            reason = "Сброс настроек: все"
        self._events.emit("settings_changed", changes=[], reason=reason)
        return {"reset": keys or "all"}

    def _active_quality(self):
        machine_manager = _app().getMachineManager()
        return {
            "quality_type": machine_manager.activeQualityType,
            "name": machine_manager.activeQualityOrQualityChangesName,
        }

    def _active_material(self):
        extruder_stack = self._extruder_stack()
        if extruder_stack is None:
            return None

        material = extruder_stack.material
        return {
            "id": material.getMetaDataEntry("base_file") or material.getId(),
            "name": material.getName(),
            "type": material.getMetaDataEntry("material"),
            "brand": material.getMetaDataEntry("brand"),
        }

    @on_main
    def list_quality_profiles(self):
        global_stack = self._global_stack()
        quality_manager = _app().getQualityManager()
        groups = quality_manager.getQualityGroups(global_stack) or {}

        quality_profiles = []
        for quality_type in sorted(groups.keys()):
            group = groups[quality_type]
            quality_profiles.append({
                "quality_type": quality_type,
                "name": group.name,
                "available": bool(group.is_available),
            })

        custom_profiles = []
        for group in quality_manager.getQualityChangesGroups(global_stack) or []:
            custom_profiles.append({"name": group.name})

        return {
            "active": self._active_quality(),
            "quality": quality_profiles,
            "custom": custom_profiles,
        }

    @on_main
    def set_quality_profile(self, name):
        global_stack = self._global_stack()
        quality_manager = _app().getQualityManager()
        machine_manager = _app().getMachineManager()
        groups = quality_manager.getQualityGroups(global_stack) or {}

        target_type = None
        for quality_type, group in groups.items():
            if name in (quality_type, group.name):
                target_type = quality_type

        if target_type is not None:
            if not groups[target_type].is_available:
                raise ToolError("Профиль %s недоступен для текущего материала/сопла" % name)
            machine_manager.setQualityGroupByQualityType(target_type)
        else:
            custom_group = self._find_custom_profile(quality_manager, global_stack, name)
            if custom_group is None:
                available = ", ".join(sorted(groups.keys()))
                raise ToolError("Профиль '%s' не найден. Доступные: %s" % (name, available))
            machine_manager.setQualityChangesGroup(custom_group)

        active = self._active_quality()
        self._events.emit("profile_changed", what="quality", value=active)
        return {"active": active}

    @staticmethod
    def _find_custom_profile(quality_manager, global_stack, name):
        for group in quality_manager.getQualityChangesGroups(global_stack) or []:
            if group.name == name:
                return group
        return None

    @on_main
    def list_materials(self, query=None):
        query = (query or "").lower()

        materials = []
        for material in self._material_entries():
            searchable_text = " ".join(str(value) for value in material.values()).lower()
            if query and query not in searchable_text:
                continue
            materials.append(material)

        return {
            "active": self._active_material(),
            "materials": materials[:150],
            "total": len(materials),
        }

    def _material_entries(self):
        materials = []
        for material_id, node in self._available_materials().items():
            materials.append({
                "id": material_id,
                "name": node.getMetaDataEntry("name"),
                "type": node.getMetaDataEntry("material"),
                "brand": node.getMetaDataEntry("brand"),
                "color": node.getMetaDataEntry("color_name"),
            })

        materials.sort(key=lambda item: (str(item["type"]), str(item["brand"]), str(item["name"])))
        return materials

    def _available_materials(self):
        material_manager = _app().getMaterialManager()
        nodes = material_manager.getAvailableMaterialsForMachineExtruder(self._global_stack(), self._extruder_stack())
        return nodes or {}

    @on_main
    def set_material(self, material_id):
        extruder_stack = self._extruder_stack()
        if extruder_stack is None:
            raise ToolError("Нет активного экструдера")

        if material_id not in self._available_materials():
            raise ToolError("Материал '%s' не найден — возьмите id из list_materials" % material_id)

        position = str(extruder_stack.getMetaDataEntry("position") or "0")
        _app().getMachineManager().setMaterialById(position, material_id)

        active = self._active_material()
        self._events.emit("profile_changed", what="material", value=active)
        return {"active": active}

    @on_main
    def save_profile(self, name):
        _app().getQualityManager().createQualityChanges(name)
        return {"saved": name, "hint": "Профиль можно выбрать через set_quality_profile"}

    def _mode_info(self):
        stack, scope = self._stack_for(PRINT_MODE_KEY)
        mode = stack.getProperty(PRINT_MODE_KEY, "value")
        options = self._global_stack().getProperty(PRINT_MODE_KEY, "options") or {}
        return {
            "key": PRINT_MODE_KEY,
            "mode": mode,
            "label": MODE_LABELS.get(mode, mode),
            "scope": scope,
            "options": [option for option in PRINT_MODES if option in options],
        }

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

        self._events.emit("settings_changed", changes=applied, reason=reason or "Режим печати: " + MODE_LABELS[mode])
        return self._mode_info()

    def _scene(self):
        return _app().getController().getScene()

    def _object_nodes(self):
        root = self._scene().getRoot()
        nodes = []
        for node in DepthFirstIterator(root):
            if node is root:
                continue

            is_object = node.callDecoration("isSliceable") or node.callDecoration("isGroup")
            if not is_object:
                continue

            parent = node.getParent()
            if parent is not None and parent.callDecoration("isGroup"):
                continue

            nodes.append(node)
        return nodes

    def _object_id(self, node):
        key = id(node)
        if key not in self._object_ids:
            self._object_ids[key] = "obj%d" % self._next_object_number
            self._next_object_number += 1
        return self._object_ids[key]

    def _find(self, object_id=None):
        nodes = self._object_nodes()
        if not nodes:
            raise ToolError("Сцена пуста — сначала загрузите модель (load_model)")

        known_ids = ", ".join(self._object_id(node) for node in nodes)

        if not object_id or object_id == "auto":
            if len(nodes) == 1:
                return nodes[0]
            raise ToolError("В сцене несколько объектов, укажите object_id: %s" % known_ids)

        for node in nodes:
            if object_id in (self._object_id(node), node.getName()):
                return node
        raise ToolError("Объект '%s' не найден. Есть: %s" % (object_id, known_ids))

    def _node_info(self, node):
        box = node.getBoundingBox()
        scale = node.getScale()
        info = {
            "object_id": self._object_id(node),
            "name": node.getName(),
            "size_mm": {
                "x": _round(box.width),
                "y": _round(box.depth),
                "z": _round(box.height),
            },
            "center_mm": {
                "x": _round(box.center.x),
                "y": _round(-box.center.z),
            },
            "bottom_z_mm": _round(box.bottom),
            "on_plate": abs(box.bottom) < 0.05,
            "scale_pct": {
                "x": _round(scale.x * 100),
                "y": _round(scale.z * 100),
                "z": _round(scale.y * 100),
            },
        }
        if hasattr(node, "isOutsideBuildArea"):
            info["outside_build_area"] = bool(node.isOutsideBuildArea())
        return info

    def _build_volume(self):
        global_stack = self._global_stack()
        return {
            "x": global_stack.getProperty("machine_width", "value"),
            "y": global_stack.getProperty("machine_depth", "value"),
            "z": global_stack.getProperty("machine_height", "value"),
        }

    def _drop_to_plate(self, node):
        GravityOperation(node).push()

    def _settle(self, node, center_x, center_z):
        box = node.getBoundingBox()
        offset = Vector(center_x - box.center.x, 0.0, center_z - box.center.z)
        if offset.length() > 1e-4:
            TranslateOperation(node, offset).push()
        self._drop_to_plate(node)

    def _rotate(self, node, scene_axis, angle_radians):
        box = node.getBoundingBox()
        center_x = box.center.x
        center_z = box.center.z

        if abs(angle_radians) > 1e-9:
            rotation = Quaternion.fromAngleAxis(angle_radians, Vector(*scene_axis))
            RotateOperation(node, rotation, rotate_around_point=box.center).push()

        self._settle(node, center_x, center_z)
        self._suggestions.pop(self._object_id(node), None)

    def _node_keys(self):
        return [id(node) for node in self._object_nodes()]

    def _infos_for_keys(self, keys):
        return [self._node_info(node) for node in self._object_nodes() if id(node) in keys]

    def _read_file(self, path):
        _app().readLocalFile(QUrl.fromLocalFile(path))

    def load_model(self, path):
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ToolError("Файл не найден: %s" % path)
        if not path.lower().endswith(MODEL_EXTENSIONS):
            raise ToolError("Поддерживаются только %s" % ", ".join(MODEL_EXTENSIONS))

        keys_before = set(run_on_main(self._node_keys))
        Logger.log("i", "loading model %s (%d objects in scene)", path, len(keys_before))
        run_on_main(self._read_file, path)

        new_keys = self._wait_for_new_objects(keys_before)
        if not new_keys:
            Logger.log("w", "model %s did not appear in scene", path)
            raise ToolError("Модель не появилась в сцене за %d с (проверьте файл и диалоги слайсера)" % LOAD_TIMEOUT)

        objects = run_on_main(self._infos_for_keys, set(new_keys))
        Logger.log("i", "model loaded: %s", objects)
        self._events.emit("scene_changed", action="load", objects=objects)
        return {"loaded": objects, "file": path}

    def _wait_for_new_objects(self, keys_before):
        deadline = time.time() + LOAD_TIMEOUT
        new_keys = []
        previous_keys = None
        stable_since = None

        while time.time() < deadline:
            time.sleep(0.4)
            new_keys = [key for key in run_on_main(self._node_keys) if key not in keys_before]

            if not new_keys or new_keys != previous_keys:
                stable_since = None
            elif stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since > 0.8:
                break

            previous_keys = new_keys

        return new_keys

    @on_main
    def list_objects(self):
        return {
            "objects": [self._node_info(node) for node in self._object_nodes()],
            "build_volume_mm": self._build_volume(),
            "coordinates": "x,y — центр объекта на столе, (0,0) — центр стола; z — вверх",
        }

    @on_main
    def get_object_dimensions(self, object_id=None):
        return self._node_info(self._find(object_id))

    @on_main
    def move_object(self, object_id=None, x=None, y=None, relative=False):
        node = self._find(object_id)
        box = node.getBoundingBox()
        current_x = box.center.x
        current_y = -box.center.z

        if relative:
            target_x = current_x + float(x or 0)
            target_y = current_y + float(y or 0)
        else:
            target_x = current_x if x is None else float(x)
            target_y = current_y if y is None else float(y)

        offset = Vector(target_x - current_x, 0.0, -(target_y - current_y))
        TranslateOperation(node, offset).push()
        self._drop_to_plate(node)

        info = self._node_info(node)
        self._events.emit("scene_changed", action="move", objects=[info])
        return info

    @on_main
    def center_object(self, object_id=None):
        return self.move_object(object_id, 0.0, 0.0, False)

    @on_main
    def rotate_object(self, axis, degrees, object_id=None):
        if axis not in PLATE_AXES_IN_SCENE:
            raise ToolError("axis должен быть x, y или z")

        node = self._find(object_id)
        self._rotate(node, PLATE_AXES_IN_SCENE[axis], math.radians(float(degrees)))

        info = self._node_info(node)
        self._events.emit("scene_changed", action="rotate", objects=[info])
        return info

    @on_main
    def scale_object(self, object_id=None, percent=None, x=None, y=None, z=None):
        node = self._find(object_id)
        if percent is None and x is None and y is None and z is None:
            raise ToolError("Укажите percent или x/y/z")

        current_scale = node.getScale()
        scale_x = self._scale_factor(x, percent, current_scale.x)
        scale_y = self._scale_factor(y, percent, current_scale.z)
        scale_z = self._scale_factor(z, percent, current_scale.y)
        if min(scale_x, scale_y, scale_z) <= 0:
            raise ToolError("Масштаб должен быть больше 0")

        box = node.getBoundingBox()
        center_x = box.center.x
        center_z = box.center.z
        new_scale = Vector(scale_x, scale_z, scale_y)
        ScaleOperation(node, new_scale, set_scale=True, scale_around_point=box.center).push()
        self._settle(node, center_x, center_z)
        self._suggestions.pop(self._object_id(node), None)

        info = self._node_info(node)
        self._events.emit("scene_changed", action="scale", objects=[info])
        return info

    @staticmethod
    def _scale_factor(axis_percent, percent, current_factor):
        if axis_percent is not None:
            return axis_percent / 100.0
        if percent is not None:
            return percent / 100.0
        return current_factor

    def arrange_all(self):
        run_on_main(_app().arrangeAll)
        time.sleep(1.5)

        result = self.list_objects()
        self._events.emit("scene_changed", action="arrange", objects=result["objects"])
        return result

    @on_main
    def delete_object(self, object_id):
        if object_id == "all":
            _app().deleteAll()
            self._suggestions = {}
            self._events.emit("scene_changed", action="delete_all", objects=[])
            return {"deleted": "all"}

        node = self._find(object_id)
        node_id = self._object_id(node)
        RemoveSceneNodeOperation(node).push()
        self._suggestions.pop(node_id, None)
        self._events.emit("scene_changed", action="delete", objects=[])
        return {"deleted": node_id}

    def _plate_triangles(self, node):
        chunks = []
        for child in DepthFirstIterator(node):
            triangles = self._world_triangles(child)
            if triangles is not None:
                chunks.append(triangles)

        if not chunks:
            raise ToolError("У объекта нет сетки")
        return geometry.scene_to_plate(numpy.concatenate(chunks))

    @staticmethod
    def _world_triangles(node):
        mesh = node.getMeshData()
        if mesh is None:
            return None

        vertices = mesh.getVertices()
        if vertices is None or len(vertices) == 0:
            return None

        indices = mesh.getIndices()
        if indices is not None:
            triangles = vertices[indices]
        elif len(vertices) % 3 == 0:
            triangles = vertices.reshape(-1, 3, 3)
        else:
            return None

        matrix = node.getWorldTransformation().getData()
        points = triangles.reshape(-1, 3).astype(numpy.float64)
        world_points = points.dot(matrix[:3, :3].T) + matrix[:3, 3]
        return world_points.reshape(-1, 3, 3)

    def _prepare_geometry(self, object_id, support_angle):
        node = self._find(object_id)
        if support_angle is None:
            stack, _ = self._stack_for("support_angle")
            support_angle = stack.getProperty("support_angle", "value") or 50.0
        return self._object_id(node), self._plate_triangles(node), float(support_angle)

    def analyze_geometry(self, object_id=None, support_angle=None):
        node_id, triangles, angle = run_on_main(self._prepare_geometry, object_id, support_angle)
        result = geometry.analyze(triangles, angle)
        result["object_id"] = node_id
        return result

    def suggest_orientations(self, object_id=None, support_angle=None, top_n=5):
        node_id, triangles, angle = run_on_main(self._prepare_geometry, object_id, support_angle)
        result = geometry.suggest_orientations(triangles, angle, top_n or 5)
        result["object_id"] = node_id
        self._suggestions[node_id] = result["candidates"]
        return result

    @on_main
    def apply_orientation(self, object_id=None, rank=None, down=None):
        node = self._find(object_id)
        node_id = self._object_id(node)

        if down is None:
            down = self._suggested_down(node_id, int(rank or 1))
        if len(down) != 3:
            raise ToolError("down — вектор из трёх чисел")

        axis, angle = geometry.rotation_to_down(down)
        self._rotate(node, geometry.plate_to_scene_vector(axis), math.radians(angle))

        info = self._node_info(node)
        self._events.emit("scene_changed", action="orient", objects=[info])
        return {
            "object": info,
            "rotation": {
                "axis_plate": [round(component, 4) for component in axis],
                "angle_deg": round(angle, 1),
            },
        }

    def _suggested_down(self, node_id, rank):
        candidates = self._suggestions.get(node_id)
        if not candidates:
            raise ToolError("Сначала вызовите suggest_orientations для %s" % node_id)

        for candidate in candidates:
            if candidate["rank"] == rank:
                return candidate["down"]
        raise ToolError("Нет варианта с rank=%d" % rank)

    def _snapshot_png(self, size):
        image = Snapshot.snapshot(width=size, height=size)
        if image is None:
            raise ToolError("В сцене нет видимых моделей для снимка")

        buffer = QBuffer()
        buffer.open(QBuffer.ReadWrite)
        image.save(buffer, "PNG")
        png_data = bytes(buffer.data())
        buffer.close()
        return png_data

    def get_scene_snapshot(self, size=DEFAULT_SNAPSHOT_SIZE):
        size = max(128, min(int(size or DEFAULT_SNAPSHOT_SIZE), 1024))
        png_data = run_on_main(self._snapshot_png, size)
        image_url = "data:image/png;base64," + base64.b64encode(png_data).decode("ascii")

        Logger.log("d", "scene snapshot %dx%d, %d bytes", size, size, len(png_data))
        self._events.emit("snapshot", image=image_url, caption="Снимок сцены")
        return {
            "image_attached": True,
            "size_px": size,
            "view": "изометрия: камера спереди-слева-сверху, смотрит на центр моделей; стол — внизу",
            "_image": image_url,
        }

    @on_main
    def review_proposal(self):
        mode = self._mode_info()
        mode_options = mode["options"] or list(PRINT_MODES)

        return {
            "print_mode": mode["mode"],
            "print_modes": [{"value": option, "label": MODE_LABELS[option]} for option in mode_options],
            "material": self._active_material(),
            "materials": self._material_entries(),
            "settings": self._review_settings(mode["mode"]),
            "objects": [self._node_info(node) for node in self._object_nodes()],
        }

    def _review_settings(self, mode):
        settings = []
        for key in REVIEW_SETTING_KEYS:
            if key in FIVE_AXIS_ONLY_KEYS and mode == "classic":
                continue
            if self._global_stack().getProperty(key, "type") is None:
                continue
            settings.append(self._describe(key))
        return settings

    def apply_review_changes(self, changes):
        applied = []
        errors = []

        print_mode = changes.get("print_mode")
        if print_mode:
            description = "режим печати → %s" % MODE_LABELS.get(print_mode, print_mode)
            self._try_change(description, lambda: self.set_print_mode(print_mode, USER_CHANGE_REASON), applied, errors)

        material_id = changes.get("material_id")
        if material_id:
            description = "материал → %s" % material_id
            self._try_change(description, lambda: self.set_material(material_id), applied, errors)

        settings = changes.get("settings")
        if settings:
            self._apply_review_settings(settings, applied, errors)

        placement = changes.get("placement")
        if placement:
            self._apply_review_placement(placement, applied, errors)

        return {"applied": applied, "errors": errors}

    @staticmethod
    def _try_change(description, change, applied, errors):
        try:
            change()
        except ToolError as error:
            errors.append("%s: %s" % (description, error))
            return
        applied.append(description)

    def _apply_review_settings(self, settings, applied, errors):
        try:
            result = self.set_settings(settings, USER_CHANGE_REASON)
        except ToolError as error:
            errors.append("настройки: %s" % error)
            return

        for change in result["applied"]:
            applied.append("%s: %s → %s" % (change["label"] or change["key"], change["old"], change["new"]))
        for error in result["errors"]:
            errors.append("%s: %s" % (error["key"], error["error"]))

    def _apply_review_placement(self, placement, applied, errors):
        object_id = placement.get("object_id")

        if "x" in placement or "y" in placement:
            x = placement.get("x")
            y = placement.get("y")
            description = "положение → X=%s, Y=%s" % (x, y)
            self._try_change(description, lambda: self.move_object(object_id, x, y), applied, errors)

        scale_percent = placement.get("scale_percent")
        if scale_percent is not None:
            description = "масштаб → %s%%" % scale_percent
            self._try_change(
                description,
                lambda: self.scale_object(object_id, percent=float(scale_percent)),
                applied,
                errors,
            )

        for axis in ("x", "y", "z"):
            degrees = placement.get("rotate_" + axis)
            if not degrees:
                continue
            description = "поворот вокруг %s на %s°" % (axis.upper(), degrees)
            self._try_change(
                description,
                lambda axis=axis, degrees=degrees: self.rotate_object(axis, float(degrees), object_id),
                applied,
                errors,
            )

    def _ensure_backend(self):
        backend = _app().getBackend()
        if backend is self._backend:
            return backend

        if self._backend is not None:
            self._backend.backendStateChange.disconnect(self._on_backend_state)
            self._backend.processingProgress.disconnect(self._on_progress)

        backend.backendStateChange.connect(self._on_backend_state)
        backend.processingProgress.connect(self._on_progress)
        self._backend = backend
        return backend

    def _on_backend_state(self, state):
        state_name = BACKEND_STATE_NAMES.get(state, str(state))
        Logger.log("d", "backend state: %s (raw %r, ours %s)", state_name, state, self._slice["state"])

        if self._slice["state"] == "idle" and state_name == "pending":
            return
        self._slice["state"] = state_name

        if state_name == "done":
            self._on_slicing_done()
        elif state_name in ("error", "disabled"):
            self._slice["message"] = "Слайсинг невозможен: проверьте настройки и положение модели"
            self._events.emit(
                "slice_state",
                state=state_name,
                message=self._slice["message"],
                validation_errors=self._validation_errors(),
            )
        elif state_name == "processing":
            self._events.emit("slice_state", state="processing")

    def _on_slicing_done(self):
        self._slice["progress"] = 1.0
        self._slice["finished"] = time.time()
        self._slice["message"] = ""
        try:
            result = self._result_dict()
        except Exception:
            Logger.logException("print information failed")
            result = {}
        self._events.emit("slice_state", state="done", result=result)

    def _on_progress(self, amount):
        amount = float(amount)
        self._slice["progress"] = amount

        progressed_enough = amount - self._last_progress_emit >= 0.05
        restarted = amount < self._last_progress_emit
        if progressed_enough or restarted or amount >= 1.0:
            self._last_progress_emit = amount
            self._events.emit("slice_progress", progress=round(amount, 3))

    def _start_slice(self):
        if not self._object_nodes():
            raise ToolError("Сцена пуста — нечего слайсить")

        backend = self._ensure_backend()
        self._slice["state"] = "processing"
        self._slice["progress"] = 0.0
        self._slice["started"] = time.time()
        self._slice["finished"] = None
        self._slice["message"] = ""
        self._last_progress_emit = 0.0

        self._events.emit("slice_state", state="processing")
        Logger.log("i", "start slicing via %s.forceSlice", type(backend).__name__)
        backend.forceSlice()

    def start_slicing(self, wait=True, timeout=SLICE_TIMEOUT):
        run_on_main(self._start_slice)
        if wait is False:
            return {"state": "processing"}

        deadline = time.time() + timeout
        pending_since = None
        restarts = 0

        while time.time() < deadline:
            time.sleep(0.5)
            state = self._slice["state"]

            if state == "done":
                result = run_on_main(self._result_dict)
                Logger.log("i", "slicing done: %s", result)
                return result

            if state in ("error", "disabled"):
                self._raise_slicing_error(state)

            if state != "pending":
                pending_since = None
                continue

            if pending_since is None:
                pending_since = time.time()
            if time.time() - pending_since > 5 and restarts < 2:
                restarts += 1
                pending_since = None
                Logger.log("w", "backend stays pending, restarting slice (%d)", restarts)
                run_on_main(self._start_slice)

        raise ToolError("Слайсинг не завершился за %d с" % timeout)

    def _raise_slicing_error(self, state):
        error_keys = run_on_main(self._validation_errors)
        Logger.log("w", "slicing failed: state=%s validation_errors=%s", state, error_keys)

        message = "Слайсинг не выполнен (%s). %s" % (state, self._slice.get("message", ""))
        if error_keys:
            message += " Ошибки в настройках: " + ", ".join(error_keys)
        raise ToolError(message)

    def get_slicing_status(self):
        status = dict(self._slice)
        if status.get("started"):
            finished_at = status.get("finished") or time.time()
            status["elapsed_s"] = round(finished_at - status["started"], 1)
        return status

    def _result_dict(self):
        print_information = _app().getPrintInformation()
        seconds = int(print_information.currentPrintTime)
        weights = [float(weight) for weight in (print_information.materialWeights or [])]
        lengths = [float(length) for length in (print_information.materialLengths or [])]

        return {
            "state": self._slice["state"],
            "print_time_s": seconds,
            "print_time": "%d ч %02d мин" % (seconds // 3600, (seconds % 3600) // 60),
            "material_g": round(sum(weights), 1),
            "material_m": round(sum(lengths), 2),
            "per_extruder": [{"g": round(weight, 1)} for weight in weights],
        }

    def get_slicing_result(self):
        if self._slice["state"] != "done":
            raise ToolError(
                "Нет готового результата (состояние: %s) — запустите start_slicing" % self._slice["state"]
            )
        return run_on_main(self._result_dict)

    def _output_path(self, path, model_name, filename):
        if path:
            path = os.path.abspath(os.path.expanduser(str(path)))
        else:
            output_dir = self._config.get("output_dir")
            if model_name:
                folder = os.path.join(output_dir, "results", _safe_name(model_name))
            else:
                folder = os.path.join(output_dir, "latest")
            path = os.path.join(folder, filename)

        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        return path

    def _get_gcode_writer(self):
        writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
        if writer is None:
            raise ToolError("Не найден GCodeWriter")
        return writer, [self._scene().getRoot()]

    def _get_project_writer(self):
        writer = _app().getWorkspaceFileHandler().getWriter("3MFWriter")
        if writer is None:
            raise ToolError("Не найден писатель проекта 3MF")
        return writer, [self._scene().getRoot()]

    def _run_writer(self, get_writer, path, binary, timeout):
        if threading.current_thread() is threading.main_thread():
            raise ToolError("Экспорт нельзя запускать из главного потока слайсера")

        writer, nodes = run_on_main(get_writer)
        writer_name = type(writer).__name__
        if binary:
            mode = MeshWriter.OutputMode.BinaryMode
        else:
            mode = MeshWriter.OutputMode.TextMode
        Logger.log("i", "writing %s with %s.%s", path, type(writer).__module__, writer_name)

        outcome = {}

        def write():
            try:
                if binary:
                    with open(path, "wb") as stream:
                        outcome["ok"] = writer.write(stream, nodes, mode)
                else:
                    with open(path, "w", encoding="utf-8", newline="") as stream:
                        outcome["ok"] = writer.write(stream, nodes, mode)
            except Exception as error:
                Logger.logException("writer %s failed" % writer_name)
                outcome["error"] = error

        worker = threading.Thread(target=write, name="AIAssistantWriter")
        worker.daemon = True
        started_at = time.time()
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            raise ToolError(
                "%s не завершился за %d с — проверьте, не открыт ли в слайсере диалог" % (writer_name, timeout)
            )
        if "error" in outcome:
            raise ToolError("%s: %s" % (writer_name, outcome["error"]))

        Logger.log("d", "%s returned %r in %.2fs", writer_name, outcome.get("ok"), time.time() - started_at)
        if outcome.get("ok") is False:
            information = writer.getInformation()
            details = ": " + information if information else ""
            raise ToolError("%s не смог записать файл%s" % (writer_name, details))

    def _write_atomic(self, get_writer, path, binary, timeout):
        temporary_path = path + ".part"
        try:
            self._run_writer(get_writer, temporary_path, binary, timeout)
        except Exception:
            if os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
            raise

        if os.path.getsize(temporary_path) == 0:
            os.remove(temporary_path)
            raise ToolError("Писатель создал пустой файл")

        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary_path, path)

    def export_gcode(self, path=None, model_name=None):
        if self._slice["state"] != "done":
            raise ToolError("Сначала выполните слайсинг (start_slicing)")

        path = self._output_path(path, model_name, "result.gcode")
        self._write_atomic(self._get_gcode_writer, path, False, WRITE_TIMEOUT)

        check = check_gcode(path)
        level = "i" if check["valid"] else "w"
        Logger.log(level, "G-code check %s: %s", path, check)
        self._events.emit("file_saved", kind="gcode", path=path, check=check)
        return {"path": path, "check": check}

    def save_project_3mf(self, path=None, model_name=None):
        path = self._output_path(path, model_name, "project.3mf")
        self._write_atomic(self._get_project_writer, path, True, WRITE_TIMEOUT)

        self._events.emit("file_saved", kind="3mf", path=path)
        return {"path": path, "size_kb": round(os.path.getsize(path) / 1024.0, 1)}

    def export_results(self, model_name):
        folder = os.path.dirname(self._output_path(None, model_name, "result.gcode"))
        result = {"folder": folder, "errors": []}

        exports = (
            ("gcode", self.export_gcode),
            ("project", self.save_project_3mf),
        )
        for key, export in exports:
            try:
                result[key] = export(model_name=model_name)
            except Exception as error:
                Logger.log("w", "export_results: %s failed: %s", key, error)
                result["errors"].append("%s: %s" % (key, error))

        if "gcode" not in result and "project" not in result:
            raise ToolError("Не удалось сохранить ни G-code, ни 3MF: %s" % "; ".join(result["errors"]))
        return result
