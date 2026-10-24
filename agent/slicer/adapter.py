import os
import threading
import time

from steslicer.SteSlicerApplication import SteSlicerApplication

from ..ai import ToolError
from ..core import Logger, config, on_main, run_on_main
from .exporter import SlicerExporter
from .scene import SlicerScene
from .settings import SlicerSettings


def _app():
    return SteSlicerApplication.getInstance()


class SlicerAPI(object):
    """
    Главный фасад для взаимодействия с ядром Uranium и CuraEngine.
    Защищён мьютексом _slicer_lock от Data Race при обращении из разных потоков и чатов.
    """

    def __init__(self, events, config_obj=None):
        self._events = events
        self._config = config_obj or config

        # Тот самый мьютекс защиты сцены и настроек от Data Race:
        self._slicer_lock = threading.Lock()

        # Подсистемы
        self.scene = SlicerScene(events)
        self.settings = SlicerSettings(events)
        self.exporter = SlicerExporter(events)

        self._backend = None
        self._slice = {
            "state": "idle",
            "progress": 0.0,
            "message": "",
            "started": None,
            "finished": None,
        }
        self._last_progress_emit = 0.0

    @property
    def lock(self):
        return self._slicer_lock

    # 1. Методы сцены
    def load_model(self, path):
        with self._slicer_lock:
            return self.scene.load_model(path)

    def list_objects(self):
        with self._slicer_lock:
            return self.scene.list_objects()

    def get_object_dimensions(self, object_id=None):
        with self._slicer_lock:
            return self.scene.node_info(self.scene.find_node(object_id))

    def move_object(self, object_id=None, x=None, y=None, relative=False):
        with self._slicer_lock:
            return self.scene.move_object(object_id, x, y, relative)

    def center_object(self, object_id=None):
        with self._slicer_lock:
            return self.scene.center_object(object_id)

    def rotate_object(self, axis, degrees, object_id=None):
        with self._slicer_lock:
            return self.scene.rotate_object(axis, degrees, object_id)

    def scale_object(self, object_id=None, percent=None, x=None, y=None, z=None):
        with self._slicer_lock:
            return self.scene.scale_object(object_id, percent, x, y, z)

    def delete_object(self, object_id):
        with self._slicer_lock:
            return self.scene.delete_object(object_id)

    def get_scene_snapshot(self, size=None):
        with self._slicer_lock:
            return self.scene.get_scene_snapshot(size)

    # 2. Методы настроек и профилей
    def get_state(self):
        with self._slicer_lock:
            g_stack = self.settings.global_stack()
            return {
                "printer": g_stack.getName(),
                "print_mode": self.settings.get_print_mode(),
                "objects": [self.scene.node_info(n) for n in self.scene.object_nodes()],
                "slicing": dict(self._slice),
                "build_volume_mm": self.scene.build_volume(),
            }

    def get_settings(self, keys):
        with self._slicer_lock:
            return self.settings.get_settings(keys)

    def search_settings(self, query, limit=25):
        with self._slicer_lock:
            return self.settings.search_settings(query, limit)

    def set_settings(self, changes, reason=""):
        with self._slicer_lock:
            return self.settings.set_settings(changes, reason)

    def reset_settings(self, keys=None):
        with self._slicer_lock:
            return self.settings.reset_settings(keys)

    def get_print_mode(self):
        with self._slicer_lock:
            return self.settings.get_print_mode()

    def set_print_mode(self, mode, reason=""):
        with self._slicer_lock:
            return self.settings.set_print_mode(mode, reason)

    def apply_chat_state(self, state):
        """Восстановление настроек конкретного чата под мьютексом."""
        with self._slicer_lock:
            self.settings.apply_chat_state(state)


    def list_quality_profiles(self):
        with self._slicer_lock:
            return self.settings.list_quality_profiles()

    def set_quality_profile(self, profile_id):
        with self._slicer_lock:
            return self.settings.set_quality_profile(profile_id)

    def list_materials(self):
        with self._slicer_lock:
            return self.settings.list_materials()

    def set_material(self, material_name):
        with self._slicer_lock:
            return self.settings.set_material(material_name)

    def save_profile(self, name):
        with self._slicer_lock:
            return self.settings.save_profile(name)

    def arrange_all(self):
        with self._slicer_lock:
            return self.scene.arrange_all()

    def apply_orientation(self, object_id=None, down_vector=None):
        with self._slicer_lock:
            return self.scene.apply_orientation(object_id, down_vector)

    def suggest_orientations(self, object_id=None, support_angle=50.0):
        with self._slicer_lock:
            from .geometry import suggest_orientations
            node = self.scene.find_node(object_id)
            mesh = node.getMeshData()
            if mesh is None:
                raise ToolError("У объекта нет полигональной сетки")
            return suggest_orientations(mesh, support_angle=support_angle)
    # 3. Управление слайсингом через Backend
    def start_slicing(self, wait=True, timeout=None):
        with self._slicer_lock:
            slice_timeout = float(timeout or self._config.get("slice_timeout", 300.0))
            run_on_main(self._start_slice)
            if not wait:
                return {"state": "processing"}

            deadline = time.time() + slice_timeout
            while time.time() < deadline:
                time.sleep(0.5)
                state = self._slice["state"]
                if state == "done":
                    result = run_on_main(self._result_dict)
                    Logger.log("i", "Слайсинг завершён успешно: %s", result)
                    return result
                if state in ("error", "disabled"):
                    raise ToolError("Слайсинг не выполнен: статус '%s'. Проверьте положение модели на столе" % state)

            raise ToolError("Слайсинг не завершился за %d с" % int(slice_timeout))

    def get_slicing_status(self):
        status = dict(self._slice)
        if status.get("started"):
            finished_at = status.get("finished") or time.time()
            status["elapsed_s"] = round(finished_at - status["started"], 1)
        return status

    def get_slicing_result(self):
        if self._slice["state"] != "done":
            raise ToolError("Нет готового результата — запустите start_slicing")
        return run_on_main(self._result_dict)

    # 4. Экспорт через Uranium
    def export_gcode(self, path=None, model_name=None):
        with self._slicer_lock:
            if self._slice["state"] != "done":
                raise ToolError("Сначала выполните слайсинг (start_slicing)")
            return self.exporter.export_gcode(path, model_name)

    def save_project_3mf(self, path=None, model_name=None):
        with self._slicer_lock:
            return self.exporter.save_project_3mf(path, model_name)

    def export_results(self, model_name):
        with self._slicer_lock:
            res = {}
            try:
                res["gcode"] = self.export_gcode(model_name=model_name)
            except Exception as e:
                res["gcode_error"] = str(e)
            try:
                res["project"] = self.save_project_3mf(model_name=model_name)
            except Exception as e:
                res["project_error"] = str(e)
            return res

    def analyze_geometry(self, object_id=None):
        with self._slicer_lock:
            from .geometry import analyze
            node = self.scene.find_node(object_id)
            mesh = node.getMeshData()
            if mesh is None:
                raise ToolError("У объекта нет полигональной сетки")
            return analyze(mesh)

    # Внутренние методы оркестрации нарезки
    def _ensure_backend(self):
        if self._backend is None:
            self._backend = _app().getBackend()
            self._backend.backendStateChange.connect(self._on_backend_state_change)
            self._backend.slicingProgress.connect(self._on_progress)
        return self._backend

    def _start_slice(self):
        if not self.scene.object_nodes():
            raise ToolError("Сцена пуста — нечего слайсить")
        backend = self._ensure_backend()
        self._slice["state"] = "processing"
        self._slice["progress"] = 0.0
        self._slice["started"] = time.time()
        self._slice["finished"] = None
        self._events.emit("slice_state", {"state": "processing"})
        backend.forceSlice()

    def _on_backend_state_change(self, backend_state):
        state_str = str(backend_state).split(".")[-1].lower()
        self._slice["state"] = state_str
        if state_str == "done":
            self._slice["progress"] = 1.0
            self._slice["finished"] = time.time()
            try:
                res = self._result_dict()
            except Exception:
                res = {}
            self._events.emit("slice_state", {"state": "done", "result": res})
        elif state_str in ("error", "disabled"):
            self._events.emit("slice_state", {"state": state_str})

    def _on_progress(self, amount):
        amount = float(amount)
        self._slice["progress"] = amount
        if abs(amount - self._last_progress_emit) >= 0.05 or amount >= 1.0:
            self._last_progress_emit = amount
            self._events.emit("slice_progress", {"progress": round(amount, 3)})

    def _result_dict(self):
        print_info = _app().getPrintInformation()
        seconds = int(print_info.currentPrintTime)
        weights = [float(w) for w in (print_info.materialWeights or [])]
        lengths = [float(l) for l in (print_info.materialLengths or [])]
        return {
            "state": self._slice["state"],
            "print_time_s": seconds,
            "print_time": "%d ч %02d мин" % (seconds // 3600, (seconds % 3600) // 60),
            "material_g": round(sum(weights), 1),
            "material_m": round(sum(lengths), 2),
        }
