import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from ..core import Logger, config
from .routes import ROUTES_GET, ROUTES_POST
from .static import serve_static_file

MAX_BODY_BYTES = 200 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_request_handler(ctx):
    web_dir = os.path.join(config.plugin_dir, "web")

    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "STE-AIAssistant/1.0"
        protocol_version = "HTTP/1.1"

        def _is_host_allowed(self):
            host_header = self.headers.get("Host", "").split(":")[0].strip("[]")
            return host_header in LOCAL_HOSTS or host_header == "127.0.0.1"

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, Authorization"
            )
            self.end_headers()

        def do_GET(self):
            if not self._is_host_allowed():
                self.send_error(403, "Forbidden")
                return

            parsed = urlparse(self.path)
            handler_func = ROUTES_GET.get(parsed.path)

            if handler_func:
                query = parse_qs(parsed.query)
                try:
                    handler_func(ctx, self, query)
                except Exception as e:
                    Logger.logException("Сбой обработки GET %s: %s", parsed.path, e)
                    self.send_json({"error": str(e)}, status=500)
            else:
                # Отдача статических файлов из web/
                serve_static_file(self, web_dir, parsed.path)

        def do_POST(self):
            if not self._is_host_allowed():
                self.send_error(403, "Forbidden")
                return

            parsed = urlparse(self.path)
            handler_func = ROUTES_POST.get(parsed.path)

            if not handler_func:
                self.send_error(404, "Маршрут не найден")
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY_BYTES:
                    self.send_error(413, "Слишком большой запрос")
                    return

                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                body = json.loads(raw) if raw.strip() else {}
                handler_func(ctx, self, body)
            except Exception as e:
                Logger.logException("Сбой обработки POST %s: %s", parsed.path, e)
                self.send_json({"error": str(e)}, status=500)

        def log_message(self, format, *args):
            # Отключаем дефолтный вывод каждого HTTP-запроса в stderr
            pass

    return RequestHandler


def start_server(ctx, addr="127.0.0.1:8765"):
    host = "127.0.0.1"
    port = 8765

    if ":" in str(addr):
        _, port_str = str(addr).rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 8765

    handler_cls = make_request_handler(ctx)
    try:
        server = ChatServer((host, port), handler_cls)
    except OSError:
        Logger.log("w", "Порт %d занят, подбираем свободный порт", port)
        server = ChatServer((host, 0), handler_cls)

    thread = threading.Thread(target=server.serve_forever, name="AIAssistantServer")
    thread.daemon = True
    thread.start()

    web_dir = os.path.join(config.plugin_dir, "web")
    Logger.log(
        "i",
        "Локальный HTTP сервер запущен на %s:%d (web: %s)",
        host,
        server.server_port,
        web_dir,
    )
    return server
