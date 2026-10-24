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
from .documents import DOC_EXT, extract_text
from .log import log, log_exception, shorten
from .version import PLUGIN_DIR, plugin_version

WEB_DIR = os.path.join(PLUGIN_DIR, "web")
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "ste_ai_uploads")
MAX_BODY_BYTES = 200 * 1024 * 1024
MAX_EVENTS_WAIT = 25.0

IMAGE_EXT = (".png", ".jpg", ".jpeg")
MODEL_EXT = (".stl", ".obj", ".3mf")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
}

ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
ALLOWED_ORIGIN_HOSTS = ("127.0.0.1", "localhost", "::1")


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _safe_filename(name):
    filename = os.path.basename(name or "file")
    filename = re.sub(r"[^\w\-. ]+", "_", filename, flags=re.UNICODE).strip(" .")
    return filename or "file"


def _image_data_url(extension, raw):
    if extension == ".png":
        mime_type = "image/png"
    else:
        mime_type = "image/jpeg"
    return "data:%s;base64,%s" % (mime_type, base64.b64encode(raw).decode("ascii"))


def _save_upload(filename, raw):
    folder = os.path.join(UPLOAD_DIR, time.strftime("%Y%m%d-%H%M%S"))
    if not os.path.isdir(folder):
        os.makedirs(folder)

    path = os.path.join(folder, filename)
    with open(path, "wb") as upload_file:
        upload_file.write(raw)

    log("d", "attachment saved: %s (%d bytes)", path, len(raw))
    return path


def _collect_attachments(attachments):
    images = []
    files = []
    documents = []

    for attachment in attachments:
        filename = _safe_filename(attachment.get("name"))
        extension = os.path.splitext(filename)[1].lower()
        raw = base64.b64decode(attachment.get("data") or "")

        if extension in DOC_EXT:
            document = extract_text(filename, raw)
            log("d", "document %s: %d chars, truncated=%s", filename, document["chars"], document["truncated"])
            documents.append(document)
        elif extension in IMAGE_EXT:
            images.append(_image_data_url(extension, raw))
        elif extension in MODEL_EXT:
            files.append({"name": filename, "path": _save_upload(filename, raw)})
        else:
            raise ValueError("Неподдерживаемый тип файла: %s" % filename)

    return images, files, documents


def _config_log_line(data):
    parts = []
    for key, value in sorted(data.items()):
        if key != "api_key":
            parts.append("%s=%s" % (key, value))
        elif value:
            parts.append("api_key=***")
    return ", ".join(parts)


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        server_version = "STE-AIAssistant/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def log_request(self, code="-", size="-"):
            path = urlparse(self.path).path
            is_api_call = path.startswith("/api/") and path != "/api/events"
            if is_api_call or path == "/mcp":
                log("d", "HTTP %s %s -> %s", self.command, path, code)

        def log_error(self, format, *args):
            log("w", "HTTP %s: %s", self.address_string(), format % args)

        def _is_local_request(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ALLOWED_HOSTS:
                return False

            origin = self.headers.get("Origin")
            if origin:
                origin_host = urlparse(origin).hostname or ""
                if origin_host not in ALLOWED_ORIGIN_HOSTS:
                    return False
            return True

        def _send(self, status, body, content_type, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _send_json(self, data, status=200):
            if status >= 400:
                error = data.get("error") if isinstance(data, dict) else data
                log("w", "%s %s -> %d: %s", self.command, self.path, status, error)
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                raise ValueError("Слишком большой запрос")
            if length == 0:
                return ""
            return self.rfile.read(length).decode("utf-8")

        def do_GET(self):
            if not self._is_local_request():
                self._send_json({"error": "forbidden"}, 403)
                return

            url = urlparse(self.path)
            try:
                if url.path == "/api/events":
                    self._get_events(parse_qs(url.query))
                elif url.path == "/api/config":
                    self._get_config()
                elif url.path == "/api/state":
                    self._get_state()
                elif url.path == "/api/system_prompt":
                    self._send_json(ctx.system_prompt.state())
                elif url.path == "/mcp":
                    self._send(405, b"", "text/plain", {"Allow": "POST"})
                else:
                    self._send_static(url.path)
            except Exception as error:
                log_exception("GET %s failed" % url.path)
                self._send_json({"error": str(error)}, 500)

        def _get_events(self, query):
            since = int(query.get("since", ["0"])[0])
            wait = min(float(query.get("wait", ["20"])[0]), MAX_EVENTS_WAIT)
            events = ctx.events.since(since, timeout=wait)
            self._send_json({"events": events, "busy": ctx.agent.busy})

        def _get_config(self):
            data = ctx.config.public()
            data["llm_ready"] = ctx.config.llm_ready()
            data["mcp_url"] = ctx.url + "/mcp"
            data["tools"] = ctx.registry.names()
            data["plugin_version"] = plugin_version()
            data["plugin_dir"] = PLUGIN_DIR
            data["system_prompt_custom"] = ctx.system_prompt.is_custom()
            self._send_json(data)

        def _get_state(self):
            try:
                state = ctx.slicer.get_state()
            except Exception as error:
                self._send_json({"ok": False, "error": str(error)})
                return
            self._send_json({"ok": True, "state": state})

        def _send_static(self, path):
            relative_path = posixpath.normpath(unquote(path)).lstrip("/")
            if relative_path in ("", "."):
                relative_path = "index.html"

            web_dir = os.path.realpath(WEB_DIR)
            full_path = os.path.realpath(os.path.join(WEB_DIR, relative_path))
            if not full_path.startswith(web_dir) or not os.path.isfile(full_path):
                self._send_json({"error": "not found"}, 404)
                return

            with open(full_path, "rb") as static_file:
                body = static_file.read()
            extension = os.path.splitext(full_path)[1].lower()
            self._send(200, body, CONTENT_TYPES.get(extension, "application/octet-stream"))

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._read_body()
            except ValueError as error:
                self._send_json({"error": str(error)}, 413)
                return

            if not self._is_local_request():
                self._send_json({"error": "forbidden"}, 403)
                return

            try:
                self._route_post(path, body)
            except ValueError as error:
                self._send_json({"error": str(error)}, 400)
            except Exception as error:
                log_exception("POST %s failed" % path)
                self._send_json({"error": str(error)}, 500)

        def _route_post(self, path, body):
            if path == "/mcp":
                self._post_mcp(body)
                return

            data = json.loads(body) if body else {}
            if path == "/api/message":
                self._post_message(data)
            elif path == "/api/system_prompt":
                self._post_system_prompt(data)
            elif path == "/api/system_prompt/reset":
                self._post_system_prompt_reset()
            elif path == "/api/review":
                self._post_review(data)
            elif path == "/api/stop":
                ctx.agent.stop()
                ctx.review.cancel()
                self._send_json({"ok": True})
            elif path == "/api/reset":
                ctx.agent.reset()
                ctx.review.cancel()
                self._send_json({"ok": True})
            elif path == "/api/config":
                self._post_config(data)
            elif path == "/api/test_llm":
                self._post_test_llm()
            else:
                self._send_json({"error": "not found"}, 404)

        def _post_mcp(self, body):
            log("d", "MCP request: %s", shorten(body, 300))
            response = ctx.mcp.handle_raw(body)
            if response is None:
                self._send(202, b"", "application/json")
            else:
                self._send_json(response)

        def _post_system_prompt(self, data):
            ctx.system_prompt.save(data.get("text"))
            state = ctx.system_prompt.state()
            log("i", "system prompt saved: custom=%s, %d chars", state["is_custom"], len(state["text"]))
            self._send_json(state)

        def _post_system_prompt_reset(self):
            ctx.system_prompt.reset()
            log("i", "system prompt reset to default")
            self._send_json(ctx.system_prompt.state())

        def _post_review(self, data):
            review_id = data.get("review_id")
            changes = data.get("changes") or {}
            comment = data.get("comment") or ""

            log("i", "review %s answered: changes=%s comment=%s", review_id, shorten(changes), shorten(comment, 200))
            ctx.review.answer(review_id, changes, comment)
            self._send_json({"ok": True})

        def _post_message(self, data):
            text = (data.get("text") or "").strip()
            attachments = data.get("attachments") or []
            if not text and not attachments:
                self._send_json({"error": "Пустое сообщение"}, 400)
                return

            if not ctx.config.llm_ready():
                self._send_json({"error": "Укажите URL, ключ и модель LLM в настройках"}, 400)
                return

            images, files, documents = _collect_attachments(attachments)
            try:
                ctx.agent.submit(text, images, files, documents)
            except AgentBusy as error:
                self._send_json({"error": str(error)}, 409)
                return
            self._send_json({"ok": True})

        def _post_config(self, data):
            log("i", "config updated: %s", _config_log_line(data))
            ctx.config.update(data)
            public_config = ctx.config.public()
            public_config["llm_ready"] = ctx.config.llm_ready()
            self._send_json(public_config)

        def _post_test_llm(self):
            try:
                self._send_json(ctx.llm.test())
            except Exception as error:
                self._send_json({"ok": False, "error": str(error)})

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

    log("i", "HTTP server listening on 127.0.0.1:%d (web dir: %s)", server.server_port, WEB_DIR)
    return server
