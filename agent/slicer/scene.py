import base64
import math
import os
import time

import numpy

from PyQt5.QtCore import QBuffer, QUrl
from steslicer.Snapshot import Snapshot
from steslicer.SteSlicerApplication import SteSlicerApplication
from UM.Math.Quaternion import Quaternion
from UM.Math.Vector import Vector
from UM.Operations.GravityOperation import GravityOperation
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
from UM.Operations.RotateOperation import RotateOperation
from UM.Operations.ScaleOperation import ScaleOperation
from UM.Operations.TranslateOperation import TranslateOperation
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator

from ..ai import ToolError
from ..core import Logger, config, on_main, run_on_main

MODEL_EXTENSIONS = (".stl", ".3mf", ".obj")
PLATE_AXES_IN_SCENE = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 0.0, -1.0),
    "z": (0.0, 1.0, 0.0),
}
DEFAULT_SNAPSHOT_SIZE = 512


def _app():
    return SteSlicerApplication.getInstance()


def _round(val, digits=2):
    return round(float(val), digits)


class SlicerScene(object):
    def __init__(self, events):
        self._events = events
        self._suggestions = {}
        self._object_ids = {}
        self._next_object_num = 1

    def scene(self):
        return _app().getController().getScene()

    def object_nodes(self):
        root = self.scene().getRoot()
        nodes = []
        for node in DepthFirstIterator(root):
            if node is root:
                continue
            is_obj = node.callDecoration("isSliceable") or node.callDecoration(
                "isGroup"
            )
            if not is_obj:
                continue
            parent = node.getParent()
            if parent is not None and parent.callDecoration("isGroup"):
                continue
            nodes.append(node)
        return nodes

    def object_id(self, node):
        key = id(node)
        if key not in self._object_ids:
            self._object_ids[key] = "obj%d" % self._next_object_num
            self._next_object_num += 1
        return self._object_ids[key]

    def find_node(self, object_id=None):
        nodes = self.object_nodes()
        if not nodes:
            raise ToolError("Сцена пуста — сначала загрузите модель (load_model)")

        known_ids = ", ".join(self.object_id(node) for node in nodes)
        if not object_id or object_id == "auto":
            if len(nodes) == 1:
                return nodes[0]
            raise ToolError(
                "В сцене несколько объектов, укажите object_id: %s" % known_ids
            )

        for node in nodes:
            if object_id in (self.object_id(node), node.getName()):
                return node
        raise ToolError("Объект '%s' не найден. Доступны: %s" % (object_id, known_ids))

    def node_info(self, node):
        box = node.getBoundingBox()
        scale = node.getScale()
        info = {
            "object_id": self.object_id(node),
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
            "on_plate": bool(abs(float(box.bottom)) < 0.05),
            "scale_pct": {
                "x": _round(scale.x * 100),
                "y": _round(scale.z * 100),
                "z": _round(scale.y * 100),
            },
        }
        if hasattr(node, "isOutsideBuildArea"):
            info["outside_build_area"] = bool(node.isOutsideBuildArea())
        return info

    def build_volume(self):
        stack = _app().getGlobalContainerStack()
        if stack is None:
            return {"x": 200, "y": 200, "z": 200}
        return {
            "x": stack.getProperty("machine_width", "value"),
            "y": stack.getProperty("machine_depth", "value"),
            "z": stack.getProperty("machine_height", "value"),
        }

    def load_model(self, path, chat_id=None):
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ToolError("Файл не найден: %s" % path)
        if not path.lower().endswith(MODEL_EXTENSIONS):
            raise ToolError(
                "Поддерживаются только форматы %s" % ", ".join(MODEL_EXTENSIONS)
            )

        keys_before = set(run_on_main(self._node_keys))
        Logger.log(
            "i", "Загрузка модели %s (было объектов: %d)", path, len(keys_before)
        )
        run_on_main(lambda: _app().readLocalFile(QUrl.fromLocalFile(path)))

        load_timeout = float(config.get("load_timeout", 30.0))
        deadline = time.time() + load_timeout
        new_keys = []
        previous_keys = None
        stable_since = None

        while time.time() < deadline:
            time.sleep(0.4)
            current_keys = [
                k for k in run_on_main(self._node_keys) if k not in keys_before
            ]
            if not current_keys or current_keys != previous_keys:
                stable_since = None
            elif stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since > 0.8:
                new_keys = current_keys
                break
            previous_keys = current_keys

        if not new_keys:
            raise ToolError(
                "Модель не появилась на столе за %d с (проверьте файл)"
                % int(load_timeout)
            )

        objects = run_on_main(
            lambda: [
                self.node_info(n) for n in self.object_nodes() if id(n) in set(new_keys)
            ]
        )
        Logger.log("i", "Модель загружена: %s", objects)
        self._events.emit("scene_changed", {"action": "load", "objects": objects}, chat_id=chat_id)
        return {"loaded": objects, "file": path}

    @on_main
    def list_objects(self):
        return {
            "objects": [self.node_info(n) for n in self.object_nodes()],
            "build_volume_mm": self.build_volume(),
            "coordinates": "x,y — центр объекта на столе, (0,0) — центр стола; z — вверх",
        }

    @on_main
    def move_object(self, object_id=None, x=None, y=None, relative=False):
        node = self.find_node(object_id)
        box = node.getBoundingBox()
        cur_x = box.center.x
        cur_y = -box.center.z

        tar_x = (
            (cur_x + float(x or 0)) if relative else (cur_x if x is None else float(x))
        )
        tar_y = (
            (cur_y + float(y or 0)) if relative else (cur_y if y is None else float(y))
        )

        offset = Vector(tar_x - cur_x, 0.0, -(tar_y - cur_y))
        TranslateOperation(node, offset).push()
        GravityOperation(node).push()

        info = self.node_info(node)
        self._events.emit("scene_changed", {"action": "move", "objects": [info]})
        return info

    @on_main
    def center_object(self, object_id=None):
        return self.move_object(object_id, 0.0, 0.0, False)

    @on_main
    def rotate_object(self, axis, degrees, object_id=None):
        if axis not in PLATE_AXES_IN_SCENE:
            raise ToolError("axis должен быть x, y или z")

        node = self.find_node(object_id)
        box = node.getBoundingBox()
        cen_x = box.center.x
        cen_z = box.center.z

        angle_rad = math.radians(float(degrees))
        if abs(angle_rad) > 1e-9:
            rot = Quaternion.fromAngleAxis(
                angle_rad, Vector(*PLATE_AXES_IN_SCENE[axis])
            )
            RotateOperation(node, rot, rotate_around_point=box.center).push()

        new_box = node.getBoundingBox()
        shift = Vector(cen_x - new_box.center.x, 0.0, cen_z - new_box.center.z)
        if shift.length() > 1e-4:
            TranslateOperation(node, shift).push()
        GravityOperation(node).push()

        info = self.node_info(node)
        self._events.emit("scene_changed", {"action": "rotate", "objects": [info]})
        return info

    @on_main
    def scale_object(self, object_id=None, percent=None, x=None, y=None, z=None):
        node = self.find_node(object_id)
        box = node.getBoundingBox()
        cen_x = box.center.x
        cen_z = box.center.z

        if percent is not None:
            factor = float(percent) / 100.0
            scale_vector = Vector(factor, factor, factor)
        else:
            cur_scale = node.getScale()
            sx = (float(x) / 100.0) if x is not None else cur_scale.x
            sy = (float(z) / 100.0) if z is not None else cur_scale.y
            sz = (float(y) / 100.0) if y is not None else cur_scale.z
            scale_vector = Vector(sx, sy, sz)

        ScaleOperation(node, scale_vector).push()
        new_box = node.getBoundingBox()
        shift = Vector(cen_x - new_box.center.x, 0.0, cen_z - new_box.center.z)
        if shift.length() > 1e-4:
            TranslateOperation(node, shift).push()
        GravityOperation(node).push()

        info = self.node_info(node)
        self._events.emit("scene_changed", {"action": "scale", "objects": [info]})
        return info

    @on_main
    def delete_object(self, object_id):
        if object_id == "all":
            _app().deleteAll()
            self._object_ids.clear()
            self._next_object_num = 1
            self._events.emit("scene_changed", {"action": "delete_all", "objects": []})
            return {"deleted": "all"}

        node = self.find_node(object_id)
        info = self.node_info(node)
        RemoveSceneNodeOperation(node).push()
        self._events.emit("scene_changed", {"action": "delete", "objects": [info]})
        return {"deleted": info}

    @on_main
    def clear_scene(self):
        """Полная очистка стола от любых старых моделей."""
        _app().deleteAll()
        self._object_ids.clear()
        self._next_object_num = 1
        self._events.emit("scene_changed", {"action": "clear", "objects": []})


    def arrange_all(self):
        run_on_main(_app().arrangeAll)
        time.sleep(1.0)
        res = self.list_objects()
        self._events.emit("scene_changed", {"action": "arrange", "objects": res["objects"]})
        return res

    def plate_triangles(self, node):
        """Треугольники объекта в координатах стола: Z вверх, миллиметры."""
        from .geometry import scene_to_plate

        chunks = []
        for child in DepthFirstIterator(node):
            triangles = self._world_triangles(child)
            if triangles is not None:
                chunks.append(triangles)
        if not chunks:
            raise ToolError("У объекта нет полигональной сетки")
        return scene_to_plate(numpy.concatenate(chunks))

    def remember_suggestions(self, object_id, candidates):
        self._suggestions[object_id] = list(candidates or [])

    @staticmethod
    def _world_triangles(node):
        from .geometry import indexed_triangles, transform_triangles

        mesh = node.getMeshData()
        if mesh is None:
            return None
        triangles = indexed_triangles(mesh.getVertices(), mesh.getIndices())
        if triangles is None:
            return None
        matrix = node.getWorldTransformation().getData()
        return transform_triangles(triangles, matrix)

    @on_main
    def apply_orientation(self, object_id=None, down_vector=None, rank=None):
        if not down_vector and rank is not None:
            down_vector = self._suggested_down(object_id, rank)
        if not down_vector or len(down_vector) != 3:
            raise ToolError(
                "Нужен вектор down из трёх чисел или rank из последнего suggest_orientations"
            )
        from .geometry import rotation_to_down, plate_to_scene_vector
        axis, angle = rotation_to_down(down_vector)
        node = self.find_node(object_id)
        box = node.getBoundingBox()
        rot = Quaternion.fromAngleAxis(math.radians(angle), Vector(*plate_to_scene_vector(axis)))
        RotateOperation(node, rot, rotate_around_point=box.center).push()
        GravityOperation(node).push()
        info = self.node_info(node)
        self._events.emit("scene_changed", {"action": "orient", "objects": [info]})
        return {"object": info, "angle": round(angle, 1)}

    def _suggested_down(self, object_id, rank):
        node = self.find_node(object_id)
        candidates = self._suggestions.get(self.object_id(node)) or []
        try:
            index = int(rank) - 1
        except (TypeError, ValueError):
            index = -1
        if index < 0 or index >= len(candidates):
            raise ToolError(
                "Нет варианта %s. Сначала вызовите suggest_orientations." % rank
            )
        return candidates[index]["down"]

    def get_scene_snapshot(self, size=DEFAULT_SNAPSHOT_SIZE):
        size = max(128, min(int(size or DEFAULT_SNAPSHOT_SIZE), 1024))
        png_data = run_on_main(lambda: self._snapshot_png(size))
        image_url = "data:image/png;base64," + base64.b64encode(png_data).decode(
            "ascii"
        )

        self._events.emit("snapshot", {"image": image_url, "caption": "Снимок сцены"})
        return {"image_attached": True, "size_px": size, "_image": image_url}

    def _snapshot_png(self, size):
        qimage = Snapshot.snapshot(width=size, height=size)
        buffer = QBuffer()
        buffer.open(QBuffer.ReadWrite)
        qimage.save(buffer, "PNG")
        return bytes(buffer.data())

    def _node_keys(self):
        return [id(n) for n in self.object_nodes()]
