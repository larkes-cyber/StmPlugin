import os
import re

from steslicer.SteSlicerApplication import SteSlicerApplication
from UM.Mesh.MeshWriter import MeshWriter
from UM.PluginRegistry import PluginRegistry

from ..ai import ToolError
from ..core import Logger, config, is_main_thread

TEMPERATURE_PATTERN = re.compile(r"[ST]([0-9]+(?:\.[0-9]+)?)")
DEFAULT_GCODE_NAME = "result.gcode"
DEFAULT_3MF_NAME = "project.3mf"


def check_gcode_header(path, max_bytes=256 * 1024):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return {"valid": False, "size_kb": 0, "problems": ["Файл пуст или отсутствует"]}

    size_kb = round(os.path.getsize(path) / 1024.0, 1)
    stats = {
        "size_kb": size_kb,
        "moves": 0,
        "layers": 0,
        "max_nozzle_temp": None,
        "max_bed_temp": None,
        "flavor": None,
    }

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(max_bytes)
            for line in chunk.splitlines():
                if line.startswith(("G0", "G1", "G2", "G3")):
                    stats["moves"] += 1
                elif line.startswith(";LAYER:"):
                    stats["layers"] += 1
                elif line.startswith(("M104", "M109", "M140", "M190")):
                    match = TEMPERATURE_PATTERN.search(line)
                    if match:
                        temp = float(match.group(1))
                        key = (
                            "max_bed_temp"
                            if line.startswith(("M140", "M190"))
                            else "max_nozzle_temp"
                        )
                        if stats[key] is None or temp > stats[key]:
                            stats[key] = temp
                elif line.startswith(";FLAVOR:") and stats["flavor"] is None:
                    stats["flavor"] = line.split(":", 1)[1].strip()
    except Exception as e:
        return {
            "valid": False,
            "size_kb": size_kb,
            "problems": ["Ошибка чтения: %s" % e],
        }

    problems = []
    if stats["moves"] == 0:
        problems.append("В начале файла не найдены команды перемещения G0-G3")

    stats["valid"] = not problems
    stats["problems"] = problems
    return stats


def _write_with(writer, path, nodes, mode, binary):
    """Писатели STE (GCodeWriter, 3MFWriter) ждут открытый поток, а не путь к файлу.

    Вызывать только из фонового потока: внутри писателей стоит @call_on_qt_thread, который всегда
    ставит работу в очередь главного потока и ждёт её — из главного потока это вечное ожидание.
    """
    if is_main_thread():
        raise ToolError("Экспорт нельзя запускать из главного потока слайсера")
    if binary:
        with open(path, "wb") as stream:
            return writer.write(stream, nodes, mode)
    with open(path, "w", encoding="utf-8", newline="") as stream:
        return writer.write(stream, nodes, mode)


class SlicerExporter(object):
    def __init__(self, events):
        self._events = events

    def get_output_path(
        self, path=None, model_name=None, default_filename=DEFAULT_GCODE_NAME
    ):
        if path:
            out = os.path.abspath(os.path.expanduser(str(path)))
        else:
            base_dir = config.get("output_dir")
            subfolder = model_name or "latest"
            out = os.path.join(base_dir, subfolder, default_filename)

        folder = os.path.dirname(out)
        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        return out

    def export_gcode(self, path=None, model_name=None):
        out_path = self.get_output_path(path, model_name, DEFAULT_GCODE_NAME)
        writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
        if writer is None:
            raise ToolError("Системный плагин Uranium 'GCodeWriter' не найден")

        app = SteSlicerApplication.getInstance()
        nodes = [app.getController().getScene().getRoot()]

        temp_path = out_path + ".part"
        try:
            success = _write_with(writer, temp_path, nodes, MeshWriter.OutputMode.TextMode, binary=False)
            if (
                not success
                or not os.path.exists(temp_path)
                or os.path.getsize(temp_path) == 0
            ):
                raise ToolError("GCodeWriter не смог сформировать файл на диске")
            if os.path.exists(out_path):
                os.remove(out_path)
            os.replace(temp_path, out_path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

        check = check_gcode_header(out_path)
        Logger.log(
            "i", "G-code успешно сохранён в %s (%.1f КБ)", out_path, check["size_kb"]
        )
        self._events.emit(
            "file_saved", {"kind": "gcode", "path": out_path, "check": check}
        )
        return {"path": out_path, "check": check}

    def save_project_3mf(self, path=None, model_name=None):
        out_path = self.get_output_path(path, model_name, DEFAULT_3MF_NAME)
        app = SteSlicerApplication.getInstance()
        writer = app.getWorkspaceFileHandler().getWriter("3MFWriter")
        if writer is None:
            raise ToolError("Системный плагин Uranium '3MFWriter' не найден")

        nodes = [app.getController().getScene().getRoot()]
        temp_path = out_path + ".part"
        try:
            success = _write_with(writer, temp_path, nodes, MeshWriter.OutputMode.BinaryMode, binary=True)
            if (
                not success
                or not os.path.exists(temp_path)
                or os.path.getsize(temp_path) == 0
            ):
                raise ToolError("3MFWriter не смог сформировать проект")
            if os.path.exists(out_path):
                os.remove(out_path)
            os.replace(temp_path, out_path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

        size_kb = round(os.path.getsize(out_path) / 1024.0, 1)
        Logger.log("i", "Проект 3MF успешно сохранён в %s (%.1f КБ)", out_path, size_kb)
        self._events.emit(
            "file_saved", {"kind": "3mf", "path": out_path, "size_kb": size_kb}
        )
        return {"path": out_path, "size_kb": size_kb}
