# -*- coding: utf-8 -*-
"""Локальный HTTP-сервер: SPA, API чата (fetch + long-polling) и MCP-эндпоинт /mcp."""
import base64
import json
import os
import posixpath
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

from .agent import AgentBusy
from .log import log, log_exception

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "ste_ai_uploads")
MAX_BODY = 200 * 1024 * 1024
IMAGE_EXT = (".png", ".jpg", ".jpeg")
MODEL_EXT = (".stl", ".obj", ".3mf")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".json": "application/json"}
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _safe_filename(name):
    base = os.path.basename(name or "file")
    base = re.sub(r"[^\w\-. ]+", "_", base, flags=re.UNICODE).strip(" .")
    return base or "file"


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        server_version = "STE-AIAssistant/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # --- утилиты ---------------------------------------------------------
        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ALLOWED_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urlparse(origin).hostname or ""
                if origin_host not in ("127.0.0.1", "localhost", "::1"):
                    return False
            return True

        def _send(self, status, body, content_type, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Слишком большой запрос")
            return self.rfile.read(length).decode("utf-8") if length else ""

        def _read_json(self):
            raw = self._read_body()
            return json.loads(raw) if raw else {}

        # --- GET -------------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "forbidden"}, 403)
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/api/events":
                    query = parse_qs(parsed.query)
                    since = int(query.get("since", ["0"])[0])
                    wait = min(float(query.get("wait", ["20"])[0]), 25.0)
                    events = ctx.events.since(since, timeout=wait)
                    return self._json({"events": events, "busy": ctx.agent.busy})
                if path == "/api/config":
                    data = ctx.config.public()
                    data["llm_ready"] = ctx.config.llm_ready()
                    data["mcp_url"] = ctx.url + "/mcp"
                    data["tools"] = ctx.registry.names()
                    return self._json(data)
                if path == "/api/state":
                    try:
                        return self._json({"ok": True, "state": ctx.slicer.get_state()})
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)})
                if path == "/mcp":
                    return self._send(405, b"", "text/plain", {"Allow": "POST"})
                return self._static(path)
            except Exception as e:
                log_exception("GET %s failed" % path)
                return self._json({"error": str(e)}, 500)

        def _static(self, path):
            rel = posixpath.normpath(unquote(path)).lstrip("/")
            if rel in ("", "."):
                rel = "index.html"
            full = os.path.realpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as f:
                body = f.read()
            ext = os.path.splitext(full)[1].lower()
            return self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"))

        # --- POST ------------------------------------------------------------
        def do_POST(self):
            if not self._host_ok():
                return self._json({"error": "forbidden"}, 403)
            path = urlparse(self.path).path
            try:
                if path == "/mcp":
                    response = ctx.mcp.handle_raw(self._read_body())
                    if response is None:
                        return self._send(202, b"", "application/json")
                    return self._json(response)
                if path == "/api/message":
                    return self._message(self._read_json())
                if path == "/api/stop":
                    ctx.agent.stop()
                    return self._json({"ok": True})
                if path == "/api/reset":
                    ctx.agent.reset()
                    return self._json({"ok": True})
                if path == "/api/config":
                    ctx.config.update(self._read_json())
                    data = ctx.config.public()
                    data["llm_ready"] = ctx.config.llm_ready()
                    return self._json(data)
                if path == "/api/test_llm":
                    try:
                        return self._json(ctx.llm.test())
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)})
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                log_exception("POST %s failed" % path)
                return self._json({"error": str(e)}, 500)

        def _message(self, data):
            text = (data.get("text") or "").strip()
            attachments = data.get("attachments") or []
            if not text and not attachments:
                return self._json({"error": "Пустое сообщение"}, 400)
            if not ctx.config.llm_ready():
                return self._json({"error": "Укажите URL, ключ и модель LLM в настройках"}, 400)
            images, files = [], []
            if not os.path.isdir(UPLOAD_DIR):
                os.makedirs(UPLOAD_DIR)
            for item in attachments:
                name = _safe_filename(item.get("name"))
                ext = os.path.splitext(name)[1].lower()
                raw = base64.b64decode(item.get("data") or "")
                if ext in IMAGE_EXT:
                    mime = "image/png" if ext == ".png" else "image/jpeg"
                    images.append("data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii")))
                elif ext in MODEL_EXT:
                    folder = os.path.join(UPLOAD_DIR, time.strftime("%Y%m%d-%H%M%S"))
                    if not os.path.isdir(folder):
                        os.makedirs(folder)
                    path = os.path.join(folder, name)
                    with open(path, "wb") as f:
                        f.write(raw)
                    files.append({"name": name, "path": path})
                else:
                    return self._json({"error": "Неподдерживаемый тип файла: %s" % name}, 400)
            try:
                ctx.agent.submit(text, images, files)
            except AgentBusy as e:
                return self._json({"error": str(e)}, 409)
            return self._json({"ok": True})

    return Handler


def start_server(ctx, port):
    handler = make_handler(ctx)
    try:
        server = ChatServer(("127.0.0.1", int(port or 0)), handler)
    except OSError:
        log("w", "port %s is busy, using a random one", port)
        server = ChatServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="AIAssistantHTTP")
    thread.daemon = True
    thread.start()
    return server
