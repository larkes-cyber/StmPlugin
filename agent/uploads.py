import os
import tempfile
import time

UPLOAD_ROOT = os.path.join(tempfile.gettempdir(), "ste_ai_uploads")


def save_upload(filename, raw):
    """Сохраняет вложение модели и возвращает абсолютный путь."""
    folder = os.path.join(UPLOAD_ROOT, time.strftime("%Y%m%d-%H%M%S"))
    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


def resolve_model_path(path):
    """
    Возвращает существующий файл модели.

    Абсолютный путь используется как есть. Если модели передали только имя
    (или путь от текущего каталога слайсера), ищется последняя загрузка
    с тем же именем файла.
    """
    raw = os.path.expanduser(str(path or "").strip().strip('"'))
    if not raw:
        return raw

    candidate = raw if os.path.isabs(raw) else os.path.abspath(raw)
    if os.path.isfile(candidate):
        return candidate

    uploaded = _newest_upload(os.path.basename(candidate))
    return uploaded or candidate


def _newest_upload(filename):
    if not filename or filename in (".", "..") or not os.path.isdir(UPLOAD_ROOT):
        return None

    wanted = filename.lower()
    best_path = None
    best_stamp = ""
    for folder in os.listdir(UPLOAD_ROOT):
        folder_path = os.path.join(UPLOAD_ROOT, folder)
        if not os.path.isdir(folder_path):
            continue
        for entry in os.listdir(folder_path):
            if entry.lower() != wanted:
                continue
            full_path = os.path.join(folder_path, entry)
            if os.path.isfile(full_path) and folder >= best_stamp:
                best_path = full_path
                best_stamp = folder
    return best_path
