# -*- coding: utf-8 -*-
"""Обёртка над API STE Slicer (форк Cura 3.6).

Правило: всё, что трогает сцену, настройки и бэкенд, выполняется в главном потоке Qt
(декоратор @on_main или run_on_main). Длительные операции (загрузка, слайсинг) запускаются
в главном потоке, а ожидание идёт в потоке агента — чтобы интерфейс слайсера не зависал.

Вызовы сверены с исходниками STE-Slicer-master и Uranium-steslicer.
"""
import base64
import math
import os
import re
import threading
import time

from . import geometry
from .log import log, log_exception, short
from .mainthread import on_main, run_on_main
from .tools import ToolError

PRINT_MODES = ("classic", "cylindrical", "cylindrical_full")
# Ключ режима в STE Slicer (fdmprinter.def.json, глобальная настройка). В определении есть и другие
# режимы (discrete, spherical…), но по условиям кейса используются только три выше.
PRINT_MODE_KEY = "printing_mode"
MODE_LABELS = {"classic": "3D (classic)", "cylindrical": "5D Spiral (cylindrical)",
               "cylindrical_full": "5D (cylindrical_full)"}
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")

# Оси стола → оси сцены Cura (сцена Y-up, передняя сторона стола — +Z сцены)
PLATE_AXES_IN_SCENE = {"x": (1.0, 0.0, 0.0), "y": (0.0, 0.0, -1.0), "z": (0.0, 1.0, 0.0)}


# ---------------------------------------------------------------------------
# Доступ к приложению
# ---------------------------------------------------------------------------

def _app():
    try:
        from steslicer.SteSlicerApplication import SteSlicerApplication
        return SteSlicerApplication.getInstance()
    except ImportError:
        from UM.Application import Application
        return Application.getInstance()


def _extruder_manager():
    try:
        from steslicer.Settings.ExtruderManager import ExtruderManager
    except ImportError:
        from cura.Settings.ExtruderManager import ExtruderManager
    return ExtruderManager.getInstance()


def _jsonable(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else round(value, 4)
    if isinstance(value, dict):
        return dict((str(k), _jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


def _r(value, digits=2):
    return round(float(value), digits)


def _safe_name(name):
    name = re.sub(r"[^\w\-. ]+", "_", name or "model", flags=re.UNICODE).strip(" .")
    return name or "model"


def _metadata(node):
    md = getattr(node, "metadata", None)
    if isinstance(md, dict):
        return md
    getter = getattr(node, "getMetaData", None)
    if callable(getter):
        return getter() or {}
    return {}


def check_gcode(path):
    """Быстрая проверка корректности G-code."""
    stats = {"size_kb": round(os.path.getsize(path) / 1024.0, 1), "lines": 0, "moves": 0,
             "layers": 0, "max_nozzle_temp": None, "max_bed_temp": None, "flavor": None}
    temp_re = re.compile(r"\bS(\d+(?:\.\d+)?)")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stats["lines"] += 1
            if line.startswith(("G0", "G1", "G2", "G3")):
                stats["moves"] += 1
            elif line.startswith(";LAYER:"):
                stats["layers"] += 1
            elif line.startswith(("M104", "M109", "M140", "M190")):
                m = temp_re.search(line)
                if m:
                    key = "max_bed_temp" if line.startswith(("M140", "M190")) else "max_nozzle_temp"
                    value = float(m.group(1))
                    stats[key] = value if stats[key] is None else max(stats[key], value)
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


class SlicerAPI(object):
    def __init__(self, events, config):
        self._events = events
        self._config = config
        self._ids = {}
        self._next_id = 1
        self._suggestions = {}
        self._mode_key = None
        self._backend = None
        self._slice = {"state": "idle", "progress": 0.0, "message": "", "started": None, "finished": None}
        self._last_progress_emit = 0.0

    # ------------------------------------------------------------------
    # Помощники (только главный поток)
    # ------------------------------------------------------------------
    def _global_stack(self):
        stack = _app().getGlobalContainerStack()
        if stack is None:
            raise ToolError("Не выбран принтер — добавьте принтер в STE Slicer")
        return stack

    def _extruder_stack(self):
        try:
            return _extruder_manager().getActiveExtruderStack()
        except Exception:
            return None

    def _stack_for(self, key):
        stack = self._global_stack()
        if stack.getProperty(key, "settable_per_extruder"):
            extruder = self._extruder_stack()
            if extruder is not None:
                return extruder, "extruder"
        return stack, "global"

    def _require_key(self, key):
        if self._global_stack().getProperty(key, "type") is None:
            raise ToolError("Неизвестная настройка '%s' — найдите ключ через search_settings" % key)

    def _describe(self, key):
        self._require_key(key)
        g = self._global_stack()
        stack, scope = self._stack_for(key)
        setting_type = g.getProperty(key, "type")
        info = {"key": key, "label": g.getProperty(key, "label"), "type": setting_type,
                "value": _jsonable(stack.getProperty(key, "value")),
                "unit": g.getProperty(key, "unit") or "", "scope": scope,
                "user_changed": stack.userChanges.getProperty(key, "value") is not None}
        if setting_type == "enum":
            info["options"] = _jsonable(g.getProperty(key, "options"))
        for prop, name in (("minimum_value", "min"), ("maximum_value", "max")):
            bound = stack.getProperty(key, prop)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                info[name] = bound
        return info

    def _convert(self, key, value):
        g = self._global_stack()
        stack, _ = self._stack_for(key)
        setting_type = g.getProperty(key, "type")
        try:
            if setting_type == "bool":
                if isinstance(value, str):
                    value = value.strip().lower() in ("true", "1", "yes", "on", "да")
                else:
                    value = bool(value)
            elif setting_type == "int":
                value = int(round(float(value)))
            elif setting_type == "float":
                value = float(value)
            elif setting_type == "enum":
                options = g.getProperty(key, "options") or {}
                value = str(value)
                if value not in options:
                    match = [k for k, label in options.items() if str(label).lower() == value.lower()]
                    if not match:
                        raise ToolError("Недопустимое значение '%s' для %s. Варианты: %s"
                                        % (value, key, ", ".join(options.keys())))
                    value = match[0]
            elif setting_type in ("str", "extruder", "optional_extruder"):
                value = str(value)
        except (TypeError, ValueError):
            raise ToolError("Значение '%s' не подходит для %s (тип %s)" % (value, key, setting_type))
        if setting_type in ("int", "float"):
            low = stack.getProperty(key, "minimum_value")
            high = stack.getProperty(key, "maximum_value")
            if isinstance(low, (int, float)) and value < low:
                raise ToolError("%s = %s меньше минимума %s" % (key, value, low))
            if isinstance(high, (int, float)) and value > high:
                raise ToolError("%s = %s больше максимума %s" % (key, value, high))
        return value

    def _apply_changes(self, changes):
        applied, errors = [], []
        for key, value in changes.items():
            try:
                self._require_key(key)
                new_value = self._convert(key, value)
                stack, scope = self._stack_for(key)
                old_value = stack.getProperty(key, "value")
                stack.userChanges.setProperty(key, "value", new_value)
                actual = stack.getProperty(key, "value")
                applied.append({"key": key, "label": self._global_stack().getProperty(key, "label"),
                                "old": _jsonable(old_value), "new": _jsonable(actual), "scope": scope})
                log("d", "setting %s [%s]: %r -> %r (requested %r)", key, scope, old_value, actual, value)
            except ToolError as e:
                log("w", "setting %s rejected: %s", key, e)
                errors.append({"key": key, "error": str(e)})
        return applied, errors

    def _validation_errors(self):
        """Ключи настроек с ошибками валидации (ContainerStack.getErrorKeys) — глобальные и экструдера."""
        keys = []
        for stack in (self._global_stack(), self._extruder_stack()):
            getter = getattr(stack, "getErrorKeys", None) if stack is not None else None
            if callable(getter):
                try:
                    keys.extend(k for k in getter() if k not in keys)
                except Exception:
                    log_exception("getErrorKeys failed")
        return keys

    # ------------------------------------------------------------------
    # Состояние и настройки
    # ------------------------------------------------------------------
    @on_main
    def get_state(self):
        g = self._global_stack()
        state = {"printer": g.getName(), "print_mode": None, "quality": None, "material": None}
        try:
            state["print_mode"] = self._mode_info()
        except ToolError as e:
            state["print_mode"] = {"error": str(e)}
        state["quality"] = self._active_quality()
        state["material"] = self._active_material()
        state["objects"] = [self._node_info(n) for n in self._object_nodes()]
        state["slicing"] = dict(self._slice)
        state["build_volume_mm"] = self._build_volume()
        return state

    @on_main
    def get_settings(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        result, errors = [], []
        for key in keys:
            try:
                result.append(self._describe(key))
            except ToolError as e:
                errors.append({"key": key, "error": str(e)})
        return {"settings": result, "errors": errors}

    @on_main
    def search_settings(self, query, limit=25):
        g = self._global_stack()
        words = [w for w in str(query).lower().split() if w]
        found = []
        for key in sorted(g.getAllKeys()):
            setting_type = g.getProperty(key, "type")
            if setting_type in (None, "category"):
                continue
            label = str(g.getProperty(key, "label") or "")
            haystack = (key + " " + label).lower()
            if all(w in haystack for w in words):
                stack, scope = self._stack_for(key)
                found.append({"key": key, "label": label, "type": setting_type,
                              "value": _jsonable(stack.getProperty(key, "value")),
                              "unit": g.getProperty(key, "unit") or "", "scope": scope})
            if len(found) >= int(limit or 25):
                break
        return {"query": query, "results": found}

    @on_main
    def set_settings(self, changes, reason=""):
        if not isinstance(changes, dict) or not changes:
            raise ToolError("changes должен быть непустым объектом ключ → значение")
        applied, errors = self._apply_changes(changes)
        if applied:
            self._events.emit("settings_changed", changes=applied, reason=reason)
        return {"applied": applied, "errors": errors, "validation_errors": self._validation_errors()}

    @on_main
    def reset_settings(self, keys=None):
        stacks = [self._global_stack()]
        extruder = self._extruder_stack()
        if extruder is not None:
            stacks.append(extruder)
        for stack in stacks:
            if keys:
                for key in keys:
                    stack.userChanges.removeInstance(key)
            else:
                stack.userChanges.clear()
        self._events.emit("settings_changed", changes=[], reason="Сброс настроек: " + (", ".join(keys) if keys else "все"))
        return {"reset": keys or "all"}

    def _active_quality(self):
        mm = _app().getMachineManager()
        info = {}
        for attr, name in (("activeQualityType", "quality_type"),
                           ("activeQualityOrQualityChangesName", "name")):
            try:
                info[name] = getattr(mm, attr)
            except Exception:
                pass
        return info

    def _active_material(self):
        extruder = self._extruder_stack()
        if extruder is None:
            return None
        material = extruder.material
        return {"id": material.getMetaDataEntry("base_file") or material.getId(),
                "name": material.getName(), "type": material.getMetaDataEntry("material"),
                "brand": material.getMetaDataEntry("brand")}

    @on_main
    def list_quality_profiles(self):
        g = self._global_stack()
        app = _app()
        result = {"active": self._active_quality(), "quality": [], "custom": []}
        qm = app.getQualityManager()
        groups = qm.getQualityGroups(g) or {}
        for quality_type in sorted(groups.keys()):
            group = groups[quality_type]
            result["quality"].append({"quality_type": quality_type, "name": group.name,
                                      "available": bool(getattr(group, "is_available", True))})
        try:
            for group in qm.getQualityChangesGroups(g) or []:
                result["custom"].append({"name": group.name})
        except Exception:
            log_exception("getQualityChangesGroups failed")
        return result

    @on_main
    def set_quality_profile(self, name):
        g = self._global_stack()
        app = _app()
        qm, mm = app.getQualityManager(), app.getMachineManager()
        groups = qm.getQualityGroups(g) or {}
        target = None
        for quality_type, group in groups.items():
            if name in (quality_type, group.name):
                target = quality_type
        if target is not None:
            if not getattr(groups[target], "is_available", True):
                raise ToolError("Профиль %s недоступен для текущего материала/сопла" % name)
            mm.setQualityGroupByQualityType(target)
        else:
            custom = [grp for grp in (qm.getQualityChangesGroups(g) or []) if grp.name == name]
            if not custom:
                raise ToolError("Профиль '%s' не найден. Доступные: %s" % (name, ", ".join(sorted(groups.keys()))))
            mm.setQualityChangesGroup(custom[0])
        active = self._active_quality()
        self._events.emit("profile_changed", what="quality", value=active)
        return {"active": active}

    @on_main
    def list_materials(self, query=None):
        g = self._global_stack()
        extruder = self._extruder_stack()
        nodes = _app().getMaterialManager().getAvailableMaterialsForMachineExtruder(g, extruder) or {}
        q = (query or "").lower()
        items = []
        for material_id, node in nodes.items():
            md = _metadata(node)
            item = {"id": material_id, "name": md.get("name"), "type": md.get("material"),
                    "brand": md.get("brand"), "color": md.get("color_name")}
            if q and q not in " ".join(str(v) for v in item.values()).lower():
                continue
            items.append(item)
        items.sort(key=lambda i: (str(i["type"]), str(i["brand"]), str(i["name"])))
        return {"active": self._active_material(), "materials": items[:150], "total": len(items)}

    @on_main
    def set_material(self, material_id):
        extruder = self._extruder_stack()
        if extruder is None:
            raise ToolError("Нет активного экструдера")
        g = self._global_stack()
        available = _app().getMaterialManager().getAvailableMaterialsForMachineExtruder(g, extruder) or {}
        if material_id not in available:
            raise ToolError("Материал '%s' не найден — возьмите id из list_materials" % material_id)
        position = str(extruder.getMetaDataEntry("position") or "0")
        _app().getMachineManager().setMaterialById(position, material_id)
        active = self._active_material()
        self._events.emit("profile_changed", what="material", value=active)
        return {"active": active}

    @on_main
    def save_profile(self, name):
        qm = _app().getQualityManager()
        qm.createQualityChanges(name)  # QualityManager.createQualityChanges(base_name: str)
        return {"saved": name, "hint": "Профиль можно выбрать через set_quality_profile"}

    # --- режим 3D / 5D ------------------------------------------------------
    def _find_mode_key(self):
        if self._mode_key:
            return self._mode_key
        g = self._global_stack()
        if g.getProperty(PRINT_MODE_KEY, "type") == "enum":
            self._mode_key = PRINT_MODE_KEY
            return PRINT_MODE_KEY
        for key in sorted(g.getAllKeys()):
            if g.getProperty(key, "type") != "enum":
                continue
            options = g.getProperty(key, "options") or {}
            if "classic" in options and ("cylindrical" in options or "cylindrical_full" in options):
                self._mode_key = key
                log("i", "print mode key: %s", key)
                return key
        raise ToolError("Не найдена настройка режима печати (classic/cylindrical) — выберите принтер Stereotech")

    def _mode_info(self):
        key = self._find_mode_key()
        stack, scope = self._stack_for(key)
        value = stack.getProperty(key, "value")
        return {"key": key, "mode": value, "label": MODE_LABELS.get(value, value), "scope": scope,
                "options": [o for o in PRINT_MODES if o in (self._global_stack().getProperty(key, "options") or {})]}

    @on_main
    def get_print_mode(self):
        return self._mode_info()

    @on_main
    def set_print_mode(self, mode, reason=""):
        if mode not in PRINT_MODES:
            raise ToolError("Режим должен быть одним из: %s" % ", ".join(PRINT_MODES))
        key = self._find_mode_key()
        applied, errors = self._apply_changes({key: mode})
        if errors:
            raise ToolError(errors[0]["error"])
        self._events.emit("settings_changed", changes=applied, reason=reason or ("Режим печати: " + MODE_LABELS[mode]))
        return self._mode_info()

    # ------------------------------------------------------------------
    # Сцена
    # ------------------------------------------------------------------
    def _scene(self):
        return _app().getController().getScene()

    def _object_nodes(self):
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
        root = self._scene().getRoot()
        nodes = []
        for node in DepthFirstIterator(root):
            if node is root:
                continue
            if not (node.callDecoration("isSliceable") or node.callDecoration("isGroup")):
                continue
            parent = node.getParent()
            if parent is not None and parent.callDecoration("isGroup"):
                continue
            nodes.append(node)
        return nodes

    def _oid(self, node):
        key = id(node)
        if key not in self._ids:
            self._ids[key] = "obj%d" % self._next_id
            self._next_id += 1
        return self._ids[key]

    def _find(self, object_id=None):
        nodes = self._object_nodes()
        if not nodes:
            raise ToolError("Сцена пуста — сначала загрузите модель (load_model)")
        if not object_id or object_id == "auto":
            if len(nodes) == 1:
                return nodes[0]
            raise ToolError("В сцене несколько объектов, укажите object_id: %s"
                            % ", ".join(self._oid(n) for n in nodes))
        for node in nodes:
            if object_id in (self._oid(node), node.getName()):
                return node
        raise ToolError("Объект '%s' не найден. Есть: %s" % (object_id, ", ".join(self._oid(n) for n in nodes)))

    def _node_info(self, node):
        bb = node.getBoundingBox()
        scale = node.getScale()
        info = {"object_id": self._oid(node), "name": node.getName(),
                "size_mm": {"x": _r(bb.width), "y": _r(bb.depth), "z": _r(bb.height)},
                "center_mm": {"x": _r(bb.center.x), "y": _r(-bb.center.z)},
                "bottom_z_mm": _r(bb.bottom), "on_plate": abs(bb.bottom) < 0.05,
                "scale_pct": {"x": _r(scale.x * 100), "y": _r(scale.z * 100), "z": _r(scale.y * 100)}}
        outside = getattr(node, "isOutsideBuildArea", None)
        if callable(outside):
            info["outside_build_area"] = bool(outside())
        return info

    def _build_volume(self):
        g = self._global_stack()
        return {"x": g.getProperty("machine_width", "value"), "y": g.getProperty("machine_depth", "value"),
                "z": g.getProperty("machine_height", "value")}

    def _drop_to_plate(self, node):
        """Опустить на стол штатной GravityOperation: она учитывает getZOffset узла (как в интерфейсе слайсера)."""
        from UM.Operations.GravityOperation import GravityOperation
        GravityOperation(node).push()

    def _settle(self, node, center_x, center_z):
        """Вернуть центр объекта на место по X/Y стола и опустить его на стол."""
        from UM.Math.Vector import Vector
        from UM.Operations.TranslateOperation import TranslateOperation
        bb = node.getBoundingBox()
        delta = Vector(center_x - bb.center.x, 0.0, center_z - bb.center.z)
        if delta.length() > 1e-4:
            TranslateOperation(node, delta).push()
        self._drop_to_plate(node)

    def _rotate(self, node, scene_axis, angle_rad):
        from UM.Math.Quaternion import Quaternion
        from UM.Math.Vector import Vector
        from UM.Operations.RotateOperation import RotateOperation
        bb = node.getBoundingBox()
        cx, cz = bb.center.x, bb.center.z
        if abs(angle_rad) > 1e-9:
            # RotateOperation по умолчанию вращает вокруг начала координат — вращаем вокруг центра модели
            RotateOperation(node, Quaternion.fromAngleAxis(angle_rad, Vector(*scene_axis)),
                            rotate_around_point=bb.center).push()
        self._settle(node, cx, cz)
        self._suggestions.pop(self._oid(node), None)

    def _node_keys(self):
        return [id(n) for n in self._object_nodes()]

    def _infos_for_keys(self, keys):
        return [self._node_info(n) for n in self._object_nodes() if id(n) in keys]

    def _read_file(self, path):
        from PyQt5.QtCore import QUrl
        _app().readLocalFile(QUrl.fromLocalFile(path))

    def load_model(self, path):
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ToolError("Файл не найден: %s" % path)
        if not path.lower().endswith(MODEL_EXTENSIONS):
            raise ToolError("Поддерживаются только %s" % ", ".join(MODEL_EXTENSIONS))
        before = set(run_on_main(self._node_keys))
        log("i", "loading model %s (%d objects in scene)", path, len(before))
        run_on_main(self._read_file, path)
        deadline = time.time() + 120
        new_keys, stable_since, last = [], None, None
        while time.time() < deadline:
            time.sleep(0.4)
            new_keys = [k for k in run_on_main(self._node_keys) if k not in before]
            if new_keys and new_keys == last:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > 0.8:
                    break
            else:
                stable_since = None
            last = new_keys
        if not new_keys:
            log("w", "model %s did not appear in scene", path)
            raise ToolError("Модель не появилась в сцене за 120 с (проверьте файл и диалоги слайсера)")
        objects = run_on_main(self._infos_for_keys, set(new_keys))
        log("i", "model loaded: %s", short(objects))
        self._events.emit("scene_changed", action="load", objects=objects)
        return {"loaded": objects, "file": path}

    @on_main
    def list_objects(self):
        return {"objects": [self._node_info(n) for n in self._object_nodes()],
                "build_volume_mm": self._build_volume(),
                "coordinates": "x,y — центр объекта на столе, (0,0) — центр стола; z — вверх"}

    @on_main
    def get_object_dimensions(self, object_id=None):
        return self._node_info(self._find(object_id))

    @on_main
    def move_object(self, object_id=None, x=None, y=None, relative=False):
        from UM.Math.Vector import Vector
        from UM.Operations.TranslateOperation import TranslateOperation
        node = self._find(object_id)
        bb = node.getBoundingBox()
        cx, cy = bb.center.x, -bb.center.z
        if relative:
            tx, ty = cx + float(x or 0), cy + float(y or 0)
        else:
            tx = cx if x is None else float(x)
            ty = cy if y is None else float(y)
        TranslateOperation(node, Vector(tx - cx, 0.0, -(ty - cy))).push()
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
        from UM.Math.Vector import Vector
        from UM.Operations.ScaleOperation import ScaleOperation
        node = self._find(object_id)
        if percent is None and x is None and y is None and z is None:
            raise ToolError("Укажите percent или x/y/z")
        current = node.getScale()
        sx = (x if x is not None else percent if percent is not None else current.x * 100) / 100.0
        sy = (y if y is not None else percent if percent is not None else current.z * 100) / 100.0
        sz = (z if z is not None else percent if percent is not None else current.y * 100) / 100.0
        if min(sx, sy, sz) <= 0:
            raise ToolError("Масштаб должен быть больше 0")
        bb = node.getBoundingBox()
        cx, cz = bb.center.x, bb.center.z
        ScaleOperation(node, Vector(sx, sz, sy), set_scale=True, scale_around_point=bb.center).push()
        self._settle(node, cx, cz)
        self._suggestions.pop(self._oid(node), None)
        info = self._node_info(node)
        self._events.emit("scene_changed", action="scale", objects=[info])
        return info

    def arrange_all(self):
        run_on_main(lambda: _app().arrangeAll())
        time.sleep(1.5)  # расстановка идёт фоновой задачей слайсера
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
        from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
        node = self._find(object_id)
        oid = self._oid(node)
        RemoveSceneNodeOperation(node).push()
        self._suggestions.pop(oid, None)
        self._events.emit("scene_changed", action="delete", objects=[])
        return {"deleted": oid}

    # ------------------------------------------------------------------
    # Анализ геометрии (данные берём в главном потоке, считаем в потоке агента)
    # ------------------------------------------------------------------
    def _plate_triangles(self, node):
        import numpy
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
        chunks = []
        for child in DepthFirstIterator(node):
            mesh = child.getMeshData()
            if mesh is None:
                continue
            vertices = mesh.getVertices()
            if vertices is None or len(vertices) == 0:
                continue
            indices = mesh.getIndices()
            if indices is not None:
                tris = vertices[indices]
            elif len(vertices) % 3 == 0:
                tris = vertices.reshape(-1, 3, 3)
            else:
                continue
            matrix = child.getWorldTransformation().getData()
            flat = tris.reshape(-1, 3).astype(numpy.float64)
            world = flat.dot(matrix[:3, :3].T) + matrix[:3, 3]
            chunks.append(world.reshape(-1, 3, 3))
        if not chunks:
            raise ToolError("У объекта нет сетки")
        return geometry.scene_to_plate(numpy.concatenate(chunks))

    def _prepare_geometry(self, object_id, support_angle):
        node = self._find(object_id)
        if support_angle is None:
            stack, _ = self._stack_for("support_angle")
            support_angle = stack.getProperty("support_angle", "value") or 50.0
        return self._oid(node), self._plate_triangles(node), float(support_angle)

    def analyze_geometry(self, object_id=None, support_angle=None):
        oid, tris, angle = run_on_main(self._prepare_geometry, object_id, support_angle)
        result = geometry.analyze(tris, angle)
        result["object_id"] = oid
        return result

    def suggest_orientations(self, object_id=None, support_angle=None, top_n=5):
        oid, tris, angle = run_on_main(self._prepare_geometry, object_id, support_angle)
        result = geometry.suggest_orientations(tris, angle, top_n or 5)
        result["object_id"] = oid
        self._suggestions[oid] = result["candidates"]
        return result

    @on_main
    def apply_orientation(self, object_id=None, rank=None, down=None):
        node = self._find(object_id)
        oid = self._oid(node)
        if down is None:
            candidates = self._suggestions.get(oid)
            if not candidates:
                raise ToolError("Сначала вызовите suggest_orientations для %s" % oid)
            rank = int(rank or 1)
            chosen = [c for c in candidates if c["rank"] == rank]
            if not chosen:
                raise ToolError("Нет варианта с rank=%d" % rank)
            down = chosen[0]["down"]
        if len(down) != 3:
            raise ToolError("down — вектор из трёх чисел")
        axis, angle = geometry.rotation_to_down(down)
        self._rotate(node, geometry.plate_to_scene_vector(axis), math.radians(angle))
        info = self._node_info(node)
        self._events.emit("scene_changed", action="orient", objects=[info])
        return {"object": info, "rotation": {"axis_plate": [round(a, 4) for a in axis], "angle_deg": round(angle, 1)}}

    # ------------------------------------------------------------------
    # Снимок сцены для vision-LLM (steslicer.Snapshot — тот же рендер, что превью в G-code)
    # ------------------------------------------------------------------
    def _snapshot_png(self, size):
        from PyQt5.QtCore import QBuffer
        from steslicer.Snapshot import Snapshot
        image = Snapshot.snapshot(width=size, height=size)  # OpenGL — только главный поток
        if image is None:
            raise ToolError("В сцене нет видимых моделей для снимка")
        buffer = QBuffer()
        buffer.open(QBuffer.ReadWrite)
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
        buffer.close()
        return data

    def get_scene_snapshot(self, size=512):
        size = max(128, min(int(size or 512), 1024))
        png = run_on_main(self._snapshot_png, size)
        url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        log("d", "scene snapshot %dx%d, %d bytes", size, size, len(png))
        self._events.emit("snapshot", image=url, caption="Снимок сцены")
        return {"image_attached": True, "size_px": size,
                "view": "изометрия: камера спереди-слева-сверху, смотрит на центр моделей; стол — внизу",
                "_image": url}

    # ------------------------------------------------------------------
    # Слайсинг
    # ------------------------------------------------------------------
    def _ensure_backend(self):
        backend = _app().getBackend()
        if backend is not self._backend:
            if self._backend is not None:
                try:
                    self._backend.backendStateChange.disconnect(self._on_backend_state)
                    self._backend.processingProgress.disconnect(self._on_progress)
                except Exception:
                    pass
            backend.backendStateChange.connect(self._on_backend_state)
            backend.processingProgress.connect(self._on_progress)
            self._backend = backend
        return backend

    @staticmethod
    def _state_name(state):
        try:
            from UM.Backend.Backend import BackendState
            names = {BackendState.NotStarted: "pending", BackendState.Processing: "processing",
                     BackendState.Done: "done", BackendState.Error: "error", BackendState.Disabled: "disabled"}
            if state in names:
                return names[state]
            return names.get(BackendState(state), str(state))
        except Exception:
            return str(state)

    def _on_backend_state(self, state):
        name = self._state_name(state)
        log("d", "backend state: %s (raw %r, ours %s)", name, state, self._slice["state"])
        if self._slice["state"] == "idle" and name == "pending":
            return
        self._slice["state"] = name
        if name == "done":
            self._slice.update(progress=1.0, finished=time.time(), message="")
            try:
                result = self._result_dict()
            except Exception:
                log_exception("print information failed")
                result = {}
            self._events.emit("slice_state", state="done", result=result)
        elif name in ("error", "disabled"):
            self._slice["message"] = "Слайсинг невозможен: проверьте настройки и положение модели"
            self._events.emit("slice_state", state=name, message=self._slice["message"],
                              validation_errors=self._validation_errors())
        elif name == "processing":
            self._events.emit("slice_state", state="processing")

    def _on_progress(self, amount):
        amount = float(amount)
        self._slice["progress"] = amount
        if amount - self._last_progress_emit >= 0.05 or amount >= 1.0 or amount < self._last_progress_emit:
            self._last_progress_emit = amount
            self._events.emit("slice_progress", progress=round(amount, 3))

    def _start_slice(self):
        if not self._object_nodes():
            raise ToolError("Сцена пуста — нечего слайсить")
        backend = self._ensure_backend()
        self._slice.update(state="processing", progress=0.0, started=time.time(), finished=None, message="")
        self._last_progress_emit = 0.0
        self._events.emit("slice_state", state="processing")
        log("i", "start slicing via %s.%s", type(backend).__name__,
            "forceSlice" if hasattr(backend, "forceSlice") else "slice")
        if hasattr(backend, "forceSlice"):
            backend.forceSlice()
        else:
            backend.slice()

    def start_slicing(self, wait=True, timeout=900):
        run_on_main(self._start_slice)
        if wait is False:
            return {"state": "processing"}
        deadline = time.time() + timeout
        pending_since, restarts = None, 0
        while time.time() < deadline:
            time.sleep(0.5)
            state = self._slice["state"]
            if state == "done":
                result = run_on_main(self._result_dict)
                log("i", "slicing done: %s", short(result))
                return result
            if state in ("error", "disabled"):
                errors = run_on_main(self._validation_errors)
                log("w", "slicing failed: state=%s validation_errors=%s", state, errors)
                raise ToolError("Слайсинг не выполнен (%s). %s%s" % (
                    state, self._slice.get("message", ""),
                    (" Ошибки в настройках: " + ", ".join(errors)) if errors else ""))
            if state == "pending":
                pending_since = pending_since or time.time()
                if time.time() - pending_since > 5 and restarts < 2:
                    restarts += 1
                    pending_since = None
                    log("w", "backend stays pending, restarting slice (%d)", restarts)
                    run_on_main(self._start_slice)
            else:
                pending_since = None
        raise ToolError("Слайсинг не завершился за %d с" % timeout)

    def get_slicing_status(self):
        status = dict(self._slice)
        if status.get("started"):
            end = status.get("finished") or time.time()
            status["elapsed_s"] = round(end - status["started"], 1)
        return status

    def _result_dict(self):
        info = _app().getPrintInformation()
        seconds = None
        duration = getattr(info, "currentPrintTime", None)
        if duration is not None:
            try:
                seconds = int(duration)
            except (TypeError, ValueError):
                try:
                    seconds = ((getattr(duration, "days", 0) * 24 + getattr(duration, "hours", 0)) * 60
                               + getattr(duration, "minutes", 0)) * 60 + getattr(duration, "seconds", 0)
                except Exception:
                    seconds = None
        weights = [float(w) for w in (getattr(info, "materialWeights", None) or [])]
        lengths = [float(l) for l in (getattr(info, "materialLengths", None) or [])]
        result = {"state": self._slice["state"], "print_time_s": seconds,
                  "material_g": round(sum(weights), 1), "material_m": round(sum(lengths), 2),
                  "per_extruder": [{"g": round(w, 1)} for w in weights]}
        if seconds is not None:
            result["print_time"] = "%d ч %02d мин" % (seconds // 3600, (seconds % 3600) // 60)
        return result

    def get_slicing_result(self):
        if self._slice["state"] != "done":
            raise ToolError("Нет готового результата (состояние: %s) — запустите start_slicing" % self._slice["state"])
        return run_on_main(self._result_dict)

    # ------------------------------------------------------------------
    # Экспорт
    # ------------------------------------------------------------------
    def _output_path(self, path, model_name, filename):
        if path:
            path = os.path.abspath(os.path.expanduser(str(path)))
        else:
            folder = os.path.join(self._config.get("output_dir"), "results", _safe_name(model_name)) if model_name \
                else os.path.join(self._config.get("output_dir"), "latest")
            path = os.path.join(folder, filename)
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        return path

    def _get_gcode_writer(self):
        from UM.PluginRegistry import PluginRegistry
        writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
        if writer is None:
            writer = _app().getMeshFileHandler().getWriterByMimeType("text/x-gcode")
        if writer is None:
            raise ToolError("Не найден GCodeWriter")
        return writer, [self._scene().getRoot()]

    def _get_project_writer(self):
        # Писатель проекта зарегистрирован плагином 3MFWriter как workspace_writer
        writer = _app().getWorkspaceFileHandler().getWriter("3MFWriter")
        if writer is None:
            raise ToolError("Не найден писатель проекта 3MF")
        return writer, [self._scene().getRoot()]

    def _run_writer(self, get_writer, path, binary, timeout):
        """Запускает писатель STE Slicer в отдельном фоновом потоке.

        Писатели STE сами переносят работу в главный поток через @call_on_qt_thread (3MF целиком,
        GCodeWriter — снимок-превью). Этот декоратор всегда делает callLater и ждёт результата,
        поэтому вызов писателя ИЗ главного потока зависает навсегда. Если внутри писателя случится
        исключение, декоратор тоже никогда не вернётся — поэтому отдельный поток и таймаут.
        """
        from UM.Mesh.MeshWriter import MeshWriter
        if threading.current_thread() is threading.main_thread():
            raise ToolError("Экспорт нельзя запускать из главного потока слайсера")
        writer, nodes = run_on_main(get_writer)
        mode = MeshWriter.OutputMode.BinaryMode if binary else MeshWriter.OutputMode.TextMode
        log("i", "writing %s with %s.%s", path, type(writer).__module__, type(writer).__name__)
        box = {}

        def work():
            try:
                if binary:
                    with open(path, "wb") as stream:
                        box["ok"] = writer.write(stream, nodes, mode)
                else:
                    with open(path, "w", encoding="utf-8", newline="") as stream:
                        box["ok"] = writer.write(stream, nodes, mode)
            except Exception as e:
                log_exception("writer %s failed" % type(writer).__name__)
                box["error"] = e

        worker = threading.Thread(target=work, name="AIAssistantWriter")
        worker.daemon = True
        started = time.time()
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise ToolError("%s не завершился за %d с — проверьте, не открыт ли в слайсере диалог"
                            % (type(writer).__name__, timeout))
        if "error" in box:
            raise ToolError("%s: %s" % (type(writer).__name__, box["error"]))
        log("d", "%s returned %r in %.2fs", type(writer).__name__, box.get("ok"), time.time() - started)
        if box.get("ok") is False:
            info = getattr(writer, "getInformation", lambda: "")()
            raise ToolError("%s не смог записать файл%s" % (type(writer).__name__, (": " + info) if info else ""))

    def _write_atomic(self, get_writer, path, binary, timeout):
        """Пишем во временный файл и переименовываем только при успехе — пустых файлов не остаётся."""
        tmp = path + ".part"
        try:
            self._run_writer(get_writer, tmp, binary, timeout)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass  # при таймауте файл ещё может быть открыт писателем
            raise
        if os.path.getsize(tmp) == 0:
            os.remove(tmp)
            raise ToolError("Писатель создал пустой файл")
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp, path)

    def export_gcode(self, path=None, model_name=None):
        if self._slice["state"] != "done":
            raise ToolError("Сначала выполните слайсинг (start_slicing)")
        path = self._output_path(path, model_name, "result.gcode")
        self._write_atomic(self._get_gcode_writer, path, False, 120)
        check = check_gcode(path)
        log("i" if check["valid"] else "w", "G-code check %s: %s", path, short(check))
        self._events.emit("file_saved", kind="gcode", path=path, check=check)
        return {"path": path, "check": check}

    def save_project_3mf(self, path=None, model_name=None):
        path = self._output_path(path, model_name, "project.3mf")
        self._write_atomic(self._get_project_writer, path, True, 120)
        self._events.emit("file_saved", kind="3mf", path=path)
        return {"path": path, "size_kb": round(os.path.getsize(path) / 1024.0, 1)}

    def export_results(self, model_name):
        """G-code — первым и независимо: даже если проект 3MF не запишется, G-code останется."""
        result = {"folder": os.path.dirname(self._output_path(None, model_name, "x")), "errors": []}
        for key, func in (("gcode", self.export_gcode), ("project", self.save_project_3mf)):
            try:
                result[key] = func(model_name=model_name)
            except Exception as e:
                log("w", "export_results: %s failed: %s", key, e)
                result["errors"].append("%s: %s" % (key, e))
        if "gcode" not in result and "project" not in result:
            raise ToolError("Не удалось сохранить ни G-code, ни 3MF: %s" % "; ".join(result["errors"]))
        return result
