import os
import re

from steslicer.SteSlicerApplication import SteSlicerApplication
from UM.Mesh.MeshWriter import MeshWriter
from UM.PluginRegistry import PluginRegistry

from ..ai import ToolError
from ..core import Logger, config

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
            app = SteSlicerApplication.getInstance()
            writer = app.getMeshFileHandler().getWriterByMimeType("text/x-gcode")
        if writer is None:
            raise ToolError("Системный плагин Uranium 'GCodeWriter' не найден")

        app = SteSlicerApplication.getInstance()
        nodes = [app.getController().getScene().getRoot()]

        temp_path = out_path + ".part"
        def _do_write():
            with open(temp_path, "w", encoding="utf-8", newline="") as stream:
                return writer.write(stream, nodes, MeshWriter.OutputMode.TextMode)

        try:
            success = run_on_main(_do_write)
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
        def _do_write():
            with open(temp_path, "wb") as stream:
                return writer.write(stream, nodes, MeshWriter.OutputMode.BinaryMode)

        try:
            success = run_on_main(_do_write)
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

    def save_snapshot(self, scene, model_name=None, size=1024):
        """Сохраняет PNG-скриншот сцены в папку результатов."""
        out_path = self.get_output_path(None, model_name, "snapshot.png")
        try:
            png_bytes = run_on_main(lambda: scene._snapshot_png(size))
            with open(out_path, "wb") as f:
                f.write(png_bytes)
            Logger.log("i", "Снимок сцены сохранён в %s", out_path)
            self._events.emit("file_saved", {"kind": "snapshot", "path": out_path})
            return {"path": out_path}
        except Exception as e:
            Logger.log("w", "Не удалось сохранить снимок сцены: %s", e)
            return {"error": str(e)}

    def save_dialog_history(self, chat, model_name=None):
        """Сохраняет текстовую историю переписки в папку результатов."""
        if not chat or not chat.messages:
            return {"error": "Нет сообщений в чате"}

        out_path = self.get_output_path(None, model_name, "dialog.txt")
        try:
            lines = [
                "============================================================",
                "Отчёт по подготовке детали: %s" % (model_name or chat.title or "Деталь"),
                "Дата: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                "ID чата: %s" % chat.id,
                "============================================================\n",
            ]

            for msg in chat.messages:
                role = msg.get("role")
                if role == "system":
                    continue
                content = msg.get("content") or ""
                if isinstance(content, list):
                    # Извлекаем текст из мультимодальных частей
                    texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    content = "\n".join(texts)

                role_label = "Пользователь" if role == "user" else ("Ассистент" if role == "assistant" else "Инструмент")
                lines.append("[%s]:" % role_label)
                lines.append(content.strip())
                lines.append("-" * 40)

            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            Logger.log("i", "История переписки сохранена в %s", out_path)
            self._events.emit("file_saved", {"kind": "dialog", "path": out_path})
            return {"path": out_path}
        except Exception as e:
            Logger.log("w", "Не удалось сохранить историю переписки: %s", e)
            return {"error": str(e)}
