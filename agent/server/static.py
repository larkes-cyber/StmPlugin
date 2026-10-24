import os
import posixpath
from urllib.parse import unquote

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
}


def serve_static_file(handler, web_dir, url_path):
    clean_path = unquote(url_path).split("?", 1)[0]
    if clean_path in ("", "/"):
        clean_path = "/index.html"

    rel_path = posixpath.normpath(clean_path).lstrip("/")
    full_path = os.path.abspath(os.path.join(web_dir, rel_path))

    if not full_path.startswith(web_dir) or not os.path.isfile(full_path):
        handler.send_error(404, "Not Found")
        return

    ext = os.path.splitext(full_path)[1].lower()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

    try:
        with open(full_path, "rb") as f:
            content = f.read()
    except OSError:
        handler.send_error(500, "Internal Server Error")
        return

    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(content)
