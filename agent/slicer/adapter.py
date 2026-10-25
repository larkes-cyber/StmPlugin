import os
import threading
import time

from steslicer.SteSlicerApplication import SteSlicerApplication
from UM.Backend.Backend import BackendState

from ..ai import ToolError
from ..core import Logger, config, on_main, run_on_main
from ..core.dispatcher import main_waits_aborted
from ..uploads import resolve_model_path
from .exporter import SlicerExporter
from .gate import SlicerGate
from .scene import SlicerScene
from .settings import MODE_LABELS, SlicerSettings

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
BACKEND_STATE_NAMES = {
    BackendState.NotStarted: "pending",
    BackendState.Processing: "processing",
    BackendState.Done: "done",
    BackendState.Error: "error",
    BackendState.Disabled: "disabled",
}


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

        # Честная очередь к столу и слайсеру:
        self._gate = SlicerGate(events)
        self._slicer_lock = threading.Lock()

        # Подсистемы
        self.scene = SlicerScene(events)
        self.settings = SlicerSettings(events)
        self.exporter = SlicerExporter(events)

        self._backend = None
        self._active_chat_id = None
        self._slice = {
            "state": "idle",
            "progress": 0.0,
            "message": "",
            "started": None,
            "finished": None,
        }
        self._last_progress_emit = 0.0

    @property
    def gate(self):
        return self._gate

    @property
    def lock(self):
        return self._slicer_lock

    @property
    def active_chat_id(self):
        with self._slicer_lock:
            return self._active_chat_id

    def sync_for_chat(self, chat):
        """
        Изоляция стола: проверяет, соответствует ли модель на столе модели чата.
        Не делает лишних перезагрузок, если модель уже на столе.
        """
        if chat is None:
            return

        with self._slicer_lock:
            target_model = (chat.slicer_state or {}).get("model_path")
            current_nodes = self.scene.object_nodes()

            # Если модель этого чата уже загружена на столе — только обновляем настройки если нужно
            if self._active_chat_id == chat.id and current_nodes:
                return

            # Проверяем: совпадает ли имя файла уже находящейся на столе детали с нужной
            if target_model and current_nodes:
                target_filename = os.path.basename(target_model)
                current_names = [n.getName() for n in current_nodes]
                if any(target_filename.startswith(name) or name.startswith(target_filename) for name in current_names):
                    self._active_chat_id = chat.id
                    if chat.slicer_state:
                        self.settings.apply_chat_state(chat.slicer_state)
                    return

            # Если стол пуст и у чата нет модели — ничего не делаем
            if not target_model and not current_nodes:
                self._active_chat_id = chat.id
                return

            Logger.log("i", "Смена модели стола под чат %s: %s", chat.id, target_model)
            if current_nodes:
                try:
                    self.scene.clear_scene()
                except Exception as e:
                    Logger.log("w", "Ошибка очистки стола при переключении: %s", e)

            if target_model and os.path.isfile(target_model):
                try:
                    self.scene.load_model(target_model, chat_id=chat.id)
                except Exception as e:
                    Logger.log("w", "Не удалось восстановить модель чата %s: %s", chat.id, e)

            if chat.slicer_state:
                self.settings.apply_chat_state(chat.slicer_state)

            self._active_chat_id = chat.id

    # 1. Методы сцены
    def load_model(self, path, chat_id=None):
        resolved = resolve_model_path(path)
        if resolved != path:
            Logger.log("d", "load_model: %s -> %s", path, resolved)
        with self._slicer_lock:
            result = self.scene.load_model(resolved, chat_id=chat_id)
            if chat_id:
                self._active_chat_id = chat_id
            return result

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

    def get_scene_snapshot(self, size=None, chat_id=None):
        target_chat = chat_id or self._active_chat_id
        with self._slicer_lock:
            return self.scene.get_scene_snapshot(size, chat_id=target_chat)

    # 2. Методы настроек и профилей
    def get_state(self):
        def read():
            g_stack = self.settings.global_stack()
            return {
                "printer": g_stack.getName() if g_stack is not None else "",
                "print_mode": self.settings.get_print_mode(),
                "objects": [self.scene.node_info(n) for n in self.scene.object_nodes()],
                "slicing": dict(self._slice),
                "build_volume_mm": self.scene.build_volume(),
            }

        return run_on_main(read, timeout=8.0)

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

    def set_quality_profile(self, name):
        with self._slicer_lock:
            return self.settings.set_quality_profile(name)

    def list_materials(self):
        with self._slicer_lock:
            return self.settings.list_materials()

    def set_material(self, material_id=None, id=None):
        chosen = material_id or id
        if not chosen:
            raise ToolError("Нужен material_id из list_materials")
        with self._slicer_lock:
            return self.settings.set_material(chosen)

    def save_profile(self, name):
        with self._slicer_lock:
            return self.settings.save_profile(name)

    def arrange_all(self):
        with self._slicer_lock:
            return self.scene.arrange_all()

    def apply_orientation(self, object_id=None, rank=None, down=None):
        with self._slicer_lock:
            return self.scene.apply_orientation(object_id, down_vector=down, rank=rank)

    def suggest_orientations(self, object_id=None, support_angle=None, top_n=5):
        node_id, triangles, angle = self._geometry_inputs(object_id, support_angle)
        from .geometry import suggest_orientations

        result = suggest_orientations(triangles, angle, int(top_n or 5))
        result["object_id"] = node_id
        self.scene.remember_suggestions(node_id, result["candidates"])
        return result
    # 3. Управление слайсингом через Backend
    def start_slicing(self, wait=True, timeout=None):
        slice_timeout = float(timeout or self._config.get("slice_timeout", 300.0))
        run_on_main(self._start_slice)
        if not wait:
            return {"state": "processing"}

        deadline = time.time() + slice_timeout
        while time.time() < deadline:
            if main_waits_aborted():
                raise ToolError("Остановлено пользователем")
            time.sleep(0.25)
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
        # 1. Если статус уже done
        if self._slice.get("state") == "done":
            return run_on_main(self._result_dict)

        # 2. Проверяем, есть ли готовый результат прямо в ядре Cura
        try:
            res = run_on_main(self._result_dict)
            if res.get("print_time_s", 0) > 0:
                self._slice["state"] = "done"
                return res
        except Exception:
            pass

        # 3. Если нарезка еще не запускалась — автоматически запускаем и возвращаем результат
        Logger.log("i", "get_slicing_result: слайсинг не был запущен, запускаем автоматически")
        return self.start_slicing(wait=True)

    # 4. Экспорт через Uranium
    def export_gcode(self, path=None, model_name=None):
        with self._slicer_lock:
            if self._slice["state"] != "done":
                raise ToolError("Сначала выполните слайсинг (start_slicing)")
            return self.exporter.export_gcode(path, model_name)

    def save_project_3mf(self, path=None, model_name=None):
        with self._slicer_lock:
            return self.exporter.save_project_3mf(path, model_name)

    def export_results(self, model_name, chat=None):
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

            # Сохраняем скриншот детали
            try:
                res["snapshot"] = self.exporter.save_snapshot(self.scene, model_name=model_name)
            except Exception as e:
                res["snapshot_error"] = str(e)

            # Сохраня��м историю переписки
            if chat:
                try:
                    res["dialog"] = self.exporter.save_dialog_history(chat, model_name=model_name)
                except Exception as e:
                    res["dialog_error"] = str(e)

            return res

    def analyze_geometry(self, object_id=None, support_angle=None):
        node_id, triangles, angle = self._geometry_inputs(object_id, support_angle)
        from .geometry import analyze

        result = analyze(triangles, angle)
        result["object_id"] = node_id
        return result

    def _geometry_inputs(self, object_id, support_angle):
        def prepare():
            node = self.scene.find_node(object_id)
            return self.scene.object_id(node), self.scene.plate_triangles(node)

        with self._slicer_lock:
            node_id, triangles = run_on_main(prepare)
            angle = self._support_angle(support_angle)
        return node_id, triangles, angle

    def _support_angle(self, value):
        if value is not None:
            return float(value)

        def read():
            stack, _ = self.settings.stack_for("support_angle")
            raw = stack.getProperty("support_angle", "value")
            return float(raw or 50.0)

        try:
            return run_on_main(read)
        except Exception:
            return 50.0

    def review_proposal(self):
        with self._slicer_lock:
            mode = self.settings.get_print_mode()
            materials = self.settings.list_materials()
            mode_name = mode.get("mode") or "classic"
            settings = []
            for key in REVIEW_SETTING_KEYS:
                if key in FIVE_AXIS_ONLY_KEYS and mode_name == "classic":
                    continue
                try:
                    settings.append(self.settings.describe(key))
                except ToolError:
                    continue
            active = materials.get("active") or {}
            for item in materials.get("materials") or []:
                if item.get("name") and item.get("name") == active.get("name"):
                    active = item
                    break
            return {
                "print_mode": mode_name,
                "print_modes": [
                    {"value": option, "label": MODE_LABELS.get(option, option)}
                    for option in (mode.get("options") or [])
                ],
                "material": active,
                "materials": materials.get("materials") or [],
                "settings": settings,
                "objects": [self.scene.node_info(node) for node in self.scene.object_nodes()],
            }

    def apply_review_changes(self, changes):
        changes = changes or {}
        applied = []
        errors = []
        if changes.get("print_mode"):
            self._try_review_change(
                applied, errors, "режим печати",
                lambda: self.set_print_mode(changes["print_mode"], USER_CHANGE_REASON),
            )
        if changes.get("material_id"):
            self._try_review_change(
                applied, errors, "материал",
                lambda: self.set_material(changes["material_id"]),
            )
        settings = changes.get("settings")
        if settings:
            try:
                result = self.set_settings(settings, USER_CHANGE_REASON)
            except ToolError as error:
                errors.append("настройки: %s" % error)
            else:
                for change in result.get("applied") or []:
                    applied.append(
                        "%s: %s → %s" % (change.get("label") or change.get("key"), change.get("old"), change.get("new"))
                    )
                for error in result.get("errors") or []:
                    errors.append("%s: %s" % (error.get("key"), error.get("error")))
        placement = changes.get("placement") or {}
        if placement:
            self._apply_review_placement(placement, applied, errors)
        return {"applied": applied, "errors": errors}

    def _apply_review_placement(self, placement, applied, errors):
        object_id = placement.get("object_id")
        if "x" in placement or "y" in placement:
            self._try_review_change(
                applied, errors, "положение",
                lambda: self.move_object(object_id, placement.get("x"), placement.get("y")),
            )
        if placement.get("scale_percent") is not None:
            percent = float(placement["scale_percent"])
            self._try_review_change(
                applied, errors, "масштаб",
                lambda: self.scale_object(object_id, percent=percent),
            )
        for axis in ("x", "y", "z"):
            degrees = placement.get("rotate_" + axis)
            if not degrees:
                continue
            self._try_review_change(
                applied, errors, "поворот %s" % axis.upper(),
                lambda axis=axis, degrees=degrees: self.rotate_object(axis, float(degrees), object_id),
            )

    @staticmethod
    def _try_review_change(applied, errors, description, change):
        try:
            change()
        except ToolError as error:
            errors.append("%s: %s" % (description, error))
            return
        applied.append(description)

    # Внутренние методы оркестрации нарезки
    def _ensure_backend(self):
        if self._backend is None:
            self._backend = _app().getBackend()
            self._backend.backendStateChange.connect(self._on_backend_state_change)
            progress = getattr(self._backend, "processingProgress", None)
            if progress is None:
                progress = getattr(self._backend, "slicingProgress", None)
            if progress is not None:
                progress.connect(self._on_progress)
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
        state_str = BACKEND_STATE_NAMES.get(backend_state)
        if state_str is None:
            state_str = str(backend_state).split(".")[-1].lower()
        self._slice["state"] = state_str
        if state_str == "done":
            self._slice["progress"] = 1.0
            self._slice["finished"] = time.time()
            try:
                res = self._result_dict()
            except Exception:
                res = {}
            self._events.emit("slice_state", {"state": "done", "result": res}, chat_id=self._active_chat_id)
        elif state_str in ("error", "disabled"):
            self._events.emit("slice_state", {"state": state_str}, chat_id=self._active_chat_id)

    def _on_progress(self, amount):
        if self._slice["state"] != "processing":
            return
        amount = float(amount)
        self._slice["progress"] = amount
        if amount >= 1.0:
            return
        if self._slice["state"] != "processing":
            return
        if abs(amount - self._last_progress_emit) >= 0.05:
            self._last_progress_emit = amount
            self._events.emit("slice_progress", {"progress": round(amount, 3)}, chat_id=self._active_chat_id)

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
